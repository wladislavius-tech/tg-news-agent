"""Публікація постів через Telegram Bot API."""
from __future__ import annotations

import json
from urllib.parse import quote

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


def _share_markup(message_id: int) -> str:
    """Inline-кнопки під постом: «Поділитися» цим постом + «Підписатися».

    Пересилання поста — головний органічний важіль росту в Telegram.
    «Поділитися» відкриває діалог надсилання з посиланням саме на цей пост.
    """
    username = config.TELEGRAM_CHANNEL.lstrip("@")
    post_url = f"https://t.me/{username}/{message_id}"
    share_url = "https://t.me/share/url?url=" + quote(post_url, safe="")
    return json.dumps({"inline_keyboard": [[
        {"text": "📤 Поділитися", "url": share_url},
        {"text": "➕ Підписатися", "url": config.CHANNEL_LINK},
    ]]})


def _add_buttons(result: dict) -> None:
    """Додає кнопки до вже опублікованого поста. Збій — не критичний."""
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not message_id:
        return
    try:
        _call("editMessageReplyMarkup", data={
            "chat_id": config.TELEGRAM_CHANNEL,
            "message_id": message_id,
            "reply_markup": _share_markup(message_id),
        })
    except Exception:  # noqa: BLE001 — пост уже опубліковано, кнопки другорядні
        pass


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
        _call(
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
        _call(
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
        _add_buttons(result)
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
        _add_buttons(result)
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
        _add_buttons(result)
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
        _add_buttons(result)
