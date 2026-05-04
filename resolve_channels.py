"""
MAX — резолвінг каналів: aliases → chatIds.
Читає channels/channels.txt, перевіряє які існують через op=89.
Результат зберігає у channels/resolved.json і виводить статистику.
"""

import asyncio
import json
import time
from pathlib import Path
from datetime import datetime

import websockets

from ws_common import (
    WS_URL, WS_HEADERS, get_device_id, get_login_token,
    handshake_payload, make_msg,
)

CHANNELS_FILE    = Path(__file__).parent / "channels" / "channels.txt"
RESOLVED_FILE    = Path(__file__).parent / "channels" / "resolved.json"
FAILED_FILE      = Path(__file__).parent / "channels" / "failed.txt"

BATCH_SIZE       = 15    # паралельних op=89 за раз
BATCH_DELAY      = 0.3   # секунд між батчами
SAVE_EVERY       = 100   # зберігати проміжний результат кожні N каналів


def ts():
    return datetime.now().strftime("%H:%M:%S")

def load_aliases() -> list[str]:
    aliases = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            aliases.append(line.lstrip("@").split("/")[-1])
    return list(dict.fromkeys(aliases))


class Resolver:
    def __init__(self, token: str, device_id: str):
        self.token = token
        self.device_id = device_id
        self._seq = 0
        self._ws = None

    def _ns(self) -> int:
        self._seq += 1
        return self._seq

    async def connect(self) -> bool:
        self._ws = await websockets.connect(WS_URL, additional_headers=WS_HEADERS, ping_interval=30)

        # Handshake
        s = self._ns()
        await self._ws.send(make_msg(s, 6, handshake_payload(self.device_id)))
        hs = await self._recv_seq(s, timeout=8)
        if not hs or hs.get("cmd") != 1:
            print(f"Handshake FAIL: {hs}")
            return False

        # Login
        s = self._ns()
        await self._ws.send(make_msg(s, 19, {"token": self.token}))
        login = await self._recv_seq(s, timeout=15)
        if not login or login.get("cmd") != 1 or not (login.get("payload") or {}).get("profile"):
            print(f"Login FAIL")
            return False

        await asyncio.sleep(1.5)  # drain початкових push
        return True

    async def _recv_seq(self, target_seq: int, timeout: float = 10) -> dict | None:
        """Читає повідомлення поки не знайде відповідь з потрібним seq."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=min(3, deadline - time.time()))
                msg = json.loads(raw)
                if msg.get("seq") == target_seq and msg.get("cmd") in (1, 3):
                    return msg
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        return None

    async def resolve_batch(self, aliases: list[str]) -> tuple[dict[str, int], list[str]]:
        """Паралельно відправляємо BATCH_SIZE запитів, збираємо відповіді."""
        seq_to_alias: dict[int, str] = {}

        for alias in aliases:
            s = self._ns()
            seq_to_alias[s] = alias
            await self._ws.send(make_msg(s, 89, {"link": f"https://max.ru/{alias}"}))

        found: dict[str, int] = {}
        failed: list[str] = []
        remaining = set(seq_to_alias.keys())
        deadline = time.time() + 12

        while remaining and time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=3)
                msg = json.loads(raw)
                s = msg.get("seq")
                if s not in remaining:
                    continue
                remaining.discard(s)
                alias = seq_to_alias[s]
                if msg.get("cmd") == 1:
                    chat = (msg.get("payload") or {}).get("chat", {})
                    chat_id = chat.get("id")
                    if chat_id:
                        found[alias] = {
                            "id":    chat_id,
                            "title": chat.get("title", alias),
                            "subs":  chat.get("participantsCount", 0),
                        }
                    else:
                        failed.append(alias)
                else:
                    failed.append(alias)
            except asyncio.TimeoutError:
                break
            except Exception:
                break

        # Таймаут для решти
        for s in remaining:
            failed.append(seq_to_alias[s])

        return found, failed

    async def close(self):
        if self._ws:
            await self._ws.close()


async def main():
    token = get_login_token()
    device_id = get_device_id()
    aliases = load_aliases()

    # Завантаження попереднього прогресу
    resolved: dict[str, dict] = {}
    failed_set: set[str] = set()
    if RESOLVED_FILE.exists():
        resolved = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
        print(f"[{ts()}] Попередній кеш: {len(resolved)} знайдених каналів", flush=True)

    # Пропускаємо вже перевірені
    to_check = [a for a in aliases if a not in resolved]
    print(f"[{ts()}] Всього в списку: {len(aliases)}", flush=True)
    print(f"[{ts()}] Вже перевірено:  {len(aliases) - len(to_check)}", flush=True)
    print(f"[{ts()}] Потрібно перевірити: {len(to_check)}", flush=True)

    if not to_check:
        print(f"\n[{ts()}] Всі канали вже резолвлені!")
        _print_stats(aliases, resolved)
        return

    eta_sec = (len(to_check) / BATCH_SIZE) * BATCH_DELAY + len(to_check) * 0.05
    print(f"[{ts()}] Орієнтовний час: ~{int(eta_sec // 60)} хв {int(eta_sec % 60)} сек", flush=True)
    print(f"[{ts()}] Підключення...\n", flush=True)

    resolver = Resolver(token, device_id)
    if not await resolver.connect():
        print("Не вдалось підключитись.")
        return

    failed_aliases: list[str] = []
    done = 0
    start = time.time()

    for i in range(0, len(to_check), BATCH_SIZE):
        batch = to_check[i:i + BATCH_SIZE]
        found, failed = await resolver.resolve_batch(batch)
        resolved.update(found)
        failed_aliases.extend(failed)
        done += len(batch)

        # Прогрес
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(to_check) - done) / rate if rate > 0 else 0
        print(
            f"[{ts()}] {done}/{len(to_check)} | знайдено: {len(resolved)} | "
            f"не знайдено: {len(failed_aliases)} | "
            f"швидкість: {rate:.1f}/с | залишилось: ~{int(eta)}с",
            flush=True
        )

        # Проміжне збереження
        if done % SAVE_EVERY == 0:
            RESOLVED_FILE.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")

        await asyncio.sleep(BATCH_DELAY)

    await resolver.close()

    # Фінальне збереження
    RESOLVED_FILE.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    FAILED_FILE.write_text("\n".join(failed_aliases), encoding="utf-8")

    print(f"\n{'='*50}")
    _print_stats(aliases, resolved)
    print(f"\nЗбережено:")
    print(f"  Знайдені → {RESOLVED_FILE}")
    print(f"  Не знайдені → {FAILED_FILE}")


def _print_stats(aliases: list[str], resolved: dict):
    found_count = sum(1 for a in aliases if a in resolved)
    not_found = len(aliases) - found_count
    pct = found_count / len(aliases) * 100 if aliases else 0
    print(f"РЕЗУЛЬТАТ:")
    print(f"  Всього в списку:  {len(aliases)}")
    print(f"  Знайдено на MAX:  {found_count} ({pct:.1f}%)")
    print(f"  Не знайдено:      {not_found} ({100-pct:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
