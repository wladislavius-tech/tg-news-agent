# -*- coding: utf-8 -*-
"""
Вертикальна (9:16) заставка для fallback-відео Instagram (текстові новини
без власного відео) — окремий рендер від tiktok/frame.py (той файл спільний
з TikTok-пайплайном іншої сесії, тут не чіпаємо), з актуальним брендом
каналу: реальне лого замість жовтої плашки, коректний підпис "@Suputnyk_news".
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from newsbot import cover as tgcover

BASE = Path(__file__).parent
LOGO_PATH = BASE / "assets" / "logo.png"

W, H = 1080, 1920
MARGIN = 90
SAFE_TOP = 260
SAFE_BOTTOM = 420
LOGO_SIZE = 160


def _gradient_background() -> Image.Image:
    """Фірмовий градієнт + м'яке радіальне підсвічування по центру —
    трохи візуально багатше за плаский лінійний градієнт, коли немає фото."""
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    top, bottom = (10, 18, 42), (26, 44, 88)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    cx, cy, r = W // 2, int(H * 0.42), int(W * 0.9)
    gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    tint = Image.new("RGB", (W, H), (60, 90, 160))
    img = Image.composite(tint, img, glow)
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
        a = int(150 + 70 * (y / H))
        vd.line([(0, y), (W, y)], fill=(8, 14, 34, a))
    canvas = Image.alpha_composite(canvas, veil)
    return canvas.convert("RGB")


def render_slide(headline: str, *, background: bytes | None, watermark_label: str) -> bytes:
    """Заставка: фон (фото новини або багатший градієнт) + великий заголовок
    у безпечній зоні + наше реальне лого й актуальний підпис каналу знизу."""
    img = (background and _photo_background(background)) or _gradient_background()
    img = img.convert("RGBA")

    if LOGO_PATH.exists():
        logo = Image.open(LOGO_PATH).convert("RGBA").resize((LOGO_SIZE, LOGO_SIZE))
        img.alpha_composite(logo, (MARGIN, 70))

    draw = ImageDraw.Draw(img)

    max_w = W - 2 * MARGIN
    avail_h = H - SAFE_TOP - SAFE_BOTTOM
    lines, line_h, font = [headline], 60, tgcover._font(46)
    for size in (88, 76, 66, 58, 50, 44):
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
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h

    draw.rectangle([0, H - SAFE_BOTTOM + 60, W, H - SAFE_BOTTOM + 66], fill=(255, 197, 0))
    draw.text(
        (W / 2, H - SAFE_BOTTOM + 130),
        f"@{watermark_label}",
        font=tgcover._font(38),
        fill=(230, 236, 248),
        anchor="mm",
    )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
