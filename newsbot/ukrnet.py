"""Читання стрічки Укрнету та сторінок кластерів."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger(__name__)


@dataclass
class FeedItem:
    cluster_id: str
    title: str
    url: str
    published: datetime  # київський час
    related_count: int
    description: str = ""  # повний текст (лише для трендів з TG-каналів)
    video_url: str = ""     # пряме відео (лише для трендів з TG-каналів)
    video_urls: list[str] = field(default_factory=list)  # усі відео медіа-групи поста
    image_url: str = ""     # фото поста (для консенсус-новин з TG-каналів)
    is_viral: bool = False  # тренд без явного зв'язку з Україною/війною (квота у config.VIRAL_QUOTA_MAX)
    ru_source_claim: bool = False  # репост рос. моніторингового каналу ("противник" = Україна), див. tgtrends._RU_MONITOR_RE


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
    body_excerpt: str = ""  # перші абзаци статті — для точності фактів у пості
    source_titles: list[str] = field(default_factory=list)
    # Фото з ГАЛЕРЕЇ статті (крім og:image): фоторепортажі містять по 5-10
    # кадрів, а og:image дає лише один. Реальний кейс 19.08.2026: пост
    # "СБУ показала ексклюзивні КАДРИ" вийшов з однією картинкою, тоді як
    # у джерела була галерея з 7 фото.
    gallery_urls: list[str] = field(default_factory=list)


def _fix_encoding(resp: requests.Response) -> None:
    """requests довіряє лише charset, явно вказаному в заголовку Content-Type
    сайту — якщо його нема (типово для деяких сайтів-першоджерел), він тихо
    вважає кодування ISO-8859-1 (RFC 2616 default), хоча реальний текст —
    UTF-8. Це ламає кирилицю в мохибейк ("Ð£ Ð»Ð¸Ð¿Ð½Ñ" замість "У липні") —
    реальний кейс: тіло поста (meta.description із fetch_article_meta)
    вийшло в такому вигляді, бо джерело не вказало charset. Довіряємо
    заголовку лише коли він явний; інакше — визначення requests за байтами
    (apparent_encoding), яке коректно відрізняє utf-8 від windows-1251."""
    if "charset" not in resp.headers.get("Content-Type", "").lower():
        resp.encoding = resp.apparent_encoding


def _via_proxy(url: str) -> requests.Response:
    """Читає url через ланцюжок шлюзів config.READER_PROXIES, по черзі, з
    повторами на кожному. Повтор важливіший за перебір: реальна причина збоїв
    (перевірено 06.08.2026) — плаваючий 403 анонімного пулу r.jina.ai, який
    минає за секунди. Кидає останню помилку, якщо не спрацював жоден."""
    last_exc: Exception = RuntimeError("немає налаштованих шлюзів")
    for template, quote_url, timeout in config.READER_PROXIES:
        target = template.format(url=quote(url, safe="") if quote_url else url)
        for attempt in range(1, config.READER_PROXY_ATTEMPTS + 1):
            headers = {"User-Agent": config.USER_AGENT, "X-Return-Format": "html"}
            # Ключ jina.ai підставляємо ЛИШЕ з другої спроби. Безкоштовна
            # квота — 10 млн токенів, а стрічку Укрнету (210 КБ) ми читаємо
            # через проксі щоразу: з дата-центрового IP прямий запит завжди
            # 403. Якби ключ ішов на кожен запит, квоти вистачило б приблизно
            # на добу. Анонімний пул спрацьовує у ~9 випадках з 10, тож ключ
            # витрачається лише на реальні збої — і квоти стає на місяці.
            if attempt > 1 and config.JINA_API_KEY and "jina.ai" in template:
                headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"
                log.info("Повтор через jina.ai з ключем (анонімний пул відмовив)")
            try:
                resp = requests.get(target, headers=headers, timeout=timeout)
                resp.raise_for_status()
                _fix_encoding(resp)
                return resp
            except Exception as exc:  # noqa: BLE001 — пробуємо наступну спробу/шлюз
                last_exc = exc
                log.warning("Шлюз %s спроба %d: %s", template.split("/")[2], attempt, exc)
                if attempt < config.READER_PROXY_ATTEMPTS:
                    time.sleep(2 * attempt)  # плаваючий ліміт минає за секунди
    raise last_exc


def _get(url: str, proxy_fallback: bool = False) -> requests.Response:
    """GET із запасним ходом: сайти (зокрема Укрнет) блокують IP дата-центрів,
    тому при 403/429 HTML-сторінки перечитуємо через шлюзи-читалки."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT, "Accept-Language": "uk"},
            timeout=config.HTTP_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        _fix_encoding(resp)
        return resp
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if not proxy_fallback or status not in (401, 403, 429, 451):
            raise
    return _via_proxy(url)


def fetch_feed(now: datetime) -> list[FeedItem]:
    """Парсить головну стрічку Укрнету. `now` — поточний київський час."""
    html = _get(config.FEED_URL, proxy_fallback=True).text
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
    html = _get(cluster_url, proxy_fallback=True).text
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
        if len(sources) >= config.SOURCE_FETCH_MAX:
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
        html = _get(article_url, proxy_fallback=True).text[:300_000]
    except Exception:
        return meta
    found: dict[str, str] = {}
    for key, value in _OG_RE.findall(html):
        found.setdefault(key.lower().split(":")[0], value)
    for value, key in _OG_RE_REV.findall(html):
        found.setdefault(key.lower().split(":")[0], value)
    meta.image_url = found.get("image", "")
    meta.description = _trim_truncated_description(
        _clean_description(_unescape(found.get("description", "")))
    )
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
    meta.body_excerpt = _extract_body_excerpt(html)
    meta.gallery_urls = _extract_gallery(html, article_url, meta.image_url)
    return meta


# Галереї-фоторепортажі майже завжди верстаються однаково: мініатюра <img>
# загорнута в <a href="...повний_файл.jpg">. Беремо саме href — мініатюри
# (у ukrinform це 80x80) не пройшли б перевірку розміру.
# Проміжних тегів між <a> і <img> дозволяємо не більше двох (буває
# <a><figure><img>). Необмежене повторення тут ЛАМАЄ пошук: між сусідніми
# посиланнями галереї стоять лише пробіли, тож жадібний вираз проходив крізь
# них і "проковтував" усю галерею одним збігом — знаходило 1 фото замість 7.
_GALLERY_LINK_RE = re.compile(
    r'<a[^>]*href=["\']([^"\']+\.(?:jpe?g|png|webp))["\'][^>]*>\s*'
    r'(?:<(?!/a)[^>]{0,120}>\s*){0,2}<img',
    re.IGNORECASE,
)


def _extract_gallery(html: str, base_url: str, main_image: str) -> list[str]:
    """Повнорозмірні фото галереї статті, без того, що вже є в og:image."""
    seen: set[str] = set()
    if main_image:
        seen.add(main_image)
    out: list[str] = []
    for m in _GALLERY_LINK_RE.finditer(html):
        url = urljoin(base_url, _unescape(m.group(1)))
        if url in seen or _PLACEHOLDER_URL_RE.search(url):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= config.GALLERY_MAX_URLS:
            break
    return out


def _extract_body_excerpt(html: str, limit: int = 900) -> str:
    """Перші змістовні абзаци статті — джерело точних цифр і фактів для поста."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        paragraphs = []
        total = 0
        for p in soup.find_all("p"):
            text = " ".join(p.get_text(" ", strip=True).split())
            # відсіюємо службові рядки: підписки, кукі, копірайти
            if len(text) < 60 or re.search(
                r"cookie|підпис|телеграм|telegram|копіюванн|©|читайте також", text, re.I
            ):
                continue
            paragraphs.append(text)
            total += len(text)
            if total >= limit:
                break
        return " ".join(paragraphs)[:limit]
    except Exception:  # noqa: BLE001
        return ""


# og:description у частини видань — не анонс новини, а заклик перейти на сайт
# ("Подробиці читайте на сайті", "Читати далі" тощо). Реальний кейс 15.08.2026:
# пост про спецоперацію НАБУ вийшов із тілом "Подробиці читайте на сайті" — на
# ЯКОМУ сайті, читач не розуміє, бо посилання в пості немає. При цьому інші
# джерела того ж кластера мали нормальні описи з фактами.
_STUB_DESC_RE = re.compile(
    r"^(?:"
    r"подробиц[іи]|деталі|детальніше|читайте|читати|дивіться|дивитися|"
    r"більше|всі новини|усі новини|джерело|переглянути"
    r")\b.{0,60}$",
    re.IGNORECASE,
)


_RU_ONLY_RE = re.compile(r"[ыъэё]", re.IGNORECASE)
_UK_ONLY_RE = re.compile(r"[іїєґ]", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[а-яіїєґыъэё]", re.IGNORECASE)


def is_russian_text(text: str) -> bool:
    """Чи текст російською (або засмічений русизмами).

    Аудит 14.08.2026: у fallback-режимі (AI недоступний) тіло поста береться
    з og:description ДОСЛІВНО, а Укрнет агрегує і російськомовні видання —
    тож у канал йшли рядки на кшталт «Сильный удар по позициям Кремля» під
    українським заголовком. Для україномовного каналу про війну це прямий
    удар по довірі.

    Дві незалежні ознаки, бо кожна ловить свій випадок:
    - у грамотному українському тексті ы/ъ/э/ё не буває ЖОДНОГО разу, тож
      два й більше — це вже засмічення (ловить змішані тексти);
    - суцільно російський текст не містить і/ї/є/ґ, тоді як український
      абзац такої довжини їх майже завжди має (ловить чисту російську,
      де ы може й не трапитись — «Сильный удар по позициям»)."""
    cyr = len(_CYRILLIC_RE.findall(text))
    if cyr < 15:
        return False  # надто коротко для надійного висновку
    # Навіть ОДНА ы/ъ/э/ё — вже сигнал: в українському тексті їх не буває, а
    # російські прикметники (-ый/-ых/-ые) без них не обходяться. Реальний
    # кейс, який поріг «>=2» пропускав: «Сильный удар по позициям Кремля».
    if _RU_ONLY_RE.search(text):
        return True
    # Друга ознака — для російського тексту, де ы випадково не трапилось.
    # Поріг 60 (а не 15): коротка українська фраза цілком може обійтися без
    # і/ї/є/ґ («ЗСУ випереджають НАТО, — Залужний»), довгий абзац — ні.
    return cyr >= 60 and not _UK_ONLY_RE.search(text)


def is_stub_description(text: str) -> bool:
    """Чи опис — це заклик «читайте на сайті», а не власне новина."""
    t = " ".join((text or "").split())
    if not t:
        return True
    if len(t) < 25:  # надто коротко, щоб нести факт
        return True
    return bool(_STUB_DESC_RE.match(t))


_DIGIT_RE = re.compile(r"\d")
_CAPWORD_RE = re.compile(r"\b[А-ЯІЇЄҐA-Z][а-яіїєґa-z']{2,}")


def description_quality(text: str) -> int:
    """Наскільки опис придатний як тіло поста. Більше — краще, -1 = непридатний.

    Раніше опис брався просто з ПЕРШОГО джерела кластера, і лише явна
    заглушка ("читайте на сайті") змушувала шукати далі. Через це в пости
    потрапляли то верстка сайту ("УНН Суспільство ✎ ..."), то російський
    текст, то одне куце речення — хоча в сусідніх джерелах того ж кластера
    лежали нормальні описи. Тепер обираємо найкращий за сукупністю ознак.
    Усі перевірки локальні, AI не потрібен."""
    t = " ".join((text or "").split())
    if not t or is_stub_description(t) or is_russian_text(t):
        return -1
    score = 0
    n = len(t)
    if n >= 220:
        score += 3          # розгорнутий абзац із деталями
    elif n >= 120:
        score += 2
    elif n >= 70:
        score += 1
    else:
        score -= 1          # одне куце речення — мало що додає до заголовка
    # Конкретика: цифри й власні назви — ознака фактів, а не загальних слів
    if _DIGIT_RE.search(t):
        score += 1
    if len(set(_CAPWORD_RE.findall(t))) >= 2:
        score += 1
    # Завершена думка (після _trim_truncated_description обрив уже мав
    # зникнути, але опис міг і не мати жодного повного речення)
    if t.endswith((".", "!", "?", "»", '"')):
        score += 1
    return score


def _unescape(text: str) -> str:
    """Деякі джерела (особливо через проксі r.jina.ai) віддають og:description
    з КІЛЬКОМА рівнями HTML-екранування (напр. "&amp;amp;#039;" замість "'") —
    реальний кейс: одинарний html.unescape() знімав лише один рівень, і в
    опублікованому пості лишався видимий сміттєвий "&amp;#039;" замість
    апострофа. Знімаємо рівні по колу, доки рядок не перестане змінюватись
    (ліміт ітерацій — бо чисто теоретично можна вигадати текст, що виглядає
    як entity і після unescape, хоч на практиці далі 2-3 рівнів не буває)."""
    import html as html_mod
    prev = text
    for _ in range(5):
        cur = html_mod.unescape(prev)
        if cur == prev:
            break
        prev = cur
    return prev.strip()


_TRAILING_ELLIPSIS_RE = re.compile(r"\s*(?:\.{2,}|…)\s*$")
_SENTENCE_END_RE = re.compile(r'[.!?]["\'»)]?(?=\s|$)')

# Службовий префікс сайту на початку опису: коротка "хлібна крихта" (назва
# видання + рубрика), відбита декоративним значком. Реальний кейс 19.08.2026:
# unn.ua віддав "УНН Суспільство ✎ Міністри оборони та закордонних справ...",
# і "УНН Суспільство ✎" пішло в пост як частина новини.
# Тільки очевидно декоративні роздільники: тире й двокрапку не чіпаємо, бо
# вони бувають у справжньому тексті ("Зеленський — про переговори: ...").
_SITE_PREFIX_RE = re.compile(r"^[^.!?\n]{1,45}?\s*[✎✏🖊📝|▸▪►]️?\s*")


def _clean_description(text: str) -> str:
    """Прибирає з og:description сміття верстки сайту-джерела.

    Два дефекти, знайдені на живих даних:
    - переноси рядків усередині речення (у пості слово з'їжджало на новий
      рядок посеред фрази: "...заради справедливого\\nмиру");
    - службова "хлібна крихта" на початку ("УНН Суспільство ✎ ")."""
    text = " ".join(text.split())  # будь-які пробільні послідовності -> один пробіл
    return _SITE_PREFIX_RE.sub("", text, count=1).strip()


def _trim_truncated_description(text: str) -> str:
    """og:description джерела нерідко сама обрізана CMS сайту-джерела серед
    речення (закінчується "..."/"…") — реальний кейс: fallback-пост без AI
    скопіював її дослівно, вийшло "...внаслідок обстрілу в ніч на …" —
    обірване речення в опублікованому пості. Прибираємо обрізок і лишаємо
    лише завершені речення; якщо жодного повного речення не залишається —
    весь опис був одним недописаним реченням, краще порожній body, ніж
    видимий читачеві обрив."""
    trimmed = _TRAILING_ELLIPSIS_RE.sub("", text)
    if trimmed == text:
        return text  # немає ознак обрізання джерелом
    ends = list(_SENTENCE_END_RE.finditer(trimmed))
    if not ends:
        return ""
    return trimmed[: ends[-1].end()].strip()


_PLACEHOLDER_URL_RE = re.compile(
    r"logo|placeholder|default|no[-_]?(?:photo|image)|noimage|stub|favicon|avatar",
    re.IGNORECASE,
)


def download_image(url: str, relaxed: bool = False) -> bytes | None:
    """Завантажує картинку статті; None — якщо бита, замала, неякісна або заглушка.

    relaxed=True — послаблена перевірка «плоскості» для ДРУГОЇ спроби, коли за
    суворим порогом не пройшло жодне фото з усіх джерел (див. main.build_post).
    Решта перевірок — розмір, формат, пропорції — лишаються тими самими."""
    if not url or _PLACEHOLDER_URL_RE.search(url):
        return None
    try:
        resp = _get(url)
    except Exception:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "image" not in ctype or len(resp.content) < config.MIN_IMAGE_BYTES:
        return None
    if not _image_quality_ok(resp.content, relaxed=relaxed):
        return None
    return resp.content


def _image_quality_ok(data: bytes, relaxed: bool = False) -> bool:
    """Відсіює замалі картинки, кадри-банери та логотипи-заглушки.

    Логотип на рівному тлі має мало відтінків і домінантний колір;
    справжнє фото — тисячі відтінків.
    """
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(data))
        w, h = img.size
    except Exception:
        return False
    if w < config.MIN_IMAGE_WIDTH or h < config.MIN_IMAGE_HEIGHT:
        return False
    aspect = w / h
    if not 0.5 <= aspect <= 2.6:
        return False

    thumb = img.convert("RGB").resize((64, 64))
    if len(thumb.getcolors(64 * 64) or []) < 200:  # зовсім плоска графіка/логотип
        return False

    # Концентрація палітри. Заглушки-обкладинки видань (Interfax, ua.news тощо) —
    # суцільний фон + логотип + текст, тому кілька квантованих кольорів покривають
    # майже все. Справжнє фото має розподілену палітру. Квантуємо, щоб згладжування
    # країв тексту не роздувало кількість кольорів. Поріг вивірений на живих даних:
    # заглушки дають top3 ≥ 0.65, справжні новинні фото — ≤ 0.51.
    q = thumb.point(lambda v: (v // 48) * 48)
    counts = sorted((c for c, _ in (q.getcolors(64 * 64) or [])), reverse=True)
    top3_share = sum(counts[:3]) / (64 * 64)
    limit = config.IMAGE_FLAT_TOP3_RELAXED if relaxed else config.IMAGE_FLAT_TOP3
    if top3_share > limit:
        return False
    return True


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
