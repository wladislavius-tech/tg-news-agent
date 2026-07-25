"""Фірмові анімовані стікери каналу @News_Ukraine_world_war.

Пак створено й підтримується сусіднім проєктом `..\\ТГ стікери\\` (той самий
бот — file_id стабільні, доки пак не перестворюють з нуля; для оновлення
переліку викликати getStickerSet на newsua_video_by_news_ukraine_war_bot).
Стікер надсилається ОКРЕМИМ повідомленням після поста (sendSticker), не
всередині тексту.
"""
from __future__ import annotations

import re

# slug -> file_id анімованого (video) стікера з паку newsua_video_by_news_ukraine_war_bot
FILE_IDS: dict[str, str] = {
    "03_urgent": "CAACAgIAAxUAAWpkj_eKbS-HJLgET6_UlagjcuBoAAIsrgACyBYpS9P8QSj5qFlfPQQ",
    "09_logo": "CAACAgIAAxUAAWpkj_d5oP-g1HoFKRuJuUI0N1XsAAJznwACGpAhSwd7fjBPXIF2PQQ",
    "10_fire": "CAACAgIAAxUAAWpkpDZx2SNQgv8b1BDqHOULgosaAAIRnQAC-6EpS6fhyWIJrX5LPQQ",
    "22_candle": "CAACAgIAAxUAAWpkpDhhoKEFtXLk6YJxdiVoVuDGAAJoqAACOS8hS1fGG3ExmeHVPQQ",
    "23_front": "CAACAgIAAxUAAWpkj_fDLP3Qs16cxzfKrCH2NPvWAAIbmQACyGYpS5J961Rh9MBgPQQ",
    "25_economy": "CAACAgIAAxUAAWpkj_dN_--ydHqIZILa-zq4ySTcAAIeoQACzOcpSwYdJJcy1OmEPQQ",
    "27_sport": "CAACAgIAAxUAAWpkj_eQ3pNveC4bnncQceltuMksAALLlQACxU8oS79zEeWph2czPQQ",
    "28_broken_heart": "CAACAgIAAxUAAWpkpDiYdUmX6Vh4NLt8GvzQ7w6iAAJnlQACoZUoS9M-_IE2_B73PQQ",
    "29_explosion": "CAACAgIAAxUAAWpkpDmMocdXyYfemS9SsSQ3IzK-AAJXoQACWvcoS3pktp9lDN4BPQQ",
    "30_shahed": "CAACAgIAAxUAAWpk78-Ejy02oXmDS5J90uzx81wOAAJ8nwACmJopS7PvbW9LQzO8PQQ",
    "31_missile_ballistic": "CAACAgIAAxUAAWpkpDrsadTu3ssKmBu288YwD8-iAAJepQACipcoS_PD2yRP3xqfPQQ",
    "32_missile_cruise": "CAACAgIAAxUAAWpkpDv1P592BC5TkoSZHTZYikqfAAK8oQACJyQoS55AVA9gHe3OPQQ",
    "33_drone_fp1": "CAACAgIAAxUAAWpkpDvAB23LnDVzmFdqDXRwHXUMAAKtpwACcsAoS_LrVSHzuWaaPQQ",
    "34_clown": "CAACAgIAAxUAAWpk788KB5urQH52xtnj4-qzIiIBAAJCnwAC1igoS3ZagRm-jBWHPQQ",
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


def pick(caption: str, source_text: str) -> str | None:
    """file_id стікера під новину, або None — якщо певного збігу немає.

    Краще без стікера, ніж випадковий/недоречний. caption — вже готовий HTML-
    підпис (беремо емодзі, який обрав Gemini для headline); source_text —
    сирий текст новини (заголовок + опис) для впізнавання типу зброї.
    """
    if _SHAHED_RE.search(source_text):
        return FILE_IDS["30_shahed"]
    if _CRUISE_RE.search(source_text):
        return FILE_IDS["32_missile_cruise"]
    if _BALLISTIC_RE.search(source_text):
        return FILE_IDS["31_missile_ballistic"]
    if _DRONE_RE.search(source_text):
        return FILE_IDS["33_drone_fp1"]
    if _EXPLOSION_RE.search(source_text):
        return FILE_IDS["29_explosion"]

    headline = caption.replace("<b>", "", 1).lstrip()
    for emoji, slug in _HEADLINE_EMOJI_SLUG:
        if headline.startswith(emoji):
            return FILE_IDS.get(slug)
    return None
