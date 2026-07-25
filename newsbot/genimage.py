"""Генерація фонового зображення ранкової картки через Pollinations.ai
(безкоштовно, без ключа). AI-ілюстрації конкретних новин НЕ використовуються —
лише реальні фото; це тло суто декоративне (не претендує зображати подію)."""
from __future__ import annotations

import logging
import urllib.parse

import requests

from . import config

log = logging.getLogger(__name__)

_BG_SCENES = [
    "aerial panorama of Kyiv city skyline at dawn with Dnipro river",
    "misty morning over Ukrainian wheat fields and distant village",
    "Kyiv old town rooftops under soft sunrise light",
    "calm Ukrainian countryside landscape at golden hour, rolling hills",
    "modern Kyiv cityscape at blue hour with glowing lights",
]
_BG_STYLE = (
    "dark navy blue tones, deep teal shadows, atmospheric haze, cinematic, "
    "muted desaturated, minimalist, moody, no people, no text, no logos"
)


def generate_background(seed: int | None = None) -> bytes | None:
    """Атмосферне тло для ранкової картки: тематичне фото у темних тонах.

    Різна сцена щодня (ротація за днем року), але завжди в темно-синій гамі,
    щоб гармонувати з панелями. Повертає None при збої — тоді буде градієнт.
    """
    import datetime as _dt

    idx = (seed if seed is not None else _dt.date.today().toordinal()) % len(_BG_SCENES)
    prompt = f"{_BG_SCENES[idx]}, {_BG_STYLE}"
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt[:400])
        + "?width=1080&height=1500&nologo=true&model=flux&enhance=true"
    )
    try:
        resp = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=150)
        resp.raise_for_status()
        if "image" not in resp.headers.get("Content-Type", ""):
            return None
        if len(resp.content) < config.MIN_IMAGE_BYTES:
            return None
        log.info("Згенеровано фон ранкової картки (%d байт)", len(resp.content))
        return resp.content
    except Exception as exc:  # noqa: BLE001
        log.warning("Фон картки недоступний: %s", exc)
        return None
