"""Публікація постів через Telegram Bot API."""
from __future__ import annotations

import requests

from . import config

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, *, data: dict, files: dict | None = None) -> dict:
    resp = requests.post(
        _API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
        data=data,
        files=files,
        timeout=60,
    )
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method}: {payload.get('description', resp.text)}")
    return payload["result"]


def send_post(caption: str, image: bytes | None) -> None:
    if image:
        _call(
            "sendPhoto",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"photo": ("news.jpg", image, "image/jpeg")},
        )
    else:
        _call(
            "sendMessage",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "text": caption,
                "parse_mode": "HTML",
                "link_preview_options": '{"is_disabled": true}',
            },
        )
