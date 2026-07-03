"""Публікація постів через Telegram Bot API."""
from __future__ import annotations

import json

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


def send_post(
    caption: str,
    image: bytes | None = None,
    video: bytes | None = None,
    youtube_url: str = "",
) -> None:
    if video:
        _call(
            "sendVideo",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": "true",
            },
            files={"video": ("news.mp4", video, "video/mp4")},
        )
    elif youtube_url:
        # Текстовий пост з великим YouTube-прев'ю (вбудований плеєр)
        _call(
            "sendMessage",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "text": caption,
                "parse_mode": "HTML",
                "link_preview_options": json.dumps(
                    {"url": youtube_url, "prefer_large_media": True}
                ),
            },
        )
    elif image:
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
