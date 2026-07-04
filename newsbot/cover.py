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
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # Windows
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
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


def make_morning_card(
    when: datetime,
    war_day: int,
    rates: dict[str, float],
    prev_rates: dict[str, float],
    observances: list[str],
) -> bytes:
    """Ранкова інфокартка в стилі УС: окремі блоки-картки для кожної валюти,
    розділені секції, стрічка дня війни, панель пам'ятних днів."""
    Wc, Hc = 1080, 1080
    img = Image.new("RGB", (Wc, Hc))
    draw = ImageDraw.Draw(img)
    top, bottom = (9, 17, 40), (26, 46, 92)
    for y in range(Hc):
        t = y / Hc
        draw.line(
            [(0, y), (Wc, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
        )
    m = 56  # зовнішнє поле

    # --- Шапка: бренд-плашка зліва, дата-пігулка справа ---
    draw.rectangle([0, 0, Wc, 10], fill=(255, 197, 0))
    _panel(draw, [m, 44, m + 470, 116], fill=(255, 197, 0), outline=(255, 197, 0))
    draw.text((m + 235, 80), "УКРАЇНСЬКІ НОВИНИ", font=_font(38), fill=(15, 23, 42), anchor="mm")
    date_str = f"{when.day} {_MONTHS_GEN[when.month - 1]} {when.year}"
    weekday = _WEEKDAYS[when.weekday()]
    _panel(draw, [Wc - m - 400, 44, Wc - m, 116])
    draw.text((Wc - m - 200, 68), date_str, font=_font(34), fill=(255, 255, 255), anchor="mm")
    draw.text((Wc - m - 200, 100), weekday, font=_font(24), fill=(160, 175, 205), anchor="mm")

    # --- Стрічка "N-й день війни" на всю ширину ---
    _panel(draw, [m, 150, Wc - m, 216], fill=(126, 24, 24), outline=(126, 24, 24))
    draw.text(
        (Wc // 2, 183), f"{war_day}-й день повномасштабної війни",
        font=_font(36), fill=(255, 235, 235), anchor="mm",
    )

    # --- Секція КУРС ВАЛЮТ: 2×2 окремі картки ---
    y0 = 268
    draw.text((m, y0), "КУРС ВАЛЮТ", font=_font(40), fill=(255, 197, 0))
    draw.text((Wc - m, y0 + 6), "за даними НБУ", font=_font(26), fill=(140, 155, 190), anchor="ra")
    y0 += 66
    card_w = (Wc - 2 * m - 28) // 2
    card_h = 170
    cells = [
        ("USD", "ДОЛАР • USD"), ("EUR", "ЄВРО • EUR"),
        ("PLN", "ЗЛОТИЙ • PLN"), ("BTC", "БІТКОЇН • BTC"),
    ]
    for idx, (code, label) in enumerate(cells):
        cx = m + (idx % 2) * (card_w + 28)
        cy = y0 + (idx // 2) * (card_h + 24)
        _panel(draw, [cx, cy, cx + card_w, cy + card_h])
        draw.text((cx + 28, cy + 24), label, font=_font(30), fill=(160, 175, 205))
        if code in rates:
            val = rates[code]
            if code == "BTC":
                val_str = f"${val:,.0f}".replace(",", " ")
            else:
                val_str = f"{val:.2f}"
            draw.text((cx + 28, cy + 74), val_str, font=_font(58), fill=(255, 255, 255))
            unit = "грн" if code != "BTC" else ""
            if unit:
                w_val = draw.textlength(val_str, font=_font(58))
                draw.text((cx + 36 + w_val, cy + 100), unit, font=_font(28), fill=(160, 175, 205))
            arrow, a_color = _arrow(val, prev_rates.get(code))
            if arrow:
                delta = val - prev_rates.get(code, val)
                delta_str = f"{arrow} {abs(delta):.2f}" if code != "BTC" else arrow
                draw.text((cx + card_w - 28, cy + 88), delta_str, font=_font(34), fill=a_color, anchor="rm")
        else:
            draw.text((cx + 28, cy + 84), "—", font=_font(58), fill=(90, 105, 140))
    y0 += 2 * card_h + 24 + 44

    # --- Секція ЦЬОГО ДНЯ: одна панель зі списком ---
    if observances:
        draw.text((m, y0), "ЦЬОГО ДНЯ ВІДЗНАЧАЮТЬ", font=_font(40), fill=(255, 197, 0))
        y0 += 62
        panel_bottom = Hc - 44
        _panel(draw, [m, y0, Wc - m, panel_bottom])
        ty = y0 + 30
        item_f = _font(30)
        for obs in observances[:4]:
            for j, line in enumerate(_wrap(draw, obs, item_f, Wc - 2 * m - 110)[:2]):
                prefix = "»  " if j == 0 else "    "
                draw.text((m + 32, ty), prefix + line, font=item_f, fill=(230, 236, 248))
                ty += 44
            ty += 6
            if ty > panel_bottom - 50:
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

    # Затемнення, щоб текст читався поверх мозаїки
    canvas = canvas.convert("RGBA")
    shade = Image.new("RGBA", (Wc, Hc), (8, 14, 35, 150))
    canvas = Image.alpha_composite(canvas, shade).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, Wc, 10], fill=(255, 197, 0))
    draw.rectangle([0, Hc - 10, Wc, Hc], fill=(255, 197, 0))
    header = f"НОВИНИ УКРАЇНИ  •  {when.strftime('%d.%m.%Y')}"
    draw.text((Wc // 2, 150), header, font=_font(40), fill=(255, 197, 0), anchor="mm")
    title_font = _font(110)
    draw.text((Wc // 2 + 3, Hc // 2 + 3), "ГОЛОВНЕ ЗА ДЕНЬ", font=title_font, fill=(0, 0, 0), anchor="mm")
    draw.text((Wc // 2, Hc // 2), "ГОЛОВНЕ ЗА ДЕНЬ", font=title_font, fill=(255, 255, 255), anchor="mm")

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
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
