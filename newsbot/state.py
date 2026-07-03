"""Стан агента: що вже запощено і коли був останній пост."""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import config

_WORD_RE = re.compile(r"[а-яіїєґa-z0-9']+", re.IGNORECASE)


def load() -> dict:
    if config.STATE_FILE.exists():
        state = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("posted_ids", [])
    state.setdefault("posted_titles", [])
    state.setdefault("last_post_at", None)
    state.setdefault("daily", {"date": "", "titles": []})
    state.setdefault("digest_date", "")
    state.setdefault("morning_date", "")
    state.setdefault("horoscope_date", "")
    state.setdefault("rates", {"date": "", "values": {}})
    return state


def save(state: dict) -> None:
    state["posted_ids"] = state["posted_ids"][-config.MAX_REMEMBERED_IDS:]
    state["posted_titles"] = state["posted_titles"][-config.MAX_REMEMBERED_TITLES:]
    config.STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def minutes_since_last_post(state: dict, now: datetime) -> float:
    if not state.get("last_post_at"):
        return 1e9
    last = datetime.fromisoformat(state["last_post_at"])
    return (now - last).total_seconds() / 60


def _words(title: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(title) if len(w) > 3}


def is_duplicate(state: dict, cluster_id: str, title: str) -> bool:
    """Дубль за ID або за схожістю заголовка (ID кластера з часом змінюється)."""
    if cluster_id in state["posted_ids"]:
        return True
    new_words = _words(title)
    if not new_words:
        return False
    for old_title in state["posted_titles"]:
        old_words = _words(old_title)
        if not old_words:
            continue
        jaccard = len(new_words & old_words) / len(new_words | old_words)
        if jaccard >= config.TITLE_SIMILARITY:
            return True
    return False


def remember_post(state: dict, cluster_id: str, title: str, now: datetime) -> None:
    state["posted_ids"].append(cluster_id)
    state["posted_titles"].append(title)
    state["last_post_at"] = now.isoformat()
    # Список заголовків дня — для вечірнього дайджесту
    today = now.date().isoformat()
    daily = state["daily"]
    if daily.get("date") != today:
        daily["date"] = today
        daily["titles"] = []
    daily["titles"].append(title)
    daily["titles"] = daily["titles"][-60:]
