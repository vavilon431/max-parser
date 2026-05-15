"""
Backfill пропущених постів зі slice'ів W0/W3/W7 — воркери, що мовчки впали
2026-05-07 і не реконнектились до рестарту парсера о ~19:53 MSK.

Архітектура (за прикладом views_fetcher._Client):
- Одна WS-сесія, pipelined op=49 з CONCURRENT_CHATS паралельними запитами.
- Slice'и обчислюємо так само як ws_parser.main: items[w*500:(w+1)*500].
- Pagination: cursor = oldest_time_in_page; зупиняємося коли oldest <= since_ms.
- INSERT OR IGNORE по UNIQUE(chat_id, msg_id) — повторні запуски безпечні.

ВАЖЛИВО: запускати ТІЛЬКИ при зупиненому max-parser (MAX дозволяє одну
активну WS-сесію на токен). Воркфлоу:

    systemctl stop max-parser
    python3 /root/backfill.py
    systemctl start max-parser
"""
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

from ws_common import (
    WS_URL, WS_HEADERS, get_device_id, get_login_token,
    handshake_payload, make_msg, PROJECT_START_MS,
)

ROOT          = Path(__file__).parent
RESOLVED_FILE = ROOT / "channels" / "resolved.json"
DB_FILE       = ROOT / "matches.db"
CHANNELS_PER_WORKER = 500

# Slice → момент з якого тягти пости (UTC). Беремо з 15-хв запасом до
# спостережуваної смерті воркера, щоб не загубити пограничні пости.
# 06.05 23:00 теж був перерваний паттерн (див. падіння темпу), тож
# додатково покриваємо запас з вечора 06.05 для всіх трьох slice'ів.
DEAD_SLICES = {
    0: datetime(2026, 5, 7,  9, 50, 0, tzinfo=timezone.utc),  # ~12:50 MSK
    3: datetime(2026, 5, 7, 13, 10, 0, tzinfo=timezone.utc),  # ~16:10 MSK
    7: datetime(2026, 5, 7, 13,  5, 0, tzinfo=timezone.utc),  # ~16:05 MSK
}

PAGE_SIZE             = 100   # backward cap для op=49
MAX_PAGES_PER_CHANNEL = 50    # запобіжник pagination на канал
INTER_REQUEST_DELAY   = 0.02  # rate-limit hygiene
CONCURRENT_CHATS      = 24
WS_TIMEOUT            = 15


def ts():
    return datetime.now().strftime("%H:%M:%S")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def save_message(conn: sqlite3.Connection, title: str, channel_link: str,
                 subs: int, chat_id: int, msg_id: str, msg_time: str,
                 post_link: str, text: str) -> bool:
    """True якщо рядок реально вставлено (не дубль).

    Backfill пише saved_at = msg_time (а не now), щоб догнані з минулого
    пости показувались на графіку у день публікації, а не догону.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(saved_at,channel_title,channel_link,channel_subs,chat_id,msg_id,msg_time,post_link,text) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (msg_time, title, channel_link, subs, chat_id, msg_id, msg_time, post_link, text)
    )
    return cur.rowcount > 0


# ── WS клієнт (pipelined op=49 у тій самій сесії) ─────────────────────────────

class Client:
    def __init__(self):
        self._token = get_login_token()
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


# ── Backfill одного каналу ────────────────────────────────────────────────────

async def backfill_channel(client: Client, alias: str, info: dict,
                           since_ms: int, conn: sqlite3.Connection,
                           db_lock: asyncio.Lock) -> tuple[int, int, int]:
    """
    Пагінуємо op=49 у зворотному порядку від тепер до since_ms.
    Повертає (added, seen, pages).
    """
    chat_id = info["id"]
    title   = info["title"]
    subs    = info["subs"]
    channel_link = f"https://max.ru/{alias}"

    cursor_ms = int(time.time() * 1000) + 60_000
    added = 0
    seen = 0
    pages = 0

    for _ in range(MAX_PAGES_PER_CHANNEL):
        resp = await client.send_op(49, {
            "chatId": chat_id,
            "from": cursor_ms,
            "forward": 0,
            "backward": PAGE_SIZE,
            "getMessages": True,
        })
        await asyncio.sleep(INTER_REQUEST_DELAY)
        pages += 1

        if not resp or resp.get("cmd") == 3:
            break
        msgs = (resp.get("payload") or {}).get("messages") or []
        if not msgs:
            break

        oldest = None
        for m in msgs:
            seen += 1
            t = m.get("time", 0) or 0
            if oldest is None or t < oldest:
                oldest = t
            if t < since_ms:
                continue
            if t < PROJECT_START_MS:
                continue  # до старту проекту — не наше
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

        if oldest is None or oldest <= since_ms:
            break
        if oldest <= PROJECT_START_MS:
            break  # дійшли до старту проекту — стоп
        if oldest >= cursor_ms:
            break  # пагінація не рухається — стоп
        cursor_ms = oldest

    return added, seen, pages


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not RESOLVED_FILE.exists():
        print(f"ПОМИЛКА: {RESOLVED_FILE} не знайдено")
        sys.exit(1)
    resolved = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    items = list(resolved.items())
    print(f"[{ts()}] Resolved: {len(items)} каналів", flush=True)

    targets: list[tuple[int, int, str, dict]] = []
    for wid, since_dt in DEAD_SLICES.items():
        start = wid * CHANNELS_PER_WORKER
        end = start + CHANNELS_PER_WORKER
        slice_items = items[start:end]
        if not slice_items:
            print(f"[{ts()}] W{wid} — slice порожній, пропускаю", flush=True)
            continue
        since_ms = int(since_dt.timestamp() * 1000)
        for alias, info in slice_items:
            targets.append((wid, since_ms, alias, info))

    total = len(targets)
    print(f"[{ts()}] Backfill targets: {total} каналів зі slice'ів {sorted(DEAD_SLICES)}", flush=True)
    if total == 0:
        return

    conn = open_db()
    db_lock = asyncio.Lock()
    client = Client()
    await client.connect()
    print(f"[{ts()}] WS connected, паралелізм={CONCURRENT_CHATS}", flush=True)

    sem = asyncio.Semaphore(CONCURRENT_CHATS)
    progress = {"done": 0, "added": 0, "seen": 0, "errors": 0}
    t_start = time.time()

    async def process(wid: int, since_ms: int, alias: str, info: dict):
        async with sem:
            try:
                added, seen, _pages = await backfill_channel(
                    client, alias, info, since_ms, conn, db_lock
                )
                progress["added"] += added
                progress["seen"] += seen
            except Exception as e:
                progress["errors"] += 1
                print(f"[{ts()}] [W{wid}] {alias}: error {type(e).__name__}: {e}", flush=True)
        progress["done"] += 1
        if progress["done"] % 50 == 0 or progress["done"] == total:
            async with db_lock:
                conn.commit()
            elapsed = time.time() - t_start
            rate = progress["done"] / max(elapsed, 0.001)
            eta = (total - progress["done"]) / max(rate, 0.001)
            print(f"[{ts()}] {progress['done']}/{total} | "
                  f"added={progress['added']} seen={progress['seen']} err={progress['errors']} | "
                  f"{rate:.1f} ch/s, ETA {eta/60:.1f} min", flush=True)

    try:
        await asyncio.gather(*(process(*t) for t in targets))
    finally:
        async with db_lock:
            conn.commit()
        await client.close()
        conn.close()

    elapsed = time.time() - t_start
    print(f"[{ts()}] Готово: +{progress['added']} нових постів за {elapsed/60:.1f} хв "
          f"(переглянуто {progress['seen']}, помилок {progress['errors']})", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
