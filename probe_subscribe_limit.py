"""
Тест гіпотези: MAX має server-side ліміт на кількість активних subscriptions
per token. Якщо так — взявши малу кількість missed-каналів (50) у окрему
сесію, ми побачимо push'і майже від усіх. Якщо ні — результат буде як у
основного парсера (більшість мовчить).

Що робить:
- Завантажує dead_channels_report.json
- Вибирає 50 каналів зі статусом 'missed' (відсортовано за subs — топ-активних)
- Subscribe тільки на них через токен _b
- Слухає LISTEN_SECONDS секунд
- Рахує per-channel push events і скільки унікальних каналів активувались

ВАЖЛИВО: запускати ТIЛЬКИ при зупиненому max-parser-b (одна сесія на токен).

    systemctl stop max-parser-b
    python3 /root/probe_subscribe_limit.py
    systemctl start max-parser-b
"""
import asyncio
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import websockets

from ws_common import (
    WS_URL, WS_HEADERS, get_device_id, get_login_token,
    handshake_payload, make_msg,
)

ROOT             = Path(__file__).parent
REPORT_FILE      = ROOT / "dead_channels_report.json"
LOGIN_TOKEN_FILE = ROOT / ".login_token_b"
DEVICE_ID_FILE   = ROOT / ".device_id_b"

PROBE_SIZE     = 200
LISTEN_SECONDS = 1800  # 30 хв — щоб мати статистично надійну вибірку push events


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def main():
    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    missed = [c for c in report["channels"] if c["status"] == "missed"]
    missed.sort(key=lambda c: c.get("subs", 0), reverse=True)
    probe = missed[:PROBE_SIZE]
    print(f"[{ts()}] Probe: {len(probe)} топ-активних missed-каналів", flush=True)
    for c in probe[:10]:
        print(f"  {c['title'][:60]:<60} subs={c.get('subs',0)}")
    print(f"  ... (+{len(probe)-10} ще)" if len(probe) > 10 else "")

    chat_ids = [c["chat_id"] for c in probe]
    id_to_title = {c["chat_id"]: c["title"] for c in probe}

    token = get_login_token(file_path=LOGIN_TOKEN_FILE)
    device_id = get_device_id(file_path=DEVICE_ID_FILE)

    ws = await websockets.connect(
        WS_URL, additional_headers=WS_HEADERS,
        ping_interval=30, ping_timeout=20, open_timeout=15,
    )
    seq = [0]
    pending: dict[int, asyncio.Future] = {}
    push_counts: Counter = Counter()

    def next_seq():
        seq[0] += 1
        return seq[0]

    async def recv_loop():
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            s = msg.get("seq")
            cmd = msg.get("cmd", 0)
            if cmd in (1, 3) and s in pending:
                fut = pending.pop(s)
                if not fut.done():
                    fut.set_result(msg)
            elif cmd == 0:
                op = msg.get("opcode")
                payload = msg.get("payload") or {}
                chat_id = payload.get("chatId")
                if op in (128, 55) and chat_id in id_to_title:
                    push_counts[chat_id] += 1

    async def send_recv(opcode: int, payload: dict, timeout: float = 15):
        s = next_seq()
        fut = asyncio.get_running_loop().create_future()
        pending[s] = fut
        await ws.send(make_msg(s, opcode, payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            pending.pop(s, None)
            return None

    recv_task = asyncio.create_task(recv_loop())

    hs = await send_recv(6, handshake_payload(device_id))
    if not hs or hs.get("cmd") == 3:
        print(f"[{ts()}] handshake fail")
        return
    login = await send_recv(19, {"token": token})
    if not login or not (login.get("payload") or {}).get("profile"):
        print(f"[{ts()}] login fail")
        return
    print(f"[{ts()}] WS connected + login OK", flush=True)
    await asyncio.sleep(1.5)

    for cid in chat_ids:
        await ws.send(make_msg(next_seq(), 75, {"chatId": cid, "subscribe": True}))
        await asyncio.sleep(0.05)
    print(f"[{ts()}] Subscribed на {len(chat_ids)} каналів, слухаю {LISTEN_SECONDS}с", flush=True)

    t_start = time.time()
    while time.time() - t_start < LISTEN_SECONDS:
        await asyncio.sleep(30)
        active = len(push_counts)
        total_pushes = sum(push_counts.values())
        print(f"[{ts()}] +{int(time.time()-t_start)}с: "
              f"активних каналів {active}/{len(chat_ids)}, push events {total_pushes}",
              flush=True)

    recv_task.cancel()
    await ws.close()

    print()
    print(f"=== ПІДСУМОК (за {LISTEN_SECONDS}с) ===")
    print(f"  Subscribe'нуто: {len(chat_ids)} каналів")
    print(f"  Активних (отримали хоч 1 push): {len(push_counts)} ({len(push_counts)*100//len(chat_ids)}%)")
    print(f"  Усього push events: {sum(push_counts.values())}")
    print()
    print("Топ-10 за активністю:")
    for cid, n in push_counts.most_common(10):
        print(f"  {n:4d}  {id_to_title.get(cid, str(cid))[:60]}")
    print()
    silent = [cid for cid in chat_ids if cid not in push_counts]
    print(f"Мовчазні ({len(silent)}):")
    for cid in silent[:10]:
        print(f"        {id_to_title.get(cid, str(cid))[:60]}")
    if len(silent) > 10:
        print(f"        ... +{len(silent)-10} ще")


if __name__ == "__main__":
    asyncio.run(main())
