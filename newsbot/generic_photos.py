"""Узагальнені "типові" фото/картки для новин-цитат без власного фото.

Ще один фолбек ПЕРЕД AI-ілюстрацією (build_post): якщо новина про добре відому
персону чи установу, а власного й конкурентського фото не знайшлось — краще
впізнаване узагальнене зображення, ніж AI-малюнок. Джерела й ліцензії фото —
newsbot/assets/generic/ATTRIBUTION.md.

Реальний кейс: один і той самий портрет Зеленського/Трампа з'являвся під
поспіль різними новинами — включно з новинами-ПОДІЯМИ ("прибув до США",
"зустрінуться о 16:30"), де портрет-цитата взагалі не пасує (це не пряма мова
людини, а подія за її участі). Тепер: (1) такі новини-події генерик-фото не
отримують взагалі — краще реальне фото з конкурентів/Укрнету (шукається до
цього кроку) або текстовий пост; (2) для новин-цитат, де портрет доречний,
ротуємо серед кількох фото персони, а не повторюємо один і той же файл.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets" / "generic"

_PEOPLE = [
    (re.compile(r"зеленськ", re.IGNORECASE), "zelensky"),
    (re.compile(r"путін", re.IGNORECASE), "putin"),
    (re.compile(r"решетилов", re.IGNORECASE), "reshetylova"),
    (re.compile(r"трамп", re.IGNORECASE), "trump"),
]
# Новини-ПОДІЇ (прибуття, поїздки, заплановані зустрічі) — портрет-цитата тут
# вводить в оману: це не пряма мова людини, а подія за її участі. Список не
# претендує на повноту (евристика на дієсловах), але покриває найчастіші
# випадки з реальних постів каналу.
_EVENT_VERB_RE = re.compile(
    r"прибу(в|ла|ли|де)|вилет(ів|іла|іли|ає)|приїха(в|ла|ли)|прилет(ів|іла|іли)|"
    r"вируши(в|ла|ли)|вирушає|вилітає|"
    r"відвідає|відвідав|відвідала|відвідали|"
    r"зустрі(неться|нуться|вся|лися)|"
    r"розпочав? візит|розпочала візит|завершив? візит|завершила візит",
    re.IGNORECASE,
)


def _photo_files(person: str) -> list[Path]:
    """Усі доступні фото персони: {person}.jpg, {person}_2.jpg, {person}_3.jpg..."""
    files = []
    base = _ASSETS / f"{person}.jpg"
    if base.exists():
        files.append(base)
    i = 2
    while True:
        p = _ASSETS / f"{person}_{i}.jpg"
        if not p.exists():
            break
        files.append(p)
        i += 1
    return files


def pick_photo(
    text: str, last_used: dict[str, str] | None = None
) -> tuple[bytes | None, tuple[str, str] | None]:
    """Фото відомої персони — ЛИШЕ для новин-цитат/заяв (не для новин-подій,
    де портрет не відповідає суті, див. _EVENT_VERB_RE). Ротує серед кількох
    фото персони, уникаючи повторення останнього використаного.

    Повертає (bytes, (person_key, filename)) або (None, None) — другий
    елемент потрібен виклику, щоб запам'ятати вибір у state для ротації
    наступного разу (main.py)."""
    person = next((key for pattern, key in _PEOPLE if pattern.search(text)), None)
    if person is None or _EVENT_VERB_RE.search(text):
        return None, None
    files = _photo_files(person)
    if not files:
        return None, None
    last = (last_used or {}).get(person)
    candidates = [f for f in files if f.name != last] or files
    chosen = random.choice(candidates)
    return chosen.read_bytes(), (person, chosen.name)


def pick(
    text: str, now: datetime, last_used: dict[str, str] | None = None
) -> tuple[bytes | None, tuple[str, str] | None]:
    """Фото відомої персони (з ротацією) або (None, None). Другий елемент
    пари — інфо для ротації (person_key, filename).

    Раніше тут була ще згенерована картка-плашка установи (єдина — "ТЦК").
    Прибрано 15.08.2026 на прохання власника: намальований прямокутник з
    написом замість фото виглядав бідно й нічого не додавав до новини.
    Тепер, коли фото персони немає, далі йде лого видання-джерела
    (source_logos.pick), а якщо і його немає — пост виходить текстом."""
    return pick_photo(text, last_used)
