"""
Одноразова розвідка: чи віддає сервер MAX кількість переглядів поста через WS API,
і якщо так — у якому полі payload-у.

Логіка:
1. Беремо 5 свіжих постів з matches.db (різних каналів, різного віку).
2. Логінимось у WS під .login_token.
3. Для кожного поста викликаємо op=49 (getMessages) — намагаємось зловити сам пост
   у вікні backward=20 від його msg_time.
4. Друкуємо повний JSON знайденого повідомлення — шукаємо вручну поля типу
   views/seen/read/viewsCount/seenCount/viewersCount.
5. Окремо пробуємо менш досліджені opcodes (66, 130, 131) — деякі чат-движки
   віддають view_count окремим op-ом «get message stats».

Запуск на VPS:  ssh max-vps "cd /root && python scout_views.py"
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import websockets

WS_URL = "wss://ws-api.oneme.ru/websocket"
ROOT = Path(__file__).parent
DB_FILE = ROOT / "matches.db"
LOGIN_TOKEN_FILE = ROOT / ".login_token"
DEVICE_ID_FILE = ROOT / ".device_id"
OUT_FILE = ROOT / "scout_views_dump.json"

PROBE_OPCODES = [49, 66, 130, 131]  # 49 — known, інші — пробуємо
SAMPLE_SIZE = 5


def load_token() -> str:
    if not LOGIN_TOKEN_FILE.exists():
        raise SystemExit(f"Нема {LOGIN_TOKEN_FILE}")
    return LOGIN_TOKEN_FILE.read_text().strip()


def load_device_id() -> str:
    if DEVICE_ID_FILE.exists():
        return DEVICE_ID_FILE.read_text().strip()
    did = f"web_{int(time.time())}"
    DEVICE_ID_FILE.write_text(did)
    return did


def pick_samples() -> list[dict]:
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT chat_id, msg_id, msg_time, channel_title, channel_link, text "
        "FROM messages WHERE saved_at >= date('now','-2 day') "
        "GROUP BY chat_id ORDER BY id DESC LIMIT ?",
        (SAMPLE_SIZE,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


class Probe:
    def __init__(self, token: str, device_id: str):
        self.token = token
        self.device_id = device_id
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._ws = None

    def _ns(self) -> int:
        self._seq += 1
        return self._seq

    async def _recv_loop(self):
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

    async def _send(self, opcode: int, payload: dict, timeout: float = 10) -> dict | None:
        s = self._ns()
        msg = json.dumps(
            {"ver": 11, "cmd": 0, "seq": s, "opcode": opcode, "payload": payload},
            ensure_ascii=False,
        )
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
        asyncio.create_task(self._recv_loop())
        hs = await self._send(6, {
            "deviceId": self.device_id,
            "userAgent": {
                "deviceType": "WEB", "locale": "ru", "deviceLocale": "ru",
                "osVersion": "Windows 10", "deviceName": "Chrome",
                "headerUserAgent": "Mozilla/5.0",
                "appVersion": "1.0.0", "screen": "1920x1080", "timezone": "Europe/Moscow",
            },
        })
        if not hs or hs.get("cmd") == 3:
            raise RuntimeError(f"handshake fail: {hs}")
        login = await self._send(19, {"token": self.token}, timeout=15)
        if not login or login.get("cmd") == 3 or not (login.get("payload") or {}).get("profile"):
            raise RuntimeError(f"login fail: {login}")

    async def close(self):
        if self._ws:
            await self._ws.close()


def parse_msg_time_ms(msg_time: str) -> int:
    """msg_time у БД зберігається як 'DD.MM HH:MM' або 'YYYY-MM-DD HH:MM:SS' — обидва варіанти."""
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            dt = datetime.strptime(msg_time, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return int(time.time() * 1000)


def find_view_like_keys(obj, path="") -> list[tuple[str, object]]:
    """Рекурсивно шукає ключі що нагадують лічильник переглядів."""
    hits = []
    needles = ("view", "seen", "read", "watch", "impression", "просмотр")
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if any(n in kl for n in needles) and not isinstance(v, (dict, list)):
                hits.append((f"{path}.{k}", v))
            hits.extend(find_view_like_keys(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):  # перші 3 елементи
            hits.extend(find_view_like_keys(v, f"{path}[{i}]"))
    return hits


async def main():
    token = load_token()
    device_id = load_device_id()
    samples = pick_samples()
    print(f"Семплів: {len(samples)}")

    p = Probe(token, device_id)
    await p.connect()
    print("Авторизовано")

    dump: dict = {"samples": []}

    for s in samples:
        chat_id = s["chat_id"]
        msg_id = str(s["msg_id"])
        from_ts = parse_msg_time_ms(s["msg_time"]) + 60_000  # +1 хв на всяк
        print(f"\n=== {s['channel_title']} chat={chat_id} msg={msg_id} ===")

        sample_dump = {"meta": s, "probes": {}}

        for op in PROBE_OPCODES:
            if op == 49:
                payload = {"chatId": chat_id, "from": from_ts,
                           "forward": 0, "backward": 20, "getMessages": True}
            elif op == 66:
                # припущення: get message by id
                payload = {"chatId": chat_id, "messageId": msg_id}
            elif op in (130, 131):
                # припущення: get reactions/views stats
                payload = {"chatId": chat_id, "messageIds": [msg_id]}
            else:
                continue

            resp = await p._send(op, payload, timeout=8)
            if resp is None:
                sample_dump["probes"][op] = {"status": "timeout"}
                print(f"  op={op}: timeout")
                continue
            if resp.get("cmd") == 3:
                sample_dump["probes"][op] = {"status": "error", "payload": resp.get("payload")}
                print(f"  op={op}: error {resp.get('payload')}")
                continue

            sample_dump["probes"][op] = {"status": "ok", "payload": resp.get("payload")}
            view_hits = find_view_like_keys(resp.get("payload", {}))
            if view_hits:
                print(f"  op={op}: ✅ знайдено view-like keys:")
                for path, val in view_hits:
                    print(f"    {path} = {val!r}")
            else:
                # для op=49 ще покажемо ключі першого повідомлення
                msgs = (resp.get("payload") or {}).get("messages", [])
                if msgs:
                    print(f"  op={op}: повідомлень={len(msgs)}, ключі m[0]: {list(msgs[0].keys())}")
                else:
                    print(f"  op={op}: ok, але без messages, top-keys: {list((resp.get('payload') or {}).keys())}")

        dump["samples"].append(sample_dump)
        await asyncio.sleep(0.5)

    await p.close()

    OUT_FILE.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nПовний дамп: {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
