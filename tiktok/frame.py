"""Вертикальний (9:16) кадр-заставка для короткого відео TikTok.

Той самий фірмовий стиль каналу (темно-синій + жовтий акцент), що й у
newsbot/cover.py — шрифти й перенос рядків беремо звідти, щоб не дублювати.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from newsbot import cover as tgcover

W, H = 1080, 1920
MARGIN = 90
# Безпечні зони TikTok: зверху (аватар/юзернейм оверлею) і знизу (підпис,
# музика, кнопки лайк/коментар/поділитись праворуч) — текст туди не кладемо.
SAFE_TOP = 260
SAFE_BOTTOM = 420


def _gradient_background() -> Image.Image:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    top, bottom = (10, 18, 42), (26, 44, 88)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def _photo_background(photo: bytes) -> Image.Image | None:
    try:
        bg = Image.open(io.BytesIO(photo)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    bg = ImageOps.fit(bg, (W, H)).filter(ImageFilter.GaussianBlur(2))
    canvas = bg.convert("RGBA")
    veil = Image.new("RGBA", (W, H))
    vd = ImageDraw.Draw(veil)
    for y in range(H):
        a = int(150 + 70 * (y / H))  # темніше донизу, під текст і бренд-плашку
        vd.line([(0, y), (W, y)], fill=(8, 14, 34, a))
    canvas = Image.alpha_composite(canvas, veil)
    return canvas.convert("RGB")


def render_slide(headline: str, *, background: bytes | None = None, tag: str = "НОВИНИ УКРАЇНИ") -> bytes:
    """Заставка: фон (фото новини/AI-ілюстрація або градієнт) + великий заголовок
    у безпечній зоні + бренд-плашка й підпис каналу знизу."""
    img = (background and _photo_background(background)) or _gradient_background()
    draw = ImageDraw.Draw(img)

    # Бренд-плашка зверху (у безпечній зоні)
    tgcover._panel(draw, [MARGIN, 90, MARGIN + 620, 172], fill=(255, 197, 0), outline=(255, 197, 0))
    draw.text((MARGIN + 310, 131), tag, font=tgcover._font(40), fill=(15, 23, 42), anchor="mm")

    # Заголовок — великий, по центру безпечної зони, підбираємо розмір під довжину
    max_w = W - 2 * MARGIN
    avail_h = H - SAFE_TOP - SAFE_BOTTOM
    for size in (96, 84, 72, 62, 54, 46):
        font = tgcover._font(size)
        lines = tgcover._wrap(draw, headline, font, max_w)
        line_h = int(size * 1.22)
        if len(lines) * line_h <= avail_h:
            break
    total_h = len(lines) * line_h
    y = SAFE_TOP + (avail_h - total_h) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        x = (W - w) / 2
        # Тонка тінь для читабельності на будь-якому фоні
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h

    # Підпис каналу знизу (у безпечній зоні)
    draw.rectangle([0, H - SAFE_BOTTOM + 60, W, H - SAFE_BOTTOM + 66], fill=(255, 197, 0))
    draw.text(
        (W / 2, H - SAFE_BOTTOM + 130),
        "Українські новини • Telegram",
        font=tgcover._font(34),
        fill=(230, 236, 248),
        anchor="mm",
    )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
