"""Налаштування агента. Значення можна перевизначити через змінні середовища."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Мінімальний завантажувач .env — без зовнішніх залежностей."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# --- Обов'язкові секрети ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# --- Джерело новин ---
FEED_URL = "https://www.ukr.net/news/main.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTTP_TIMEOUT = 25

# --- Розклад (київський час) ---
# День: 06:00–00:59, ніч: 01:00–05:59
NIGHT_START_HOUR = 1
NIGHT_END_HOUR = 6

# Мінімальні інтервали між постами, хвилин
DAY_INTERVAL_HOT = 30      # вдень, якщо новина гаряча
DAY_INTERVAL_NORMAL = 60   # вдень, звичайна новина
NIGHT_INTERVAL_HOT = 120   # вночі, гаряча
NIGHT_INTERVAL_NORMAL = 180

# "Гаряча" новина = стільки або більше пов'язаних публікацій на Укрнеті
HOT_THRESHOLD = 30
# Мінімум пов'язаних публікацій, щоб новина взагалі розглядалась
MIN_RELATED = 2
# Не постити новини, старші за стільки годин
MAX_AGE_HOURS = 3
# Максимум постів за один запуск (2 — лише вдень і лише якщо обидві гарячі)
MAX_POSTS_DAY = 2
MAX_POSTS_NIGHT = 1

# --- Стан ---
STATE_FILE = ROOT / "state.json"
MAX_REMEMBERED_IDS = 500
MAX_REMEMBERED_TITLES = 60
# Поріг схожості заголовків (Жаккар за словами), щоб вважати новину дублем
TITLE_SIMILARITY = 0.55

# --- Пости ---
CAPTION_LIMIT = 950  # ліміт Telegram для підпису до фото — 1024
MIN_IMAGE_BYTES = 8_000  # менші картинки вважаємо битими/заглушками
