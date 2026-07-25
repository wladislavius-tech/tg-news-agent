"""Збірка вертикального MP4 (кадр + Ken Burns zoom + озвучка) через ffmpeg."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

TAIL_SECONDS = 1.3  # трохи тиші наприкінці, щоб відео не обривалось разом з голосом
FPS = 30


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def render(frame_path: Path, audio_path: Path, out_path: Path) -> float:
    """Рендерить вертикальне відео: нерухомий кадр з повільним зумом (Ken Burns)
    + аудіодоріжка озвучки. Повертає тривалість відео в секундах."""
    duration = probe_duration(audio_path) + TAIL_SECONDS
    # Апскейл перед zoompan (інакше зум "розсипає" пікселі на статичному кадрі)
    vf = (
        "scale=1350:2400,"
        f"zoompan=z='min(zoom+0.0006,1.15)':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={FPS}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(frame_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "main",
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration:.2f}",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg (код {result.returncode}): {result.stderr[-1500:]}")
    return duration
