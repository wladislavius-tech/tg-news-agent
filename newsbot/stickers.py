"""Фірмові анімовані custom-emoji каналу @Suputnyk_news.

Пак створено й підтримується сусіднім проєктом `..\\ТГ стікери\\` (той самий
бот, пак newsua_emoji_by_news_ukraine_war_bot). На відміну від sendSticker
(окреме повідомлення), custom emoji вставляється ПРЯМО в headline поста через
HTML-тег <tg-emoji emoji-id="..."> (підтримується Telegram Bot API у
parse_mode=HTML) — виглядає як звичайний емодзі на початку заголовка, але
рухомий.
"""
from __future__ import annotations

import re

# slug -> custom_emoji_id з паку newsua_emoji_by_news_ukraine_war_bot
CUSTOM_EMOJI_IDS: dict[str, str] = {
    "03_urgent": "5416127173855584039",
    "09_logo": "5418312534820166196",
    "10_fire": "5416034209288466073",
    "22_candle": "5415603084766259903",
    "23_front": "5415816613360344398",
    "25_economy": "5415642869048320482",
    "27_sport": "5417895188553047478",
    "28_broken_heart": "5416049847264388768",
    "29_explosion": "5415984976078351975",
    "30_shahed": "5418206839969979365",
    "31_missile_ballistic": "5415701443812303271",
    "32_missile_cruise": "5415590483332213016",
    "33_drone_fp1": "5415848980233889976",
    "34_clown": "5415902018785030368",
}

# Видимий символ-заглушка всередині <tg-emoji> — показується клієнтам без
# підтримки custom emoji; точна форма не критична, головне — впізнавана.
_GLYPH = {
    "03_urgent": "⚡", "09_logo": "🇺🇦", "10_fire": "🔥", "22_candle": "🕯",
    "23_front": "⚔️", "25_economy": "💰", "27_sport": "⚽", "28_broken_heart": "💔",
    "29_explosion": "💥", "30_shahed": "💣", "31_missile_ballistic": "🚀",
    "32_missile_cruise": "🚀", "33_drone_fp1": "🛸", "34_clown": "🤡",
}

# Зброя/тип атаки — найспецифічніші, перевіряємо по сирому тексту новини (title+опис)
_SHAHED_RE = re.compile(r"шахед", re.IGNORECASE)
_CRUISE_RE = re.compile(r"крилат\w*\s+ракет", re.IGNORECASE)
_BALLISTIC_RE = re.compile(r"баліст", re.IGNORECASE)
_DRONE_RE = re.compile(r"fpv|дрон", re.IGNORECASE)
_EXPLOSION_RE = re.compile(r"вибух", re.IGNORECASE)

# Ті самі емодзі, якими Gemini вже сам розмічає headline (llm._PROMPT) — якщо
# зброю не впізнали, довіряємо його вибору настрою замість повторної класифікації.
_HEADLINE_EMOJI_SLUG = [
    ("💔", "28_broken_heart"),
    ("🔥", "10_fire"),
    ("🇺🇦", "09_logo"),
    ("💰", "25_economy"),
    ("⚽️", "27_sport"),
    ("⚽", "27_sport"),
    ("😁", "34_clown"),
    ("⚡️", "03_urgent"),
    ("⚡", "03_urgent"),
]


def _weapon_slug(source_text: str) -> str | None:
    if _SHAHED_RE.search(source_text):
        return "30_shahed"
    if _CRUISE_RE.search(source_text):
        return "32_missile_cruise"
    if _BALLISTIC_RE.search(source_text):
        return "31_missile_ballistic"
    if _DRONE_RE.search(source_text):
        return "33_drone_fp1"
    if _EXPLOSION_RE.search(source_text):
        return "29_explosion"
    return None


def apply_to_headline(headline: str, source_text: str) -> str:
    """Вбудовує custom emoji в headline, без дублювання видимого символу.

    Пріоритет 1 (тип зброї за source_text) — на початку headline такого
    символу ще немає, тож емодзі ДОДАЄТЬСЯ. Пріоритет 2 (headline вже
    починається зі свого емодзі за легендою Gemini) — цей символ уже є в
    тексті, тож він ЗАМІНЮЄТЬСЯ на анімовану версію (не додається другий).
    Без збігу — headline лишається без змін.

    headline — вже заекранований (html.escape) заголовок, як він піде в
    <b>...</b>; source_text — сирий текст новини (заголовок+опис) для
    впізнавання типу зброї.
    """
    weapon_slug = _weapon_slug(source_text)
    if weapon_slug:
        emoji_id = CUSTOM_EMOJI_IDS.get(weapon_slug)
        if emoji_id:
            tag = f'<tg-emoji emoji-id="{emoji_id}">{_GLYPH[weapon_slug]}</tg-emoji>'
            leading_ws = headline[: len(headline) - len(headline.lstrip())]
            return f"{leading_ws}{tag} {headline.lstrip()}"
        return headline

    stripped = headline.lstrip()
    leading_ws = headline[: len(headline) - len(stripped)]
    for emoji, slug in _HEADLINE_EMOJI_SLUG:
        if stripped.startswith(emoji):
            emoji_id = CUSTOM_EMOJI_IDS.get(slug)
            if not emoji_id:
                return headline
            tag = f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
            return f"{leading_ws}{tag}{stripped[len(emoji):]}"
    return headline
