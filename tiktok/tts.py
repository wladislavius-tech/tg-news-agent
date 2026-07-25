"""Безкоштовна озвучка українською через Edge TTS (без ключа, без ліміту)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

VOICE = "uk-UA-OstapNeural"


async def _synth(text: str, out_path: Path, voice: str) -> None:
    communicate = edge_tts.Communicate(text, voice, rate="+3%")
    await communicate.save(str(out_path))


def synthesize(text: str, out_path: Path, voice: str = VOICE) -> None:
    asyncio.run(_synth(text, out_path, voice))
