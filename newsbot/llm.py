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
- ТОЧНІСТЬ ПОНАД УСЕ. Інтригуючі фрази («названо кількість», «стало відомо скільки»,
  «розкрито деталі») ДОЗВОЛЕНІ в headline як гачок — але тоді в paragraphs ОБОВ'ЯЗКОВО
  має бути конкретна відповідь: сама цифра, ім'я, факт. Інтрига без відповіді в тексті —
  ЗАБОРОНЕНА. Якщо точної цифри в матеріалах немає — не натякай на неї взагалі,
  сформулюй лише те, що відомо достеменно.
- Кожне речення має бути завершеною думкою з конкретикою: хто, що, де, скільки.
- headline: один ударний рядок до 90 символів. Почни з 1–2 емодзі під настрій новини:
  ⚡️ термінове/щойно, 💔 трагедія, 😨 тривожне, 🤯 шокуюче, 🔥 гаряче, ✈️/🚀/💥 атаки та ППО,
  🇺🇦 перемоги, 💰 гроші, ⚽️ спорт, 🌦 погода, 😁 курйози. Без крапки в кінці.
- АТРИБУЦІЯ: якщо з матеріалів видно, ХТО повідомив (Генштаб, ОВА, конкретне ЗМІ,
  посадовець) — додай у кінець headline через кому-тире: «..., — Генштаб». Це додає ваги.
- ДОВЖИНА ЗАЛЕЖИТЬ ВІД ВАГИ НОВИНИ: коротка заява/реакція/курйоз — paragraphs з ОДНОГО
  короткого абзацу (1–2 речення), і цього досить. Велика подія з деталями (атака,
  трагедія, важливе рішення) — 2–3 абзаци. Не роздувай дрібниці.
- Якщо новина містить ПЕРЕЛІК фактів (кілька напрямків, кілька цифр, кілька наслідків) —
  оформи їх окремими рядками одного абзацу, кожен рядок починай з «➡️ ».
- Редполітика: «росія», «рф», «путін» та похідні — З МАЛОЇ літери (Україна, США,
  інші країни — як завжди, з великої).
- paragraphs: перший абзац — головний факт. Найважливіші цифри, місця й імена виділи
  **подвійними зірочками**. Пиши енергійно, наче розповідаєш другові, але тримайся фактів.
  Трагедії — стримано, без смайлів у тексті.
- Жодних хештегів.

Новина (заголовок агрегатора): {title}
Опис з першоджерела: {description}
Фрагмент статті першоджерела (бери конкретні цифри й факти звідси):
{body_excerpt}
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
    source_url: str = "",
    source_name: str = "",
    require_ai: bool = False,
) -> str:
    """Повертає готовий підпис поста (HTML для Telegram).

    video_credit — автор/видання, чиє відео постимо (обов'язкове зазначення авторства).
    youtube_url — YouTube-відео новини; додається посиланням у пост.
    ai_illustration — картинка згенерована ШІ; чесно позначаємо це у пості.
    source_url/source_name — явне посилання на джерело (для трендів з TG-каналів).
    """
    has_ai = bool(config.AI_AVAILABLE)
    generated = _gemini_generate(item, sources, meta) if has_ai else None
    if generated:
        headline, body = generated
    else:
        if require_ai:
            # Тренди з TG-каналів без AI-переписування не публікуємо:
            # дослівна копія чужого поста — плагіат і ризик скарг
            raise RuntimeError("AI недоступний, тренд пропущено (без переписування не постимо)")
        headline = "📰 " + item.title
        body = meta.description

    body_html = _md_bold_to_html(body) if body else ""

    parts = [f"<b>{_md_bold_strip(headline)}</b>"]
    if body_html:
        parts.append(body_html)
    if youtube_url:
        parts.append(f'▶️ <a href="{html.escape(youtube_url, quote=True)}">Дивитися відео</a>')

    footer_lines = []
    if source_url:
        footer_lines.append(
            f'🔎 <a href="{html.escape(source_url, quote=True)}">Джерело: {html.escape(source_name or "Telegram")}</a>'
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


def is_same_event(title: str, alt_titles: list[str], recent_titles: list[str]) -> bool:
    """Семантична перевірка на дубль: чи нова новина про ту саму подію, що недавні пости.

    alt_titles — заголовки інших видань про цю ж новину: різні видання називають різні
    деталі (місце, ім'я), і саме це дозволяє зв'язати перефразовані дублі.
    При збої AI — False (краще зрідка дубль, ніж пропущена новина).
    """
    if not recent_titles or not (config.AI_AVAILABLE):
        return False
    alt = "\n".join(f"  (інші видання: {t})" for t in alt_titles[:4])
    listing = "\n".join(f"- {t}" for t in recent_titles)
    data = _gemini_json(
        f"Нова новина: «{title}»\n{alt}\n\n"
        f"Вже опубліковані сьогодні пости каналу:\n{listing}\n\n"
        f"Чи нова новина про ТОЙ САМИЙ інцидент/подію, що будь-який з опублікованих "
        f"постів? Той самий інцидент може бути описаний з різних ракурсів і різними "
        f"словами — звіряй суть події, місце, учасників, а не формулювання.\n"
        f"ЗА ЗАМОВЧУВАННЯМ та сама подія = ДУБЛЬ (duplicate: true).\n"
        f"ВИНЯТОК (duplicate: false) — ЛИШЕ якщо нова новина несе ПРИНЦИПОВИЙ РОЗВИТОК "
        f"події: зросла кількість жертв, ухвалено нове рішення, з'явився новий поворот "
        f"чи наслідок. Просто ІНШІ ПОДРОБИЦІ того самого факту (нові деталі, біографія, "
        f"реакції, інший ракурс) — це ДУБЛЬ, а не розвиток.\n"
        f"Приклад: «Помер сенатор X» і пізніше «Помер сенатор X: що відомо про його "
        f"смерть» — ДУБЛЬ (та сама смерть, лише більше деталей). "
        f'Відповідай строго JSON: {{"duplicate": true|false}}',
        temperature=0.1,
    )
    return bool(data and data.get("duplicate") is True)


def fetch_observances(day: int, month_gen: str) -> list[str]:
    """Загальновідомі пам'ятні дні на сьогодні (для ранкової картки)."""
    if not (config.AI_AVAILABLE):
        return []
    data = _gemini_json(
        f"Які міжнародні, всесвітні та українські пам'ятні дні або свята відзначають "
        f"{day} {month_gen}? Наведи ЛИШЕ загальновідомі й реальні (жодних вигадок), "
        f"від 2 до 5. Назви українською, коротко, без дати в назві. "
        f'Відповідай строго JSON: {{"days": ["...", "..."]}}'
    )
    if not data or not isinstance(data.get("days"), list):
        return []
    return [str(d).strip() for d in data["days"] if str(d).strip()][:5]


_ZODIAC = [
    "♈ Овен", "♉ Телець", "♊ Близнюки", "♋ Рак", "♌ Лев", "♍ Діва",
    "♎ Терези", "♏ Скорпіон", "♐ Стрілець", "♑ Козоріг", "♒ Водолій", "♓ Риби",
]


def compose_horoscope(date_str: str) -> str | None:
    """Гороскоп на день для всіх 12 знаків. None — якщо Gemini недоступний."""
    if not (config.AI_AVAILABLE):
        return None
    signs = ", ".join(z.split()[1] for z in _ZODIAC)
    data = _gemini_json(
        f"Напиши легкий доброзичливий гороскоп на {date_str} для 12 знаків зодіаку "
        f"({signs}). Для КОЖНОГО знака — одне жваве речення до 110 символів українською: "
        f"порада або настрій дня (робота, стосунки, гроші, енергія). Без песимізму й "
        f"страшилок, можна з гумором. Відповідай строго JSON: "
        f'{{"signs": ["текст для Овна", "текст для Тельця", ...]}} — рівно 12 рядків '
        f"у порядку знаків вище, БЕЗ назв знаків у тексті."
    )
    if not data or not isinstance(data.get("signs"), list) or len(data["signs"]) < 12:
        return None
    lines = []
    for i, t in enumerate(data["signs"][:12]):
        text = str(t).strip().rstrip(".")
        if not text.endswith(("!", "?", "…")):
            text += "."
        lines.append(f"<b>{_ZODIAC[i]}</b> — {html.escape(text)}")
    footer = f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    return (
        f"<b>🔮 Гороскоп на сьогодні, {date_str}</b>\n\n"
        + "\n\n".join(lines)
        + f"\n\n{footer}"
    )


_DIGEST_PROMPT = """Ти — редактор українського Telegram-каналу новин. Ось заголовки
постів за сьогодні. Обери {max_lines} НАЙВАЖЛИВІШИХ РІЗНИХ подій і стисни кожну
в один рядок до 90 символів. Порядок — від найважливішої.

Правила:
- КРИТИЧНО: жодні два рядки не можуть стосуватися однієї події, навіть переказаної
  різними словами (наприклад «Суд призначив заставу» і «ВАКС обрав заставу» — це
  ОДНА подія, лиши один рядок). Перевір список на дублі перед відповіддю.
- Емодзі на початку рядка має ТОЧНО відповідати темі: ⚽️ футбол, 🏀 лише баскетбол,
  🎾 теніс, 💰 гроші/застава/фінанси/бюджет, ⚖️ суд/вироки/прокуратура, 🚔 поліція/обшуки,
  💥 обстріли/вибухи, 🕯 загиблі, 🚂 залізниця, 🚗 ДТП, ✈️ авіація/дрони, 🌍 міжнародне,
  ⚡️ енергетика, 🌦 погода. Якщо сумніваєшся — бери нейтральне 📌.

Відповідай строго JSON: {{"lines": ["...", "..."]}}

Заголовки:
{titles}"""


def compose_digest(titles: list[str], now_str: str) -> str:
    """Вечірній дайджест «Головне за день»."""
    lines: list[str] | None = None
    if config.AI_AVAILABLE:
        prompt = _DIGEST_PROMPT.format(
            max_lines=config.DIGEST_MAX_LINES,
            titles="\n".join(f"- {t}" for t in titles),
        )
        data = _gemini_json(prompt)
        if data and isinstance(data.get("lines"), list):
            lines = [str(l).strip() for l in data["lines"] if str(l).strip()]
    if not lines:
        lines = ["• " + t[:90] for t in titles[: config.DIGEST_MAX_LINES]]

    body = "\n\n".join(html.escape(l) for l in lines[: config.DIGEST_MAX_LINES])
    footer = f'📌 <a href="{config.CHANNEL_LINK}">{html.escape(config.CHANNEL_NAME)} — підписатися</a>'
    return f"<b>🌙 Головне за {now_str}</b>\n\n{body}\n\n{footer}"


def _groq_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит до Groq (OpenAI-сумісний API) — другий безкоштовний провайдер."""
    if not config.GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={
                "model": config.GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Groq %s: %s", config.GROQ_MODEL, exc)
        return None


def _github_models_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит до GitHub Models (OpenAI-сумісний) — третій безкоштовний провайдер."""
    if not config.GITHUB_TOKEN:
        return None
    try:
        resp = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"},
            json={
                "model": config.GITHUB_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub Models %s: %s", config.GITHUB_MODEL, exc)
        return None


def _gemini_json(prompt: str, temperature: float = 0.4) -> dict | None:
    """JSON-запит з каскадом провайдерів: Gemini (2 моделі) → Groq → GitHub Models.

    Кожен наступний вмикається, лише коли попередній без квоти чи впав.
    """
    if config.GEMINI_API_KEY:
        for model in (config.GEMINI_MODEL, config.GEMINI_FALLBACK_MODEL):
            gen_cfg: dict = {
                "temperature": temperature,
                "maxOutputTokens": 4000,
                "responseMimeType": "application/json",
            }
            if "2.5" in model:  # thinkingConfig підтримують лише 2.5-моделі
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
            try:
                resp = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=payload, timeout=60)
                resp.raise_for_status()
                return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Gemini %s: %s", model, exc)
                continue
    return _groq_json(prompt, temperature) or _github_models_json(prompt, temperature)


def _gemini_generate(
    item: FeedItem, sources: list[SourceArticle], meta: ArticleMeta
) -> tuple[str, str] | None:
    alt_titles = "\n".join(f"- {s.title} ({s.domain})" for s in sources[:4]) or "- немає"
    # Для трендів з TG повний текст поста лежить у item.description
    excerpt = meta.body_excerpt or getattr(item, "description", "") or "немає"
    prompt = _PROMPT.format(
        title=item.title,
        description=meta.description or "немає",
        body_excerpt=excerpt,
        alt_titles=alt_titles,
    )
    data = _gemini_json(prompt, temperature=0.6)
    try:
        headline = str(data["headline"]).strip()
        paragraphs = [str(p).strip() for p in data.get("paragraphs", []) if str(p).strip()]
        body = "\n\n".join(paragraphs)
        if not headline or not body:
            raise ValueError("порожня відповідь")
        return headline, body
    except Exception:  # noqa: BLE001 — будь-який збій AI не має зупиняти постинг
        log.warning("Gemini недоступний, використовую простий формат поста")
        return None
