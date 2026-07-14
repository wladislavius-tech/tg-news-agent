"""Резервне джерело новин: гарячі пости великих українських Telegram-каналів.

Коли стрічка Укрнету не дає гідних кандидатів, агент бере найпопулярніший
свіжий пост із каналів config.TREND_CHANNELS (за переглядами) і пише про цю
подію ВЛАСНИЙ текст через Gemini — без копіювання, з посиланням на джерело.
Читання — через публічні сторінки https://t.me/s/<канал>, без API-ключів.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup

from . import config
from .ukrnet import FeedItem, _get

log = logging.getLogger(__name__)

# Пости-нерелевантності: реклама, розіграші, службові оголошення каналів
_AD_RE = re.compile(
    r"промокод|реклам|розіграш|букмекер|казино|знижк|подаруємо|конкурс|"
    r"набір на|вакансі|запрошуємо на курс",
    re.IGNORECASE,
)
# Хвости-підписи каналів ("ТРУХА⚡️Україна | Надіслати новину" тощо)
_TAIL_RE = re.compile(
    r"(ТРУХА|Надіслати новину|Підписатися|Прислать новость|支持).*$",
    re.IGNORECASE | re.DOTALL,
)
# Літери, унікальні для російської (ыэъё), та для української (іїєґ)
_RU_ONLY = re.compile(r"[ыэъё]", re.IGNORECASE)
_UA_ONLY = re.compile(r"[іїєґ]", re.IGNORECASE)


def _looks_russian(text: str) -> bool:
    """Пост переважно російською (цитата ворога тощо) — не для укр. каналу."""
    ru = len(_RU_ONLY.findall(text))
    ua = len(_UA_ONLY.findall(text))
    return ru >= 3 and ru > ua


@dataclass
class TrendPost:
    channel: str
    post_id: int
    text: str
    views: int
    published: datetime  # aware, київський час
    url: str
    video_url: str = ""  # пряме коротке відео з t.me CDN (перше)
    video_urls: list[str] = field(default_factory=list)  # усі відео медіа-групи
    image_url: str = ""  # фото поста (для консенсус-новин)


def _parse_views(raw: str) -> int:
    m = re.match(r"([\d.,]+)\s*([KMkm]?)", raw.strip())
    if not m:
        return 0
    value = float(m.group(1).replace(",", "."))
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(value * mult)


def clean_text(text: str) -> str:
    """Прибирає службові хвости-підписи каналів і зайві пробіли."""
    text = _TAIL_RE.sub("", text)
    return " ".join(text.split()).strip()


def fetch_channel(channel: str, now: datetime) -> list[TrendPost]:
    """Свіжі змістовні пости одного каналу з публічної сторінки t.me/s/."""
    try:
        html = _get(f"https://t.me/s/{channel}", proxy_fallback=True).text
    except Exception as exc:  # noqa: BLE001
        log.warning("t.me/s/%s: %s", channel, exc)
        return []
    soup = BeautifulSoup(html, "html.parser")
    posts: list[TrendPost] = []
    for msg in soup.select(".tgme_widget_message"):
        data_post = msg.get("data-post", "")
        m = re.match(rf"{re.escape(channel)}/(\d+)", data_post, re.IGNORECASE)
        if not m:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = clean_text(text_el.get_text(" ", strip=True)) if text_el else ""
        if len(text) < config.TREND_MIN_TEXT or _AD_RE.search(text) or _looks_russian(text):
            continue
        views_el = msg.select_one(".tgme_widget_message_views")
        views = _parse_views(views_el.get_text(strip=True)) if views_el else 0
        time_el = msg.select_one("time[datetime]")
        if not time_el:
            continue
        try:
            published = datetime.fromisoformat(time_el["datetime"]).astimezone(now.tzinfo)
        except ValueError:
            continue
        post_id = int(m.group(1))
        # t.me/s/ інколи дублює той самий <video> (десктоп+мобайл) — прибираємо
        # дублі за іменем файлу (частина URL до "?", токен щоразу інакший)
        video_urls, seen_files = [], set()
        for v in msg.select("video[src]"):
            src = v["src"]
            key = src.split("?")[0]
            if key not in seen_files:
                seen_files.add(key)
                video_urls.append(src)
        # Фото поста лежить у style="background-image:url('...')"
        image_url = ""
        photo = msg.select_one(".tgme_widget_message_photo_wrap")
        if photo and photo.get("style"):
            mimg = re.search(r"background-image:url\('([^']+)'\)", photo["style"])
            if mimg:
                image_url = mimg.group(1)
        posts.append(TrendPost(
            channel=channel, post_id=post_id, text=text, views=views,
            published=published, url=f"https://t.me/{channel}/{post_id}",
            video_url=video_urls[0] if video_urls else "",
            video_urls=video_urls,
            image_url=image_url,
        ))
    return posts


def fetch_trends(
    now: datetime,
    *,
    video_only: bool = False,
    max_age_hours: int | None = None,
    min_views: int | None = None,
) -> list[TrendPost]:
    """Гарячі свіжі пости всіх каналів-джерел, найпопулярніші першими.

    video_only — лише пости з відео (для квоти відео, з м'якшими порогами).
    """
    age_limit = (max_age_hours or config.TREND_MAX_AGE_HOURS) * 3600
    min_v = min_views if min_views is not None else config.TREND_MIN_VIEWS
    trends: list[TrendPost] = []
    for channel in config.TREND_CHANNELS:
        for p in fetch_channel(channel, now):
            if video_only and not p.video_urls:
                continue
            age = (now - p.published).total_seconds()
            if 0 <= age <= age_limit and p.views >= min_v:
                trends.append(p)
    # Пости з коротким відео цінніші — піднімаємо їх у черзі (×1.5 до переглядів)
    trends.sort(key=lambda p: p.views * (1.5 if p.video_url else 1.0), reverse=True)
    return trends


def to_feed_item(post: TrendPost) -> FeedItem:
    """Адаптер до FeedItem, щоб тренд ішов звичайним конвеєром постингу.

    related_count масштабуємо з переглядів (10 тис. переглядів ≈ 1 публікація),
    щоб працювала та сама логіка "гарячості" (HOT_THRESHOLD).
    """
    title = post.text[:110].rsplit(" ", 1)[0] if len(post.text) > 110 else post.text
    return FeedItem(
        cluster_id=f"tg:{post.channel}/{post.post_id}",
        title=title,
        url=post.url,
        published=post.published,
        related_count=max(2, post.views // 10_000),
        description=post.text,
        video_url=post.video_url,
        video_urls=post.video_urls,
        image_url=post.image_url,
    )


_MATCH_WORD_RE = re.compile(r"[а-яіїєґa-z0-9']{4,}", re.IGNORECASE)


def _sig_words(text: str) -> set[str]:
    return {w.lower() for w in _MATCH_WORD_RE.findall(text)}


def _same_topic(words_a: set[str], words_b: set[str]) -> bool:
    """Чи два пости про ту саму подію — за перетином значущих слів."""
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    return len(overlap) >= 4 and len(overlap) / min(len(words_a), len(words_b)) >= 0.28


_KYIV_RE = re.compile(r"київ|києв|столиц|кмва|кличко", re.IGNORECASE)
_THREAT_RE = re.compile(
    r"ппо|обстріл|атак|ракет|балісти|шахед|дрон|вибух|тривог|укритт|удар|приліт|"
    r"кабом|запуск|загроз",
    re.IGNORECASE,
)
_SOURCE_RE = re.compile(r"кличко|кмва|квд|повітрян", re.IGNORECASE)  # авторитетне джерело


def find_kyiv_alert(now: datetime) -> FeedItem | None:
    """Свіжий пост про повітряну загрозу/обстріл Києва в каналах-гігантах.

    Такі новини (особливо від Кличка чи КМВА) — обов'язкові й невідкладні.
    Достатньо ОДНОГО каналу (на відміну від консенсусу). Пріоритет — постам
    з авторитетним джерелом і найбільше переглядів.
    """
    window = config.KYIV_ALERT_AGE_MIN * 60
    best: TrendPost | None = None
    best_key = (-1, 0)
    for ch in config.CONSENSUS_CHANNELS:
        for p in fetch_channel(ch, now):
            if (now - p.published).total_seconds() > window:
                continue
            if not (_KYIV_RE.search(p.text) and _THREAT_RE.search(p.text)):
                continue
            key = (1 if _SOURCE_RE.search(p.text) else 0, p.views)
            if key > best_key:
                best, best_key = p, key
    return to_feed_item(best) if best else None


def find_consensus(now: datetime) -> FeedItem | None:
    """Новина, яку СИНХРОННО опублікували кілька каналів-гігантів (Труха, УС, ОКО).
    Це сильний сигнал термінової важливої події — постимо невідкладно.

    Повертає FeedItem найкращого поста (з фото/відео, найбільше переглядів) або None.
    """
    window = config.CONSENSUS_AGE_MIN * 60
    groups: dict[str, list[TrendPost]] = {}
    for ch in config.CONSENSUS_CHANNELS:
        groups[ch] = [
            p for p in fetch_channel(ch, now)
            if 0 <= (now - p.published).total_seconds() <= window
        ]

    best: TrendPost | None = None
    best_key = (-1, 0)  # (кількість каналів, перегляди)
    for ch_a, posts_a in groups.items():
        for pa in posts_a:
            words_a = _sig_words(pa.text)
            hits = {ch_a: pa}
            for ch_b, posts_b in groups.items():
                if ch_b == ch_a:
                    continue
                match = next((pb for pb in posts_b if _same_topic(words_a, _sig_words(pb.text))), None)
                if match:
                    hits[ch_b] = match
                    posts_b.remove(match)  # не рахувати той самий пост двічі
            if len(hits) >= config.CONSENSUS_MIN:
                # Словесний матчинг дає хибні збіги для тематично близьких, але РІЗНИХ
                # новин. Підтверджуємо через AI, що це справді та сама подія.
                from . import llm

                others = [p.text[:200] for ch, p in hits.items() if ch != ch_a]
                if not llm.is_same_event(pa.text[:200], [], others):
                    continue
                winner = max(
                    hits.values(),
                    key=lambda p: (bool(p.video_urls or p.image_url), p.views),
                )
                key = (len(hits), winner.views)
                if key > best_key:
                    best, best_key = winner, key
    return to_feed_item(best) if best else None


def match_feed_item(trend_text: str, items: list[FeedItem]) -> FeedItem | None:
    """Шукає цю ж подію в стрічці Укрнету (за перетином значущих слів).

    Якщо знайдено — краще постити укрнетівський кластер: звичайний конвеєр
    дасть фото та описи від видань-першоджерел.
    """
    trend_words = {w.lower() for w in _MATCH_WORD_RE.findall(trend_text)}
    if not trend_words:
        return None
    best, best_score = None, 0.0
    for it in items:
        title_words = {w.lower() for w in _MATCH_WORD_RE.findall(it.title)}
        if not title_words:
            continue
        overlap = trend_words & title_words
        score = len(overlap) / len(title_words)
        if len(overlap) >= 3 and score > best_score:
            best, best_score = it, score
    return best if best_score >= 0.5 else None
