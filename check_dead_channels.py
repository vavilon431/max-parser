"""
Діагностика "мертвих" каналів: для кожного каналу з resolved.json, який не
публікував у matches.db за останні QUIET_DAYS днів, запитуємо MAX через op=49
останні N повідомлень і класифікуємо:
  - dead    — 0 повідомлень узагалі
  - old     — є пости, але всі старіші за QUIET_DAYS днів (канал реально затих)
  - missed  — є СВІЖІ пости (новіше за QUIET_DAYS), але їх немає в нашій БД
              (наш WS не отримав — потенційний пропуск)
  - error   — таймаут/відмова на запит

Використовує токен `_b` — окрема WS-сесія, паралельно з основним парсером.

    python3 /root/check_dead_channels.py
"""
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import websockets

from ws_common import (
    WS_URL, WS_HEADERS, get_device_id, get_login_token,
    handshake_payload, make_msg,
)

ROOT             = Path(__file__).parent
RESOLVED_FILE    = ROOT / "channels" / "resolved.json"
DB_FILE          = ROOT / "matches.db"
REPORT_FILE      = ROOT / "dead_channels_report.json"
LOGIN_TOKEN_FILE = ROOT / ".login_token_b"
DEVICE_ID_FILE   = ROOT / ".device_id_b"

QUIET_DAYS          = 7
RECENT_POSTS_LIMIT  = 10
INTER_REQUEST_DELAY = 0.02
CONCURRENT_CHATS    = 16
WS_TIMEOUT          = 15


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Client:
    """Pipelined WS клієнт — копія паттерну з backfill.py, але з токеном _b."""

    def __init__(self):
        self._token = get_login_token(file_path=LOGIN_TOKEN_FILE)
        self._device_id = get_device_id(file_path=DEVICE_ID_FILE)
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


def find_quiet_channels(resolved: dict, quiet_days: int) -> list[tuple[str, dict]]:
    """Канали які не публікували за останні quiet_days днів у matches.db."""
    cutoff = (datetime.now() - timedelta(days=quiet_days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE, timeout=30)
    cur = conn.execute(
        "SELECT DISTINCT chat_id FROM messages WHERE saved_at > ?", (cutoff,)
    )
    active_ids = {row[0] for row in cur.fetchall()}
    conn.close()

    quiet = []
    for alias, info in resolved.items():
        if info.get("id") not in active_ids:
            quiet.append((alias, info))
    return quiet


async def probe_channel(client: Client, alias: str, info: dict,
                        recent_cutoff_ms: int) -> dict:
    chat_id = info["id"]
    cursor_ms = int(time.time() * 1000) + 60_000
    resp = await client.send_op(49, {
        "chatId": chat_id,
        "from": cursor_ms,
        "forward": 0,
        "backward": RECENT_POSTS_LIMIT,
        "getMessages": True,
    })
    await asyncio.sleep(INTER_REQUEST_DELAY)

    result = {
        "alias": alias,
        "chat_id": chat_id,
        "title": info.get("title", ""),
        "subs": info.get("subs", 0),
    }

    if not resp or resp.get("cmd") == 3:
        result.update(status="error", latest_msg_time=None, msg_count=0)
        return result

    msgs = (resp.get("payload") or {}).get("messages") or []
    if not msgs:
        result.update(status="dead", latest_msg_time=None, msg_count=0)
        return result

    latest_ms = max((m.get("time", 0) or 0) for m in msgs)
    latest_iso = datetime.fromtimestamp(latest_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    status = "missed" if latest_ms > recent_cutoff_ms else "old"
    result.update(status=status, latest_msg_time=latest_iso, msg_count=len(msgs))
    return result


async def main():
    if not RESOLVED_FILE.exists():
        print(f"ПОМИЛКА: {RESOLVED_FILE} не знайдено")
        sys.exit(1)

    resolved = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
    print(f"[{ts()}] Resolved: {len(resolved)} каналів", flush=True)

    quiet = find_quiet_channels(resolved, QUIET_DAYS)
    print(f"[{ts()}] Тихі за {QUIET_DAYS}d у БД: {len(quiet)} каналів", flush=True)
    if not quiet:
        return

    recent_cutoff_ms = int(
        (datetime.now() - timedelta(days=QUIET_DAYS)).timestamp() * 1000
    )

    client = Client()
    await client.connect()
    print(f"[{ts()}] WS connected (token _b), паралелізм={CONCURRENT_CHATS}", flush=True)

    sem = asyncio.Semaphore(CONCURRENT_CHATS)
    results: list[dict] = []
    progress = {"done": 0}
    total = len(quiet)
    t_start = time.time()

    async def process(alias: str, info: dict):
        async with sem:
            try:
                r = await probe_channel(client, alias, info, recent_cutoff_ms)
            except Exception as e:
                r = {
                    "alias": alias, "chat_id": info.get("id"),
                    "title": info.get("title", ""), "subs": info.get("subs", 0),
                    "status": "error", "latest_msg_time": None, "msg_count": 0,
                    "error": f"{type(e).__name__}: {e}",
                }
        results.append(r)
        progress["done"] += 1
        if progress["done"] % 50 == 0 or progress["done"] == total:
            elapsed = time.time() - t_start
            rate = progress["done"] / max(elapsed, 0.001)
            eta = (total - progress["done"]) / max(rate, 0.001)
            print(f"[{ts()}] {progress['done']}/{total} "
                  f"({rate:.1f}/c, ETA {eta:.0f}c)", flush=True)

    await asyncio.gather(*(process(a, i) for a, i in quiet))
    await client.close()

    by_status: dict[str, list[dict]] = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    summary = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "quiet_days_cutoff": QUIET_DAYS,
        "total_resolved": len(resolved),
        "total_quiet_checked": len(quiet),
        "by_status": {k: len(v) for k, v in by_status.items()},
        "channels": results,
    }
    REPORT_FILE.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"[{ts()}] === ЗВІТ ===")
    print(f"  Перевірено каналів: {len(quiet)}")
    for status, items in sorted(by_status.items()):
        print(f"  {status:10s}: {len(items)}")
    print(f"  файл: {REPORT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
