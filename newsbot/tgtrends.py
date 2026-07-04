"""Резервне джерело новин: гарячі пости великих українських Telegram-каналів.

Коли стрічка Укрнету не дає гідних кандидатів, агент бере найпопулярніший
свіжий пост із каналів config.TREND_CHANNELS (за переглядами) і пише про цю
подію ВЛАСНИЙ текст через Gemini — без копіювання, з посиланням на джерело.
Читання — через публічні сторінки https://t.me/s/<канал>, без API-ключів.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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


@dataclass
class TrendPost:
    channel: str
    post_id: int
    text: str
    views: int
    published: datetime  # aware, київський час
    url: str
    video_url: str = ""  # пряме коротке відео з t.me CDN


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
        if len(text) < config.TREND_MIN_TEXT or _AD_RE.search(text):
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
        video_el = msg.select_one("video[src]")
        posts.append(TrendPost(
            channel=channel, post_id=post_id, text=text, views=views,
            published=published, url=f"https://t.me/{channel}/{post_id}",
            video_url=video_el["src"] if video_el else "",
        ))
    return posts


def fetch_trends(now: datetime) -> list[TrendPost]:
    """Гарячі свіжі пости всіх каналів-джерел, найпопулярніші першими."""
    age_limit = config.TREND_MAX_AGE_HOURS * 3600
    trends: list[TrendPost] = []
    for channel in config.TREND_CHANNELS:
        for p in fetch_channel(channel, now):
            age = (now - p.published).total_seconds()
            if 0 <= age <= age_limit and p.views >= config.TREND_MIN_VIEWS:
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
    )


_MATCH_WORD_RE = re.compile(r"[а-яіїєґa-z0-9']{4,}", re.IGNORECASE)


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
