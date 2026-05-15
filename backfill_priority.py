"""
Періодичний backfill для пріоритетних main-flow каналів — догон пропущених
постів через op=49 (getMessages). Live push у MAX дропає 50-70% постів
у високочастотних каналах (verified 08.05: ТАСС 37%, СВО 27%, Пятий 22%),
тому повне покриття можливе лише через регулярне опитування.

Стратегія:
1. Беремо top-100 main-flow каналів за останні 7 днів у порядку активності
   (виключаючи alert-канали з channels/alert_channels.txt — їх високочастотний
   характер робить backfill для них занадто дорогим, лишаємо їм лише live push).
2. Для кожного каналу пагінуємо op=49 від now назад. Самоадаптивна зупинка:
   щойно зустрічаємо SYNC_WINDOW (=30) підряд msg_id, які вже в БД —
   канал синхронізований, переходимо до наступного.
3. INSERT OR IGNORE через UNIQUE(chat_id, msg_id) — повторні запуски ідемпотентні.

При першому запуску дотягне всі прогалини за 7 днів (~30+ хв роботи).
При наступних — ~2-5 хв на цикл, бо sync-window зупиняє рано.

Запуск: systemd timer `backfill-priority.timer` що 30 хв.
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
ALERT_FILE    = ROOT / "channels" / "alert_channels.txt"
DB_FILE       = ROOT / "matches.db"

# ── Конфіг ────────────────────────────────────────────────────────────────────
TOP_N               = 300   # скільки top main-flow каналів брати
LOOKBACK_DAYS       = 7     # за який період визначаємо "топ" і шукаємо прогалини
PAGE_SIZE           = 100   # backward cap на op=49
MAX_PAGES           = 100   # запобіжник: до 10k постів на канал
SYNC_WINDOW         = int(os.environ.get("BACKFILL_SYNC_WINDOW", "30"))   # підряд відомих msg_id → канал синхронізований; env override для deep-runs
CONCURRENT_CHATS    = 4     # помірна паралельність — MAX дратується від >8
INTER_REQUEST_DELAY = 0.1
WS_TIMEOUT          = 15


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_alert_aliases() -> set[str]:
    """Читає channels/alert_channels.txt — alias-и без https://max.ru/, lowercase."""
    if not ALERT_FILE.exists():
        return set()
    out: set[str] = set()
    for line in ALERT_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s.lower())
    return out


def get_top_main_flow(conn: sqlite3.Connection, alert_aliases: set[str]) -> list[tuple]:
    """Повертає top-N main-flow каналів за активністю за LOOKBACK_DAYS днів."""
    rows = conn.execute(
        """
        SELECT channel_link, chat_id, channel_title, COUNT(*) AS posts
        FROM messages
        WHERE saved_at >= datetime('now', 'localtime', ?)
        GROUP BY channel_link
        ORDER BY posts DESC
        """,
        (f"-{LOOKBACK_DAYS} days",)
    ).fetchall()

    out: list[tuple] = []
    for link, chat_id, title, posts in rows:
        alias = link.rsplit("/", 1)[-1].lower()
        if alias in alert_aliases:
            continue
        out.append((alias, chat_id, title, posts))
        if len(out) >= TOP_N:
            break
    return out


def known_msg_ids(conn: sqlite3.Connection, chat_id: int, since_days: int) -> set[str]:
    """msg_id які вже в БД для каналу за останні N днів."""
    rows = conn.execute(
        "SELECT msg_id FROM messages WHERE chat_id = ? "
        "AND saved_at >= datetime('now', 'localtime', ?)",
        (chat_id, f"-{since_days} days")
    ).fetchall()
    return {r[0] for r in rows}


def save_message(conn: sqlite3.Connection, title: str, channel_link: str,
                 subs: int, chat_id: int, msg_id: str, msg_time: str,
                 post_link: str, text: str) -> bool:
    # Backfill: saved_at = msg_time, щоб дашборд групував по даті публікації,
    # а не по даті догону. Інакше всі historical-пости злипаються в "сьогодні".
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(saved_at,channel_title,channel_link,channel_subs,chat_id,msg_id,msg_time,post_link,text) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (msg_time, title, channel_link, subs, chat_id, msg_id, msg_time, post_link, text)
    )
    return cur.rowcount > 0


# ── WS клієнт (один conn, pipelined op=49) ────────────────────────────────────

class Client:
    def __init__(self):
        # Multi-account: за замовчуванням використовуються A-токен/device. Якщо передати
        # env vars BACKFILL_TOKEN_FILE/BACKFILL_DEVICE_FILE — backfill піде через 2-й
        # акаунт, не конкуруючи з live-парсером A за квоту op=49 від MAX-сервера
        # (MAX дропає "сторонні" сесії коли з одного IP паралельно вже багато).
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


# ── Backfill одного каналу з sync-window логікою ──────────────────────────────

async def backfill_channel(client: Client, alias: str, chat_id: int,
                           title: str, subs_default: int,
                           known: set[str],
                           conn: sqlite3.Connection,
                           db_lock: asyncio.Lock) -> tuple[int, int, int]:
    """
    Повертає (added, seen, pages).
    Зупиняється коли SYNC_WINDOW підряд msg_id вже в `known`.
    """
    channel_link = f"https://max.ru/{alias}"
    cursor_ms = int(time.time() * 1000) + 60_000
    added = seen = pages = 0
    consecutive_known = 0

    for _ in range(MAX_PAGES):
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
            # Ранній стоп — не парсимо нічого до старту проекту
            if t < PROJECT_START_MS:
                continue
            mid = str(m.get("id", ""))
            if not mid:
                continue

            if mid in known:
                consecutive_known += 1
                if consecutive_known >= SYNC_WINDOW:
                    return added, seen, pages
                continue

            consecutive_known = 0
            text = m.get("text", "")
            if not text:
                # БД має NOT NULL на text — медіа-пости без тексту пропускаємо.
                continue
            msg_time = datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M:%S")
            post_link = f"https://max.ru/{alias}/{mid}"
            async with db_lock:
                if save_message(conn, title, channel_link, subs_default,
                                chat_id, mid, msg_time, post_link, text):
                    added += 1
                    known.add(mid)

        if oldest is None:
            break
        if oldest <= PROJECT_START_MS:
            break  # дійшли до старту проекту — стоп
        if oldest >= cursor_ms:
            break  # пагінація не рухається — стоп
        cursor_ms = oldest

    return added, seen, pages


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not DB_FILE.exists():
        print(f"ПОМИЛКА: {DB_FILE} не знайдено")
        sys.exit(1)

    alert_aliases = load_alert_aliases()
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    targets = get_top_main_flow(conn, alert_aliases)
    print(f"[{ts()}] Backfill priority: top-{len(targets)} main-flow за {LOOKBACK_DAYS} днів", flush=True)
    if not targets:
        print(f"[{ts()}] Немає каналів для обробки")
        conn.close()
        return

    # Прев'ю топ-5
    for i, (alias, _, title, posts) in enumerate(targets[:5], 1):
        print(f"  #{i:>3}. [{posts:>5} постів] {title} ({alias})", flush=True)
    if len(targets) > 5:
        print(f"  ... ще {len(targets) - 5} каналів", flush=True)

    db_lock = asyncio.Lock()
    client = Client()
    await client.connect()
    print(f"[{ts()}] WS connected, паралелізм={CONCURRENT_CHATS}", flush=True)

    sem = asyncio.Semaphore(CONCURRENT_CHATS)
    progress = {"done": 0, "added": 0, "seen": 0, "pages": 0, "errors": 0}
    t_start = time.time()
    total = len(targets)

    async def process(idx: int, alias: str, chat_id: int, title: str, posts_in_db: int):
        async with sem:
            try:
                # Завантажуємо тільки для цього chat_id, щоб не тримати в RAM усе:
                known = known_msg_ids(conn, chat_id, LOOKBACK_DAYS)
                added, seen, pages = await backfill_channel(
                    client, alias, chat_id, title, 0, known, conn, db_lock
                )
                progress["added"] += added
                progress["seen"] += seen
                progress["pages"] += pages
                if added > 0:
                    print(f"[{ts()}] #{idx:>3}/{total} {title[:40]:<40} +{added:<5} "
                          f"(scan={seen} pages={pages})", flush=True)
            except Exception as e:
                progress["errors"] += 1
                print(f"[{ts()}] #{idx:>3} {alias}: error {type(e).__name__}: {e}", flush=True)

        progress["done"] += 1
        if progress["done"] % 25 == 0 or progress["done"] == total:
            async with db_lock:
                conn.commit()
            elapsed = time.time() - t_start
            rate = progress["done"] / max(elapsed, 0.001)
            eta = (total - progress["done"]) / max(rate, 0.001)
            print(f"[{ts()}] === {progress['done']}/{total} | added={progress['added']} "
                  f"pages={progress['pages']} err={progress['errors']} | "
                  f"{rate:.2f} ch/s ETA {eta/60:.1f} min ===", flush=True)

    try:
        coros = [
            process(i + 1, alias, chat_id, title, posts)
            for i, (alias, chat_id, title, posts) in enumerate(targets)
        ]
        await asyncio.gather(*coros)
    finally:
        async with db_lock:
            conn.commit()
        await client.close()
        conn.close()

    elapsed = time.time() - t_start
    print(f"[{ts()}] Готово: +{progress['added']} нових постів за {elapsed/60:.1f} хв "
          f"(seen={progress['seen']}, pages={progress['pages']}, "
          f"errors={progress['errors']})", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
