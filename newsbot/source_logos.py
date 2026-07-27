"""Логотипи видань-джерел — фолбек для новин-цитат конкретного видання без
власного фото (напр. огляд BBC/Reuters без ілюстрації першоджерела).

Обмежений список найчастіших видань в атрибуції заголовків (див.
llm.attributed_source) — джерела й ліцензії у assets/logos/ATTRIBUTION.md.
Решта видань (необмежений список — AI може згадати будь-яке) лишаються без
картинки, як і раніше: постійно поповнювати логотипи під кожне нове видання
нереально.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import cover

_ASSETS = Path(__file__).resolve().parent / "assets" / "logos"

_LOGOS = [
    (re.compile(r"\bbbc\b", re.IGNORECASE), "bbc.png"),
    (re.compile(r"\breuters\b", re.IGNORECASE), "reuters.png"),
    (re.compile(r"\bassociated press\b|\bap\b", re.IGNORECASE), "ap.png"),
    (re.compile(r"\bmeduza\b|\bмедуза\b", re.IGNORECASE), "meduza.png"),
    (re.compile(r"\bcnn\b", re.IGNORECASE), "cnn.png"),
    (re.compile(r"\bthe guardian\b|\bguardian\b", re.IGNORECASE), "guardian.png"),
    (re.compile(r"\bbloomberg\b", re.IGNORECASE), "bloomberg.png"),
    (re.compile(r"\bfinancial times\b|\bft\b", re.IGNORECASE), "ft.png"),
]


def pick(source: str) -> bytes | None:
    """Картка з логотипом видання за назвою атрибуції («, — Джерело» з
    заголовка), або None — для решти видань немає готового логотипу."""
    if not source:
        return None
    for pattern, filename in _LOGOS:
        if pattern.search(source):
            path = _ASSETS / filename
            if path.exists():
                return cover.make_source_card(path.read_bytes())
    return None
