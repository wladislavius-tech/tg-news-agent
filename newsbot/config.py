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
# Особистий chat_id власника: сюди бот шле сповіщення про збої (необов'язково)
TELEGRAM_ADMIN_CHAT = os.environ.get("TELEGRAM_ADMIN_CHAT", "")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Резервна модель: у кожної моделі своя квота безкоштовного тарифу
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
# Другий AI-провайдер (безкоштовний Groq): вмикається, коли Gemini без квоти
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# --- Джерело новин ---
FEED_URL = "https://www.ukr.net/news/main.html"
# Шлюз-читалка для обходу блокування дата-центрових IP (GitHub Actions)
READER_PROXY = "https://r.jina.ai/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTTP_TIMEOUT = 25

# --- Розклад (київський час) ---
# День: 06:00–00:59, ніч: 01:00–05:59
NIGHT_START_HOUR = 1
NIGHT_END_HOUR = 6

# Мінімальні інтервали між постами, хвилин.
# Трохи менші за крок запусків (30 хв), інакше межові секунди + випадковий зсув
# змушують пропускати цикли: фактичний ритм — гаряча ~30 хв, звичайна ~60 хв.
DAY_INTERVAL_HOT = 16      # вдень, якщо новина гаряча
DAY_INTERVAL_NORMAL = 36   # вдень, звичайна новина
NIGHT_INTERVAL_HOT = 110   # вночі, гаряча (фактично ~2 год)
NIGHT_INTERVAL_NORMAL = 165  # вночі, звичайна (фактично ~3 год)

# "Гаряча" новина = стільки або більше пов'язаних публікацій на Укрнеті
HOT_THRESHOLD = 25
# Другий пост за один запуск — лише для ДУЖЕ термінових новин (100+ публікацій)
SECOND_POST_THRESHOLD = 100
# Пауза між постами в межах одного запуску: випадкова, хвилин (від-до)
POSTS_GAP_MINUTES = (5, 10)
# "Живий" розклад: випадкова затримка перед постом, щоб час не був рівно :00/:30
JITTER_MAX_SECONDS = 8 * 60

# Підпис каналу в кінці поста
CHANNEL_NAME = "Українські новини"
CHANNEL_LINK = "https://t.me/News_Ukraine_world_war"

# Вечірній дайджест "Головне за день"
DIGEST_HOUR = 21          # київська година публікації
DIGEST_MIN_ITEMS = 5      # мінімум постів за день, щоб дайджест мав сенс
DIGEST_MAX_LINES = 8

# Ранковий дайджест-картка (курси, день війни, пам'ятні дні)
MORNING_HOUR = 7
# Гороскоп на день
HOROSCOPE_HOUR = 9

# Альбом із кількох фото — для великих подій
ALBUM_THRESHOLD = 50      # від скількох публікацій шукати кілька фото
ALBUM_MAX_PHOTOS = 3
ALBUM_SOURCE_TRIES = 5    # у скількох статей кластера шукати фото для альбому
# У скількох джерелах кластера шукати пряме відео (відео цінніше за фото)
VIDEO_SOURCE_TRIES = 4
# Добірка коротких відео однієї теми (media group з відео, як у УС)
VIDEO_ALBUM_MAX = 6       # максимум відео в добірці (ліміт Telegram — 10)
VIDEO_ALBUM_MIN = 2       # від скількох відео постити добіркою
# Мінімум пов'язаних публікацій, щоб новина взагалі розглядалась
MIN_RELATED = 2
# Не постити новини, старші за стільки годин
MAX_AGE_HOURS = 3
# Максимум постів за один запуск (2 — лише вдень і лише якщо обидві гарячі)
MAX_POSTS_DAY = 2
MAX_POSTS_NIGHT = 1

# --- Тренди з великих Telegram-каналів (резервне джерело новин) ---
# Використовуються, лише коли Укрнет не дав гідних кандидатів.
TREND_CHANNELS = [
    "truexanewsua",   # Труха Україна
    "lachentyt",      # Лачен пише
    "insiderUKR",     # Інсайдер UA
    "operativnoZSU",  # Оперативний ЗСУ
    "suspilnenews",   # Суспільне Новини
    "ukrpravda_news", # Українська правда
    "unian",          # УНІАН
]
TREND_MIN_VIEWS = 30_000    # мінімум переглядів, щоб пост вважався "гарячим"
TREND_MAX_AGE_HOURS = 3     # не старіші за стільки годин
TREND_MIN_TEXT = 80         # мінімальна довжина тексту (відсіює фото без контексту)

# --- Стан ---
STATE_FILE = ROOT / "state.json"
MAX_REMEMBERED_IDS = 500
MAX_REMEMBERED_TITLES = 60
# Поріг схожості заголовків (Жаккар за словами), щоб вважати новину дублем
TITLE_SIMILARITY = 0.55

# --- Пости ---
CAPTION_LIMIT = 950  # ліміт Telegram для підпису до фото — 1024
MIN_IMAGE_BYTES = 8_000  # менші картинки вважаємо битими/заглушками
MIN_IMAGE_WIDTH = 550    # відсіюємо неякісні фото (кадри з відео, прев'юшки)
MIN_IMAGE_HEIGHT = 320
IMAGE_SOURCE_TRIES = 3   # у скількох статей кластера шукати пристойне фото
MIN_VIDEO_BYTES = 100_000
MAX_VIDEO_BYTES = 45_000_000  # ліміт завантаження для ботів Telegram — 50 МБ
