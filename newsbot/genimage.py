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
    "editorial news illustration, symbolic, modern digital art, muted colors, "
    "dramatic lighting, no text, no letters, no logos, no watermarks"
)
_SAFETY = ", no graphic violence, no blood, no realistic faces of real people"

_PROMPT_CRAFT = """Переклади суть новини в короткий (до 20 слів) англійський опис
символічної ілюстрації для неї. Без імен реальних людей, без тексту на зображенні,
без жорстоких сцен — лише символи та атмосфера (прапори, силуети, техніка, будівлі,
погода, предмети). Відповідай ЛИШЕ описом англійською, без пояснень.

Новина: {title}
{description}"""


def generate_illustration(title: str, description: str) -> bytes | None:
    scene = _craft_scene_prompt(title, description) or title
    prompt = f"{scene}, {_STYLE}{_SAFETY}"
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt[:400])
        + "?width=1280&height=720&nologo=true"
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


def _craft_scene_prompt(title: str, description: str) -> str | None:
    if not config.GEMINI_API_KEY:
        return None
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{
            "parts": [{
                "text": _PROMPT_CRAFT.format(title=title, description=description or "")
            }]
        }],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 500},
    }
    try:
        resp = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=payload, timeout=45)
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text.splitlines()[0][:250] if text else None
    except Exception:
        return None
