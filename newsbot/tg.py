"""Публікація постів через Telegram Bot API."""
from __future__ import annotations

import json
import logging
import re

import requests

from . import config

log = logging.getLogger(__name__)

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


def send_sticker(file_id: str) -> None:
    """Фірмовий анімований стікер каналу — окремим повідомленням після поста."""
    _call("sendSticker", data={"chat_id": config.TELEGRAM_CHANNEL, "sticker": file_id})


def edit_message_text(message_id: int, text: str) -> None:
    """Редагує вже опублікований текстовий пост (напр. дописати відбій тривоги
    в терміновий алерт замість окремого повідомлення)."""
    _call(
        "editMessageText",
        data={
            "chat_id": config.TELEGRAM_CHANNEL,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": '{"is_disabled": true}',
        },
    )


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


_MEDIA_FAIL_RE = re.compile(r"failed to send message #(\d+)")


def _media_group(kind: str, blobs: list[bytes], caption: str) -> dict | list:
    """sendMediaGroup, що НЕ втрачає пост через одне зіпсоване медіа.

    Telegram обробляє групу цілком: якщо він не зміг прожувати один файл
    (IMAGE_PROCESS_FAILED / VIDEO_PROCESS_FAILED), падає ВЕСЬ запит — і
    новина не виходить узагалі. Реальний кейс 14.10.2026: 9 падінь job за
    день, щоразу з листом "Run failed" власнику.

    Тому деградуємо поступово: прибираємо файл, на який свариться Telegram
    (він називає його номер у помилці, нумерація з 1), і пробуємо знову.
    Коли медіа лишилось менше двох — шлемо одним повідомленням, а в
    безнадійному разі хай вирішує викликач (повертаємо помилку далі)."""
    ext, mime = ("jpg", "image/jpeg") if kind == "photo" else ("mp4", "video/mp4")
    items = list(blobs)
    for _ in range(config.MEDIA_GROUP_RETRIES):
        if len(items) < 2:
            break
        media, files = [], {}
        for i, blob in enumerate(items):
            name = f"{kind}{i}"
            files[name] = (f"{name}.{ext}", blob, mime)
            entry: dict = {"type": kind, "media": f"attach://{name}"}
            if i == 0:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
        try:
            return _call(
                "sendMediaGroup",
                data={"chat_id": config.TELEGRAM_CHANNEL, "media": json.dumps(media)},
                files=files,
            )
        except RuntimeError as exc:
            m = _MEDIA_FAIL_RE.search(str(exc))
            if not m:
                raise
            idx = int(m.group(1)) - 1
            if not 0 <= idx < len(items):
                raise
            log.warning("Telegram не прийняв %s #%d — прибираю й пробую знову (%s)",
                        kind, idx + 1, exc)
            items.pop(idx)
    # Лишилось одне (або все решта не підійшло) — звичайний одиночний пост
    single, data_key = items[0], kind
    log.info("Альбом не зібрався — публікую одним %s", kind)
    return _call(
        "sendPhoto" if kind == "photo" else "sendVideo",
        data={"chat_id": config.TELEGRAM_CHANNEL, "caption": caption, "parse_mode": "HTML"},
        files={data_key: (f"news.{ext}", single, mime)},
    )


def _send_text(caption: str) -> dict:
    return _call(
        "sendMessage",
        data={
            "chat_id": config.TELEGRAM_CHANNEL,
            "text": caption,
            "parse_mode": "HTML",
            "link_preview_options": '{"is_disabled": true}',
        },
    )


def _send_content(
    caption: str,
    image: bytes | None,
    video: bytes | None,
    youtube_url: str,
    album: list[bytes] | None,
    video_album: list[bytes] | None,
) -> dict | list:
    if video_album:
        # Добірка коротких відео однієї теми (media group з відео)
        return _media_group("video", video_album, caption)
    if album:
        return _media_group("photo", album, caption)
    if video:
        return _call(
            "sendVideo",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": "true",
            },
            files={"video": ("news.mp4", video, "video/mp4")},
        )
    if youtube_url:
        # Текстовий пост з великим YouTube-прев'ю (вбудований плеєр)
        return _call(
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
    if image:
        return _call(
            "sendPhoto",
            data={
                "chat_id": config.TELEGRAM_CHANNEL,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"photo": ("news.jpg", image, "image/jpeg")},
        )
    return _send_text(caption)


def send_post(
    caption: str,
    image: bytes | None = None,
    video: bytes | None = None,
    youtube_url: str = "",
    album: list[bytes] | None = None,
    video_album: list[bytes] | None = None,
    media_fallback: dict | None = None,
) -> int | None:
    """Публікує пост, повертає message_id (для посилання на пост із дайджесту).

    media_fallback — якщо передано словник, то коли Telegram відхиляє саме
    МЕДІА (400 Bad Request: IMAGE_PROCESS_FAILED, PHOTO_INVALID_DIMENSIONS
    тощо), новина виходить текстом, а не губиться разом з упалим запуском;
    у словник пишеться {"dropped": True, "reason": ...}, щоб викликач не
    рахував фото/відео, якого читач не побачив. Без словника помилка
    піднімається як раніше (рубрики з картками: ранок, дайджест)."""
    has_media = bool(video_album or album or video or (image and not youtube_url))
    try:
        result = _send_content(caption, image, video, youtube_url, album, video_album)
    except RuntimeError as exc:
        if media_fallback is None or not has_media or "Bad Request" not in str(exc):
            raise
        log.warning("Telegram не прийняв медіа — публікую текстом (%s)", exc)
        media_fallback["dropped"] = True
        media_fallback["reason"] = str(exc)
        result = _send_text(caption)
    message_id = _first_message_id(result)
    _seed_reaction(message_id)
    return message_id
