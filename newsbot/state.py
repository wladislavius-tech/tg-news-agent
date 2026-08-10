"""Стан агента: що вже запощено і коли був останній пост."""
from __future__ import annotations

import hashlib
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
    state.setdefault("posted_facts", [])
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
    state.setdefault("generic_photo_last", {})
    state.setdefault("recent_image_hashes", [])
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


# Вікно порівняння. Було 20/10 — розраховано на 20-30 постів на добу, коли це
# покривало майже добу. Аудит 10.08.2026: канал вийшов на 63 пости/добу, і ті
# самі 20 заголовків стали покривати лише ~7.6 год — два дублі тижня (різниця
# 23 год і 7.7 год) агент просто не бачив. 70/30 повертає покриття до ~доби.
# Для AI-промпту список окремо вкорочується (див. llm.classify_relation), щоб
# не роздувати запит; локальні перевірки використовують повне вікно.
def recent_titles(state: dict, limit: int = 70, tail: int = 30) -> list[str]:
    """Заголовки за сьогодні + "хвіст" останніх учорашніх (posted_titles) —
    для семантичної перевірки (llm.is_same_event) дублів, що спливають рано
    вранці нового дня.

    Реальний кейс: state["daily"]["titles"] обнуляється щоночі (див.
    remember_post) — тож перевірка ЛИШЕ проти нього вранці порівнює новий
    кандидат з порожнім списком, і вчорашній дубль (напр. той самий звіт
    Генштабу з іншого TG-каналу) проходить непоміченим. posted_titles —
    ролінг-список, що НЕ обнуляється щоночі, тож дає "хвіст" через межу доби."""
    daily_titles = (state.get("daily") or {}).get("titles", [])
    tail_titles = state.get("posted_titles", [])[-tail:]
    seen: set[str] = set()
    out: list[str] = []
    for t in daily_titles + tail_titles:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[-limit:]


def recent_facts(state: dict, limit: int = 70, tail: int = 30) -> list[str]:
    """Тексти постів (без HTML/футера, див. main._plain_fact) за сьогодні +
    "хвіст" учорашніх — та сама логіка, що recent_titles(), але для ТІЛА
    поста, а не заголовка.

    Реальний кейс (01.08.2026): два пости про потоплення контейнеровоза
    Yanina мали ОДНАКОВЕ тіло тексту (той самий опис із джерела), але РІЗНІ
    заголовки — item.title кластера Укрнету змінився між двома запусками
    (кластер "мутує"), а AI був недоступний (429), тож спрацював резервний
    формат "заголовок + опис джерела". Перевірка ЛИШЕ заголовків
    (recent_titles/is_near_exact_duplicate) цього не ловить, бо заголовки
    справді різні — а тіло документа те саме."""
    daily_facts = (state.get("daily") or {}).get("facts", [])
    tail_facts = state.get("posted_facts", [])[-tail:]
    seen: set[str] = set()
    out: list[str] = []
    for t in daily_facts + tail_facts:
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[-limit:]


def is_duplicate_image(state: dict, image_bytes: bytes) -> bool:
    """Те саме РЕАЛЬНЕ фото вже було в одному з нещодавніх постів — сильний,
    незалежний від AI сигнал дублю.

    Реальний кейс (01.08.2026): та сама подія (замах на командира "Хартії",
    відмова Трампа передати Patriot, атака на НПЗ рф тощо) публікувалась по
    кілька разів — щоразу з ПЕРЕПИСАНИМ по-іншому заголовком (текстовий
    Jaccard/AI-дедуп це не завжди ловить), але з тим самим фото з
    першоджерела. generic/logo-фото (main.py: media["_local_asset"]) сюди
    НЕ потрапляють — вони легітимно повторюються завжди."""
    h = hashlib.md5(image_bytes).hexdigest()
    return h in state.get("recent_image_hashes", [])


def remember_image_hash(state: dict, image_bytes: bytes) -> None:
    h = hashlib.md5(image_bytes).hexdigest()
    hashes = state.setdefault("recent_image_hashes", [])
    hashes.append(h)
    state["recent_image_hashes"] = hashes[-config.MAX_REMEMBERED_IMAGE_HASHES:]


def is_near_exact_duplicate(title: str, recent: list[str], threshold: float = 0.9) -> bool:
    """Дешева перевірка БЕЗ AI: чи заголовок майже дослівно збігається з
    одним із нещодавніх (Жаккар >= threshold).

    Реальний кейс (30.07.2026): коли Gemini/Groq одночасно віддавали 429
    (квота), llm.classify_relation/is_same_event не спрацьовували і за
    задумом за замовчуванням вважали "не дублікат" ("краще рідкісний дубль,
    ніж пропущена новина") — той самий текст ("Росія масовано атакувала
    Україну 70 ракетами...") опублікувався поспіль 3 рази за 1.5 години.
    Поріг НАБАГАТО вищий за TITLE_SIMILARITY (0.55, м'який сигнал для
    AI-контексту в is_duplicate) — тут ловимо лише практично ідентичний
    повтор, щоб не блокувати легітимний розвиток події зі схожим заголовком.
    Викликати ПЕРЕД llm.is_same_event/classify_relation (коротке замикання
    `or` економить виклик AI, і працює навіть коли AI взагалі недоступний)."""
    new_words = _words(title)
    if not new_words:
        return False
    for old in recent:
        old_words = _words(old)
        if not old_words:
            continue
        jaccard = len(new_words & old_words) / len(new_words | old_words)
        if jaccard >= threshold:
            return True
        if _is_rephrasing(new_words, old_words):
            return True
    return False


# Поріг для «перефразування» (див. _is_rephrasing). Аудит 10.08.2026 показав:
# з 7 реальних дублів тижня 5 мали перетин значущих слів 88-100%, але Жаккар
# лише 0.44-0.64 — тобто поріг 0.9 їх не бачив, і вони проходили щоразу, коли
# AI-перевірка мовчала через вичерпані квоти.
REPHRASE_OVERLAP = 0.8
# Скільки значущих слів має бути в ОБОХ заголовках: на коротких (3-4 слова)
# перетин 80% трапляється випадково.
REPHRASE_MIN_WORDS = 5
# Наскільки заголовки можуть різнитися довжиною. Ключова відмінність дубля від
# РОЗВИТКУ події: дубль переказує ті самі факти (довжина приблизно та сама), а
# розвиток ДОДАЄ нові (нові жертви, рішення суду) — і помітно довшає. Тому
# блокуємо лише схожі за обсягом; несиметричні пари лишаємо AI, який уміє
# відрізнити duplicate від development.
REPHRASE_LEN_RATIO = 1.6


def _is_rephrasing(new_words: set[str], old_words: set[str]) -> bool:
    """Чи це той самий факт іншими словами (а не розвиток події)."""
    if len(new_words) < REPHRASE_MIN_WORDS or len(old_words) < REPHRASE_MIN_WORDS:
        return False
    ratio = len(new_words) / len(old_words)
    if not (1 / REPHRASE_LEN_RATIO <= ratio <= REPHRASE_LEN_RATIO):
        return False
    overlap = len(new_words & old_words) / min(len(new_words), len(old_words))
    return overlap >= REPHRASE_OVERLAP


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
    if fact:
        state["posted_facts"].append(fact)
        state["posted_facts"] = state["posted_facts"][-config.MAX_REMEMBERED_TITLES:]
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
