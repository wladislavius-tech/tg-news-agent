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
    state.setdefault("last_regular_post_at", None)
    state.setdefault("daily", {"date": "", "titles": [], "message_ids": [], "facts": [], "videos": 0})
    state.setdefault("digest_date", "")
    state.setdefault("morning_date", "")
    state.setdefault("modgov_losses_date", "")
    state.setdefault("horoscope_date", "")
    state.setdefault("rates", {"date": "", "values": {}})
    state.setdefault("fuel", {"date": "", "values": {}})
    state.setdefault("active_alert", None)
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


def minutes_since_regular_post(state: dict, now: datetime) -> float:
    """Хвилин від останньої ЗВИЧАЙНОЇ новини. Алерти/консенсус/рубрики цей
    таймер не чіпають — звичайні новини мають незалежний розклад."""
    if not state.get("last_regular_post_at"):
        return 1e9
    last = datetime.fromisoformat(state["last_regular_post_at"])
    return (now - last).total_seconds() / 60


def _words(title: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(title) if len(w) > 3}


def is_posted(state: dict, cluster_id: str) -> bool:
    """Той самий кластер (URL) уже публікувався — однозначний дубль, без
    винятків (на відміну від is_duplicate, тут нема місця для "розвитку
    події": це буквально та сама стаття)."""
    return cluster_id in state["posted_ids"]


def is_duplicate(state: dict, cluster_id: str, title: str) -> bool:
    """Дубль за ID або за схожістю заголовка (ID кластера з часом змінюється).

    УВАГА: схожість заголовків — це грубий сигнал "та сама подія", який не
    відрізняє переказ від принципового розвитку (зросла кількість жертв,
    нова заява тощо). Для рішення "постити чи ні" перед фінальним постом
    краще is_posted() + llm.is_same_event() (семантична перевірка з winятком
    для розвитку) — is_duplicate лишається для дешевого попереднього
    відсіювання явно нерелевантного (напр. offline-дедуп без AI)."""
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


def remember_post(
    state: dict, cluster_id: str, title: str, now: datetime,
    image_url: str = "", is_video: bool = False, is_regular: bool = False,
    is_viral: bool = False, message_id: int | None = None, fact: str = "",
) -> None:
    state["posted_ids"].append(cluster_id)
    state["posted_titles"].append(title)
    state["last_post_at"] = now.isoformat()
    if is_regular:
        # Окремий таймер звичайних новин — не зсувається алертами/консенсусом
        state["last_regular_post_at"] = now.isoformat()
    # Заголовки, фото і лічильники дня — для дайджесту, колажу та квот відео/вірусного
    today = now.date().isoformat()
    daily = state["daily"]
    if daily.get("date") != today:
        daily["date"] = today
        daily["titles"] = []
        daily["message_ids"] = []
        daily["facts"] = []
        daily["image_urls"] = []
        daily["videos"] = 0
        daily["viral"] = 0
    daily["titles"].append(title)
    daily["titles"] = daily["titles"][-60:]
    # message_ids і facts йдуть синхронно з titles (той самий індекс = той
    # самий пост). Старий стан міг не мати цих полів — вирівнюємо перед додаванням.
    message_ids = daily.setdefault("message_ids", [])
    while len(message_ids) < len(daily["titles"]) - 1:
        message_ids.append(None)
    message_ids.append(message_id)
    daily["message_ids"] = message_ids[-60:]
    facts = daily.setdefault("facts", [])
    while len(facts) < len(daily["titles"]) - 1:
        facts.append("")
    facts.append(fact)
    daily["facts"] = facts[-60:]
    if image_url:
        daily.setdefault("image_urls", []).append(image_url)
        daily["image_urls"] = daily["image_urls"][-12:]
    if is_video:
        daily["videos"] = daily.get("videos", 0) + 1
    if is_viral:
        daily["viral"] = daily.get("viral", 0) + 1


def video_share_today(state: dict, now: datetime) -> tuple[int, float]:
    """(кількість постів сьогодні, частка відео серед них)."""
    daily = state.get("daily") or {}
    if daily.get("date") != now.date().isoformat():
        return 0, 0.0
    posts = len(daily.get("titles", []))
    if posts == 0:
        return 0, 0.0
    return posts, daily.get("videos", 0) / posts


def viral_count_today(state: dict, now: datetime) -> int:
    """Скільки вірусних (не про Україну/війну) постів уже опубліковано сьогодні."""
    daily = state.get("daily") or {}
    if daily.get("date") != now.date().isoformat():
        return 0
    return daily.get("viral", 0)
