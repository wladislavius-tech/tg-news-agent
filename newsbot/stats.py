# -*- coding: utf-8 -*-
"""Розгорнута щоденна статистика Telegram-каналу та Threads-акаунта.

Запускається разом з основним постингом (кожні 30 хв, окремий крок у
post-news.yml): щоразу забирає нові reaction-/member-події Bot API
(без втрат — офсет getUpdates зберігається в stats_state.json), а після
config.STATS_REPORT_HOUR (за Києвом) раз на добу формує розгорнуте зведення
й шле його власнику в TELEGRAM_ADMIN_CHAT: топ-пости з повним текстом і
посиланням, ER% (реакції/лайки відносно переглядів), який НАПРЯМОК контенту
заходить найкраще сьогодні й за останні 14 днів, тренд приросту підписників
за 7 днів. Після відправки добові лічильники обнуляються, а компактний
підсумок дня лягає в history (до 90 днів) — це і є база для трендів.

Bot API НЕ дає індивідуальних підписників і джерел приєднання (звідки саме
прийшла людина — пошук/посилання/інший канал): це є лише в нативній
статистиці каналу і вимагає логіну в особистий акаунт (MTProto), що свідомо
НЕ використовується тут (значно чутливіший доступ, ніж токен бота) —
рішення користувача 01.08.2026. Тут лише те, що дає офіційний Bot API +
публічна сторінка t.me/s/<канал> + офіційний Threads Insights API.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from . import config

KYIV = ZoneInfo("Europe/Kyiv")
TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
THREADS_API = "https://graph.threads.net/v1.0"
_VIEW_MULT = {"K": 1_000, "M": 1_000_000}
HISTORY_MAX_DAYS = 90

# --- Категоризація напрямку контенту (те саме чуття, що й crosspost.py:
# is_strike_news/is_scandal, розширене світом/економікою для аналітики) ---
_STRIKE_VERB_RE = re.compile(r"вразил|уразил|уражен|знищ|завдал.{0,15}удар", re.IGNORECASE)
_RU_TARGET_RE = re.compile(
    r"\bрф\b|росі|нафтопереробн|нпз\b|танкер|аеродром|пускову установку|"
    r"с-?400|переправ|окупант|катер", re.IGNORECASE)
_RU_AS_ATTACKER_RE = re.compile(
    r"(росі[яєюі]|рф)\w*\s+(вдарил|атакувал|обстріл|завдал.{0,15}удар|уразил|вразил)",
    re.IGNORECASE)
_CASUALTY_RE = re.compile(r"загинул|поранен|постражд|жертв|обстріл|атак|дрон|ракет", re.IGNORECASE)
_SCANDAL_RE = re.compile(
    r"корупці|хабар|розтрат|привласнен|зловживанн|шахрайств|викрит.{0,15}схем|"
    r"\bсап\b|\bнабу\b|\bдбр\b|\bбеб\b|оголосил.{0,15}підозр|скандал", re.IGNORECASE)
_WORLD_RE = re.compile(r"\bсша\b|трамп|путін|нато|\bоон\b|санкці|переговор|байден", re.IGNORECASE)
_ECONOMY_RE = re.compile(r"курс|гривн|долар|ціни|ціна|тариф|бюджет|інфляц", re.IGNORECASE)

CATEGORY_LABELS = {
    "strike": "⚔️ Контрудари по РФ",
    "casualty": "🚨 Обстріли/жертви",
    "scandal": "🕵️ Скандали/корупція",
    "world": "🌍 Світова політика",
    "economy": "💱 Економіка",
    "other": "📰 Інше",
}


def categorize(text: str) -> str:
    if _STRIKE_VERB_RE.search(text) and _RU_TARGET_RE.search(text) and not _RU_AS_ATTACKER_RE.search(text):
        return "strike"
    if _SCANDAL_RE.search(text):
        return "scandal"
    if _CASUALTY_RE.search(text):
        return "casualty"
    if _WORLD_RE.search(text):
        return "world"
    if _ECONOMY_RE.search(text):
        return "economy"
    return "other"


def _load() -> dict:
    if config.STATS_STATE_FILE.exists():
        d = json.loads(config.STATS_STATE_FILE.read_text(encoding="utf-8"))
    else:
        d = {}
    d.setdefault("tg_offset", 0)
    d.setdefault("tg_bot_id", 0)
    d.setdefault("tg_reactions", {})  # {"<message_id>": {"total": int, "top_emoji": str}}
    d.setdefault("tg_joined", 0)
    d.setdefault("tg_left", 0)
    d.setdefault("last_report_date", "")
    d.setdefault("history", [])
    return d


def _save(d: dict) -> None:
    config.STATS_STATE_FILE.write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _tg_call(method: str, **params):
    r = requests.get(f"{TG_API}/{method}", params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"{method}: {payload}")
    return payload["result"]


def _bot_id(d: dict) -> int:
    if not d["tg_bot_id"]:
        d["tg_bot_id"] = _tg_call("getMe")["id"]
    return d["tg_bot_id"]


_MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}


def poll_events(d: dict) -> None:
    """Забирає нові reaction-/member-події з Bot API. Викликати щоразу
    (кожні 30 хв) — офсет у стані гарантує, що жодна подія не губиться
    й не читається двічі, навіть якщо запуск пропущено."""
    bot_id = _bot_id(d)
    updates = _tg_call(
        "getUpdates",
        offset=d["tg_offset"] + 1 if d["tg_offset"] else 0,
        timeout=0,
        allowed_updates=json.dumps(["message_reaction_count", "chat_member"]),
    )
    for upd in updates:
        d["tg_offset"] = max(d["tg_offset"], upd["update_id"])
        if "message_reaction_count" in upd:
            mrc = upd["message_reaction_count"]
            mid = str(mrc["message_id"])
            reactions = mrc.get("reactions", [])
            total = sum(r.get("total_count", 0) for r in reactions)
            top = max(reactions, key=lambda r: r.get("total_count", 0), default=None)
            entry = d["tg_reactions"].setdefault(mid, {"total": 0, "top_emoji": ""})
            entry["total"] = total
            if top and isinstance(top.get("type"), dict) and top["type"].get("emoji"):
                entry["top_emoji"] = top["type"]["emoji"]
        elif "chat_member" in upd:
            cm = upd["chat_member"]
            if (cm.get("new_chat_member", {}).get("user", {}) or {}).get("id") == bot_id:
                continue  # зміна прав самого бота — не підписник
            was_member = cm.get("old_chat_member", {}).get("status") in _MEMBER_STATUSES
            is_member = cm.get("new_chat_member", {}).get("status") in _MEMBER_STATUSES
            if is_member and not was_member:
                d["tg_joined"] += 1
            elif was_member and not is_member:
                d["tg_left"] += 1


def fetch_own_posts_today(now: dt.datetime) -> list[dict]:
    """Пости каналу за сьогодні (Київ) з публічної t.me/s/: id, час, текст,
    перегляди, посилання, категорія напрямку контенту."""
    channel = config.TELEGRAM_CHANNEL.lstrip("@")
    try:
        r = requests.get(
            f"https://t.me/s/{channel}",
            headers={"User-Agent": config.USER_AGENT}, timeout=25,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[!] t.me/s/{channel}: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    today = now.date()
    posts = []
    for msg in soup.select(".tgme_widget_message"):
        m = re.match(rf"{re.escape(channel)}/(\d+)", msg.get("data-post", ""), re.IGNORECASE)
        time_el = msg.select_one("time[datetime]")
        if not m or not time_el:
            continue
        try:
            published = dt.datetime.fromisoformat(time_el["datetime"]).astimezone(now.tzinfo)
        except ValueError:
            continue
        if published.date() != today:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        views = 0
        views_el = msg.select_one(".tgme_widget_message_views")
        if views_el:
            vm = re.match(r"([\d.]+)([KM]?)", views_el.get_text(strip=True))
            if vm:
                views = int(float(vm.group(1)) * _VIEW_MULT.get(vm.group(2), 1))
        post_id = int(m.group(1))
        posts.append({
            "id": post_id,
            "time": published.strftime("%H:%M"),
            "text": text,
            "title": (text[:150] + "…") if len(text) > 150 else text,
            "views": views,
            "url": f"https://t.me/{channel}/{post_id}",
            "category": categorize(text),
        })
    return posts


def _metric_value(item: dict):
    if "total_value" in item:
        return item["total_value"].get("value")
    values = item.get("values") or []
    return values[-1]["value"] if values else None


def fetch_threads_stats(now: dt.datetime) -> dict:
    """Профільні метрики + пости за сьогодні через офіційний Threads Insights API."""
    out: dict = {"followers": None, "views_today": 0, "likes_today": 0, "posts": []}
    token = config.THREADS_TOKEN
    if not token:
        return out
    try:
        r = requests.get(
            f"{THREADS_API}/me/threads_insights",
            params={"metric": "views,likes,followers_count", "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("data", []):
            val = _metric_value(item)
            if item["name"] == "followers_count":
                out["followers"] = val
            elif item["name"] == "views":
                out["views_today"] = val or 0
            elif item["name"] == "likes":
                out["likes_today"] = val or 0
    except requests.RequestException as e:
        print(f"[!] Threads insights: {e}")

    try:
        r = requests.get(
            f"{THREADS_API}/me/threads",
            params={"fields": "id,timestamp,permalink,text", "limit": 25, "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        today = now.date()
        for post in r.json().get("data", []):
            try:
                ts = dt.datetime.fromisoformat(
                    post["timestamp"].replace("Z", "+00:00")
                ).astimezone(now.tzinfo)
            except (KeyError, ValueError):
                continue
            if ts.date() != today:
                continue
            metrics: dict = {}
            try:
                ins = requests.get(
                    f"{THREADS_API}/{post['id']}/insights",
                    params={"metric": "views,likes,replies,reposts,quotes", "access_token": token},
                    timeout=30,
                )
                if ins.ok:
                    for m in ins.json().get("data", []):
                        metrics[m["name"]] = _metric_value(m) or 0
            except requests.RequestException:
                pass
            text = post.get("text") or ""
            out["posts"].append({
                "time": ts.strftime("%H:%M"),
                "text": text,
                "title": (text[:150] + "…") if len(text) > 150 else text,
                "url": post.get("permalink", ""),
                "category": categorize(text),
                **metrics,
            })
    except requests.RequestException as e:
        print(f"[!] Threads posts: {e}")
    return out


def aggregate_by_category(posts: list[dict], engagement_key: str) -> dict:
    """{"strike": {"posts": N, "engagement": сума лайків/реакцій, "views": сума переглядів}, ...}"""
    agg: dict[str, dict] = {}
    for p in posts:
        cat = p.get("category", "other")
        a = agg.setdefault(cat, {"posts": 0, "engagement": 0, "views": 0})
        a["posts"] += 1
        a["engagement"] += p.get(engagement_key, 0) or 0
        a["views"] += p.get("views", 0) or 0
    return agg


def _fmt_category_ranking(agg: dict, min_posts: int = 1) -> list[str]:
    rows = []
    for cat, a in agg.items():
        if a["posts"] < min_posts:
            continue
        er = (a["engagement"] / a["views"] * 100) if a["views"] else 0.0
        rows.append((er, cat, a))
    rows.sort(key=lambda row: -row[0])
    return [
        f"  {CATEGORY_LABELS.get(cat, cat)}: {a['posts']} пост(ів), "
        f"ER {er:.1f}% ({a['engagement']}/{a['views']})"
        for er, cat, a in rows
    ]


def _trailing_avg(history: list[dict], n: int, key: str) -> float | None:
    recent = [h.get(key) for h in history[-n:] if h.get(key) is not None]
    return sum(recent) / len(recent) if recent else None


def _category_trend(history: list[dict], n: int, field: str) -> dict:
    totals: dict[str, dict] = {}
    for h in history[-n:]:
        for cat, agg in (h.get(field) or {}).items():
            t = totals.setdefault(cat, {"posts": 0, "engagement": 0, "views": 0})
            t["posts"] += agg.get("posts", 0)
            t["engagement"] += agg.get("engagement", 0)
            t["views"] += agg.get("views", 0)
    return totals


def build_report(d: dict, own_posts: list[dict], threads: dict, subs: int | None, now: dt.datetime) -> str:
    for p in own_posts:
        r = d["tg_reactions"].get(str(p["id"]), {})
        p["reactions"] = r.get("total", 0)
        p["top_emoji"] = r.get("top_emoji") or "👍"

    history = d.get("history", [])
    lines = [f"📊 Розгорнута статистика — {now.strftime('%d.%m.%Y')}", ""]

    # ================= TELEGRAM =================
    lines.append("📣 TELEGRAM-КАНАЛ")
    if subs is not None:
        lines.append(f"Підписників зараз: {subs}")
    net = d["tg_joined"] - d["tg_left"]
    lines.append(f"Сьогодні: +{d['tg_joined']} приєдналось / -{d['tg_left']} відписалось (нетто {net:+d})")
    avg7 = _trailing_avg(history, 7, "tg_net")
    if avg7 is not None:
        lines.append(f"Середній нетто-приріст за 7 днів: {avg7:+.1f}/день")

    tg_views_total = sum(p["views"] for p in own_posts)
    tg_reactions_total = sum(p["reactions"] for p in own_posts)
    lines.append(f"Постів сьогодні: {len(own_posts)} · перегляди: {tg_views_total} · реакції: {tg_reactions_total}")

    ranked = sorted(own_posts, key=lambda p: (-p["reactions"], -p["views"]))
    if ranked:
        lines.append("")
        lines.append("🏆 Топ постів дня:")
        for p in ranked[:5]:
            er = (p["reactions"] / p["views"] * 100) if p["views"] else 0.0
            cat = CATEGORY_LABELS.get(p["category"], p["category"])
            lines.append(
                f"  {p['time']} [{cat}] {p['reactions']}{p['top_emoji']} · "
                f"{p['views']} перегл. · ER {er:.1f}%\n"
                f"  «{p['title']}»\n  {p['url']}"
            )
    else:
        lines.append("")
        lines.append("Постів сьогодні ще не було.")

    cat_today = _fmt_category_ranking(aggregate_by_category(own_posts, "reactions"))
    if cat_today:
        lines.append("")
        lines.append("📊 Що заходить сьогодні (за ER):")
        lines.extend(cat_today)

    cat_14d = _fmt_category_ranking(_category_trend(history, 14, "tg_by_category"), min_posts=2)
    if cat_14d:
        lines.append("")
        lines.append("📈 Що заходить за останні 14 днів:")
        lines.extend(cat_14d[:3])

    # ================= THREADS =================
    lines.append("")
    lines.append("🧵 THREADS")
    if threads.get("followers") is not None:
        lines.append(f"Followers: {threads['followers']}")
        prev_followers = history[-1].get("threads_followers") if history else None
        if prev_followers is not None:
            lines.append(f"Приріст з учора: {threads['followers'] - prev_followers:+d}")
    lines.append(f"Перегляди профілю: {threads.get('views_today', 0)} · Лайки: {threads.get('likes_today', 0)}")

    posts = threads.get("posts") or []
    ranked_th = sorted(posts, key=lambda p: -(p.get("likes") or 0))
    if ranked_th:
        lines.append("")
        lines.append("🏆 Топ постів дня:")
        for p in ranked_th[:5]:
            views = p.get("views") or 0
            er = (p.get("likes", 0) / views * 100) if views else 0.0
            cat = CATEGORY_LABELS.get(p.get("category", "other"), "інше")
            row = (
                f"  {p['time']} [{cat}] {p.get('likes', 0)}❤️ · {views} перегл. · "
                f"{p.get('replies', 0)} відп. · ER {er:.1f}%\n  «{p['title']}»"
            )
            if p.get("url"):
                row += f"\n  {p['url']}"
            lines.append(row)
    else:
        lines.append("")
        lines.append("Постів сьогодні ще не було.")

    th_cat_today = _fmt_category_ranking(aggregate_by_category(posts, "likes"))
    if th_cat_today:
        lines.append("")
        lines.append("📊 Що заходить сьогодні (за ER):")
        lines.extend(th_cat_today)

    th_cat_14d = _fmt_category_ranking(_category_trend(history, 14, "threads_by_category"), min_posts=2)
    if th_cat_14d:
        lines.append("")
        lines.append("📈 Що заходить за останні 14 днів:")
        lines.extend(th_cat_14d[:3])

    return "\n".join(lines)


def _make_history_record(d: dict, own_posts: list[dict], threads: dict, subs: int | None, now: dt.datetime) -> dict:
    for p in own_posts:
        p.setdefault("reactions", d["tg_reactions"].get(str(p["id"]), {}).get("total", 0))
    return {
        "date": now.date().isoformat(),
        "tg_subscribers": subs,
        "tg_joined": d["tg_joined"],
        "tg_left": d["tg_left"],
        "tg_net": d["tg_joined"] - d["tg_left"],
        "tg_posts": len(own_posts),
        "tg_views": sum(p["views"] for p in own_posts),
        "tg_reactions": sum(p.get("reactions", 0) for p in own_posts),
        "tg_by_category": aggregate_by_category(own_posts, "reactions"),
        "threads_followers": threads.get("followers"),
        "threads_views": threads.get("views_today", 0),
        "threads_likes": threads.get("likes_today", 0),
        "threads_posts": len(threads.get("posts") or []),
        "threads_by_category": aggregate_by_category(threads.get("posts") or [], "likes"),
    }


def _send_text(text: str) -> None:
    if not config.TELEGRAM_ADMIN_CHAT:
        print(text)
        return
    chunks = [text]
    if len(text) > 4000:
        marker = "\n🧵 THREADS"
        idx = text.find(marker)
        chunks = [text[:idx], text[idx + 1:]] if idx > 0 else [
            text[i:i + 4000] for i in range(0, len(text), 4000)
        ]
    for chunk in chunks:
        try:
            requests.post(
                f"{TG_API}/sendMessage",
                data={"chat_id": config.TELEGRAM_ADMIN_CHAT, "text": chunk[:4096]},
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"[!] send report: {e}")


def send_report(d: dict, now: dt.datetime) -> None:
    own_posts = fetch_own_posts_today(now)
    threads = fetch_threads_stats(now)
    try:
        subs = _tg_call("getChatMemberCount", chat_id=config.TELEGRAM_CHANNEL)
    except Exception as e:  # noqa: BLE001
        subs = None
        print(f"[!] getChatMemberCount: {e}")

    text = build_report(d, own_posts, threads, subs, now)
    _send_text(text)

    record = _make_history_record(d, own_posts, threads, subs, now)
    d["history"].append(record)
    d["history"] = d["history"][-HISTORY_MAX_DAYS:]


def run(force_report: bool = False) -> None:
    d = _load()
    try:
        poll_events(d)
    except Exception as e:  # noqa: BLE001 — збій опитування не має валити крок
        print(f"[!] poll_events: {e}")
    _save(d)

    now = dt.datetime.now(KYIV)
    today = now.date().isoformat()
    if not force_report and (now.hour < config.STATS_REPORT_HOUR or d["last_report_date"] == today):
        return

    send_report(d, now)
    d["tg_joined"] = 0
    d["tg_left"] = 0
    d["tg_reactions"] = {}
    d["last_report_date"] = today
    _save(d)
    print("Звіт надіслано, добові лічильники обнулено, історія оновлена.")


if __name__ == "__main__":
    run(force_report="--force" in sys.argv)
