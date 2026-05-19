"""
Backfill для каналів які WS-парсер мовчки пропускає — для них op=75 subscribe
не приймається MAX (підтверджено probe_subscribe_limit.py: 2/200 push events
за 30 хв замість очікуваних ~190). Полінгом через op=49 покриваємо їх стабільно.

Стратегія:
1. Беремо ВСІ канали з resolved.json, які НЕ мали постів у БД за останні
   STALE_MIN хвилин — це наш "stale set" (1000-2500 каналів зазвичай).
2. Для кожного — op=49 backward:5 (останні 5 повідомлень). INSERT OR IGNORE
   через UNIQUE(chat_id, msg_id) — повторні запуски ідемпотентні.
3. Працюючі через WS канали матимуть свіжі пости в БД → пропускаються.
   Тільки реально missed і реально мертві потрапляють у polling.

Запуск: systemd timer `backfill-missed.timer` кожні 5 хв через токен B
(той самий що backfill-priority).

Очікуваний час прохід: 1-3 хв при ~1500 stale-каналах + паралелізм 24.
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import websockets

from ws_common import (
    WS_URL, WS_HEADERS, get_device_id, get_login_token,
    handshake_payload, make_msg, PROJECT_START_MS,
)

ROOT          = Path(__file__).parent
RESOLVED_FILE = ROOT / "channels" / "resolved.json"
DB_FILE       = ROOT / "matches.db"

STALE_MIN           = 10    # канал stale якщо немає постів у БД за STALE_MIN хв
PAGE_SIZE           = 5     # backward:5 на op=49 — досить для catch-up між запусками
CONCURRENT_CHATS    = 24
INTER_REQUEST_DELAY = 0.02
WS_TIMEOUT          = 15


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Client:
    """Pipelined WS клієнт. Токен/device через env vars BACKFILL_TOKEN_FILE/
    BACKFILL_DEVICE_FILE — щоб не конкурувати з основними парсерами A/B."""

    def __init__(self):
        token_file = os.environ.get("BACKFILL_TOKEN_FILE") or None
        device_file = os.environ.get("BACKFILL_DEVICE_FILE") or None
        self._token = get_login_token(file_path=token_file)
        self._device_id = get_device_id(file_path=device_file)
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
                if msg.get("cmd", 0) in (1, 3) and seq in self._pending:
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
        self._ws = await websockets.connect(
            WS_URL, additional_headers=WS_HEADERS,
            ping_interval=30, ping_timeout=20,
            open_timeout=15, close_timeout=10,
        )
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
                await asyncio.wait_for(self._ws.close(), timeout=5)
            except Exception:
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def find_stale(conn: sqlite3.Connection, resolved: dict, stale_min: int) -> list[tuple[str, dict]]:
    cutoff = (datetime.now() - timedelta(minutes=stale_min)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute("SELECT DISTINCT chat_id FROM messages WHERE saved_at > ?", (cutoff,))
    fresh_ids = {row[0] for row in cur.fetchall()}
    return [(alias, info) for alias, info in resolved.items() if info.get("id") not in fresh_ids]


def save_message(conn: sqlite3.Connection, title: str, channel_link: str,
                 subs: int, chat_id: int, msg_id: str, msg_time: str,
                 post_link: str, text: str) -> bool:
    # saved_at = msg_time щоб поллінговий догон не зсувався в "тепер".
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(saved_at,channel_title,channel_link,channel_subs,chat_id,msg_id,msg_time,post_link,text) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (msg_time, title, channel_link, subs, chat_id, msg_id, msg_time, post_link, text)
    )
    return cur.rowcount > 0


async def poll_channel(client: Client, alias: str, info: dict,
                       conn: sqlite3.Connection, db_lock: asyncio.Lock) -> tuple[int, int]:
    chat_id = info["id"]
    title = info["title"]
    subs = info.get("subs", 0)
    channel_link = f"https://max.ru/{alias}"

    cursor_ms = int(time.time() * 1000) + 60_000
    resp = await client.send_op(49, {
        "chatId": chat_id,
        "from": cursor_ms,
        "forward": 0,
        "backward": PAGE_SIZE,
        "getMessages": True,
    })
    await asyncio.sleep(INTER_REQUEST_DELAY)

    if not resp or resp.get("cmd") == 3:
        return 0, 0

    msgs = (resp.get("payload") or {}).get("messages") or []
    seen = 0
    added = 0
    for m in msgs:
        seen += 1
        t = m.get("time", 0) or 0
        if t < PROJECT_START_MS:
            continue
        text = m.get("text", "")
        if not text:
            continue
        msg_id = str(m.get("id", ""))
        if not msg_id:
            continue
        msg_time = datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M:%S")
        post_link = f"https://max.ru/{alias}/{msg_id}"
        async with db_lock:
            if save_message(conn, title, channel_link, subs, chat_id,
                            msg_id, msg_time, post_link, text):
                added += 1
    return added, seen


async def main():
    if not RESOLVED_FILE.exists():
        print(f"ПОМИЛКА: {RESOLVED_FILE} не знайдено")
        sys.exit(1)

    resolved = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    conn = open_db()
    stale = find_stale(conn, resolved, STALE_MIN)
    print(f"[{ts()}] Resolved: {len(resolved)}, stale (>{STALE_MIN}хв без постів): {len(stale)}", flush=True)
    if not stale:
        conn.close()
        return

    client = Client()
    await client.connect()
    print(f"[{ts()}] WS connected, паралелізм={CONCURRENT_CHATS}", flush=True)

    sem = asyncio.Semaphore(CONCURRENT_CHATS)
    db_lock = asyncio.Lock()
    progress = {"polled": 0, "added": 0, "seen": 0, "errors": 0}
    total = len(stale)
    t_start = time.time()

    async def process(alias: str, info: dict):
        async with sem:
            try:
                added, seen = await poll_channel(client, alias, info, conn, db_lock)
                progress["added"] += added
                progress["seen"] += seen
            except Exception:
                progress["errors"] += 1
        progress["polled"] += 1
        if progress["polled"] % 200 == 0 or progress["polled"] == total:
            async with db_lock:
                conn.commit()
            elapsed = time.time() - t_start
            rate = progress["polled"] / max(elapsed, 0.001)
            print(f"[{ts()}] {progress['polled']}/{total} "
                  f"(+{progress['added']} нових, {rate:.0f}/c)", flush=True)

    await asyncio.gather(*(process(a, i) for a, i in stale))
    await client.close()

    async with db_lock:
        conn.commit()
    conn.close()

    elapsed = time.time() - t_start
    print(f"[{ts()}] DONE: polled={progress['polled']}, added={progress['added']}, "
          f"seen={progress['seen']}, errors={progress['errors']}, "
          f"elapsed={elapsed:.1f}с", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
