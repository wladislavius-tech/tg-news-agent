"""Щоденний звіт Генштабу про бойові втрати ворога — з офіційного сайту
Міністерства оборони України (mod.gov.ua), а не з переказів TG-каналів.

Реальний кейс: той самий звіт (той самий "1590 окупантів, 8 танків") пішов
у канал і 27, і 28 липня — генерик-конвеєр трендів бере це як звичайну
новину, і Jaccard/AI-перевірка дублів не завжди ловить збіг, коли текст
прийшов із різних каналів-переказів. Тут же дата звіту — частина самого
URL сайту МОУ, тож дедуп природний і надійний: один пост на одну дату.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass, field
from datetime import date

import requests
from bs4 import BeautifulSoup

from . import config
from .cover import _MONTHS_GEN
from .ukrnet import _get

log = logging.getLogger(__name__)

# Транслітерація місяця (родовий відмінок) для URL mod.gov.ua, формат
# bojovi-vtrati-voroga-na-{day}-{month}-{year}-roku. Перевірено по sitemap.xml
# сайту для 7 місяців (січень-липень 2026 — відколи сайт перейшов на цей
# формат URL). Серпень-грудень виведено за тим самим послідовним патерном
# транслітерації (г→g, и→i, я→ya, ю→yu тощо) — не підтверджено наживо, бо ці
# місяці 2026 ще не настали, але патерн стабільний на всіх 7 перевірених.
_MONTHS_TRANSLIT = [
    "sichnya", "lyutogo", "bereznya", "kvitnya", "travnya", "chervnya",
    "lipnya", "serpnya", "veresnya", "zhovtnya", "lystopada", "grudnya",
]

# Форми (1 / 2-4 / 5+) для узгодження числівника з іменником у "➡️ N ___" —
# лише стилістичне оформлення, самих чисел не чіпає. Абревіатури (РСЗВ, ППО,
# БПЛА) не відмінюються — усі три форми однакові.
_NOUN_FORMS = {
    "танки": ("танк", "танки", "танків"),
    "бойові броньовані машини": (
        "бойова броньована машина", "бойові броньовані машини", "бойових броньованих машин",
    ),
    "спеціальна техніка": ("одиниця спецтехніки", "одиниці спецтехніки", "одиниць спецтехніки"),
    "автомобільна техніка та автоцистерни": (
        "одиниця автотехніки", "одиниці автотехніки", "одиниць автотехніки",
    ),
    "наземні робототехнічні комплекси": (
        "наземний робокомплекс", "наземні робокомплекси", "наземних робокомплексів",
    ),
    "рсзв": ("РСЗВ", "РСЗВ", "РСЗВ"),
    "засоби ппо": ("засіб ППО", "засоби ППО", "засобів ППО"),
    "літаки": ("літак", "літаки", "літаків"),
    "гелікоптери": ("гелікоптер", "гелікоптери", "гелікоптерів"),
    "крилаті ракети": ("крилата ракета", "крилаті ракети", "крилатих ракет"),
    "кораблі/катери": ("корабель/катер", "кораблі/катери", "кораблів/катерів"),
    "підводні човни": ("підводний човен", "підводні човни", "підводних човнів"),
}


# Особовий склад, БПЛА і артилерія — стала "велика трійка" Генштабу, завжди
# в лід-реченні (daily_line); решту категорій показуємо в списку "➡️".
_IN_HEADLINE_LABELS = {"військовослужбовці", "бпла оперативно-тактичного рівня", "артилерійські системи"}


def _agree(n: int, forms: tuple[str, str, str]) -> str:
    """Число + іменник у правильному відмінку: 1 танк, 2 танки, 5 танків."""
    if 11 <= n % 100 <= 14:
        return forms[2]
    return {1: forms[0], 2: forms[1], 3: forms[1], 4: forms[1]}.get(n % 10, forms[2])


_ITEM_RE = re.compile(
    r"^(?P<label>.+?)\s*[‒–-]\s*(?:близько\s*)?(?P<total>[\d][\d\s]*)"
    r"\s*(?:\(\+(?P<delta>[\d\s]+)\))?\s*(?:осіб)?[.;]*$"
)


@dataclass
class LossItem:
    label: str
    total: str  # "12 226" — з тисячними пробілами, як у джерела
    delta: str = ""  # "" — без змін сьогодні


@dataclass
class DailyLosses:
    when: date
    image_url: str
    daily_line: str  # "Втрати ворога за 27 липня 2026 року: знищено ..."
    items: list[LossItem] = field(default_factory=list)


def _url_for(d: date) -> str:
    month = _MONTHS_TRANSLIT[d.month - 1]
    return f"https://mod.gov.ua/news/bojovi-vtrati-voroga-na-{d.day}-{month}-{d.year}-roku"


def _parse_item(text: str) -> LossItem | None:
    m = _ITEM_RE.match(text.strip())
    if not m:
        return None
    return LossItem(
        label=m.group("label").strip(),
        total=m.group("total").strip(),
        delta=(m.group("delta") or "").strip(),
    )


def fetch_daily_losses(d: date) -> DailyLosses | None:
    """Звіт Генштабу за конкретну дату, або None — якщо ще не опубліковано
    (типово з'являється близько 6:40-7:35 ранку) чи сторінку не вдалось розпарсити."""
    url = _url_for(d)
    try:
        html = _get(url, proxy_fallback=True).text
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None  # звіт за цю дату ще не опублікували — це нормально
        log.warning("mod.gov.ua %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("mod.gov.ua %s: %s", url, exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    # Перший <figure> на сторінці — декоративний (без img/figcaption), тому
    # шукаємо саме той, що має обидва: це і є контентна інфографіка статті.
    figure = next(
        (f for f in soup.select("figure") if f.select_one("img") and f.select_one("figcaption")),
        None,
    )
    img = figure.select_one("img") if figure else None
    image_url = img.get("src", "") if img else ""
    wrapper = figure.find_next_sibling("div") if figure else None
    if not wrapper:
        return None

    daily_line = ""
    items: list[LossItem] = []
    for el in wrapper.find_all(["p", "ul"]):
        if el.name == "p":
            strong = el.find("strong")
            if not strong:
                continue
            text = el.get_text(" ", strip=True)
            if not daily_line and "Втрати ворога за" in text:
                daily_line = text
        elif el.name == "ul":
            for li in el.find_all("li"):
                item = _parse_item(li.get_text(" ", strip=True))
                if item:
                    items.append(item)

    if not daily_line or not items:
        return None
    return DailyLosses(when=d, image_url=image_url, daily_line=daily_line, items=items)


def download_infographic(url: str) -> bytes | None:
    """Завантажує офіційну інфографіку МОУ, БЕЗ фільтра "фото чи заглушка"
    (ukrnet.download_image відсіює саме такі концентровані плоскі палітри —
    для звичайних новин це логотипи-заглушки, але тут навпаки: інфографіка
    МОУ — легітимне й бажане зображення для цього конкретного звіту).

    Сайт віддає .webp — конвертуємо в JPEG, бо tg.send_post завжди підписує
    фото як image/jpeg (Telegram інколи вередує з невідповідністю формату)."""
    if not url:
        return None
    try:
        resp = requests.get(
            url, headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Інфографіка МОУ %s: %s", url, exc)
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "image" not in ctype:
        return None
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(resp.content)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("Конвертація інфографіки МОУ в JPEG: %s", exc)
        return None


def compose_caption(losses: DailyLosses) -> str:
    """Готовий HTML-підпис у стилі "Україна Сейчас": лід-абзац з добовими
    цифрами (як подав сам Генштаб), розбивка по решті категорій рядками
    "➡️ ", і підсумок з початку повномасштабної війни. Без AI: цифри мають
    бути дослівні, тож жодного ризику, що переписування щось перекрутить.

    Категорії, чиї числа вже названі в лід-реченні (типово: особовий склад,
    БПЛА, артилерія — стандартна "велика трійка" Генштабу), у список "➡️" не
    дублюються — так само, як з атрибуцією джерела в звичайних постах.
    """
    headline = f"🔥 <b>{losses.daily_line.rstrip('.')}, — Генштаб</b>"
    parts = [headline]

    # Матчимо за міткою (не за числом): цифри розділені пробілом як тисячні
    # ("1 560"), тож пошук підрядка/межі слова ненадійний для коротких дельт.
    lines = []
    for item in losses.items:
        if not item.delta or item.label.lower() in _IN_HEADLINE_LABELS:
            continue
        n = int(item.delta.replace(" ", ""))
        forms = _NOUN_FORMS.get(item.label.lower())
        label = _agree(n, forms) if forms else item.label
        lines.append(f"➡️ {html_mod.escape(item.delta)} {html_mod.escape(label)}")
    if lines:
        parts.append("\n".join(lines))

    by_label = {i.label.lower(): i.total for i in losses.items}
    personnel = by_label.get("військовослужбовці")
    tanks = by_label.get("танки")
    bbm = by_label.get("бойові броньовані машини")
    arty = by_label.get("артилерійські системи")
    if personnel and tanks and bbm and arty:
        month_gen = _MONTHS_GEN[losses.when.month - 1]
        parts.append(
            f"З 24 лютого 2022 по {losses.when.day} {month_gen} {losses.when.year} року "
            f"ворог втратив орієнтовно <b>{html_mod.escape(personnel)}</b> осіб, "
            f"<b>{html_mod.escape(tanks)}</b> танків, "
            f"<b>{html_mod.escape(bbm)}</b> бойових броньованих машин і "
            f"<b>{html_mod.escape(arty)}</b> артилерійських систем."
        )

    parts.append(
        f'📌 <a href="{config.CHANNEL_LINK}">{html_mod.escape(config.CHANNEL_NAME)} — підписатися</a>'
    )
    return "\n\n".join(p for p in parts if p)
