"""Читання стрічки Укрнету та сторінок кластерів."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from . import config


@dataclass
class FeedItem:
    cluster_id: str
    title: str
    url: str
    published: datetime  # київський час
    related_count: int


@dataclass
class SourceArticle:
    title: str
    url: str
    domain: str


@dataclass
class ArticleMeta:
    image_url: str = ""
    description: str = ""
    site_name: str = ""
    video_url: str = ""    # пряме посилання на відеофайл (og:video)
    youtube_url: str = ""  # вбудоване YouTube-відео
    source_titles: list[str] = field(default_factory=list)


def _get(url: str) -> requests.Response:
    resp = requests.get(
        url,
        headers={"User-Agent": config.USER_AGENT, "Accept-Language": "uk"},
        timeout=config.HTTP_TIMEOUT,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp


def fetch_feed(now: datetime) -> list[FeedItem]:
    """Парсить головну стрічку Укрнету. `now` — поточний київський час."""
    html = _get(config.FEED_URL).text
    soup = BeautifulSoup(html, "html.parser")
    items: list[FeedItem] = []
    for section in soup.select("section.im"):
        link = section.select_one("a.im-tl_a")
        time_el = section.select_one("time.im-tm")
        if not link or not link.get("href") or "/cluster/" not in link["href"]:
            continue
        title = link.get_text(strip=True)
        m = re.search(r"-(\d+)\.html", link["href"])
        cluster_id = m.group(1) if m else link["href"]

        published = now
        if time_el:
            tm = re.match(r"(\d{1,2}):(\d{2})", time_el.get_text(strip=True))
            if tm:
                published = now.replace(
                    hour=int(tm.group(1)), minute=int(tm.group(2)),
                    second=0, microsecond=0,
                )
                # Час без дати: якщо він "у майбутньому" — це вчорашня новина
                if published > now + timedelta(minutes=5):
                    published -= timedelta(days=1)

        related = 1
        amount_el = section.select_one(".im-pr_span")
        if amount_el:
            am = re.search(r"\d+", amount_el.get_text())
            if am:
                related = int(am.group())

        items.append(FeedItem(cluster_id, title, link["href"], published, related))
    return items


def fetch_cluster_sources(cluster_url: str) -> list[SourceArticle]:
    """Повертає статті-першоджерела з сторінки кластера (найсвіжіші першими)."""
    html = _get(cluster_url).text
    soup = BeautifulSoup(html, "html.parser")
    sources: list[SourceArticle] = []
    seen_domains: set[str] = set()
    for link in soup.select("a.im-tl_a[href]"):
        href = link["href"]
        if "ukr.net" in href or not href.startswith("http"):
            continue
        domain = urlparse(href).netloc.removeprefix("www.")
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        sources.append(SourceArticle(link.get_text(strip=True), href, domain))
        if len(sources) >= 5:
            break
    return sources


_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:(image|description|site_name|video(?::(?:secure_)?url)?)["\']'
    r'[^>]+content=["\']([^"\']+)',
    re.IGNORECASE,
)
_OG_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property|name)=["\']og:(image|description|site_name|video(?::(?:secure_)?url)?)["\']',
    re.IGNORECASE,
)
_YOUTUBE_RE = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{11})'
)


def fetch_article_meta(article_url: str) -> ArticleMeta:
    """Дістає og:image, og:description, og:video та YouTube-вставки з першоджерела."""
    meta = ArticleMeta()
    try:
        html = _get(article_url).text[:300_000]
    except Exception:
        return meta
    found: dict[str, str] = {}
    for key, value in _OG_RE.findall(html):
        found.setdefault(key.lower().split(":")[0], value)
    for value, key in _OG_RE_REV.findall(html):
        found.setdefault(key.lower().split(":")[0], value)
    meta.image_url = found.get("image", "")
    meta.description = _unescape(found.get("description", ""))
    meta.site_name = _unescape(found.get("site_name", ""))

    video = found.get("video", "")
    if video:
        yt = _YOUTUBE_RE.search(video)
        if yt:
            meta.youtube_url = f"https://www.youtube.com/watch?v={yt.group(1)}"
        elif video.lower().split("?")[0].endswith((".mp4", ".mov", ".webm")):
            meta.video_url = video
    if not meta.youtube_url:
        yt = _YOUTUBE_RE.search(html)
        if yt:
            meta.youtube_url = f"https://www.youtube.com/watch?v={yt.group(1)}"
    return meta


def _unescape(text: str) -> str:
    import html as html_mod
    return html_mod.unescape(text).strip()


def download_image(url: str) -> bytes | None:
    """Завантажує картинку статті; None — якщо бита, замала або не картинка."""
    if not url:
        return None
    try:
        resp = _get(url)
    except Exception:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "image" not in ctype or len(resp.content) < config.MIN_IMAGE_BYTES:
        return None
    return resp.content


def download_video(url: str) -> bytes | None:
    """Завантажує відео першоджерела; None — якщо не відео або завелике для Telegram."""
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        length = int(resp.headers.get("Content-Length") or 0)
        if "video" not in ctype or length > config.MAX_VIDEO_BYTES:
            return None
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=1 << 18):
            chunks.append(chunk)
            total += len(chunk)
            if total > config.MAX_VIDEO_BYTES:
                return None
        data = b"".join(chunks)
        return data if len(data) >= config.MIN_VIDEO_BYTES else None
    except Exception:
        return None
