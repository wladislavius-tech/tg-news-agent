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


# Репости російських моніторингових/попереджувальних каналів ("Радар РВК" тощо) —
# слово "противник" у них означає УКРАЇНУ (з погляду росіян, це їхній супротивник),
# а не навпаки. Реальний кейс: канал-джерело репостнув скріншот такого каналу з
# написом "противник планирует запустить от 1000 БПЛА" (по рос. областях —
# перелік цілей був видно лише на скріншоті, у скрейпленому тексті його нема).
# AI переклала "противник" як звичне "ворог" і вийшов пост, ніби це рф готує удар
# по Україні — сенс перевернувся на протилежний. Замість пропуску такого поста
# ставимо позначку (TrendPost.ru_source_claim) — llm.py додає до промпту явну
# поправку напрямку й вимогу позначити новину як неперевірену.
_RU_MONITOR_RE = re.compile(
    r"\bпротивник\w*\b|росі[йи]ськ\w*\s*(?:моніт\w*|монитор\w*|пво)\b|радар рвк",
    re.IGNORECASE,
)

# Щоденний зведений звіт Генштабу про бойові втрати ворога — реальний кейс:
# той самий звіт (той самий "1590 окупантів, 8 танків") пішов у канал і
# 27, і 28 липня, бо різні TG-канали переказують той самий звіт по-різному
# і Jaccard/AI-дедуп не завжди ловить збіг. Тепер це окрема фіча (main.py
# maybe_post_daily_losses, джерело — mod.gov.ua напряму, природний дедуп за
# датою в URL), тож із генерик-конвеєра трендів такі пости просто прибираємо,
# щоб не дублювати той самий звіт двічі різними шляхами.
#
# Реальний кейс #2 (01.08.2026): фраза "з 24 лютого 2022" ловить лише повний
# формат звіту — AI на трендах перефразовує ту саму цифру без цієї фрази
# ("1 470 загарбників знищили Сили оборони України на фронті, — Генштаб",
# "Генштаб ЗСУ: Росія за добу втратила майже півтори тисячі військових") і
# проходить непоміченим. Тому крім сталої фрази шукаємо ще й "Генштаб" РАЗОМ
# із втратами особового складу (а не окремо — інші звіти Генштабу, напр. про
# уражені об'єкти рф, з цим фільтром не мають плутатись).
_DAILY_LOSSES_RE = re.compile(
    r"з 24 лютого 2022|з 24\.02\.(?:20)?22|"
    r"генштаб.{0,80}(?:втрат\w+|знищ\w+|ліквідув\w+).{0,80}(?:окупант\w+|загарбник\w+|військов\w+|особов\w+\s+склад\w*)|"
    r"(?:втрат\w+|знищ\w+|ліквідув\w+).{0,80}(?:окупант\w+|загарбник\w+|військов\w+|особов\w+\s+склад\w*).{0,80}генштаб",
    re.IGNORECASE | re.DOTALL,
)


# Ключові слова, що вказують на зв'язок з Україною/війною/світовою політикою.
# Джерела — TREND_CHANNELS — часом дають пости російською (цитати, репости),
# що не відсіюються _looks_russian (немає літер ы/э/ъ/ё) — тому словник охоплює
# й українські, і російські форми ключових термінів.
# Пости-тренди БЕЗ жодного збігу вважаються "вірусним офтопом" (здоров'я,
# лайфстайл, наука тощо) — дозволені, але в межах денної квоти (config.VIRAL_QUOTA_MAX).
_TOPIC_RE = re.compile(
    r"україн|укра[иі]н|рос(і|с)|\bрф\b|кремл|путін|путин|зеленськ|зеленск|війн|войн|"
    r"фронт|зсу|окупант|оккупант|обстріл|обстрел|ракет|дрон|безпілотник|"
    r"беспилотник|шахед|бпла|ппо|мобілізац|мобилизац|санкці|санкц|нато|трамп|"
    r"мвф|полон|плен|штурм|наступ|загин|погиб|поранен|ранен|постражд|жертв|"
    r"харків|харьков|києв|київ|киев|одес|херсон|запоріжж|запорож|донбас|крим|"
    r"крым|бахмут|покровськ|покровск|суми|сумськ|чернігів|чернигов|дніпро|"
    r"днепр|маріупол|мариупол|донеч|донец|луган",
    re.IGNORECASE,
)


def is_off_topic(text: str) -> bool:
    """Тренд без явного зв'язку з Україною/війною — потенційно вірусний офтоп."""
    return not _TOPIC_RE.search(text)


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
    ru_source_claim: bool = False  # репост рос. моніторингового каналу, див. _RU_MONITOR_RE


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


def fetch_channel(channel: str, now: datetime, before: int = 0) -> list[TrendPost]:
    """Змістовні пости одного каналу з публічної сторінки t.me/s/.

    before — id повідомлення, старіші за яке гортає прев'ю (пагінація t.me/s/,
    та сама, що й на самій сторінці): 0 означає останню "живу" сторінку.
    """
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    try:
        html = _get(url, proxy_fallback=True).text
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
        if (
            len(text) < config.TREND_MIN_TEXT
            or _AD_RE.search(text)
            or _looks_russian(text)
            or _DAILY_LOSSES_RE.search(text)
        ):
            continue
        ru_source_claim = bool(_RU_MONITOR_RE.search(text))
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
        if is_alert_post(text):
            continue  # сповіщення про тривогу/загрозу — не наш контент
        posts.append(TrendPost(
            channel=channel, post_id=post_id, text=text, views=views,
            published=published, url=f"https://t.me/{channel}/{post_id}",
            video_url=video_urls[0] if video_urls else "",
            video_urls=video_urls,
            image_url=image_url,
            ru_source_claim=ru_source_claim,
        ))
    return posts


def fetch_channel_history(
    channel: str, now: datetime, max_age_hours: float, max_pages: int = 6
) -> list[TrendPost]:
    """Як fetch_channel, але гортає історію глибше через ?before= (та сама
    пагінація, що на самій сторінці t.me/s/), поки пости не старіші за
    max_age_hours або не вичерпано max_pages сторінок.

    Для max_age_hours у межах однієї "живої" сторінки (типово 3 год) зупиняється
    вже на першій сторінці — без зайвих запитів. Потрібно для пошуку фото
    ретроспективних новин (аналітична стаття про подію кількаденної давнини),
    де саму подію інший канал міг висвітлити задовго до публікації в нас.
    """
    page = fetch_channel(channel, now)
    all_posts = list(page)
    seen_ids = {p.post_id for p in page}
    for _ in range(max_pages - 1):
        if not page:
            break
        oldest = min(page, key=lambda p: p.post_id)
        if (now - oldest.published).total_seconds() > max_age_hours * 3600:
            break
        page = [p for p in fetch_channel(channel, now, before=oldest.post_id) if p.post_id not in seen_ids]
        if not page:
            break
        seen_ids.update(p.post_id for p in page)
        all_posts.extend(page)
    return all_posts


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
        is_viral=is_off_topic(post.text),
        ru_source_claim=post.ru_source_claim,
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


def find_matching_media(
    text: str, now: datetime, *, exclude_channel: str = "", max_age_hours: float = 3.0
) -> tuple[str, str]:
    """Фото/відео цієї ж події з інших каналів (image_url, video_url).

    Використовується, коли влучний тренд-пост не має власного медіа: перед тим,
    як генерувати AI-ілюстрацію, перевіряємо, чи цю ж новину вже висвітлили
    канали-конкуренти з фото чи відео.
    """
    words = _sig_words(text)
    if not words:
        return "", ""
    for ch in config.TREND_CHANNELS:
        if ch == exclude_channel:
            continue
        for p in fetch_channel_history(ch, now, max_age_hours):
            age = (now - p.published).total_seconds()
            if not (0 <= age <= max_age_hours * 3600):
                continue
            if not (p.image_url or p.video_url):
                continue
            if _same_topic(words, _sig_words(p.text)):
                return p.image_url, p.video_url
    return "", ""


# Пости-СПОВІЩЕННЯ про повітряну загрозу (тривога, жовтий/червоний рівень,
# загроза БпЛА/балістики, відбій) — НЕ постимо взагалі. Причина не технічна:
# ми передруковуємо з чужих каналів, тож алерт доходить із запізненням, а
# застаріла тривога гірша за жодну — читач орієнтується на офіційні застосунки.
# Рішення користувача 26.09.2026; раніше тут жила ціла підсистема
# (find_urgent_alert / find_all_clear + llm.compose_alert), вона видалена.
#
# Перевіряємо ЛИШЕ ПЕРШЕ РЕЧЕННЯ: новина про НАСЛІДКИ удару часто згадує
# тривогу далі в тексті («…зруйновані квартири. Приліт стався одразу після
# оголошення повітряної тривоги») — така новина лишається, бо вона вже про
# подію, а не про сповіщення. Вікна в N символів тут мало: другe речення
# реальної новини влізало в нього й хибно відсікалося.
_ALERT_LEAD_CHARS = 160
_SENTENCE_END_RE = re.compile(r"[.!?\n]")
_ALERT_POST_RE = re.compile(
    r"повітрян\w*\s+(?:тривог\w*|небезпек\w*)|"
    r"(?:жовт|червон|синь?ов?|помаранчев)\w*\s+(?:рівень\s+)?тривог|"
    r"рівень\s+(?:небезпеки|тривоги)|"
    r"оголошено\s+(?:повітряну\s+)?тривог|оголошено\s+загрозу|"
    r"ракетн\w*\s+небезпек|зафіксовано\s+пуск|"
    r"відбій\s+(?:повітряної\s+)?тривог|^\s*відбій\b",
    re.IGNORECASE,
)
# Голе «загроза БпЛА/балістики» — ЛИШЕ на самому початку поста (після емодзі
# та розділових). Сповіщення завжди починається з загрози, а новина згадує її
# в середині речення («У Польщі через загрозу дронів закривали аеропорт») —
# таку новину ріже́мо помилково, якщо не прив'язатися до початку.
_ALERT_THREAT_LEAD_RE = re.compile(
    r"^[^\w]{0,12}\s*загроз\w*\s+(?:застосування\s+)?"
    r"(?:бпла|безпілотник|дрон|шахед|балісти|ракетн|удар|обстріл|каб)",
    re.IGNORECASE,
)


def is_alert_post(text: str) -> bool:
    """Чи це пост-сповіщення про повітряну загрозу (а не новина про подію)."""
    lead = text[:_ALERT_LEAD_CHARS]
    end = _SENTENCE_END_RE.search(lead)
    if end:
        lead = lead[:end.start()]
    return bool(_ALERT_POST_RE.search(lead) or _ALERT_THREAT_LEAD_RE.search(lead))


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
