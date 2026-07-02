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
