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


# Стартова реакція під постом (прийом «живого» каналу): бот ставить одну з
# доступних реакцій каналу. Порядок переваги — енергійні для новин.
_SEED_PREFER = ["⚡", "🔥", "👍", "❤️", "😱", "🙏"]
_seed_emoji: str | None = None  # кеш на час запуску (None — ще не питали)


def _get_seed_emoji() -> str:
    """Перша доступна емодзі-реакція каналу (з урахуванням переваги). '' — якщо немає."""
    global _seed_emoji
    if _seed_emoji is not None:
        return _seed_emoji
    _seed_emoji = ""
    try:
        chat = _call("getChat", data={"chat_id": config.TELEGRAM_CHANNEL})
        available = [
            a.get("emoji") for a in (chat.get("available_reactions") or [])
            if a.get("type") == "emoji" and a.get("emoji")
        ]
        if available:
            _seed_emoji = next((e for e in _SEED_PREFER if e in available), available[0])
    except Exception:  # noqa: BLE001
        pass
    return _seed_emoji


def _seed_reaction(message_id) -> None:
    """Ставить стартову реакцію під постом. Будь-який збій — тихо ігнорується."""
    emoji = _get_seed_emoji()
    if not emoji or not message_id:
        return
    try:
        _call("setMessageReaction", data={
            "chat_id": config.TELEGRAM_CHANNEL,
            "message_id": message_id,
            "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
        })
    except Exception:  # noqa: BLE001
        pass


def _first_message_id(result) -> int | None:
    if isinstance(result, list) and result:
        return result[0].get("message_id")
    if isinstance(result, dict):
        return result.get("message_id")
    return None


def send_post(
    caption: str,
    image: bytes | None = None,
    video: bytes | None = None,
    youtube_url: str = "",
    album: list[bytes] | None = None,
    video_album: list[bytes] | None = None,
) -> None:
    if video_album:
        # Добірка коротких відео однієї теми (media group з відео)
        media, files = [], {}
        for i, vid in enumerate(video_album):
            name = f"video{i}"
            files[name] = (f"{name}.mp4", vid, "video/mp4")
            entry: dict = {"type": "video", "media": f"attach://{name}"}
            if i == 0:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
        result = _call(
            "sendMediaGroup",
            data={"chat_id": config.TELEGRAM_CHANNEL, "media": json.dumps(media)},
            files=files,
        )
    elif album:
        media, files = [], {}
        for i, img in enumerate(album):
            name = f"photo{i}"
            files[name] = (f"{name}.jpg", img, "image/jpeg")
            entry: dict = {"type": "photo", "media": f"attach://{name}"}
            if i == 0:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
        result = _call(
            "sendMediaGroup",
            data={"chat_id": config.TELEGRAM_CHANNEL, "media": json.dumps(media)},
            files=files,
        )
    elif video:
        result = _call(
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
        result = _call(
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
        result = _call(
            "sendPhoto",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"photo": ("news.jpg", image, "image/jpeg")},
        )
    else:
        result = _call(
            "sendMessage",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "text": caption,
                "parse_mode": "HTML",
                "link_preview_options": '{"is_disabled": true}',
            },
        )
    _seed_reaction(_first_message_id(result))
