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

CHANNEL = os.environ.get("CHANNEL", "Suputnyk_news")
CHANNEL_URL = f"https://t.me/{CHANNEL}"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
SEED_LAST_ID = int(os.environ.get("SEED_LAST_ID", "0"))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_posted_id": 0, "threads": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


_BG_IMAGE_RE = re.compile(r"background-image:url\('([^']+)'\)")


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
        if not text:
            continue
        # Перше фото поста (з альбому теж перше) — публічний URL на CDN
        # Telegram (cdn*.telesco.pe), Threads сам його завантажить за IMAGE.
        image = None
        photo_el = msg.select_one(".tgme_widget_message_photo_wrap")
        if photo_el and photo_el.get("style"):
            img_m = _BG_IMAGE_RE.search(photo_el["style"])
            if img_m:
                image = img_m.group(1)
        # Відео поста — теж публічний прямий URL (cdn*.telesco.pe?token=...),
        # Threads сам його завантажить за VIDEO. Пост має або фото, або відео.
        video = None
        video_el = msg.select_one("video.tgme_widget_message_video")
        if video_el and video_el.get("src"):
            video = video_el["src"]
        posts.append({"id": int(m.group(1)), "text": text, "image": image, "video": video})
    return posts


# Теги для пошуку на Threads (для ранжування ваги не мають, лише дискавер)
TAGS = "\n\n#новини #Україна #війна"
# Префікси постів, які НЕ постимо на Threads (контент для підписників, не для стрічки).
# "❗" — термінові алерти повітряної тривоги (compose_alert у newsbot/llm.py,
# більше ніде в кодовій базі не використовується) — на прохання користувача
# такі пости в Threads не дублюємо.
SKIP_PREFIXES = ("🔮", "☕️", "☕", "🌙", "❗")


def is_news(text: str) -> bool:
    t = text.strip()
    if t.startswith(SKIP_PREFIXES):
        return False
    if "Гороскоп" in t[:40] or "Доброго ранку" in t[:40] or "Головне за" in t[:40]:
        return False
    return True


# Новини про удари УКРАЇНИ У ВІДПОВІДЬ по цілях на рф (танкери, НПЗ, аеродроми
# тощо) стабільно дають помітно більше лайків, ніж типовий пост (7-12 проти
# 1-2, спостереження 2026-07-25, вибірка поки мала — переглянути, коли
# з'являться детальні перегляди по постах від 100 підписників). Такий
# контент піднімаємо в черзі кросспосту й додаємо профільний тег.
_STRIKE_VERB_RE = re.compile(r"вразил|уразил|уражен|знищ|завдал.{0,15}удар", re.IGNORECASE)
_RU_TARGET_RE = re.compile(
    r"\bрф\b|росі|нафтопереробн|нпз\b|танкер|аеродром|пускову установку|"
    r"с-?400|переправ|окупант|катер",
    re.IGNORECASE,
)
# "росіяни вдарили", "рф атакувала" тощо — це рф як АТАКУЮЧИЙ (звичайний
# обстріл України), протилежна ситуація до контрудару; без цього винятку
# _RU_TARGET_RE хибно спрацьовував лише тому, що "росі" згадано десь у реченні.
_RU_AS_ATTACKER_RE = re.compile(
    r"(росі[яєюі]|рф)\w*\s+(вдарил|атакувал|обстріл|завдал.{0,15}удар|уразил|вразил)",
    re.IGNORECASE,
)


def is_strike_news(text: str) -> bool:
    if _RU_AS_ATTACKER_RE.search(text):
        return False
    return bool(_STRIKE_VERB_RE.search(text) and _RU_TARGET_RE.search(text))


# Скандали/корупція — теж пріоритетна категорія для кросспосту (поруч з ударами).
_SCANDAL_RE = re.compile(
    r"корупці|хабар|розтрат|привласнен|зловживанн|шахрайств|викрит.{0,15}схем|"
    r"\bсап\b|\bнабу\b|\bдбр\b|\bбеб\b|оголосил.{0,15}підозр|скандал",
    re.IGNORECASE,
)


def is_scandal(text: str) -> bool:
    return bool(_SCANDAL_RE.search(text))


def is_priority(text: str) -> bool:
    """Пост, який слід перевагати без звернення до AI: контрудари/скандали."""
    return is_strike_news(text) or is_scandal(text)


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_GEMINI_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_MODEL = os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_MODELS_TOKEN", "")
GITHUB_MODEL = os.environ.get("GITHUB_MODEL", "openai/gpt-4o-mini")

_COMPARE_PROMPT = """Ти редактор українського новинного каналу (тема: війна, Україна, світові події).
Дано два коротких уривки новин. Обери, який ВАЖЛИВІШИЙ для суспільства прямо зараз —
масштабніша подія, ширший вплив, гучніший резонанс. Якщо приблизно рівні за вагою — обери "a".

Новина A: {a}

Новина B: {b}

Відповідай строго JSON: {{"winner": "a"}} або {{"winner": "b"}}."""


def _gemini_compare(prompt: str) -> dict | None:
    if not GEMINI_API_KEY:
        return None
    for model in _GEMINI_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2, "maxOutputTokens": 200,
                    "responseMimeType": "application/json",
                },
            }
            r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=30)
            r.raise_for_status()
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as e:  # noqa: BLE001 — збій одного провайдера не критичний
            print(f"[!] Gemini compare {model}: {e}")
            continue
    return None


def _lenient_json(text: str) -> dict:
    """Парсить JSON навіть якщо провайдер обгорнув його в markdown чи текст
    навколо (деякі OpenAI-сумісні провайдери не завжди чисто дотримуються
    response_format: json_object) — той самий патерн, що в newsbot/llm.py."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise exc
        return json.loads(text[start : end + 1])


def _groq_compare(prompt: str) -> dict | None:
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2, "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        print(f"[!] Groq compare: {e}")
        return None


def _cloudflare_compare(prompt: str) -> dict | None:
    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        return None
    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
            json={
                "model": CLOUDFLARE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2, "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        r.raise_for_status()
        return _lenient_json(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        print(f"[!] Cloudflare compare: {e}")
        return None


def _openrouter_compare(prompt: str) -> dict | None:
    if not OPENROUTER_API_KEY:
        return None
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2, "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        r.raise_for_status()
        return _lenient_json(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        print(f"[!] OpenRouter compare: {e}")
        return None


def _github_models_compare(prompt: str) -> dict | None:
    if not GITHUB_TOKEN:
        return None
    try:
        r = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            json={
                "model": GITHUB_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2, "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        print(f"[!] GitHub Models compare: {e}")
        return None


def ai_pick_more_important(text_a: str, text_b: str) -> str:
    """"a" чи "b" — яка новина важливіша для суспільства. Каскад провайдерів
    (як newsbot/llm.py): Gemini → Groq → Cloudflare → OpenRouter → GitHub
    Models — наступний вмикається, лише коли попередній без квоти чи впав.
    Фолбек на "a" (хронологічно перша), якщо жоден не відповів."""
    prompt = _COMPARE_PROMPT.format(a=text_a[:500], b=text_b[:500])
    for name, fn in (("Gemini", _gemini_compare), ("Groq", _groq_compare),
                     ("Cloudflare", _cloudflare_compare),
                     ("OpenRouter", _openrouter_compare), ("GitHub Models", _github_models_compare)):
        data = fn(prompt)
        if data:
            winner = "b" if data.get("winner") == "b" else "a"
            print(f"  [AI-порівняння: {name} відповів, переможець {winner}]")
            return winner
    print("  [AI-порівняння: усі провайдери недоступні, фолбек на хронологічно першу]")
    return "a"


def pick_winner(pending: dict, candidate: dict) -> dict:
    """З пари двох новин повертає ту, яку постимо в Threads."""
    a_pri, b_pri = is_priority(pending["text"]), is_priority(candidate["text"])
    if a_pri and not b_pri:
        return pending
    if b_pri and not a_pri:
        return candidate
    # обидва пріоритетні або жоден — просимо AI визначити важливішу
    winner = ai_pick_more_important(pending["text"], candidate["text"])
    return pending if winner == "a" else candidate


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


def _publish(token: str, retries: int = 5, delay: int = 5, **fields) -> str | None:
    """Створити контейнер і опублікувати. Повертає id опублікованого поста або None.
    Відео обробляється довше за фото — виклик з відео передає більший retries/delay."""
    r = requests.post(f"{THREADS_API}/me/threads",
                      data={"access_token": token, **fields}, timeout=30)
    if r.status_code != 200 or "id" not in r.json():
        print(f"[!] Threads create: {r.status_code} {r.text[:200]}")
        return None
    creation_id = r.json()["id"]
    for _ in range(retries):
        time.sleep(delay)
        r2 = requests.post(f"{THREADS_API}/me/threads_publish",
                           data={"creation_id": creation_id, "access_token": token}, timeout=30)
        if r2.status_code == 200:
            return r2.json().get("id")
        if '"error_subcode":4279009' in r2.text or 'is_transient":true' in r2.text:
            continue
        print(f"[!] Threads publish: {r2.status_code} {r2.text[:200]}")
        return None
    return None


def post_threads(token: str, body: str, extra_tag: str = "", image_url: str | None = None,
                  video_url: str | None = None) -> bool:
    """Головний пост — БЕЗ зовнішнього посилання (лінк у тексті вбиває охоплення).
    Посилання на канал додаємо окремою відповіддю-коментарем.
    Якщо в оригінальному пості є відео/фото — постимо як VIDEO/IMAGE (медіа
    стабільно підвищує залучення в стрічці Threads); якщо публікація з медіа
    не вдалась — фолбек на TEXT."""
    try:
        tags = f"{TAGS} {extra_tag}" if extra_tag else TAGS
        caption = f"{body}{tags}"[:500]
        post_id = None
        if video_url:
            # відео обробляється довше за фото — 12 спроб по 8с (~96с) замість 25с
            post_id = _publish(token, retries=12, delay=8,
                               media_type="VIDEO", video_url=video_url, text=caption)
        elif image_url:
            post_id = _publish(token, media_type="IMAGE", image_url=image_url, text=caption)
        if not post_id:
            post_id = _publish(token, media_type="TEXT", text=caption)
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


PENDING_STALE_HOURS = 4  # якщо пара не зібралась так довго — постимо pending окремо


def _hold(p: dict) -> dict:
    return {"id": p["id"], "text": p["text"], "image": p.get("image"),
            "video": p.get("video"),
            "seen_at": dt.datetime.now().isoformat(timespec="seconds")}


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
    # Порядок публікації лишається строго хронологічним (last_posted_id — це
    # водяний знак; переставляти чергу небезпечно — понизить його і призведе
    # або до повторної публікації, або до тихого пропуску старіших постів).
    # Постимо не кожну новину, а одну з КОЖНОЇ ПАРИ — щоб не дублювати 1:1 усе,
    # що вже є в каналі (див. state["pending"] нижче): перший news-пост пари
    # тримаємо без публікації, з другим порівнюємо й публікуємо важливіший.

    if not fresh:
        print("Нових постів немає.")
        save_state(state)
        return

    posted = 0
    pending = state.get("pending")
    for p in fresh:
        if posted >= MAX_PER_RUN:
            break

        if not is_news(p["text"]):
            # гороскоп/ранкова картка/дайджест — на Threads не постимо, просто йдемо далі
            state["last_posted_id"] = p["id"]
            save_state(state)
            continue

        if pending is None:
            pending = _hold(p)
            state["last_posted_id"] = p["id"]
            state["pending"] = pending
            save_state(state)
            continue

        stale = (dt.datetime.now() - dt.datetime.fromisoformat(pending["seen_at"])
                  ) > dt.timedelta(hours=PENDING_STALE_HOURS)
        winner = pending if stale else pick_winner(pending, p)

        body = format_body(winner["text"])
        extra_tag = "#контрудар" if is_strike_news(winner["text"]) else ""
        print(f"Пара {pending['id']}/{p['id']}{' [прострочено]' if stale else ''} → "
              f"пост {winner['id']}: {body[:60]}...{' [ударна тема]' if extra_tag else ''}"
              f"{' [відео]' if winner.get('video') else ' [фото]' if winner.get('image') else ''}")
        if post_threads(token, body, extra_tag, winner.get("image"), winner.get("video")):
            print("  Threads: опубліковано")
            posted += 1
            state["last_posted_id"] = p["id"]
            # якщо pending прострочений, публікуємо його окремо, а p стає
            # початком НОВОЇ пари (а не другим у щойно завершеній парі)
            pending = _hold(p) if stale else None
            state["pending"] = pending
            save_state(state)
            time.sleep(3)
        else:
            print("  Threads: помилка — спробую наступного разу")
            # watermark НЕ просунуто повз p — наступного разу повторимо ту саму пару
            break
    save_state(state)
    print(f"Готово. Опубліковано новин: {posted}")


if __name__ == "__main__":
    main()
