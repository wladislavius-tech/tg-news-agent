"""Узагальнені "типові" фото/картки для новин-цитат без власного фото.

Ще один фолбек ПЕРЕД AI-ілюстрацією (build_post): якщо новина про добре відому
персону чи установу, а власного й конкурентського фото не знайшлось — краще
впізнаване узагальнене зображення, ніж AI-малюнок. Джерела й ліцензії фото —
newsbot/assets/generic/ATTRIBUTION.md.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets" / "generic"

_ZELENSKY_RE = re.compile(r"зеленськ", re.IGNORECASE)
_PUTIN_RE = re.compile(r"путін", re.IGNORECASE)
_RESHETYLOVA_RE = re.compile(r"решетилов", re.IGNORECASE)
_TCK_RE = re.compile(r"\bтцк\b|територіальн\w*\s+центр\w*\s+комплектуванн", re.IGNORECASE)


def pick_photo(text: str) -> bytes | None:
    """Фото відомої персони за файлом з assets/generic/, або None.

    Перевіряється ПЕРЕД картками установ (pick()) — новина-цитата конкретної
    людини (напр. омбудсменки) має пріоритет над узагальненою карткою
    установи, навіть якщо в тексті також згадується ця установа.
    """
    if _ZELENSKY_RE.search(text):
        path = _ASSETS / "zelensky.jpg"
    elif _PUTIN_RE.search(text):
        path = _ASSETS / "putin.jpg"
    elif _RESHETYLOVA_RE.search(text):
        path = _ASSETS / "reshetylova.jpg"
    else:
        return None
    return path.read_bytes() if path.exists() else None


def pick_institution_card(text: str, now: datetime) -> bytes | None:
    """Згенерована картка-плашка установи (напр. ТЦК), або None."""
    if _TCK_RE.search(text):
        from . import cover

        return cover.make_institution_card("ТЦК", now)
    return None


def pick(text: str, now: datetime) -> bytes | None:
    """Фото персони, або картка установи, або None — у цьому пріоритеті."""
    return pick_photo(text) or pick_institution_card(text, now)
