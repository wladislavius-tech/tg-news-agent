"""Стан генератора коротких відео: який пост каналу вже перетворено на відео
(watermark за id, той самий підхід, що й у crosspost.py — не переставляти
чергу, інакше нижчі id тихо пропускаються назавжди)."""
from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state.json"


def load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_processed_id": 0}


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
