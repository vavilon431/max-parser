"""
MAX Messenger — розвідувальний скрипт для аналізу публічних каналів.
Логує всі HTTP-запити і відповіді, щоб знайти API-ендпоінти.
"""

import asyncio
import json
import sys
import argparse
from datetime import datetime
from pathlib import Path

import httpx


# ── Налаштування ─────────────────────────────────────────────────────────────

DEFAULT_CHANNELS = [
    # Додай сюди username публічних каналів MAX, наприклад "rt" або "ria_novosti"
]

BASE_URLS = [
    "https://max.ru",
    "https://web.max.ru",
    "https://app.max.ru",
]

CANDIDATE_API_PATTERNS = [
    "/api/v1/channels/{channel}",
    "/api/v1/channels/{channel}/messages",
    "/v1/channels/{channel}",
    "/v1/channels/{channel}/feed",
    "/channels/{channel}",
    "/channels/{channel}/posts",
    "/channel/{channel}",
    "/public/{channel}",
    "/c/{channel}",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

LOG_FILE = Path("scout_results.json")


# ── Логування ─────────────────────────────────────────────────────────────────

results = []

def log(label: str, data: dict):
    entry = {"time": datetime.now().isoformat(), "label": label, **data}
    results.append(entry)
    status = data.get("status")
    url = data.get("url", "")
    print(f"  [{status}] {url}", flush=True)


# ── Перевірка одного URL ──────────────────────────────────────────────────────

async def probe(client: httpx.AsyncClient, url: str, label: str):
    try:
        r = await client.get(url, follow_redirects=True, timeout=5)
        content_type = r.headers.get("content-type", "")
        is_json = "json" in content_type
        body_preview = ""
        if is_json:
            try:
                body_preview = json.dumps(r.json(), ensure_ascii=False)[:500]
            except Exception:
                body_preview = r.text[:500]
        else:
            body_preview = r.text[:300]

        log(label, {
            "url": url,
            "status": r.status_code,
            "content_type": content_type,
            "redirected_to": str(r.url) if str(r.url) != url else None,
            "body_preview": body_preview,
        })
        return r.status_code
    except httpx.ConnectError:
        log(label, {"url": url, "status": "CONNECT_ERROR"})
    except httpx.TimeoutException:
        log(label, {"url": url, "status": "TIMEOUT"})
    except Exception as e:
        log(label, {"url": url, "status": f"ERROR: {e}"})
    return None


# ── Головна логіка ────────────────────────────────────────────────────────────

async def run(channels: list[str], proxy: str | None):
    transport_kwargs = {}
    if proxy:
        transport_kwargs["proxy"] = proxy
        print(f"\nВикористовую проксі: {proxy}")

    async with httpx.AsyncClient(headers=HEADERS, **transport_kwargs) as client:

        # 1. Перевіряємо чи взагалі доступний домен
        print("\n=== Перевірка доступності доменів ===")
        for base in BASE_URLS:
            await probe(client, base, "domain_check")

        # 2. Пробуємо прямий URL каналу (як він відкривається в браузері)
        for channel in channels:
            print(f"\n=== Канал: {channel} ===", flush=True)

            # Прямий URL
            for base in BASE_URLS:
                await probe(client, f"{base}/{channel}", "channel_direct")

            # Кандидати API-ендпоінтів
            print(f"  -- Перебираємо API-шаблони --")
            for base in BASE_URLS:
                for pattern in CANDIDATE_API_PATTERNS:
                    url = base + pattern.format(channel=channel)
                    await probe(client, url, "api_candidate")

        # 3. Пробуємо публічне API без каналу
        print("\n=== Загальні API-ендпоінти ===")
        general_paths = [
            "/api/v1/",
            "/api/",
            "/v1/",
            "/.well-known/",
            "/openapi.json",
            "/swagger.json",
        ]
        for base in BASE_URLS:
            for path in general_paths:
                await probe(client, base + path, "general_api")


# ── Збереження результатів ────────────────────────────────────────────────────

def save_results():
    LOG_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\nРезультати збережено у: {LOG_FILE.resolve()}")

    # Підсумок цікавих знахідок
    interesting = [
        r for r in results
        if isinstance(r.get("status"), int) and r["status"] in (200, 201, 301, 302)
    ]
    if interesting:
        print(f"\n=== Цікаві відповіді ({len(interesting)}) ===")
        for r in interesting:
            print(f"  [{r['status']}] {r['url']}")
            if r.get("body_preview"):
                print(f"       {r['body_preview'][:200]}")
    else:
        print("\nЖодної успішної відповіді не знайдено.")
        print("Спробуй інший проксі або додай правильні username каналів.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="MAX Messenger — розвідка публічних каналів"
    )
    parser.add_argument(
        "--proxy",
        help="Адреса проксі, наприклад: socks5://user:pass@1.2.3.4:1080 або http://1.2.3.4:8080",
        default=None,
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        help="Username каналів для перевірки, наприклад: rt ria_novosti",
        default=DEFAULT_CHANNELS,
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if not args.channels:
        print("Помилка: вкажи хоча б один канал через --channels")
        print("Приклад: python scout.py --proxy socks5://1.2.3.4:1080 --channels rt")
        sys.exit(1)

    print("MAX Messenger Scout")
    print(f"Канали: {args.channels}")

    asyncio.run(run(args.channels, args.proxy))
    save_results()
