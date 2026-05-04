"""
Спільні WS-хелпери для всіх клієнтів MAX (ws_parser, views_fetcher, resolve_channels).

Раніше handshake/login + константи + читання токена/device_id дублювалися в трьох
файлах. Тепер усе тут, кожен клас будує своє поверх цих хелперів.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

WS_URL           = "wss://ws-api.oneme.ru/websocket"
_ROOT            = Path(__file__).parent
LOGIN_TOKEN_FILE = _ROOT / ".login_token"
DEVICE_ID_FILE   = _ROOT / ".device_id"

_USER_AGENT_HTTP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")

WS_HEADERS = {
    "Origin": "https://web.max.ru",
    "User-Agent": _USER_AGENT_HTTP,
}


def get_device_id() -> str:
    """Читає або створює персистентний deviceId — використовується у handshake."""
    if DEVICE_ID_FILE.exists():
        return DEVICE_ID_FILE.read_text().strip()
    device_id = f"web_{int(time.time())}"
    DEVICE_ID_FILE.write_text(device_id)
    return device_id


def get_login_token(strict: bool = True) -> str:
    """Читає LOGIN-токен. strict=True → sys.exit при відсутності файлу."""
    if LOGIN_TOKEN_FILE.exists():
        return LOGIN_TOKEN_FILE.read_text().strip()
    if strict:
        print(f"ПОМИЛКА: файл {LOGIN_TOKEN_FILE} не знайдено.")
        print(f"Створи: echo 'ТВІЙ_ТОКЕН' > {LOGIN_TOKEN_FILE}")
        sys.exit(1)
    raise RuntimeError(f"нема {LOGIN_TOKEN_FILE}")


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
