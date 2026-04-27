"""
MAX Messenger — Playwright розвідка.
Перехоплює реальні XHR/Fetch запити браузера для знаходження JSON API.
"""

import asyncio
import json
import argparse
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Route, Request, Response


LOG_FILE = Path("playwright_results.json")
captured = []


def ts() -> str:
    return datetime.now().isoformat()


async def handle_response(response: Response):
    url = response.url
    status = response.status
    content_type = response.headers.get("content-type", "")

    # Ігноруємо лише зображення і шрифти
    if any(ext in url for ext in (".png", ".ico", ".woff", ".woff2", ".svg", ".gif", ".jpg")):
        return

    entry = {
        "time": ts(),
        "url": url,
        "status": status,
        "content_type": content_type,
        "request_headers": dict(response.request.headers),
        "response_headers": dict(response.headers),
        "body": None,
    }

    try:
        if "json" in content_type:
            entry["body"] = await response.json()
        elif "text" in content_type and "html" not in content_type:
            text = await response.text()
            entry["body"] = text[:2000]
        else:
            # HTML — зберігаємо тільки перші 500 символів
            text = await response.text()
            entry["body"] = text[:500]
    except Exception as e:
        entry["body"] = f"[read error: {e}]"

    captured.append(entry)

    # Позначаємо цікаві знахідки
    is_json = "json" in content_type
    marker = " *** JSON ***" if is_json else ""
    print(f"  [{status}] {url}{marker}", flush=True)


async def capture_ws(ws):
    url = ws.url
    print(f"  [WS CONNECT] {url}", flush=True)
    captured.append({"time": ts(), "label": "websocket_connect", "url": url})

    def on_frame_sent(payload):
        entry = {"time": ts(), "label": "ws_sent", "url": url, "payload": payload[:1000] if payload else None}
        captured.append(entry)
        print(f"  [WS SENT] {str(payload)[:200]}", flush=True)

    def on_frame_received(payload):
        entry = {"time": ts(), "label": "ws_received", "url": url, "payload": payload[:2000] if payload else None}
        captured.append(entry)
        print(f"  [WS RECV] {str(payload)[:300]}", flush=True)

    ws.on("framesent", lambda p: on_frame_sent(p if isinstance(p, str) else p.payload))
    ws.on("framereceived", lambda p: on_frame_received(p if isinstance(p, str) else p.payload))
    ws.on("close", lambda: print(f"  [WS CLOSE] {url}", flush=True))


async def scout_channel(page, channel: str, base_url: str):
    url = f"{base_url}/{channel}"
    print(f"\n=== Відкриваю: {url} ===", flush=True)

    try:
        # domcontentloaded — не чекаємо networkidle, JS запуститься сам
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  [ПОМИЛКА завантаження]: {e}", flush=True)
        return

    # Чекаємо поки JS виконається і зроблені XHR запити
    print("  Чекаємо виконання JS...", flush=True)
    await asyncio.sleep(8)

    # Прокрутка вниз — тригерить підвантаження нових повідомлень
    print("  Прокручую сторінку...", flush=True)
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(1.5)

    # Ще раз чекаємо мережу
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    print(f"  Завершено: {channel}", flush=True)


async def run(channels: list[str], proxy: str | None):
    proxy_config = None
    if proxy:
        # Playwright потребує окремий формат проксі
        proxy_config = {"server": proxy}
        print(f"Проксі: {proxy}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_config,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Маскуємо headless
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                # Замінюємо HeadlessChrome на звичайний Chrome
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )

        page = await context.new_page()

        # Прибираємо webdriver флаг
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page.on("response", handle_response)

        # Перехоплення WebSocket фреймів
        page.on("websocket", lambda ws: asyncio.ensure_future(capture_ws(ws)))

        for channel in channels:
            await scout_channel(page, channel, "https://web.max.ru")

        await browser.close()


def save_and_summarize():
    LOG_FILE.write_text(
        json.dumps(captured, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nВсього перехоплено запитів: {len(captured)}")
    print(f"Збережено у: {LOG_FILE.resolve()}")

    json_entries = [e for e in captured if e.get("body") and "json" in e.get("content_type", "")]
    if json_entries:
        print(f"\n=== JSON відповіді ({len(json_entries)}) — це і є API ===")
        for e in json_entries:
            print(f"\n  [{e['status']}] {e['url']}")
            body = e["body"]
            if isinstance(body, dict):
                keys = list(body.keys())
                print(f"  Ключі: {keys}")
                preview = json.dumps(body, ensure_ascii=False)[:400]
            else:
                preview = str(body)[:400]
            print(f"  Тіло: {preview}")
    else:
        print("\nJSON відповідей не знайдено.")
        print("Можливо сайт використовує WebSocket або інший механізм передачі даних.")


def parse_args():
    parser = argparse.ArgumentParser(description="MAX Messenger Playwright Scout")
    parser.add_argument("--proxy", default=None, help="socks5://user:pass@host:port")
    parser.add_argument("--channels", nargs="+", required=True, help="Username каналів")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print("MAX Playwright Scout")
    print(f"Канали: {args.channels}", flush=True)

    asyncio.run(run(args.channels, args.proxy))
    save_and_summarize()
