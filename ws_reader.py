"""
MAX WS Reader — читає публічні канали без підписки.

Стратегія мінімізації ризиків:
- op=75 subscribe замість polling — сервер сам надсилає нові пости (push)
- op=49 як fallback раз на FALLBACK_INTERVAL секунд якщо push мовчить
- Один постійний WS conn з ping_interval=30
- device_id зберігається між запусками (файл .device_id)
- Затримка RESOLVE_DELAY між op=89 запитами — не спамимо сервер
"""

import asyncio
import json
import time
import sys
import os
from pathlib import Path
from datetime import datetime

import websockets

# ── Конфіг ────────────────────────────────────────────────────────────────────

WS_URL = "wss://ws-api.oneme.ru/websocket"
LOGIN_TOKEN_FILE = Path(__file__).parent / ".login_token"
DEVICE_ID_FILE   = Path(__file__).parent / ".device_id"
CHANNELS_DIR     = Path(__file__).parent / "channels"

RESOLVE_DELAY    = 1.5    # секунд між op=89 запитами
FALLBACK_INTERVAL = 120   # секунд — якщо push мовчить, робимо op=49
RECONNECT_DELAY  = 15     # секунд між reconnect-спробами

# ── Утиліти ───────────────────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%d.%m %H:%M:%S")

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
    print(f"Створи файл: {LOGIN_TOKEN_FILE}")
    print("Вміст: LOGIN токен з авторизації (op=115 tokenAttrs.LOGIN.token)")
    sys.exit(1)

def load_channels() -> list[str]:
    aliases = []
    for f in CHANNELS_DIR.glob("*.txt"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                aliases.append(line)
    if not aliases:
        print(f"ПОМИЛКА: не знайдено каналів у {CHANNELS_DIR}/")
        print("Додай aliases каналів у .txt файл (один на рядок)")
        sys.exit(1)
    return list(dict.fromkeys(aliases))  # дедупліація зі збереженням порядку

# ── WS клієнт ─────────────────────────────────────────────────────────────────

class MaxClient:
    def __init__(self, token: str, device_id: str):
        self.token = token
        self.device_id = device_id
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._ws = None
        self._recv_task = None

    def _ns(self) -> int:
        self._seq += 1
        return self._seq

    async def _recv_loop(self):
        """Фоновий диспетчер — розкидає відповіді та push-події."""
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            seq = msg.get("seq")
            cmd = msg.get("cmd", 0)
            # cmd=1 — відповідь, cmd=3 — помилка
            if cmd in (1, 3) and seq in self._pending:
                fut = self._pending.pop(seq)
                if not fut.done():
                    fut.set_result(msg)
            elif cmd == 0:
                # push-подія — обробляємо в on_push
                await self._on_push(msg)

    async def _on_push(self, msg: dict):
        """Обробка серверних push-подій (cmd=0)."""
        op = msg.get("opcode")
        payload = msg.get("payload") or {}

        if op == 55:
            # Нові повідомлення в каналі (push після op=75 subscribe)
            messages = payload.get("messages", [])
            chat_id = payload.get("chatId")
            for m in messages:
                text = m.get("text", "")
                mid = m.get("id")
                mts = datetime.fromtimestamp(m.get("time", 0) / 1000).strftime("%d.%m %H:%M")
                print(f"[{ts()}] PUSH chatId={chat_id} [{mts}] {text[:200]}", flush=True)

    async def _send_recv(self, opcode: int, payload: dict, timeout: float = 10) -> dict | None:
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

    async def connect(self) -> bool:
        headers = {
            "Origin": "https://web.max.ru",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        }
        self._ws = await websockets.connect(WS_URL, additional_headers=headers, ping_interval=30)
        self._recv_task = asyncio.create_task(self._recv_loop())

        # Handshake
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
            print(f"[{ts()}] Handshake FAIL: {hs}", flush=True)
            return False

        # Login
        login = await self._send_recv(19, {"token": self.token}, timeout=15)
        if not login or login.get("cmd") == 3 or not (login.get("payload") or {}).get("profile"):
            err = (login.get("payload") or {}).get("message", "unknown") if login else "timeout"
            print(f"[{ts()}] Login FAIL: {err}", flush=True)
            return False

        name = (login["payload"]["profile"].get("contact") or {}).get("names", [{}])[0].get("name", "?")
        print(f"[{ts()}] Авторизовано: {name}", flush=True)

        # Drain початкових push після логіну
        await asyncio.sleep(2)
        return True

    async def resolve_channel(self, alias: str) -> int | None:
        """op=89: alias → chatId."""
        resp = await self._send_recv(89, {"link": f"https://max.ru/{alias}"})
        if not resp or resp.get("cmd") == 3:
            err = (resp.get("payload") or {}).get("error", "?") if resp else "timeout"
            print(f"[{ts()}] op=89 FAIL {alias}: {err}", flush=True)
            return None
        chat = (resp.get("payload") or {}).get("chat", {})
        chat_id = chat.get("id")
        title = chat.get("title", alias)
        subs = chat.get("participantsCount", 0)
        print(f"[{ts()}] Канал: {title} | chatId={chat_id} | підписників={subs:,}", flush=True)
        return chat_id

    async def subscribe(self, chat_id: int):
        """op=75: підписка на push нових повідомлень."""
        await self._send_recv(75, {"chatId": chat_id, "subscribe": True})

    async def get_messages(self, chat_id: int, count: int = 20, from_ts: int | None = None) -> list[dict]:
        """op=49: отримати останні повідомлення."""
        if from_ts is None:
            from_ts = int(time.time() * 1000)
        resp = await self._send_recv(49, {
            "chatId": chat_id, "from": from_ts,
            "forward": 0, "backward": count, "getMessages": True,
        })
        if not resp or resp.get("cmd") == 3:
            return []
        return (resp.get("payload") or {}).get("messages", [])

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()


# ── Головний цикл ─────────────────────────────────────────────────────────────

async def monitor(channels: list[str]):
    token = get_login_token()
    device_id = get_device_id()
    print(f"[{ts()}] device_id={device_id}", flush=True)
    print(f"[{ts()}] Каналів до моніторингу: {len(channels)}", flush=True)

    # chat_id -> остання відома відмітка часу (для fallback polling)
    last_seen: dict[int, int] = {}
    channel_ids: dict[str, int] = {}  # alias -> chat_id

    while True:
        client = MaxClient(token, device_id)
        try:
            ok = await client.connect()
            if not ok:
                await client.close()
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            # Резолвимо всі канали з затримкою між запитами
            for alias in channels:
                chat_id = await client.resolve_channel(alias)
                if chat_id:
                    channel_ids[alias] = chat_id
                    await client.subscribe(chat_id)
                    # Читаємо початкові повідомлення
                    msgs = await client.get_messages(chat_id, count=5)
                    if msgs:
                        last_seen[chat_id] = max(m.get("time", 0) for m in msgs)
                        print(f"[{ts()}]   Останнє повідомлення: {datetime.fromtimestamp(last_seen[chat_id]/1000).strftime('%d.%m %H:%M')}", flush=True)
                await asyncio.sleep(RESOLVE_DELAY)

            print(f"\n[{ts()}] Моніторинг {len(channel_ids)} каналів. Ctrl+C для зупинки.\n", flush=True)

            # Основний цикл — fallback polling кожні FALLBACK_INTERVAL секунд
            while True:
                await asyncio.sleep(FALLBACK_INTERVAL)
                now_ts = int(time.time() * 1000)
                for alias, chat_id in channel_ids.items():
                    msgs = await client.get_messages(chat_id, count=10, from_ts=now_ts)
                    new = [m for m in msgs if m.get("time", 0) > last_seen.get(chat_id, 0)]
                    if new:
                        last_seen[chat_id] = max(m.get("time", 0) for m in new)
                        for m in new:
                            mts = datetime.fromtimestamp(m.get("time", 0) / 1000).strftime("%d.%m %H:%M")
                            print(f"[{ts()}] POLL [{alias}] [{mts}] {m.get('text','')[:200]}", flush=True)
                    await asyncio.sleep(0.5)  # між каналами

        except (websockets.ConnectionClosed, OSError) as e:
            print(f"[{ts()}] З'єднання розірвано: {e}. Reconnect через {RECONNECT_DELAY}s...", flush=True)
        except Exception as e:
            print(f"[{ts()}] Помилка: {e}. Reconnect через {RECONNECT_DELAY}s...", flush=True)
        finally:
            await client.close()

        await asyncio.sleep(RECONNECT_DELAY)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    channels = load_channels()
    try:
        asyncio.run(monitor(channels))
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Зупинено.", flush=True)
