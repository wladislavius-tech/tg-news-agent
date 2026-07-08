"""Публікація щоденного дайджесту як сторінки Telegraph (telegra.ph).

Telegraph — власна платформа Telegram: сторінки миттєво відкриваються в
застосунку (Instant View), індексуються Google і легко пересилаються.
Вечірній дайджест «Головне за день» додатково виходить окремою сторінкою
telegra.ph з посиланням на канал — ще одна безкоштовна точка входу.

API не потребує реєстрації: createAccount повертає токен, createPage публікує.
Будь-який збій тут не має впливати на публікацію дайджесту в каналі.
"""
from __future__ import annotations

import json
import logging

import requests

from . import config

log = logging.getLogger(__name__)
_API = "https://api.telegra.ph"


def _account_token() -> str | None:
    try:
        r = requests.post(f"{_API}/createAccount", data={
            "short_name": "novyny_ua",
            "author_name": config.CHANNEL_NAME,
            "author_url": config.CHANNEL_LINK,
        }, timeout=30)
        j = r.json()
        return j["result"]["access_token"] if j.get("ok") else None
    except requests.RequestException as e:
        log.warning("Telegraph createAccount: %s", e)
        return None


def publish_digest(title: str, headlines: list[str]) -> str:
    """Створює сторінку Telegraph зі списком новин дня. Повертає URL або ''."""
    token = _account_token()
    if not token or not headlines:
        return ""
    content: list = [
        {"tag": "p", "children": [
            "Головні новини України та світу за день. "
            "Оновлення щодня — у Telegram-каналі."]},
        {"tag": "hr"},
    ]
    for h in headlines:
        text = " ".join(str(h).split())
        if text:
            content.append({"tag": "p", "children": [text]})
    content.append({"tag": "hr"})
    content.append({"tag": "p", "children": [{
        "tag": "a",
        "attrs": {"href": config.CHANNEL_LINK},
        "children": [f"👉 Підписатися на канал «{config.CHANNEL_NAME}»"],
    }]})
    try:
        r = requests.post(f"{_API}/createPage", data={
            "access_token": token,
            "title": title[:256],
            "author_name": config.CHANNEL_NAME,
            "author_url": config.CHANNEL_LINK,
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false",
        }, timeout=30)
        j = r.json()
        if j.get("ok"):
            return j["result"]["url"]
        log.warning("Telegraph createPage: %s", j.get("error"))
    except requests.RequestException as e:
        log.warning("Telegraph createPage: %s", e)
    return ""
