"""
Live view-count fetcher для експорту xlsx.

Логіниться в MAX WS, групує пости за chat_id, через op=49 (getMessages)
збирає актуальні значення payload.messages[i].stats.views на момент виклику.
Якщо пост не вліз у вікно pagination або сервер не повернув його — None.
"""

import asyncio
import json
import threading
import time
from datetime import datetime

import websockets

from ws_common import (
    WS_URL, WS_HEADERS, get_device_id, get_login_token,
    handshake_payload, make_msg,
)

MAX_PER_REQUEST     = 100   # backward cap для op=49
MAX_ROUNDS_PER_CHAT = 20    # запобіжник pagination на канал
WS_TIMEOUT          = 10
INTER_REQUEST_DELAY = 0.02  # rate-limit hygiene між op=49 у межах одного chat
CONCURRENT_CHATS    = 24    # pipelining у тому самому WS-conn: websockets дозволяє
                            # багато паралельних send (recv-loop один). Окремі
                            # conn-и не відкриваємо — MAX блокує паралельні сесії
                            # на той самий токен. Підняли з 5 для прискорення reach.

# In-memory кеш `(chat_id, msg_id) -> (views, ts)`. Views ростуть повільно,
# 30-хв застаріння для UI прийнятне; зекономлює op=49 при перемиканнях період/канал.
_VIEWS_CACHE_TTL = 1800
_views_cache: dict[tuple[int, str], tuple[int, float]] = {}
_views_cache_lock = threading.Lock()


def _cache_get(chat_id: int, msg_id: str) -> int | None:
    now = time.time()
    with _views_cache_lock:
        entry = _views_cache.get((chat_id, msg_id))
        if entry and now - entry[1] < _VIEWS_CACHE_TTL:
            return entry[0]
    return None


def _cache_put(chat_id: int, msg_id: str, views: int) -> None:
    with _views_cache_lock:
        _views_cache[(chat_id, msg_id)] = (views, time.time())


def _parse_msg_time_ms(s: str) -> int:
    if not s:
        return int(time.time() * 1000)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return int(time.time() * 1000)


class _Client:
    def __init__(self):
        self._token = get_login_token(strict=False)
        self._device_id = get_device_id()
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._ws = None
        self._recv_task = None

    def _ns(self) -> int:
        self._seq += 1
        return self._seq

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                seq = msg.get("seq")
                cmd = msg.get("cmd", 0)
                if cmd in (1, 3) and seq in self._pending:
                    fut = self._pending.pop(seq)
                    if not fut.done():
                        fut.set_result(msg)
        except Exception:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("ws closed"))
            self._pending.clear()

    async def send_op(self, opcode: int, payload: dict, timeout: float = WS_TIMEOUT):
        s = self._ns()
        fut = asyncio.get_running_loop().create_future()
        self._pending[s] = fut
        await self._ws.send(make_msg(s, opcode, payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(s, None)
            return None

    async def connect(self):
        self._ws = await websockets.connect(WS_URL, additional_headers=WS_HEADERS, ping_interval=30)
        self._recv_task = asyncio.create_task(self._recv_loop())
        hs = await self.send_op(6, handshake_payload(self._device_id))
        if not hs or hs.get("cmd") == 3:
            raise RuntimeError(f"handshake fail: {hs}")
        login = await self.send_op(19, {"token": self._token}, timeout=15)
        if not login or login.get("cmd") == 3 or not (login.get("payload") or {}).get("profile"):
            raise RuntimeError("login fail")

    async def close(self):
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()


async def _fetch_one_chat(client: _Client, chat_id: int,
                          want_msg_ids: set[str], newest_ts_ms: int,
                          oldest_ts_ms: int = 0) -> dict[str, int]:
    """Повертає {msg_id: views} для запитаних msg_ids у межах одного каналу.
    oldest_ts_ms — нижня межа періоду пошуку; пагінація глибше вже не дасть
    нових матчів, тож раніше виходимо."""
    out: dict[str, int] = {}
    remaining = set(want_msg_ids)
    cursor = newest_ts_ms + 60_000  # +60с буфер на випадок розбіжності timestamps

    for _ in range(MAX_ROUNDS_PER_CHAT):
        if not remaining:
            break
        if oldest_ts_ms and cursor < oldest_ts_ms:
            break
        resp = await client.send_op(49, {
            "chatId": chat_id, "from": cursor,
            "forward": 0, "backward": MAX_PER_REQUEST, "getMessages": True,
        })
        await asyncio.sleep(INTER_REQUEST_DELAY)
        if resp is None or resp.get("cmd") == 3:
            break
        msgs = (resp.get("payload") or {}).get("messages") or []
        if not msgs:
            break
        for m in msgs:
            mid = str(m.get("id") or "")
            if mid in remaining:
                stats = m.get("stats") or {}
                v = stats.get("views")
                if isinstance(v, int):
                    out[mid] = v
                remaining.discard(mid)
        oldest = min((m.get("time") or 0) for m in msgs)
        if oldest <= 0 or oldest >= cursor:
            break
        cursor = oldest

    return out


async def _fetch_views_async(posts: list[dict], log,
                             oldest_ts_ms: int = 0) -> dict[tuple[int, str], int | None]:
    if not posts:
        return {}

    # 1. Кеш-хіти забираємо одразу — не відкриваючи WS-сесію.
    result: dict[tuple[int, str], int | None] = {}
    posts_to_fetch: list[dict] = []
    for p in posts:
        key = (p["chat_id"], str(p["msg_id"]))
        cached = _cache_get(p["chat_id"], str(p["msg_id"]))
        if cached is not None:
            result[key] = cached
        else:
            result[key] = None
            posts_to_fetch.append(p)

    cached_n = len(posts) - len(posts_to_fetch)
    if cached_n:
        log(f"[views] cache hit {cached_n}/{len(posts)}")

    if not posts_to_fetch:
        return result

    client = _Client()
    await client.connect()
    log(f"[views] WS connected, постів={len(posts_to_fetch)}, concurrent={CONCURRENT_CHATS}")
    try:
        by_chat: dict[int, list[dict]] = {}
        for p in posts_to_fetch:
            by_chat.setdefault(p["chat_id"], []).append(p)

        total = len(by_chat)
        progress = {"done": 0}
        sem = asyncio.Semaphore(CONCURRENT_CHATS)

        async def _process(chat_id: int, items: list[dict]):
            want = {str(p["msg_id"]) for p in items}
            newest_ts = max(_parse_msg_time_ms(p["msg_time"]) for p in items)
            async with sem:
                try:
                    got = await _fetch_one_chat(client, chat_id, want, newest_ts, oldest_ts_ms)
                except Exception as e:
                    log(f"[views] chat={chat_id} помилка: {e}")
                    got = {}
            for mid, v in got.items():
                result[(chat_id, mid)] = v
                _cache_put(chat_id, mid, v)
            progress["done"] += 1
            log(f"[views] {progress['done']}/{total} chat={chat_id} got {len(got)}/{len(want)}")

        await asyncio.gather(*(_process(c, items) for c, items in by_chat.items()))
        return result
    finally:
        await client.close()


def fetch_views(posts: list[dict], log=print,
                oldest_ts_ms: int = 0) -> dict[tuple[int, str], int | None]:
    """
    Sync wrapper. posts — список dict-ів з ключами chat_id, msg_id, msg_time.
    oldest_ts_ms — нижня межа періоду в мс епохи; pagination не йде глибше.
    Повертає {(chat_id, msg_id_str): views | None}.
    """
    return asyncio.run(_fetch_views_async(posts, log=log, oldest_ts_ms=oldest_ts_ms))
