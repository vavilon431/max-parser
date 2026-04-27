"""
MAX Messenger — WS розвідка з авторизацією.
Підключається напряму до wss://ws-api.oneme.ru/websocket,
авторизується (opcode 19), потім читає канал і логує всі відповіді.
"""

import asyncio
import json
import time
import sys
from datetime import datetime
from pathlib import Path

import websockets

WS_URL = "wss://ws-api.oneme.ru/websocket"
LOG_FILE = Path("ws_auth_results.json")

captured = []
seq = 0


def ts():
    return datetime.now().isoformat()


def next_seq():
    global seq
    seq += 1
    return seq


def make_msg(opcode: int, payload: dict, cmd: int = 0) -> str:
    return json.dumps({
        "ver": 11,
        "cmd": cmd,
        "seq": next_seq(),
        "opcode": opcode,
        "payload": payload,
    }, ensure_ascii=False)


def log(label: str, data):
    entry = {"time": ts(), "label": label, "data": data}
    captured.append(entry)
    preview = json.dumps(data, ensure_ascii=False)[:300] if isinstance(data, dict) else str(data)[:300]
    print(f"[{label}] {preview}", flush=True)


async def recv_with_timeout(ws, timeout=10):
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        log(f"RECV opcode={msg.get('opcode')} seq={msg.get('seq')}", msg)
        return msg
    except asyncio.TimeoutError:
        return None


async def drain(ws, count=5, timeout=3):
    """Читаємо кілька повідомлень підряд."""
    results = []
    for _ in range(count):
        msg = await recv_with_timeout(ws, timeout)
        if msg is None:
            break
        results.append(msg)
    return results


async def run(phone: str, password: str, channels: list[str]):
    headers = {
        "Origin": "https://web.max.ru",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    }

    print(f"Підключаємось до {WS_URL}...", flush=True)

    async with websockets.connect(WS_URL, additional_headers=headers, ping_interval=30) as ws:
        print("З'єднання встановлено.", flush=True)

        # --- Handshake (opcode 6) ---
        handshake_payload = {
            "app_id": "web",
            "version": "1.0.0",
            "lang": "ru",
            "device_id": f"web_{int(time.time())}",
        }
        await ws.send(make_msg(6, handshake_payload))
        log("SENT handshake opcode=6", handshake_payload)

        # Читаємо відповідь на handshake
        hs_resp = await recv_with_timeout(ws, 10)
        if not hs_resp:
            print("Немає відповіді на handshake!", flush=True)
            return

        # --- Login (opcode 19) ---
        print(f"\nАвторизуємось: {phone}", flush=True)
        login_payload = {
            "login": phone,
            "password": password,
            "remember": True,
        }
        await ws.send(make_msg(19, login_payload))
        log("SENT login opcode=19", {"login": phone, "password": "***"})

        # Читаємо відповіді на логін
        print("Очікуємо відповідь авторизації...", flush=True)
        auth_msgs = await drain(ws, count=10, timeout=8)

        token = None
        for msg in auth_msgs:
            payload = msg.get("payload", {})
            if isinstance(payload, dict):
                # Шукаємо токен в різних полях
                token = payload.get("token") or payload.get("auth_token") or payload.get("session_token")
                if token:
                    print(f"Токен знайдено: {token[:40]}...", flush=True)
                    break
                if payload.get("error"):
                    print(f"Помилка авторизації: {payload}", flush=True)
                    return

        if not token:
            print("Токен не знайдено в відповіді. Дивись ws_auth_results.json для деталей.", flush=True)

        # --- Читаємо канали ---
        for channel in channels:
            print(f"\n=== Читаємо канал: {channel} ===", flush=True)

            # Пробуємо різні opcodes для отримання каналу/повідомлень
            # Спочатку — знайти канал по username
            for opcode, label, payload in [
                # Пошук каналу
                (37, "search", {"query": channel, "limit": 5}),
                (38, "get_channel_by_username", {"username": channel}),
                (50, "get_chat", {"username": channel}),
                (51, "get_chat_info", {"alias": channel}),
                # Прямий запит повідомлень
                (100, "get_messages", {"username": channel, "limit": 20}),
                (101, "get_channel_messages", {"channel": channel, "limit": 20}),
                (200, "feed_request", {"source": channel, "count": 20}),
                (201, "channel_feed", {"alias": channel, "offset": 0, "limit": 20}),
                (256, "opcode_256", {"username": channel}),
                (257, "opcode_257", {"alias": channel}),
                (300, "opcode_300", {"channel_id": channel}),
            ]:
                await ws.send(make_msg(opcode, payload))
                log(f"SENT {label} opcode={opcode}", payload)
                await asyncio.sleep(0.3)

            # Читаємо всі відповіді
            print(f"Читаємо відповіді для {channel}...", flush=True)
            responses = await drain(ws, count=30, timeout=4)
            print(f"Отримано {len(responses)} повідомлень", flush=True)

        # Ще раз drain — можуть бути push-події
        print("\nФінальний drain...", flush=True)
        await drain(ws, count=20, timeout=5)

    save_results()


def save_results():
    LOG_FILE.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nВсього повідомлень: {len(captured)}")
    print(f"Збережено: {LOG_FILE.resolve()}")

    # Підсвічуємо цікаві opcodes
    interesting = [
        e for e in captured
        if e["label"].startswith("RECV")
        and isinstance(e.get("data"), dict)
        and e["data"].get("payload")
    ]
    print(f"\n=== Отримані відповіді з payload ({len(interesting)}) ===")
    for e in interesting:
        d = e["data"]
        op = d.get("opcode")
        p = d.get("payload", {})
        preview = json.dumps(p, ensure_ascii=False)[:200] if isinstance(p, dict) else str(p)[:200]
        print(f"  opcode={op}: {preview}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--channels", nargs="+", required=True)
    args = parser.parse_args()

    asyncio.run(run(args.phone, args.password, args.channels))
