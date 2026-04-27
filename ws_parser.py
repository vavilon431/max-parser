"""
MAX WS Parser — збір всіх нових постів з 2000+ публічних каналів.

Архітектура:
- Фаза 1: завантаження resolved.json (alias → chatId)
- Фаза 2: N WS воркерів, кожен моніторить ~500 каналів через op=75 subscribe + push
- Зберігає всі нові повідомлення у SQLite (без фільтрації)
"""

import asyncio
import json
import sqlite3
import time
import sys
from pathlib import Path
from datetime import datetime

import websockets

# ── Конфіг ────────────────────────────────────────────────────────────────────

WS_URL            = "wss://ws-api.oneme.ru/websocket"
RESOLVED_FILE     = Path(__file__).parent / "channels" / "resolved.json"
LOGIN_TOKEN_FILE  = Path(__file__).parent / ".login_token"
DEVICE_ID_FILE    = Path(__file__).parent / ".device_id"
DB_FILE           = Path(__file__).parent / "matches.db"

CHANNELS_PER_WORKER  = 500   # каналів на одне WS з'єднання
FALLBACK_INTERVAL    = 180   # секунд між fallback polling циклами
RECONNECT_DELAY      = 20    # секунд перед reconnect

# ── Утиліти ───────────────────────────────────────────────────────────────────

def ts():
    """Короткий час для логів (HH:MM:SS)."""
    return datetime.now().strftime("%H:%M:%S")

def now_iso():
    """Повний timestamp для збереження в БД (YYYY-MM-DD HH:MM:SS)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_device_id() -> str:
    if DEVICE_ID_FILE.exists():
        return DEVICE_ID_FILE.read_text().strip()
    device_id = f"web_{int(time.time())}"
    DEVICE_ID_FILE.write_text(device_id)
    return device_id

def get_login_token() -> str:
    if LOGIN_TOKEN_FILE.exists():
        return LOGIN_TOKEN_FILE.read_text().strip()
    print("ПОМИЛКА: файл .login_token не знайдено.")
    print(f"Створи: echo 'ТВІЙ_ТОКЕН' > {LOGIN_TOKEN_FILE}")
    sys.exit(1)

def load_resolved() -> dict[str, dict]:
    if not RESOLVED_FILE.exists():
        print(f"ПОМИЛКА: {RESOLVED_FILE} не знайдено. Запусти resolve_channels.py спочатку.")
        sys.exit(1)
    data = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    print(f"[{ts()}] Завантажено {len(data)} каналів з resolved.json", flush=True)
    return data

# ── SQLite ────────────────────────────────────────────────────────────────────

BATCH_COMMIT_INTERVAL = 2.0   # секунд між автокоммітами
BATCH_COMMIT_SIZE     = 100   # або коли накопичилось N повідомлень

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            saved_at        TEXT NOT NULL,
            channel_title   TEXT NOT NULL,
            channel_link    TEXT NOT NULL,
            channel_subs    INTEGER NOT NULL,
            chat_id         INTEGER NOT NULL,
            msg_id          TEXT NOT NULL,
            msg_time        TEXT NOT NULL,
            post_link       TEXT NOT NULL,
            text            TEXT NOT NULL,
            UNIQUE(chat_id, msg_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_link)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_at ON messages(saved_at)")

    # Міграція: до цього `ts()` зберігало тільки HH:MM:SS, через що
    # лексикографічне порівняння з повним timestamp у фільтрах ламалось.
    # Виправляємо існуючі рядки: беремо дату з msg_time + наявний час saved_at.
    broken = conn.execute("SELECT COUNT(*) FROM messages WHERE length(saved_at) = 8").fetchone()[0]
    if broken > 0:
        print(f"[{ts()}] migration: виправляю {broken} рядків з некоректним saved_at...", flush=True)
        conn.execute("""
            UPDATE messages
            SET saved_at = substr(msg_time, 1, 10) || ' ' || saved_at
            WHERE length(saved_at) = 8
        """)
        conn.commit()
        print(f"[{ts()}] migration: готово", flush=True)

    conn.commit()
    return conn


# Лічильник і час останнього commit для batching
_last_commit_ts = 0.0
_uncommitted    = 0

def save_message(conn: sqlite3.Connection, channel_title: str, channel_link: str,
                 channel_subs: int, chat_id: int, msg_id: str, msg_time: str,
                 post_link: str, text: str):
    global _last_commit_ts, _uncommitted
    try:
        conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(saved_at,channel_title,channel_link,channel_subs,chat_id,msg_id,msg_time,post_link,text) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), channel_title, channel_link, channel_subs, chat_id, msg_id, msg_time, post_link, text)
        )
        _uncommitted += 1
        now = time.time()
        if _uncommitted >= BATCH_COMMIT_SIZE or (now - _last_commit_ts) >= BATCH_COMMIT_INTERVAL:
            conn.commit()
            _uncommitted = 0
            _last_commit_ts = now
    except Exception as e:
        print(f"[{ts()}] DB error: {e}", flush=True)


async def commit_watchdog(conn: sqlite3.Connection):
    """Гарантує commit хоча б раз на BATCH_COMMIT_INTERVAL навіть якщо немає нових повідомлень."""
    global _uncommitted, _last_commit_ts
    while True:
        await asyncio.sleep(BATCH_COMMIT_INTERVAL)
        if _uncommitted > 0:
            try:
                conn.commit()
                _uncommitted = 0
                _last_commit_ts = time.time()
            except Exception as e:
                print(f"[{ts()}] commit watchdog error: {e}", flush=True)

# ── WS базовий клієнт ─────────────────────────────────────────────────────────

class WSClient:
    def __init__(self, token: str, device_id: str, worker_id: str = ""):
        self.token = token
        self.device_id = device_id
        self.worker_id = worker_id
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._ws = None
        self._recv_task = None

    def _ns(self) -> int:
        self._seq += 1
        return self._seq

    def log(self, msg: str):
        prefix = f"[W{self.worker_id}]" if self.worker_id else ""
        print(f"[{ts()}]{prefix} {msg}", flush=True)

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                self._dispatch(msg)
                if msg.get("cmd") == 0 and hasattr(self, "_on_push"):
                    await self._on_push(msg)
        except Exception as e:
            print(f"[{ts()}][W{self.worker_id}] _recv_loop exception: {e}", flush=True)

    async def connect(self) -> bool:
        headers = {
            "Origin": "https://web.max.ru",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        }
        self._ws = await websockets.connect(WS_URL, additional_headers=headers, ping_interval=30)
        self._recv_task = asyncio.create_task(self._recv_loop())

        hs = await self._send_recv(6, {
            "deviceId": self.device_id,
            "userAgent": {
                "deviceType": "WEB", "locale": "ru", "deviceLocale": "ru",
                "osVersion": "Windows 10", "deviceName": "Chrome",
                "headerUserAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "appVersion": "1.0.0", "screen": "1920x1080", "timezone": "Europe/Moscow",
            },
        })
        if not hs or hs.get("cmd") == 3:
            return False

        login = await self._send_recv(19, {"token": self.token}, timeout=15)
        if not login or login.get("cmd") == 3 or not (login.get("payload") or {}).get("profile"):
            err = (login.get("payload") or {}).get("message", "timeout") if login else "timeout"
            self.log(f"Login FAIL: {err}")
            return False

        await asyncio.sleep(1.5)
        return True

    async def _send_recv(self, opcode: int, payload: dict, timeout: float = 10) -> dict | None:
        s = self._ns()
        fut = asyncio.get_event_loop().create_future()
        self._pending[s] = fut
        await self._ws.send(json.dumps(
            {"ver": 11, "cmd": 0, "seq": s, "opcode": opcode, "payload": payload},
            ensure_ascii=False
        ))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(s, None)
            return None

    def _dispatch(self, msg: dict):
        s = msg.get("seq")
        if msg.get("cmd") in (1, 3) and s in self._pending:
            fut = self._pending.pop(s)
            if not fut.done():
                fut.set_result(msg)

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()

# ── Моніторинг ────────────────────────────────────────────────────────────────

async def worker(worker_id: int, token: str, device_id: str,
                 channel_map: dict[str, dict], all_channels: dict[str, dict],
                 db: sqlite3.Connection):
    """Один WS воркер: підписується на канали і обробляє push-події."""

    # chatId → метадані для ВСІХ каналів (push приходить усім воркерам одночасно)
    id_to_meta: dict[int, dict] = {
        info["id"]: {"alias": alias, "title": info["title"], "subs": info["subs"]}
        for alias, info in all_channels.items()
    }
    # підписуємось тільки на свій slice
    chat_ids = [info["id"] for info in channel_map.values()]
    wid = str(worker_id)

    def log(msg):
        print(f"[{ts()}][W{wid}] {msg}", flush=True)

    def handle_message(chat_id: int, m: dict):
        text = m.get("text", "")
        if not text:
            return
        meta = id_to_meta.get(chat_id, {})
        alias = meta.get("alias", str(chat_id))
        title = meta.get("title", alias)
        subs  = meta.get("subs", 0)
        msg_id    = str(m.get("id", ""))
        msg_time  = datetime.fromtimestamp(m.get("time", 0) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        channel_link = f"https://max.ru/{alias}"
        post_link    = f"https://max.ru/{alias}/{msg_id}"
        log(f"[{title}] [{msg_time}] {text[:120]}")
        save_message(db, title, channel_link, subs, chat_id, msg_id, msg_time, post_link, text)

    while True:
        client = WSClient(token, device_id, wid)

        async def on_push(msg: dict):
            op = msg.get("opcode")
            payload = msg.get("payload") or {}
            chat_id = payload.get("chatId")
            if op == 128:
                m = payload.get("message")
                if m:
                    handle_message(chat_id, m)
            elif op == 55:
                for m in payload.get("messages", []):
                    handle_message(chat_id, m)

        client._on_push = on_push

        try:
            if not await client.connect():
                log("Не вдалось підключитись, retry...")
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            log(f"Підписуємось на {len(chat_ids)} каналів...")
            for chat_id in chat_ids:
                await client._ws.send(json.dumps(
                    {"ver": 11, "cmd": 0, "seq": client._ns(), "opcode": 75,
                     "payload": {"chatId": chat_id, "subscribe": True}},
                    ensure_ascii=False
                ))
                await asyncio.sleep(0.03)

            log(f"Підписка відправлена. Слухаємо push...")

            # Чекаємо поки recv_loop не завершиться (розрив з'єднання)
            await client._recv_task

        except (websockets.ConnectionClosed, OSError) as e:
            log(f"З'єднання розірвано: {e}. Reconnect через {RECONNECT_DELAY}s...")
        except Exception as e:
            log(f"Помилка: {e}. Reconnect через {RECONNECT_DELAY}s...")
        finally:
            await client.close()

        await asyncio.sleep(RECONNECT_DELAY)

# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    token = get_login_token()
    device_id = get_device_id()
    resolved = load_resolved()

    if not resolved:
        print(f"[{ts()}] Немає каналів для моніторингу!", flush=True)
        return

    db = init_db()

    items = list(resolved.items())
    groups: list[dict[str, dict]] = [
        dict(items[i:i + CHANNELS_PER_WORKER])
        for i in range(0, len(items), CHANNELS_PER_WORKER)
    ]
    print(f"[{ts()}] Каналів: {len(resolved)} | Воркерів: {len(groups)} (по ~{CHANNELS_PER_WORKER})", flush=True)
    print(f"[{ts()}] База даних: {DB_FILE}", flush=True)
    print(f"[{ts()}] Моніторинг запущено. Ctrl+C для зупинки.\n", flush=True)

    tasks = [
        asyncio.create_task(worker(i, token, device_id, group, resolved, db))
        for i, group in enumerate(groups)
    ]
    tasks.append(asyncio.create_task(commit_watchdog(db)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Зупинено.", flush=True)
