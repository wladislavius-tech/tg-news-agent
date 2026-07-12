"""AI-ілюстрація новини через Pollinations.ai (безкоштовно, без ключа).

Використовується, коли у статей-джерел немає якісного фото.
Gemini перекладає новину в англійський опис сцени, Pollinations малює.
"""
from __future__ import annotations

import logging
import urllib.parse

import requests

from . import config

log = logging.getLogger(__name__)

_STYLE = (
    "masterful editorial illustration for a news magazine cover, cinematic wide "
    "composition with foreground and background depth, dramatic volumetric lighting, "
    "rich detailed environment, atmospheric mood, high detail digital painting, "
    "no text, no letters, no logos, no watermarks"
)
_SAFETY = ", no graphic violence, no blood, no realistic faces of real people"

_PROMPT_CRAFT = """Опиши англійською (30–45 слів) детальну сцену-ілюстрацію для новини.
ГОЛОВНЕ: сцена мусить впізнавано показувати САМЕ ЦЮ подію — конкретні об'єкти й місце
з новини (наприклад: санкції ЄС → прапори ЄС і РФ, документи, печатка; удар по енергетиці →
пошкоджена ТЕЦ, дроти, темні вікна; зерно → порт, судно, елеватор). Не загальний
«настрій міста», а предметна сцена. Насичена композиція, як обкладинка журналу:
головний об'єкт + оточення + атмосфера, освітлення, кольорова гама. Без імен реальних
людей, без тексту на зображенні, без жорстоких сцен. Відповідай ЛИШЕ описом англійською.

Новина: {title}
{description}"""


def generate_illustration(title: str, description: str) -> bytes | None:
    scene = _craft_scene_prompt(title, description) or title
    prompt = f"{scene}, {_STYLE}{_SAFETY}"
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt[:500])
        + "?width=1280&height=720&nologo=true&model=flux&enhance=true"
    )
    try:
        resp = requests.get(
            url, headers={"User-Agent": config.USER_AGENT}, timeout=150
        )
        resp.raise_for_status()
        if "image" not in resp.headers.get("Content-Type", ""):
            return None
        if len(resp.content) < config.MIN_IMAGE_BYTES:
            return None
        log.info("Згенеровано AI-ілюстрацію (%d байт)", len(resp.content))
        return resp.content
    except Exception as exc:  # noqa: BLE001 — збій генерації не критичний
        log.warning("Pollinations недоступний: %s", exc)
        return None


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


def _craft_scene_prompt(title: str, description: str) -> str | None:
    if not (config.AI_AVAILABLE):
        return None
    from . import llm

    data = llm._gemini_json(
        _PROMPT_CRAFT.format(title=title, description=description or "")
        + '\n\nВідповідай строго JSON: {"scene": "..."}',
        temperature=0.6,
    )
    if not data or not data.get("scene"):
        return None
    return " ".join(str(data["scene"]).split())[:400]
