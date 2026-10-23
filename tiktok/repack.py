# -*- coding: utf-8 -*-
"""Готує для TikTok лише ГОТОВІ відео — так само, як instagram_crosspost.py.

Нічого не генеруємо: ні слайдів, ні озвучки, ні картинок. Для кожного нового
поста каналу шукаємо РЕАЛЬНЕ відео у такому пріоритеті:
  1. Власне відео поста (те саме, що бачать підписники в Telegram).
  2. Відео ЦІЄЇ Ж новини на інших моніторингових каналах (tgtrends).
Якщо реального відео немає ніде — пост пропускається.

Відео обробляється тією самою функцією, що й для Instagram
(instagram_crosspost.prepare_video): підганяється під 9:16 і отримує
водяний знак каналу. Результат — tiktok/output/<id>.mp4 + <id>.txt (підпис).
ПУБЛІКАЦІЮ ця програма не робить — це tiktok.publish.

Запуск: python -m tiktok.repack [--count N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instagram_crosspost as ig  # noqa: E402 — реюз вибору відео, 9:16 і водяного знаку
from tiktok import state as state_mod  # noqa: E402

log = logging.getLogger("tiktok.repack")
KYIV = ZoneInfo("Europe/Kyiv")

OUTPUT_DIR = Path(__file__).parent / "output"
HEADLINE_LIMIT = 100  # publish.py ріже підпис до 150 символів — лишаємо місце під хештеги
HASHTAGS = "#новини #україна #війна"
# Швидкі "Оновлено"-допости про ту саму подію виглядають як дублі — те саме
# правило, що й в Instagram.
UPDATE_COOLDOWN_MINUTES = int(os.environ.get("UPDATE_COOLDOWN_MINUTES", "90"))


def fetch_posts() -> list[dict]:
    """Як ig.fetch_posts, але зберігає розбиття на рядки: перший рядок поста —
    це його заголовок, і без переносів він "втікає" в тіло новини."""
    r = requests.get(f"https://t.me/s/{ig.CHANNEL}", headers=ig.HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for msg in soup.select(".tgme_widget_message"):
        m = re.match(rf"{re.escape(ig.CHANNEL)}/(\d+)", msg.get("data-post", ""), re.IGNORECASE)
        text_el = msg.select_one(".tgme_widget_message_text")
        if not m or not text_el:
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        text = text_el.get_text("", strip=False).strip()
        if not text:
            continue
        video_el = msg.select_one("video.tgme_widget_message_video")
        video = video_el["src"] if video_el and video_el.get("src") else None
        posts.append({"id": int(m.group(1)), "text": text, "video": video})
    return posts


def _headline(text: str, limit: int = HEADLINE_LIMIT) -> str:
    """Заголовок поста — його перший рядок, обрізаний по межі слова."""
    first = next((ln.strip() for ln in text.split("📌")[0].splitlines() if ln.strip()), "")
    if len(first) <= limit:
        return first
    return first[:limit].rsplit(" ", 1)[0] + "…"


def build_caption(post: dict) -> str:
    tags = HASHTAGS + (" #контрудар" if ig.is_strike_news(post["text"]) else "")
    return f"{_headline(post['text'])} {tags}"


def process_post(post: dict, video_url: str) -> bool:
    with tempfile.TemporaryDirectory() as td:
        video_path = ig.prepare_video(video_url, post["id"], Path(td))
        if not video_path:
            return False
        OUTPUT_DIR.mkdir(exist_ok=True)
        shutil.copyfile(video_path, OUTPUT_DIR / f"{post['id']}.mp4")
    (OUTPUT_DIR / f"{post['id']}.txt").write_text(build_caption(post), encoding="utf-8")
    return True


def run(count: int) -> None:
    state = state_mod.load()
    posts = fetch_posts()
    fresh = sorted((p for p in posts if p["id"] > state["last_processed_id"]),
                   key=lambda p: p["id"])
    if state["last_processed_id"] == 0:
        fresh = fresh[-15:]  # відео рідкісні; на перший запуск — без заливу архіву

    if not fresh:
        log.info("Нових постів каналу немає.")
        return

    prepared = 0
    for post in fresh:
        if prepared >= count:
            break
        pid = post["id"]

        if not ig.is_news(post["text"]):
            state["last_processed_id"] = pid
            state_mod.save(state)
            continue

        if ig.is_update(post["text"]):
            last = state.get("last_update_at")
            if last and (dt.datetime.now() - dt.datetime.fromisoformat(last)
                         ) < dt.timedelta(minutes=UPDATE_COOLDOWN_MINUTES):
                log.info("Пост %d: 'Оновлено' занадто швидко після попереднього — пропускаю", pid)
                state["last_processed_id"] = pid
                state_mod.save(state)
                continue

        video_url = post.get("video")
        source = "власне відео"
        if not video_url:
            video_url = ig.find_source_video(post)
            source = "відео іншого каналу"
        if not video_url:
            # Реального відео немає — пропускаємо, нічого не генеруємо
            state["last_processed_id"] = pid
            state_mod.save(state)
            continue

        log.info("Пост %d [%s]: %s", pid, source, post["text"][:60])
        try:
            ok = process_post(post, video_url)
        except Exception:  # noqa: BLE001 — один збій не має зупиняти всю партію
            log.exception("Не вдалося підготувати відео для поста %d", pid)
            ok = False
        if not ok:
            # Збій завантаження/обробки: пост не позначаємо оброблений — спробуємо
            # наступного запуску, але далі партію не продовжуємо (як і в Instagram)
            break
        prepared += 1
        if ig.is_update(post["text"]):
            state["last_update_at"] = dt.datetime.now().isoformat(timespec="seconds")
        state["last_processed_id"] = pid
        state_mod.save(state)

    log.info("Готово. Підготовлено відео: %d (у %s)", prepared, OUTPUT_DIR)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3, help="Максимум відео за запуск")
    args = parser.parse_args()
    run(args.count)


if __name__ == "__main__":
    main()
