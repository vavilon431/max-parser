"""
Спільні WS-хелпери для всіх клієнтів MAX (ws_parser, views_fetcher, resolve_channels).

Раніше handshake/login + константи + читання токена/device_id дублювалися в трьох
файлах. Тепер усе тут, кожен клас будує своє поверх цих хелперів.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WS_URL           = "wss://ws-api.oneme.ru/websocket"
_ROOT            = Path(__file__).parent
LOGIN_TOKEN_FILE = _ROOT / ".login_token"
DEVICE_ID_FILE   = _ROOT / ".device_id"

# Точка старту проекту: 22.04.2026 00:00 MSK. Усе раніше — не наше:
# парсер ігнорує такі пости (репости старого), backfill зупиняє пагінацію.
_MSK = timezone(timedelta(hours=3))
PROJECT_START_DT = datetime(2026, 4, 22, 0, 0, 0, tzinfo=_MSK)
PROJECT_START_MS = int(PROJECT_START_DT.timestamp() * 1000)
PROJECT_START_STR = "2026-04-22 00:00:00"  # для SQL/string-фільтрів по msg_time (MSK)

_USER_AGENT_HTTP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")

WS_HEADERS = {
    "Origin": "https://web.max.ru",
    "User-Agent": _USER_AGENT_HTTP,
}


def get_device_id(file_path: Path | str | None = None) -> str:
    """Читає або створює персистентний deviceId — використовується у handshake.

    file_path: кастомний шлях (для multi-account, наприклад .device_id_b).
    Якщо None — використовується дефолтний DEVICE_ID_FILE.
    """
    path = Path(file_path) if file_path else DEVICE_ID_FILE
    if path.exists():
        return path.read_text().strip()
    device_id = f"web_{int(time.time())}"
    path.write_text(device_id)
    return device_id


def get_login_token(strict: bool = True, file_path: Path | str | None = None) -> str:
    """Читає LOGIN-токен. strict=True → sys.exit при відсутності файлу.

    file_path: кастомний шлях (для multi-account, наприклад .login_token_b).
    Якщо None — використовується дефолтний LOGIN_TOKEN_FILE.
    """
    path = Path(file_path) if file_path else LOGIN_TOKEN_FILE
    if path.exists():
        return path.read_text().strip()
    if strict:
        print(f"ПОМИЛКА: файл {path} не знайдено.")
        print(f"Створи: echo 'ТВІЙ_ТОКЕН' > {path}")
        sys.exit(1)
    raise RuntimeError(f"нема {path}")


def handshake_payload(device_id: str) -> dict:
    """Payload для op=6 (handshake). userAgent має бути об'єктом, не рядком —
    інакше сервер відхиляє з'єднання."""
    return {
        "deviceId": device_id,
        "userAgent": {
            "deviceType": "WEB", "locale": "ru", "deviceLocale": "ru",
            "osVersion": "Windows 10", "deviceName": "Chrome",
            "headerUserAgent": _USER_AGENT_HTTP,
            "appVersion": "1.0.0", "screen": "1920x1080", "timezone": "Europe/Moscow",
        },
    }


def make_msg(seq: int, opcode: int, payload: dict) -> str:
    """Серіалізує WS-повідомлення у JSON. ensure_ascii=False — кирилиця як є."""
    return json.dumps(
        {"ver": 11, "cmd": 0, "seq": seq, "opcode": opcode, "payload": payload},
        ensure_ascii=False,
    )
