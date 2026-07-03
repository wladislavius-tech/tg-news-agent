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

_PROMPT = """Ти — редактор популярного українського Telegram-каналу новин (стиль на кшталт
«Україна Сейчас»: живо, коротко, по-людськи, але БЕЗ вигадок і паніки).

Напиши пост про новину нижче. Вимоги:
- Жива розмовна українська, без канцеляриту («росіяни вдарили», а не «було здійснено удар»).
- Використовуй ЛИШЕ факти з наданих матеріалів. Нічого не додумуй, цифри не змінюй.
- headline: один ударний рядок до 90 символів. Почни з 1–2 емодзі під настрій новини:
  ⚡️ термінове/щойно, 💔 трагедія, 😨 тривожне, 🤯 шокуюче, 🔥 гаряче, ✈️/🚀/💥 атаки та ППО,
  🇺🇦 перемоги, 💰 гроші, ⚽️ спорт, 🌦 погода, 😁 курйози. Без крапки в кінці.
- paragraphs: 2–3 КОРОТКІ абзаци по 1–2 речення кожен. Перший — головний факт,
  далі — деталі та контекст. Найважливіші цифри, місця й імена виділи **подвійними зірочками**.
  Пиши енергійно, наче розповідаєш другові, але тримайся фактів. Трагедії — стримано, без смайлів у тексті.
- Жодних хештегів.

Новина (заголовок агрегатора): {title}
Опис з першоджерела: {description}
Заголовки інших видань про цю ж подію:
{alt_titles}

Відповідай строго JSON-об'єктом: {{"headline": "...", "paragraphs": ["...", "...", "..."]}}"""

_BOLD_RE = None  # ініціалізується у _md_bold_to_html


def _md_bold_to_html(text: str) -> str:
    """Екранує HTML і перетворює **текст** на <b>текст</b>."""
    global _BOLD_RE
    import re
    if _BOLD_RE is None:
        _BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    return _BOLD_RE.sub(r"<b>\1</b>", html.escape(text))


def _md_bold_strip(text: str) -> str:
    """Екранує HTML і прибирає **зірочки** (для заголовка, який і так жирний)."""
    global _BOLD_RE
    import re
    if _BOLD_RE is None:
        _BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    return _BOLD_RE.sub(r"\1", html.escape(text))


def compose_post(
    item: FeedItem,
    sources: list[SourceArticle],
    meta: ArticleMeta,
    video_credit: str = "",
    youtube_url: str = "",
    ai_illustration: bool = False,
) -> str:
    """Повертає готовий підпис поста (HTML для Telegram).

    video_credit — автор/видання, чиє відео постимо (обов'язкове зазначення авторства).
    youtube_url — YouTube-відео новини; додається посиланням у пост.
    ai_illustration — картинка згенерована ШІ; чесно позначаємо це у пості.
    """
    generated = _gemini_generate(item, sources, meta) if config.GEMINI_API_KEY else None
    if generated:
        headline, body = generated
    else:
        headline = "📰 " + item.title
        body = meta.description

    body_html = _md_bold_to_html(body) if body else ""

    parts = [f"<b>{_md_bold_strip(headline)}</b>"]
    if body_html:
        parts.append(body_html)
    if youtube_url:
        parts.append(f'▶️ <a href="{html.escape(youtube_url, quote=True)}">Дивитися відео</a>')

    footer_lines = []
    if sources:
        src = sources[0]
        footer_lines.append(
            f'🔗 <a href="{html.escape(src.url, quote=True)}">Джерело: {html.escape(src.domain)}</a>'
        )
    if video_credit:
        footer_lines.append(f"🎥 Відео: {html.escape(video_credit)}")
    if ai_illustration:
        footer_lines.append("🎨 Ілюстрація: згенерована ШІ")
    footer_lines.append(
        f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    )
    parts.append("\n".join(footer_lines))

    caption = "\n\n".join(parts)
    if len(caption) > config.CAPTION_LIMIT and body_html:
        overflow = len(caption) - config.CAPTION_LIMIT
        parts[1] = body_html[: max(0, len(body_html) - overflow - 1)].rstrip() + "…"
        caption = "\n\n".join(parts)
    return caption


def _gemini_generate(
    item: FeedItem, sources: list[SourceArticle], meta: ArticleMeta
) -> tuple[str, str] | None:
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
            "temperature": 0.6,
            "maxOutputTokens": 4000,
            "responseMimeType": "application/json",
            # без "роздумів": короткому посту вони не потрібні, а токени з'їдають
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    last_exc: Exception | None = None
    for _attempt in range(2):
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
            paragraphs = [str(p).strip() for p in data.get("paragraphs", []) if str(p).strip()]
            body = "\n\n".join(paragraphs)
            if not headline or not body:
                raise ValueError("порожня відповідь")
            return headline, body
        except Exception as exc:  # noqa: BLE001 — будь-який збій AI не має зупиняти постинг
            last_exc = exc
    log.warning("Gemini недоступний (%s), використовую простий формат поста", last_exc)
    return None
