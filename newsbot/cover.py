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


def make_morning_card(
    when: datetime,
    war_day: int,
    rates: dict[str, float],
    prev_rates: dict[str, float],
    observances: list[str],
) -> bytes:
    """Ранкова інфокартка: дата, день війни, курси валют, пам'ятні дні."""
    Wc, Hc = 1080, 1080
    img = Image.new("RGB", (Wc, Hc))
    draw = ImageDraw.Draw(img)
    top, bottom = (11, 21, 48), (30, 52, 100)
    for y in range(Hc):
        t = y / Hc
        draw.line(
            [(0, y), (Wc, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
        )
    m = 70  # поле

    # Шапка
    draw.rectangle([0, 0, Wc, 12], fill=(255, 197, 0))
    draw.text((m, 52), "УКРАЇНСЬКІ НОВИНИ", font=_font(46), fill=(255, 197, 0))
    date_str = f"{when.day} {_MONTHS_GEN[when.month - 1]} {when.year}, {_WEEKDAYS[when.weekday()]}"
    draw.text((m, 118), date_str, font=_font(36), fill=(230, 236, 248))

    # Стрічка "N-й день війни"
    ribbon_text = f"{war_day}-й день повномасштабної війни"
    rf = _font(34)
    tw = draw.textlength(ribbon_text, font=rf)
    draw.rounded_rectangle([m, 176, m + tw + 48, 232], radius=14, fill=(140, 30, 30))
    draw.text((m + 24, 186), ribbon_text, font=rf, fill=(255, 235, 235))

    y = 286
    # Курси валют
    draw.text((m, y), "КУРС ВАЛЮТ (НБУ)", font=_font(38), fill=(255, 197, 0))
    y += 64
    row_f, val_f = _font(40), _font(40)
    labels = {"USD": "Долар $", "EUR": "Євро €", "PLN": "Злотий zł", "BTC": "Біткоїн ₿"}
    for code in ("USD", "EUR", "PLN", "BTC"):
        if code not in rates:
            continue
        val = rates[code]
        val_str = f"${val:,.0f}".replace(",", " ") if code == "BTC" else f"{val:.2f} грн"
        arrow, a_color = _arrow(val, prev_rates.get(code))
        draw.text((m, y), labels[code], font=row_f, fill=(230, 236, 248))
        draw.text((m + 480, y), val_str, font=val_f, fill=(255, 255, 255))
        if arrow:
            draw.text((m + 760, y), arrow, font=val_f, fill=a_color)
        y += 62
    y += 30
    draw.line([(m, y), (Wc - m, y)], fill=(70, 90, 135), width=2)
    y += 34

    # Цього дня
    if observances:
        draw.text((m, y), "ЦЬОГО ДНЯ ВІДЗНАЧАЮТЬ", font=_font(38), fill=(255, 197, 0))
        y += 64
        item_f = _font(32)
        for obs in observances[:5]:
            for j, line in enumerate(_wrap(draw, obs, item_f, Wc - 2 * m - 40)[:2]):
                prefix = "»  " if j == 0 else "    "
                draw.text((m, y), prefix + line, font=item_f, fill=(230, 236, 248))
                y += 44
            y += 8
            if y > Hc - 120:
                break

    draw.rectangle([0, Hc - 12, Wc, Hc], fill=(255, 197, 0))
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
