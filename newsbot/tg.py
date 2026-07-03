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


def send_admin(text: str) -> None:
    """Сповіщення власнику в особисті. Збій сповіщення не має валити агента."""
    if not config.TELEGRAM_ADMIN_CHAT:
        return
    try:
        _call(
            "sendMessage",
            data={"chat_id": config.TELEGRAM_ADMIN_CHAT, "text": text[:4000]},
        )
    except Exception:  # noqa: BLE001
        pass


def send_post(
    caption: str,
    image: bytes | None = None,
    video: bytes | None = None,
    youtube_url: str = "",
    album: list[bytes] | None = None,
) -> None:
    if album:
        media, files = [], {}
        for i, img in enumerate(album):
            name = f"photo{i}"
            files[name] = (f"{name}.jpg", img, "image/jpeg")
            entry: dict = {"type": "photo", "media": f"attach://{name}"}
            if i == 0:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
        _call(
            "sendMediaGroup",
            data={"chat_id": config.TELEGRAM_CHANNEL, "media": json.dumps(media)},
            files=files,
        )
    elif video:
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
