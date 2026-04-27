"""
Live view-count fetcher для експорту xlsx.

Логіниться в MAX WS, групує пости за chat_id, через op=49 (getMessages)
збирає актуальні значення payload.messages[i].stats.views на момент виклику.
Якщо пост не вліз у вікно pagination або сервер не повернув його — None.
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import websockets

WS_URL = "wss://ws-api.oneme.ru/websocket"
ROOT = Path(__file__).parent
LOGIN_TOKEN_FILE = ROOT / ".login_token"
DEVICE_ID_FILE = ROOT / ".device_id"

MAX_PER_REQUEST     = 100   # backward cap для op=49
MAX_ROUNDS_PER_CHAT = 20    # запобіжник pagination на канал
WS_TIMEOUT          = 10
INTER_REQUEST_DELAY = 0.05  # rate-limit hygiene (на окремому WS-conn, не на парсерному)


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
        if not LOGIN_TOKEN_FILE.exists():
            raise RuntimeError(f"нема {LOGIN_TOKEN_FILE}")
        self._token = LOGIN_TOKEN_FILE.read_text().strip()
        if DEVICE_ID_FILE.exists():
            self._device_id = DEVICE_ID_FILE.read_text().strip()
        else:
            self._device_id = f"web_{int(time.time())}"
            DEVICE_ID_FILE.write_text(self._device_id)
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
        msg = json.dumps({"ver": 11, "cmd": 0, "seq": s, "opcode": opcode, "payload": payload}, ensure_ascii=False)
        fut = asyncio.get_event_loop().create_future()
        self._pending[s] = fut
        await self._ws.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(s, None)
            return None

    async def connect(self):
        headers = {
            "Origin": "https://web.max.ru",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        }
        self._ws = await websockets.connect(WS_URL, additional_headers=headers, ping_interval=30)
        self._recv_task = asyncio.create_task(self._recv_loop())
        hs = await self.send_op(6, {
            "deviceId": self._device_id,
            "userAgent": {
                "deviceType": "WEB", "locale": "ru", "deviceLocale": "ru",
                "osVersion": "Windows 10", "deviceName": "Chrome",
                "headerUserAgent": "Mozilla/5.0",
                "appVersion": "1.0.0", "screen": "1920x1080", "timezone": "Europe/Moscow",
            },
        })
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
                          want_msg_ids: set[str], newest_ts_ms: int) -> dict[str, int]:
    """Повертає {msg_id: views} для запитаних msg_ids у межах одного каналу."""
    out: dict[str, int] = {}
    remaining = set(want_msg_ids)
    cursor = newest_ts_ms + 60_000  # +60с буфер на випадок розбіжності timestamps

    for _ in range(MAX_ROUNDS_PER_CHAT):
        if not remaining:
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


async def _fetch_views_async(posts: list[dict], log) -> dict[tuple[int, str], int | None]:
    if not posts:
        return {}
    client = _Client()
    await client.connect()
    log(f"[views] WS connected, постів={len(posts)}")
    try:
        by_chat: dict[int, list[dict]] = {}
        for p in posts:
            by_chat.setdefault(p["chat_id"], []).append(p)

        result: dict[tuple[int, str], int | None] = {
            (p["chat_id"], str(p["msg_id"])): None for p in posts
        }

        for i, (chat_id, items) in enumerate(by_chat.items(), 1):
            want = {str(p["msg_id"]) for p in items}
            newest_ts = max(_parse_msg_time_ms(p["msg_time"]) for p in items)
            try:
                got = await _fetch_one_chat(client, chat_id, want, newest_ts)
            except Exception as e:
                log(f"[views] chat={chat_id} помилка: {e}")
                got = {}
            for mid, v in got.items():
                result[(chat_id, mid)] = v
            log(f"[views] {i}/{len(by_chat)} chat={chat_id} got {len(got)}/{len(want)}")

        return result
    finally:
        await client.close()


def fetch_views(posts: list[dict], log=print) -> dict[tuple[int, str], int | None]:
    """
    Sync wrapper. posts — список dict-ів з ключами chat_id, msg_id, msg_time.
    Повертає {(chat_id, msg_id_str): views | None}.
    """
    return asyncio.run(_fetch_views_async(posts, log=log))
