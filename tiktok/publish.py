# -*- coding: utf-8 -*-
"""Production-публікація відео з tiktok/output/ на TikTok через Content Posting API.

Секрети — ЛИШЕ зі змінних середовища (GitHub Actions secrets):
    TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN

Флоу: refresh access_token -> creator_info/query (щоб дізнатись, які
privacy_level дозволені саме для цього акаунта — постити з довільним
значенням API забороняє) -> publish/video/init -> upload -> polling статусу.

Запуск: python -m tiktok.publish [--dry-run] [--file NAME.mp4]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("tiktok.publish")
OUTPUT_DIR = Path(__file__).parent / "output"

API = "https://open.tiktokapis.com/v2"


def _creds() -> tuple[str, str, str]:
    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    refresh = os.environ.get("TIKTOK_REFRESH_TOKEN")
    if not (key and secret and refresh):
        sys.exit("Задай TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN")
    return key, secret, refresh


def refresh_access_token() -> str:
    key, secret, refresh = _creds()
    r = requests.post(
        f"{API}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": key,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    log.info("access_token оновлено (діє %sс)", data["expires_in"])
    return data["access_token"]


def query_creator_info(token: str) -> dict:
    r = requests.post(
        f"{API}/post/publish/creator_info/query/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


def pick_video() -> Path | None:
    videos = sorted(OUTPUT_DIR.glob("*.mp4"))
    return videos[0] if videos else None


def read_caption(video_path: Path) -> str:
    txt_path = video_path.with_suffix(".txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8").strip()
    return video_path.stem


def publish_video(token: str, video_path: Path, title: str, privacy_level: str) -> str:
    size = video_path.stat().st_size
    log.info("Ініціалізую публікацію: %s (%d байт), privacy_level=%s", video_path.name, size, privacy_level)
    r = requests.post(
        f"{API}/post/publish/video/init/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
        json={
            "post_info": {
                "title": title[:150],
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"init (код {r.status_code}): {r.text[:500]}")
    data = r.json()["data"]
    publish_id, upload_url = data["publish_id"], data["upload_url"]
    log.info("publish_id=%s", publish_id)

    video_bytes = video_path.read_bytes()
    r = requests.put(
        upload_url,
        headers={"Content-Type": "video/mp4", "Content-Range": f"bytes 0-{size - 1}/{size}"},
        data=video_bytes,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Завантаження відео: HTTP {r.status_code} {r.text[:300]}")

    status = "PROCESSING_UPLOAD"
    for _ in range(15):
        r = requests.post(
            f"{API}/post/publish/status/fetch/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": publish_id},
            timeout=30,
        )
        r.raise_for_status()
        status = r.json()["data"]["status"]
        log.info("статус: %s", status)
        if status not in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"):
            break
        time.sleep(3)
    return status


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Лише показати, що було б опубліковано")
    parser.add_argument("--file", help="Конкретний файл у tiktok/output/ (за замовчуванням — перший)")
    args = parser.parse_args()

    video_path = OUTPUT_DIR / args.file if args.file else pick_video()
    if not video_path or not video_path.exists():
        sys.exit(f"Немає відео для публікації в {OUTPUT_DIR}")
    caption = read_caption(video_path)

    access_token = refresh_access_token()
    creator = query_creator_info(access_token)
    options = creator.get("privacy_level_options") or ["SELF_ONLY"]
    log.info("Акаунт: @%s, дозволені privacy_level=%s", creator.get("creator_username"), options)
    if "PUBLIC_TO_EVERYONE" in options:
        privacy_level = "PUBLIC_TO_EVERYONE"
    elif "SELF_ONLY" in options:
        privacy_level = "SELF_ONLY"
    else:
        privacy_level = options[0]

    if args.dry_run:
        print(f"[dry-run] {video_path.name}: privacy_level={privacy_level}\n{caption[:200]}")
        return

    status = publish_video(access_token, video_path, caption, privacy_level)
    if status == "PUBLISH_COMPLETE":
        log.info("Опубліковано ✔ (%s)", privacy_level)
    else:
        log.warning("Кінцевий статус: %s — перевір TikTok Studio", status)


if __name__ == "__main__":
    main()
