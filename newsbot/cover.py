"""Шаблонна обкладинка для новин без фото (генерується локально, безкоштовно)."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
MARGIN = 90

_FONT_CANDIDATES = [
    # GitHub Actions (ubuntu)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    # Windows
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES if bold else _FONT_CANDIDATES_REGULAR:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=font) <= max_width:
            line = probe
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


_MONTHS_GEN = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
_WEEKDAYS = [
    "понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя",
]


def _arrow(cur: float | None, prev: float | None) -> tuple[str, tuple[int, int, int]]:
    if cur is None or prev is None or abs(cur - prev) < 1e-9:
        return "", (160, 175, 205)
    return ("▲", (235, 87, 87)) if cur > prev else ("▼", (76, 187, 111))


def _panel(draw, box, radius=22, fill=(20, 34, 70), outline=(58, 80, 130)):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def _make_background(Wc: int, Hc: int, photo: bytes | None) -> Image.Image:
    """Тло картки: атмосферне фото під м'яким затемненням (кольори лишаються
    насиченими й видимими, а не тонуть у суцільній темряві) або багатший
    градієнт з теплим акцентним сяйвом зверху, якщо фото немає."""
    base = Image.new("RGB", (Wc, Hc), (12, 20, 46))
    if photo:
        try:
            from PIL import ImageEnhance, ImageFilter, ImageOps

            bg = Image.open(io.BytesIO(photo)).convert("RGB")
            bg = ImageOps.fit(bg, (Wc, Hc)).filter(ImageFilter.GaussianBlur(2))
            bg = ImageEnhance.Color(bg).enhance(1.25)
            bg = ImageEnhance.Contrast(bg).enhance(1.08)
            base = bg
        except Exception:  # noqa: BLE001
            photo = None
    canvas = base.convert("RGBA")
    if photo:
        # М'якша вуаль, ніж раніше (було 205→150) — фото лишається читабельним,
        # але не сірим/пласким
        veil = Image.new("RGBA", (Wc, Hc))
        vd = ImageDraw.Draw(veil)
        for y in range(Hc):
            a = int(150 - 60 * (y / Hc))  # 150 → 90
            vd.line([(0, y), (Wc, y)], fill=(9, 16, 38, a))
        canvas = Image.alpha_composite(canvas, veil)
    else:
        # Тришаровий градієнт (темно-синій → синій → приглушений фіолетовий)
        # замість плаского двоколірного — виглядає глибше й насиченіше
        gd = ImageDraw.Draw(canvas)
        stops = [(9, 17, 40), (23, 40, 82), (36, 30, 68)]
        for y in range(Hc):
            t = y / Hc
            if t < 0.6:
                a, b, lt = stops[0], stops[1], t / 0.6
            else:
                a, b, lt = stops[1], stops[2], (t - 0.6) / 0.4
            gd.line([(0, y), (Wc, y)],
                    fill=tuple(int(ac + (bc - ac) * lt) for ac, bc in zip(a, b)) + (255,))

    # Тепле акцентне сяйво зверху зліва (під брендовою плашкою) — додає глибини
    # й фірмового кольору навіть на найпростішому градієнтному тлі
    glow = Image.new("RGBA", (Wc, Hc), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gx, gy, gr = Wc * 0.2, Hc * 0.05, Wc * 0.55
    for r in range(int(gr), 0, -10):
        a = int(24 * (1 - r / gr))
        gdraw.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(255, 197, 0, a))
    canvas = Image.alpha_composite(canvas, glow)
    return canvas.convert("RGB")


def _value_card(draw, cx, cy, card_w, card_h, label, value, unit, arrow_ref):
    """Малює одну картку «назва / значення / одиниця / стрілка зміни»."""
    _panel(draw, [cx, cy, cx + card_w, cy + card_h], fill=(18, 30, 62), outline=(64, 88, 140))
    draw.text((cx + 28, cy + 22), label, font=_font(28), fill=(150, 168, 205))
    if value is None:
        draw.text((cx + 28, cy + 74), "—", font=_font(54), fill=(90, 105, 140))
        return
    draw.text((cx + 28, cy + 68), value, font=_font(54), fill=(255, 255, 255))
    if unit:
        w_val = draw.textlength(value, font=_font(54))
        draw.text((cx + 36 + w_val, cy + 92), unit, font=_font(26), fill=(150, 168, 205))
    cur, prev = arrow_ref
    arrow, a_color = _arrow(cur, prev)
    if arrow:
        draw.text((cx + card_w - 26, cy + 84), arrow, font=_font(32), fill=a_color, anchor="rm")


def make_morning_card(
    when: datetime,
    war_day: int,
    rates: dict[str, float],
    prev_rates: dict[str, float],
    fuel: dict[str, float],
    prev_fuel: dict[str, float],
    observances: list[str],
    background: bytes | None = None,
) -> bytes:
    """Ранкова інфокартка: фонове фото + курси валют + ціни пального + пам'ятні дні."""
    # 4:5 — надто вузькі/високі фото (був 1080×1500 = 0.72) Telegram у стрічці
    # обрізає вертикально, лишаючи чорні смуги по краях; 4:5 показує на весь екран.
    Wc, Hc = 1080, 1350
    img = _make_background(Wc, Hc, background)
    draw = ImageDraw.Draw(img)
    m = 56  # зовнішнє поле

    # --- Шапка: бренд-плашка зліва, дата-пігулка справа ---
    draw.rectangle([0, 0, Wc, 10], fill=(255, 197, 0))
    _panel(draw, [m, 44, m + 470, 116], fill=(255, 197, 0), outline=(255, 197, 0))
    draw.text((m + 235, 80), "УКРАЇНСЬКІ НОВИНИ", font=_font(38), fill=(15, 23, 42), anchor="mm")
    date_str = f"{when.day} {_MONTHS_GEN[when.month - 1]} {when.year}"
    weekday = _WEEKDAYS[when.weekday()]
    _panel(draw, [Wc - m - 400, 44, Wc - m, 116], fill=(18, 30, 62))
    draw.text((Wc - m - 200, 68), date_str, font=_font(34), fill=(255, 255, 255), anchor="mm")
    draw.text((Wc - m - 200, 100), weekday, font=_font(24), fill=(170, 185, 215), anchor="mm")

    # --- Стрічка "N-й день війни" ---
    _panel(draw, [m, 150, Wc - m, 216], fill=(140, 26, 26), outline=(140, 26, 26))
    draw.text((Wc // 2, 183), f"{war_day}-й день повномасштабної війни",
              font=_font(36), fill=(255, 235, 235), anchor="mm")

    card_w = (Wc - 2 * m - 28) // 2
    card_h = 140

    def section_grid(title, note, cells, y0):
        draw.text((m, y0), title, font=_font(40), fill=(255, 197, 0))
        if note:
            draw.text((Wc - m, y0 + 6), note, font=_font(26), fill=(150, 168, 205), anchor="ra")
        y0 += 58
        for idx, (label, value, unit, ref) in enumerate(cells):
            cx = m + (idx % 2) * (card_w + 28)
            cy = y0 + (idx // 2) * (card_h + 18)
            _value_card(draw, cx, cy, card_w, card_h, label, value, unit, ref)
        return y0 + 2 * card_h + 18 + 32

    # --- КУРС ВАЛЮТ ---
    def fmt(code):
        if code not in rates:
            return (None, "")
        v = rates[code]
        return (f"${v:,.0f}".replace(",", " "), "") if code == "BTC" else (f"{v:.2f}", "грн")
    cur_cells = [
        ("ДОЛАР • USD", *fmt("USD"), (rates.get("USD"), prev_rates.get("USD"))),
        ("ЄВРО • EUR", *fmt("EUR"), (rates.get("EUR"), prev_rates.get("EUR"))),
        ("ЗЛОТИЙ • PLN", *fmt("PLN"), (rates.get("PLN"), prev_rates.get("PLN"))),
        ("БІТКОЇН • BTC", *fmt("BTC"), (rates.get("BTC"), prev_rates.get("BTC"))),
    ]
    y0 = section_grid("КУРС ВАЛЮТ", "за даними НБУ", cur_cells, 250)

    # --- ВАРТІСТЬ ПАЛЬНОГО ---
    def fuf(code):
        return (f"{fuel[code]:.2f}", "грн") if code in fuel else (None, "")
    fuel_cells = [
        ("БЕНЗИН • А-95", *fuf("А-95"), (fuel.get("А-95"), prev_fuel.get("А-95"))),
        ("БЕНЗИН • А-92", *fuf("А-92"), (fuel.get("А-92"), prev_fuel.get("А-92"))),
        ("ДИЗЕЛЬ • ДП", *fuf("Дизель"), (fuel.get("Дизель"), prev_fuel.get("Дизель"))),
        ("АВТОГАЗ • LPG", *fuf("Газ"), (fuel.get("Газ"), prev_fuel.get("Газ"))),
    ]
    y0 = section_grid("ВАРТІСТЬ ПАЛЬНОГО", "середня по Україні", fuel_cells, y0)

    # --- ЦЬОГО ДНЯ ---
    if observances:
        draw.text((m, y0), "ЦЬОГО ДНЯ ВІДЗНАЧАЮТЬ", font=_font(40), fill=(255, 197, 0))
        y0 += 60
        panel_bottom = Hc - 42
        _panel(draw, [m, y0, Wc - m, panel_bottom], fill=(18, 30, 62))
        ty = y0 + 26
        item_f = _font(29)
        for obs in observances[:3]:
            for j, line in enumerate(_wrap(draw, obs, item_f, Wc - 2 * m - 110)[:2]):
                prefix = "»  " if j == 0 else "    "
                draw.text((m + 32, ty), prefix + line, font=item_f, fill=(230, 236, 248))
                ty += 42
            ty += 6
            if ty > panel_bottom - 46:
                break

    draw.rectangle([0, Hc - 10, Wc, Hc], fill=(255, 197, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def make_digest_collage(image_blobs: list[bytes], when: datetime) -> bytes | None:
    """Обкладинка вечірнього дайджесту: мозаїка з фото подій дня + заголовок.

    None — якщо придатних фото менше двох (тоді береться звичайна обкладинка).
    """
    from PIL import ImageOps

    imgs = []
    for blob in image_blobs:
        try:
            imgs.append(Image.open(io.BytesIO(blob)).convert("RGB"))
        except Exception:  # noqa: BLE001
            continue
        if len(imgs) == 4:
            break
    if len(imgs) < 2:
        return None

    Wc, Hc = 1280, 720
    canvas = Image.new("RGB", (Wc, Hc), (11, 21, 48))
    if len(imgs) == 2:
        cells = [(0, 0, 640, 720), (640, 0, 640, 720)]
    elif len(imgs) == 3:
        cells = [(0, 0, 640, 720), (640, 0, 640, 360), (640, 360, 640, 360)]
    else:
        cells = [(0, 0, 640, 360), (640, 0, 640, 360), (0, 360, 640, 360), (640, 360, 640, 360)]
    for img, (x, y, w, h) in zip(imgs, cells):
        canvas.paste(ImageOps.fit(img, (w, h)), (x, y))

    # Затемнення, щоб текст читався поверх мозаїки (розташування — як у
    # вихідній версії, по центру; лише розмір заголовка лишається зменшеним).
    canvas = canvas.convert("RGBA")
    shade = Image.new("RGBA", (Wc, Hc), (8, 14, 35, 150))
    canvas = Image.alpha_composite(canvas, shade).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, Wc, 10], fill=(255, 197, 0))
    draw.rectangle([0, Hc - 10, Wc, Hc], fill=(255, 197, 0))

    # "НОВИНИ УКРАЇНИ • дата" і "Головне за день" — впритул одне до одного,
    # обидва по центру кадру, обидва кольору акценту (жовтий).
    accent = (255, 197, 0)
    header = f"НОВИНИ УКРАЇНИ  •  {when.strftime('%d.%m.%Y')}"
    header_font = _font(40)
    title_font = _font(40)
    title = "ГОЛОВНЕ ЗА ДЕНЬ"

    header_h = draw.textbbox((0, 0), header, font=header_font)[3]
    title_h = draw.textbbox((0, 0), title, font=title_font)[3]
    gap = 14
    block_top = Hc // 2 - (header_h + gap + title_h) // 2
    header_cy = block_top + header_h // 2
    title_cy = block_top + header_h + gap + title_h // 2

    for dx, dy, color in ((2, 2, (0, 0, 0)), (0, 0, accent)):
        draw.text((Wc // 2 + dx, header_cy + dy), header, font=header_font, fill=color, anchor="mm")
        draw.text((Wc // 2 + dx, title_cy + dy), title, font=title_font, fill=color, anchor="mm")

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def make_institution_card(label: str, when: datetime) -> bytes:
    """Узагальнена картка-плашка для установи без "типового" фото (напр. ТЦК —
    у кожному місті своя будівля, реальне фото було б оманливим). Простий
    силует будівлі + назва, у фірмовому темно-синьому стилі каналу."""
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    top, bottom = (11, 21, 48), (28, 48, 94)
    for y in range(H):
        t = y / H
        draw.line(
            [(0, y), (W, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
        )

    # Силует будівлі: корпус + ряди вікон + флагшток (у верхній половині кадру,
    # щоб нижче лишалось чисте місце під назву — без накладання на вікна)
    bw, bh = 480, 260
    bx, by = (W - bw) // 2, 90
    draw.rectangle([bx, by, bx + bw, by + bh], fill=(30, 48, 92), outline=(70, 95, 145), width=3)
    win_w, win_h, gap = 44, 54, 24
    cols = (bw - gap) // (win_w + gap)
    rows = (bh - 50) // (win_h + gap)
    for r in range(rows):
        for c in range(cols):
            wx = bx + gap + c * (win_w + gap)
            wy = by + 36 + r * (win_h + gap)
            draw.rectangle([wx, wy, wx + win_w, wy + win_h], fill=(255, 210, 90))
    draw.rectangle([bx + bw // 2 - 6, by - 70, bx + bw // 2 + 6, by], fill=(200, 210, 225))
    draw.polygon(
        [(bx + bw // 2 + 6, by - 70), (bx + bw // 2 + 90, by - 52), (bx + bw // 2 + 6, by - 34)],
        fill=(255, 197, 0),
    )

    draw.rectangle([0, H - 14, W, H], fill=(255, 197, 0))
    font = _font(110)
    draw.text((W // 2, by + bh + 130), label, font=font, fill=(255, 255, 255), anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def make_cover(title: str, when: datetime) -> bytes:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    # Вертикальний градієнт: темно-синій → синій
    top, bottom = (11, 21, 48), (28, 48, 94)
    for y in range(H):
        t = y / H
        draw.line(
            [(0, y), (W, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
        )
    # Жовта смуга-акцент
    draw.rectangle([0, H - 14, W, H], fill=(255, 197, 0))

    header_font = _font(34)
    draw.text((MARGIN, 60), "НОВИНИ УКРАЇНИ", font=header_font, fill=(255, 197, 0))
    draw.text(
        (W - MARGIN, 60),
        when.strftime("%d.%m.%Y  %H:%M"),
        font=header_font,
        fill=(160, 175, 205),
        anchor="ra",
    )
    draw.line([(MARGIN, 120), (W - MARGIN, 120)], fill=(70, 90, 135), width=2)

    # Заголовок: підбираємо розмір, щоб влізло
    for size in (72, 62, 52, 44, 38):
        title_font = _font(size)
        lines = _wrap(draw, title, title_font, W - 2 * MARGIN)
        line_h = int(size * 1.25)
        if len(lines) * line_h <= H - 300:
            break
    y = 190
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=(255, 255, 255))
        y += line_h

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
