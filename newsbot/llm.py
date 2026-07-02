"""Генерація тексту поста через Google Gemini (безкоштовний тариф).

Якщо ключа немає або запит не вдався — повертається простий пост
із заголовка та опису, тож агент працює і без AI.
"""
from __future__ import annotations

import html
import json
import logging

import requests

from . import config
from .ukrnet import ArticleMeta, FeedItem, SourceArticle

log = logging.getLogger(__name__)

_PROMPT = """Ти — редактор українського новинного Telegram-каналу.
Напиши пост про новину нижче. Вимоги:
- Українською мовою, нейтральний фактичний тон, без клікбейту та вигадок.
- Використовуй ЛИШЕ факти з наданих матеріалів. Нічого не додумуй.
- headline: один рядок до 80 символів, почни з 1 доречного емодзі.
- body: 2–4 короткі речення, найважливіше — першим.
- hashtags: 2–3 українські хештеги без пробілів, наприклад "#Україна".

Новина (заголовок агрегатора): {title}
Опис з першоджерела: {description}
Заголовки інших видань про цю ж подію:
{alt_titles}

Відповідай строго JSON-об'єктом: {{"headline": "...", "body": "...", "hashtags": ["#...", "#..."]}}"""


def compose_post(item: FeedItem, sources: list[SourceArticle], meta: ArticleMeta) -> str:
    """Повертає готовий підпис поста (HTML для Telegram)."""
    generated = _gemini_generate(item, sources, meta) if config.GEMINI_API_KEY else None
    if generated:
        headline, body, hashtags = generated
    else:
        headline = "📰 " + item.title
        body = meta.description
        hashtags = ["#новини", "#Україна"]

    parts = [f"<b>{html.escape(headline)}</b>"]
    if body:
        parts.append(html.escape(body))
    if sources:
        src = sources[0]
        parts.append(f'🔗 <a href="{html.escape(src.url, quote=True)}">Джерело: {html.escape(src.domain)}</a>')
    if hashtags:
        parts.append(html.escape(" ".join(hashtags)))

    caption = "\n\n".join(parts)
    if len(caption) > config.CAPTION_LIMIT:
        overflow = len(caption) - config.CAPTION_LIMIT
        body_cut = html.escape(body)[: max(0, len(html.escape(body)) - overflow - 1)].rstrip() + "…"
        parts[1] = body_cut
        caption = "\n\n".join(parts)
    return caption


def _gemini_generate(
    item: FeedItem, sources: list[SourceArticle], meta: ArticleMeta
) -> tuple[str, str, list[str]] | None:
    alt_titles = "\n".join(f"- {s.title} ({s.domain})" for s in sources[:4]) or "- немає"
    prompt = _PROMPT.format(
        title=item.title,
        description=meta.description or "немає",
        alt_titles=alt_titles,
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2000,
            "responseMimeType": "application/json",
        },
    }
    try:
        resp = requests.post(
            url,
            params={"key": config.GEMINI_API_KEY},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        headline = str(data["headline"]).strip()
        body = str(data["body"]).strip()
        hashtags = [str(h).strip() for h in data.get("hashtags", []) if str(h).startswith("#")]
        if not headline or not body:
            raise ValueError("порожня відповідь")
        return headline, body, hashtags[:3]
    except Exception as exc:  # noqa: BLE001 — будь-який збій AI не має зупиняти постинг
        log.warning("Gemini недоступний (%s), використовую простий формат поста", exc)
        return None
