# -*- coding: utf-8 -*-
"""
Кросспост нових постів каналу в Instagram — ВІДЕО-ONLY (Reels).

Instagram не приймає текстові пости, тому для кожного поста шукаємо РЕАЛЬНЕ
відео у такому пріоритеті:
  1. Власне відео поста (те саме, що бачать підписники в Telegram).
  2. Відео ЦІЄЇ Ж новини на інших моніторингових каналах (tgtrends,
     newsbot/config.TREND_CHANNELS) — якщо власного поста немає, але подію
     висвітлив хтось із конкурентів з відео.
Якщо реального відео немає ніде — пост просто пропускається (без синтетичної
генерації).

Перед публікацією відео тимчасово завантажується, на нього накладається
текстовий водяний знак каналу (ffmpeg drawtext) і результат недовго
хоститься як asset у GitHub Release — Instagram Graph API приймає лише
публічний video_url, не файл напряму. Asset видаляється одразу після
успішної публікації.

Підпис — текст самого поста (обрізаний), хештеги і посилання на цей
конкретний пост у каналі (не клікабельне в Instagram, але видиме як текст).

Стан (watermark за id) — instagram_crosspost_state.json (кеш Actions).
Секрети: INSTAGRAM_TOKEN, IG_USER_ID. Для gh release потрібен GH_TOKEN —
в Actions це вбудований GITHUB_TOKEN, gh CLI підхоплює його сам.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from newsbot import tgtrends

BASE = Path(__file__).parent
KYIV = ZoneInfo("Europe/Kyiv")
STATE_FILE = BASE / "instagram_crosspost_state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
INSTAGRAM_API = "https://graph.instagram.com"

CHANNEL = os.environ.get("CHANNEL", "Suputnyk_news")  # технічний @username, для скрейпінгу/URL
CHANNEL_URL = f"https://t.me/{CHANNEL}"
WATERMARK_LABEL = os.environ.get("WATERMARK_LABEL", "Suputnyk_news")  # публічна назва каналу для водяного знаку
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
SEED_LAST_ID = int(os.environ.get("SEED_LAST_ID", "0"))
IG_USER_ID = os.environ.get("IG_USER_ID", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "wladislavius-tech/tg-news-agent")
RELEASE_TAG = "instagram-media"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_posted_id": 0, "instagram": {}}


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
        if not text:
            continue
        video = None
        video_el = msg.select_one("video.tgme_widget_message_video")
        if video_el and video_el.get("src"):
            video = video_el["src"]
        posts.append({"id": int(m.group(1)), "text": text, "video": video})
    return posts


SKIP_PREFIXES = ("🔮", "☕️", "☕", "🌙")


def is_news(text: str) -> bool:
    t = text.strip()
    if t.startswith(SKIP_PREFIXES):
        return False
    if "Гороскоп" in t[:40] or "Доброго ранку" in t[:40] or "Головне за" in t[:40]:
        return False
    return True


_STRIKE_VERB_RE = re.compile(r"вразил|уразил|уражен|знищ|завдал.{0,15}удар", re.IGNORECASE)
_RU_TARGET_RE = re.compile(
    r"\bрф\b|росі|нафтопереробн|нпз\b|танкер|аеродром|пускову установку|"
    r"с-?400|переправ|окупант|катер",
    re.IGNORECASE,
)
_RU_AS_ATTACKER_RE = re.compile(
    r"(росі[яєюі]|рф)\w*\s+(вдарил|атакувал|обстріл|завдал.{0,15}удар|уразил|вразил)",
    re.IGNORECASE,
)


def is_strike_news(text: str) -> bool:
    if _RU_AS_ATTACKER_RE.search(text):
        return False
    return bool(_STRIKE_VERB_RE.search(text) and _RU_TARGET_RE.search(text))


TAGS = "#новини #Україна #війна"


def format_body(text: str, limit: int = 800) -> str:
    text = text.split("📌")[0]
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= 120:
        return cut[:end + 1].strip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def build_caption(post: dict) -> str:
    body = format_body(post["text"])
    extra_tag = " #контрудар" if is_strike_news(post["text"]) else ""
    link = f"{CHANNEL_URL}/{post['id']}"
    return f"{body}\n\n{TAGS}{extra_tag}\n\n🔗 Джерело: {link}"[:2200]


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return " ".join(_EMOJI_RE.sub(" ", text).split())


def headline(text: str, max_chars: int = 45) -> str:
    """Коротка плашка-заголовок унизу кадру (орієнтовно 2-5 слів) — не підпис
    під відео (той повний, у build_caption), а візуальний акцент на суть
    новини. Ріжемо по природній межі (коми чи кінця фрази), а не за жорсткою
    кількістю слів — інакше речення обривається на прийменнику."""
    t = _strip_emoji(text)
    t = re.split(r"\s+—\s+", t, maxsplit=1)[0]  # відкидаємо "— Джерело" в кінці
    t = t.split(",")[0].strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,:;—-")


# --- Водяний знак (ffmpeg drawtext) -----------------------------------------

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux (GitHub Actions)
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]
LOGO_PATH = BASE / "assets" / "logo.png"  # кругла напівпрозора іконка каналу (з альфа-каналом)
LOGO_SIZE = 110
WM_MARGIN = 20


def _find_font() -> str | None:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _probe_dimensions(path: Path) -> tuple[int, int] | None:
    """Реальні ширина/висота джерельного відео, або None при збої ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        w, h = out.stdout.strip().split(",")
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return None


def _has_burned_text(path: Path) -> bool:
    """Чи вже є на джерельному відео власний вбудований текст/підпис (типово
    в конкурентів — дата, локація, назва каналу тощо в нижній третині кадру,
    саме там, де ми б поклали свій заголовок). Якщо є — накладати ще й наш
    текст поверх не варто, вийде каша з двох написів. Кадр із середини
    ролика (не перший — там частіше "чорний" вступ), обрізаний до нижніх
    40% висоти, розпізнається через tesseract; точність розпізнавання не
    важлива, лише сам факт наявності помітного тексту (поріг у символах)."""
    try:
        frame_path = path.with_suffix(".ocr.png")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1.5", "-i", str(path), "-frames:v", "1",
             "-vf", "crop=iw:ih*0.4:0:ih*0.6", str(frame_path)],
            capture_output=True, text=True, timeout=20,
        )
        if not frame_path.exists():
            return False
        out = subprocess.run(
            ["tesseract", str(frame_path), "stdout", "-l", "ukr+eng"],
            capture_output=True, text=True, timeout=20,
        )
        frame_path.unlink(missing_ok=True)
        alnum = "".join(ch for ch in out.stdout if ch.isalnum())
        return len(alnum) >= 8
    except Exception as e:  # noqa: BLE001 — збій OCR не має валити публікацію
        print(f"[!] OCR-перевірка тексту на відео: {e}")
        return False


def _escape_drawtext(text: str) -> str:
    """Екранування для значення drawtext: кома розриває ланцюжок фільтрів,
    двокрапка/апостроф/бекслеш конфліктують із синтаксисом самого фільтра."""
    return (text.replace("\\", "\\\\").replace(":", r"\:")
                .replace("'", r"\'").replace(",", r"\,"))


def _wrap_lines_px(text: str, font_path: str, fontsize: int, max_width: int,
                    max_lines: int = 2) -> str:
    """Розбиває підпис на рядки за РЕАЛЬНОЮ шириною гліфів (не кількістю
    символів — жирний шрифт і широкі літери інакше вилазили за межі кадру,
    особливо на вужчих джерельних відео). КОЖЕН рядок перевіряється окремо —
    перша версія завжди робила рівно 2 рядки й запихала всі залишкові слова
    в другий без перевірки його ширини, тож він однаково міг вилізти за межі
    кадру; тепер перенос триває, поки рядків не набереться max_lines (зайві
    слова понад це відкидаються — заголовок і так короткий). Саме СПРАВЖНІЙ
    символ переносу рядка (не послідовність \\n) — drawtext ділить на рядки
    лише за буквальним байтом 0x0A у значенні text=; символьна послідовність
    '\\n' з'їдається парсером опису фільтра ще до drawtext (перевірено окремо)."""
    from PIL import ImageFont
    font = ImageFont.truetype(font_path, fontsize)
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for w in words:
        trial = " ".join(current + [w])
        if current and font.getlength(trial) > max_width:
            lines.append(" ".join(current))
            if len(lines) >= max_lines:
                current = []
                break
            current = [w]
        else:
            current.append(w)
    if current and len(lines) < max_lines:
        lines.append(" ".join(current))
    return "\n".join(lines[:max_lines])


def add_watermark(src: Path, dst: Path, title: str = "") -> bool:
    """Водяний знак у правому верхньому куті: напівпрозорий логотип каналу
    (assets/logo.png, з альфою) над написом-посиланням, і коротка (2-5 слів)
    плашка-заголовок по центру внизу кадру. Повертає False (без винятку),
    якщо ffmpeg не впорався — тоді постимо оригінал без нього, оскільки
    водяний знак другорядний, а публікація реального відео — головне."""
    font = _find_font()
    if not font:
        return False
    # Латиниця свідомо: уникає і проблем з кодуванням аргументів у Windows-
    # підпроцесі, і ризику "тофу"-гліфів, якщо на рантайм-машині не буде
    # шрифту з кирилицею. Лише назва каналу (без "t.me/") — повне посилання
    # вже є в підписі під відео, тут дублювати його не треба.
    # WATERMARK_LABEL — відображувана назва каналу (Suputnyk_news), яка
    # НЕ збігається з технічним username (CHANNEL=News_Ukraine_world_war,
    # той самий, що й раніше — це лише публічний бренд-заголовок каналу).
    label = f"@{WATERMARK_LABEL}"
    font_escaped = font.replace("\\", "/").replace(":", r"\:")
    text_y = WM_MARGIN + LOGO_SIZE + 12
    # Напис лишається прив'язаним ПРАВИМ краєм до фіксованого відступу (як і
    # раніше — це безпечно, не виходить за кадр). Логотип натомість
    # позиціюємо так, щоб опинитись по центру над написом: ширину напису
    # рахуємо заздалегідь у Python (той самий шрифт/розмір), бо в overlay
    # (він іде ДО drawtext у ланцюжку фільтрів) недоступна tw з drawtext.
    from PIL import ImageFont
    text_w = ImageFont.truetype(font, 30).getlength(label)
    text_box_w = text_w + 16  # +boxborderw*2 (падінг рамки підпису)
    # В overlay-фільтрі мала "w" — це ширина САМОГО лого (overlay_w), а не
    # відео; головне відео тут позначається великою "W" (main_w).
    logo_x = f"W-{WM_MARGIN}-{text_box_w / 2:.0f}-{LOGO_SIZE / 2:.0f}"

    # Мобільна стрічка Instagram масштабує/обрізає невертикальне відео під
    # весь екран (десктоп-вебу цей нюанс не стосується — там показує з
    # полями), і текст біля країв "живого" кадру може випасти з обрізаної
    # частини. Тому аналізуємо РЕАЛЬНІ пропорції джерела через ffprobe: якщо
    # відео вже майже вертикальне (близько до 9:16) — лишаємо оригінал як є
    # (найкраща якість); якщо горизонтальне/квадратне — підганяємо під 9:16
    # без полів (crop-to-fill, трохи країв кадру може обрізатись, це не
    # критично), щоб напис гарантовано лишався у видимій зоні всюди.
    dims = _probe_dimensions(src)
    aspect = (dims[0] / dims[1]) if dims else None
    # Якщо ffprobe не зміг визначити пропорції (мережа/збій) — безпечніше
    # ПІДСТРАХУВАТИСЯ й кадрувати, ніж мовчки лишити ризиковане широке відео
    # як є: гірший наслідок помилкового кадрування (трохи зайвого обрізано)
    # значно дешевший за втрату всього тексту в мобільній стрічці.
    needs_reformat = aspect is None or aspect > 0.75
    print(f"  [аспект джерела: {aspect}, кадрування під 9:16: {needs_reformat}]")
    crop_fill = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    base_in = f"[0:v]{crop_fill}[base];" if needs_reformat else ""
    base_ref = "[base]" if needs_reformat else "[0:v]"
    # Фінальна ширина кадру, яку побачить drawtext: 1080, якщо кадруємо під
    # 9:16, інакше реальна ширина джерела (якщо відома — бо саме на вузьких
    # джерельних відео заголовок раніше вилазив за межі кадру з обох боків).
    final_width = 1080 if needs_reformat else (dims[0] if dims else 1080)

    # Коротка плашка-заголовок по центру внизу кадру (2-5 слів, суть новини),
    # у 2 рядки — перенос за РЕАЛЬНОЮ шириною гліфів під фінальну ширину кадру.
    # АЛЕ якщо на самому відео вже є вбудований текст (типово в конкурентів —
    # дата/локація в нижній третині кадру) — наш заголовок туди НЕ кладемо,
    # щоб не робити кашу з двох накладених написів; лишається тільки
    # оригінальний текст джерела.
    title_filter = ""
    if title and not _has_burned_text(src):
        TITLE_FONTSIZE = 54
        TITLE_MARGIN = 40
        TITLE_BOXBORDER = 16
        max_title_w = final_width - 2 * TITLE_MARGIN - 2 * TITLE_BOXBORDER
        wrapped_raw = _wrap_lines_px(title[:90], font, TITLE_FONTSIZE, max_title_w)
        title_text = "\n".join(_escape_drawtext(line) for line in wrapped_raw.split("\n"))
        if title_text:
            title_filter = (
                f"drawtext=fontfile='{font_escaped}':text='{title_text}':fontsize={TITLE_FONTSIZE}:fontcolor=white:"
                f"line_spacing=8:box=1:boxcolor=0x0A122A@0.65:boxborderw={TITLE_BOXBORDER}:x=(w-tw)/2:y=h-th-90"
            )
    else:
        print("  [на відео вже є вбудований текст — не накладаємо власний заголовок]")

    if LOGO_PATH.exists():
        filter_complex = (
            f"{base_in}"
            f"[1:v]scale={LOGO_SIZE}:{LOGO_SIZE}[logo];"
            f"{base_ref}[logo]overlay={logo_x}:{WM_MARGIN}[wm];"
            f"[wm]drawtext=fontfile='{font_escaped}':text='{label}':fontsize=30:fontcolor=white:"
            f"box=1:boxcolor=0x0A122A@0.6:boxborderw=8:x=w-tw-{WM_MARGIN}:y={text_y}[wm2]"
        )
        filter_complex += f";[wm2]{title_filter}[out]" if title_filter else ";[wm2]copy[out]"
        cmd = [
            "ffmpeg", "-y", "-i", str(src), "-i", str(LOGO_PATH),
            "-filter_complex", filter_complex, "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "main",
            "-c:a", "aac", "-b:a", "128k",
            str(dst),
        ]
    else:
        filter_complex = (
            f"{base_in}"
            f"{base_ref}drawtext=fontfile='{font_escaped}':text='{label}':fontsize=26:fontcolor=white:"
            f"box=1:boxcolor=0x0A122A@0.6:boxborderw=10:x=w-tw-20:y=30[wm2]"
        )
        filter_complex += f";[wm2]{title_filter}[out]" if title_filter else ";[wm2]copy[out]"
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-filter_complex", filter_complex, "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "main",
            "-c:a", "aac", "-b:a", "128k",
            str(dst),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[!] ffmpeg watermark: {result.stderr[-500:]}")
    return result.returncode == 0 and dst.exists()


def prepare_video(video_url: str, post_id: int, post_text: str, tmpdir: Path) -> Path | None:
    """Завантажує оригінальне відео поста, накладає водяний знак (якщо вдасться)
    і повертає шлях до фінального файлу, готового для заливки в GitHub Release."""
    src = tmpdir / f"src_{post_id}.mp4"
    try:
        r = requests.get(video_url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        src.write_bytes(r.content)
    except requests.RequestException as e:
        print(f"[!] Завантаження відео: {e}")
        return None

    watermarked = tmpdir / f"ig_{post_id}.mp4"
    if add_watermark(src, watermarked, title=headline(post_text)):
        return watermarked
    print("  [watermark не накладено — постимо оригінал]")
    final = tmpdir / f"ig_{post_id}_plain.mp4"
    src.rename(final)
    return final


# --- Тимчасовий хостинг відео через GitHub Release --------------------------

def upload_release_asset(path: Path) -> str | None:
    subprocess.run(
        ["gh", "release", "create", RELEASE_TAG, "--repo", GH_REPO,
         "--title", "Instagram media (тимчасові файли)",
         "--notes", "Тимчасовий публічний хостинг відео для Instagram Graph API."],
        capture_output=True, text=True,
    )  # якщо реліз вже існує — команда просто впаде, це очікувано й не критично
    r = subprocess.run(
        ["gh", "release", "upload", RELEASE_TAG, str(path), "--repo", GH_REPO, "--clobber"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[!] gh release upload: {r.stderr[-300:]}")
        return None
    return f"https://github.com/{GH_REPO}/releases/download/{RELEASE_TAG}/{path.name}"


def delete_release_asset(name: str) -> None:
    subprocess.run(
        ["gh", "release", "delete-asset", RELEASE_TAG, name, "--repo", GH_REPO, "--yes"],
        capture_output=True, text=True,
    )


# --- Instagram Graph API -----------------------------------------------------

def instagram_token(state: dict) -> str | None:
    ig = state.setdefault("instagram", {})
    token = ig.get("token") or os.environ.get("INSTAGRAM_TOKEN", "")
    if not token:
        return None
    last = dt.datetime.fromisoformat(ig["refreshed_at"]) if ig.get("refreshed_at") else None
    if last and (dt.datetime.now() - last) < dt.timedelta(hours=24):
        return token
    try:
        r = requests.get(
            f"{INSTAGRAM_API}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("access_token"):
            token = r.json()["access_token"]
            ig["token"] = token
            ig["refreshed_at"] = dt.datetime.now().isoformat(timespec="seconds")
            print("Instagram: токен продовжено")
        else:
            ig["token"] = token  # токен ще < 24 год — рефреш недоступний, це нормально
    except requests.RequestException as e:
        print(f"[!] Instagram refresh: {e}")
    return token


def _wait_finished(token: str, container_id: str, retries: int, delay: int) -> bool:
    for _ in range(retries):
        time.sleep(delay)
        try:
            r = requests.get(f"{INSTAGRAM_API}/{container_id}",
                             params={"fields": "status_code", "access_token": token}, timeout=30)
            status = r.json().get("status_code")
        except requests.RequestException:
            continue
        if status == "FINISHED":
            return True
        if status in ("ERROR", "EXPIRED"):
            print(f"[!] Instagram container {status}")
            return False
    return False


def post_instagram_video(token: str, video_url: str, caption: str) -> str | None:
    r = requests.post(
        f"{INSTAGRAM_API}/{IG_USER_ID}/media",
        data={"access_token": token, "media_type": "REELS",
              "video_url": video_url, "caption": caption},
        timeout=30,
    )
    if r.status_code != 200 or "id" not in r.json():
        print(f"[!] Instagram create: {r.status_code} {r.text[:200]}")
        return None
    container_id = r.json()["id"]
    if not _wait_finished(token, container_id, retries=18, delay=10):
        return None
    r2 = requests.post(
        f"{INSTAGRAM_API}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    if r2.status_code != 200:
        print(f"[!] Instagram publish: {r2.status_code} {r2.text[:200]}")
        return None
    return r2.json().get("id")


def publish_post(token: str, post: dict) -> bool:
    with tempfile.TemporaryDirectory() as td:
        video_path = prepare_video(post["video"], post["id"], post["text"], Path(td))
        if not video_path:
            return False
        video_url = upload_release_asset(video_path)
        if not video_url:
            return False
        try:
            post_id = post_instagram_video(token, video_url, build_caption(post))
        finally:
            delete_release_asset(video_path.name)
        return bool(post_id)


def find_source_video(post: dict) -> str | None:
    """Якщо власного відео немає — шукає відео ЦІЄЇ Ж новини на інших
    моніторингових каналах (tgtrends.find_matching_media, той самий
    механізм, що вже реюзається для фото) — краще реальне відео конкурента,
    ніж синтетичне AI-відео, навіть коли наш пост відео не має."""
    try:
        _, video_url = tgtrends.find_matching_media(
            post["text"], dt.datetime.now(KYIV), exclude_channel=CHANNEL,
        )
        return video_url or None
    except Exception as e:  # noqa: BLE001
        print(f"[!] Пошук відео на інших каналах: {e}")
        return None


def main() -> None:
    state = load_state()
    if state["last_posted_id"] == 0 and SEED_LAST_ID:
        state["last_posted_id"] = SEED_LAST_ID
    if not IG_USER_ID:
        print("Немає IG_USER_ID")
        return
    token = instagram_token(state)
    if not token:
        print("Немає токена Instagram")
        return

    posts = fetch_posts()
    fresh = sorted((p for p in posts if p["id"] > state["last_posted_id"]),
                   key=lambda p: p["id"])
    if state["last_posted_id"] == 0:
        fresh = fresh[-15:]  # відео рідкісні — ширше вікно на перший запуск, без заливу архіву

    if not fresh:
        print("Нових постів немає.")
        save_state(state)
        return

    posted = 0
    for p in fresh:
        if posted >= MAX_PER_RUN:
            break

        if not is_news(p["text"]):
            state["last_posted_id"] = p["id"]
            save_state(state)
            continue

        video_url = p.get("video")
        source = "власне відео"
        if not video_url:
            video_url = find_source_video(p)
            source = "відео іншого каналу"

        if not video_url:
            state["last_posted_id"] = p["id"]
            save_state(state)
            continue

        print(f"Пост {p['id']} [{source}]: {p['text'][:60]}...")
        ok = publish_post(token, {**p, "video": video_url})

        if ok:
            print("  Instagram: опубліковано")
            posted += 1
            state["last_posted_id"] = p["id"]
            save_state(state)
            time.sleep(3)
        else:
            print("  Instagram: помилка — спробую наступного разу")
            break
    save_state(state)
    print(f"Готово. Опубліковано новин: {posted}")


if __name__ == "__main__":
    main()
