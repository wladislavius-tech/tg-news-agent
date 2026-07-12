# -*- coding: utf-8 -*-
"""
Кросспост нових постів каналу у Threads — виконується як окрема задача
в тому ж workflow, що й контент-агент (надійний розклад кожні 30 хв).

Стан (останній опублікований пост + продовжений токен) — у кеші Actions.
Токен Threads продовжується сам раз на добу. Секрет THREADS_TOKEN.
SEED_LAST_ID — з якого id почати, якщо кеш порожній (щоб не дублювати).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
STATE_FILE = BASE / "crosspost_state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
THREADS_API = "https://graph.threads.net/v1.0"

CHANNEL = os.environ.get("CHANNEL", "News_Ukraine_world_war")
CHANNEL_URL = f"https://t.me/{CHANNEL}"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
SEED_LAST_ID = int(os.environ.get("SEED_LAST_ID", "0"))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_posted_id": 0, "threads": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_posts() -> list[dict]:
    r = requests.get(f"https://t.me/s/{CHANNEL}", headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for msg in soup.select(".tgme_widget_message"):
        m = re.match(rf"{re.escape(CHANNEL)}/(\d+)", msg.get("data-post", ""), re.IGNORECASE)
        if not m:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if text:
            posts.append({"id": int(m.group(1)), "text": text})
    return posts


# Теги для пошуку на Threads (для ранжування ваги не мають, лише дискавер)
TAGS = "\n\n#новини #Україна #війна"
# Префікси постів, які НЕ постимо на Threads (контент для підписників, не для стрічки)
SKIP_PREFIXES = ("🔮", "☕️", "☕", "🌙")


def is_news(text: str) -> bool:
    t = text.strip()
    if t.startswith(SKIP_PREFIXES):
        return False
    if "Гороскоп" in t[:40] or "Доброго ранку" in t[:40] or "Головне за" in t[:40]:
        return False
    return True


def format_body(text: str, limit: int = 280) -> str:
    """Короткий чіпкий фрагмент: заголовок+початок, обрізаний по кінцю речення."""
    text = text.split("📌")[0]
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= 120:  # є пристойна межа речення — ріжемо по ній
        return cut[:end + 1].strip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def threads_token(state: dict) -> str | None:
    th = state.setdefault("threads", {})
    token = th.get("token") or os.environ.get("THREADS_TOKEN", "")
    if not token:
        return None
    last = dt.datetime.fromisoformat(th["refreshed_at"]) if th.get("refreshed_at") else None
    if last and (dt.datetime.now() - last) < dt.timedelta(hours=24):
        return token
    try:
        r = requests.get(
            "https://graph.threads.net/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": token},
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("access_token"):
            token = r.json()["access_token"]
            th["token"] = token
            th["refreshed_at"] = dt.datetime.now().isoformat(timespec="seconds")
            print("Threads: токен продовжено")
        else:
            th["token"] = token
    except requests.RequestException as e:
        print(f"[!] Threads refresh: {e}")
    return token


def _publish(token: str, **fields) -> str | None:
    """Створити контейнер і опублікувати. Повертає id опублікованого поста або None."""
    r = requests.post(f"{THREADS_API}/me/threads",
                      data={"access_token": token, **fields}, timeout=30)
    if r.status_code != 200 or "id" not in r.json():
        print(f"[!] Threads create: {r.status_code} {r.text[:200]}")
        return None
    creation_id = r.json()["id"]
    for _ in range(5):
        time.sleep(5)
        r2 = requests.post(f"{THREADS_API}/me/threads_publish",
                           data={"creation_id": creation_id, "access_token": token}, timeout=30)
        if r2.status_code == 200:
            return r2.json().get("id")
        if '"error_subcode":4279009' in r2.text or 'is_transient":true' in r2.text:
            continue
        print(f"[!] Threads publish: {r2.status_code} {r2.text[:200]}")
        return None
    return None


def post_threads(token: str, body: str) -> bool:
    """Головний пост — БЕЗ зовнішнього посилання (лінк у тексті вбиває охоплення).
    Посилання на канал додаємо окремою відповіддю-коментарем."""
    try:
        post_id = _publish(token, media_type="TEXT", text=f"{body}{TAGS}"[:500])
        if not post_id:
            return False
        # Лінк — у відповідь (best-effort): зберігає охоплення головного поста
        try:
            _publish(token, media_type="TEXT", reply_to_id=post_id,
                     text=f"🔗 Повна стрічка новин: {CHANNEL_URL}")
        except requests.RequestException:
            pass
        return True
    except requests.RequestException as e:
        print(f"[!] Threads: {e}")
        return False


def main() -> None:
    state = load_state()
    if state["last_posted_id"] == 0 and SEED_LAST_ID:
        state["last_posted_id"] = SEED_LAST_ID  # не дублювати вже опубліковане
    token = threads_token(state)
    if not token:
        print("Немає токена Threads")
        return

    posts = fetch_posts()
    fresh = sorted((p for p in posts if p["id"] > state["last_posted_id"]),
                   key=lambda p: p["id"])
    if state["last_posted_id"] == 0:
        fresh = fresh[-8:]  # перший запуск — вікно останніх, без заливу архіву

    if not fresh:
        print("Нових постів немає.")
        save_state(state)
        return

    posted = 0
    for p in fresh:
        if posted >= MAX_PER_RUN:
            break
        if not is_news(p["text"]):
            # гороскоп/ранкова картка/дайджест — на Threads не постимо, просто йдемо далі
            state["last_posted_id"] = p["id"]
            save_state(state)
            continue
        body = format_body(p["text"])
        print(f"Пост {p['id']}: {body[:60]}...")
        if post_threads(token, body):
            print("  Threads: опубліковано")
            state["last_posted_id"] = p["id"]
            save_state(state)
            posted += 1
            time.sleep(3)
        else:
            print("  Threads: помилка — спробую наступного разу")
            break
    save_state(state)
    print(f"Готово. Опубліковано новин: {posted}")


if __name__ == "__main__":
    main()
