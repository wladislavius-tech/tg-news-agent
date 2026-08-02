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
# Третій AI-провайдер (безкоштовний Cerebras): додано 2026-08-01, коли GitHub
# Models пішов у retirement brownout (410 Gone) і весь каскад одночасно
# впав разом з вичерпаною квотою Gemini/Groq — канал на 30+ хв лишився без
# постів із TG-трендів (Ukrnet-фолбек без AI все ще працював). Найщедріший
# безкоштовний тариф з відомих альтернатив (1M токенів/добу, без картки).
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
# Четвертий AI-провайдер — GitHub Models. У GitHub Actions працює через вбудований
# GITHUB_TOKEN (потрібен permissions: models: read у workflow), без окремого ключа.
# Лишений останнім у каскаді (не видалений) — на випадок, якщо retirement
# brownout виявиться тимчасовим, а не остаточним згортанням сервісу.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_MODELS_TOKEN", "")
GITHUB_MODEL = os.environ.get("GITHUB_MODEL", "openai/gpt-4o-mini")
# Той самий довгоживучий токен, що продовжує crosspost.py — тільки для читання
# (stats.py викликає лише *_insights, нічого не публікує й не продовжує сам).
THREADS_TOKEN = os.environ.get("THREADS_TOKEN", "")

# Чи доступний хоч один AI-провайдер (для генерації текстів)
AI_AVAILABLE = bool(GEMINI_API_KEY or GROQ_API_KEY or CEREBRAS_API_KEY or GITHUB_TOKEN)

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

# Мінімальні інтервали між постами, хвилин — ЦЕ і є справжній регулятор темпу.
# Кожен інтервал — діапазон (від, до): фактичне число перемішується у ньому
# окремо для кожного вікна очікування (main.allowed_to_post), тож пости йдуть
# не рівним кроком, а органічно варіюються в межах діапазону.
#
# КРИТИЧНО: верхня межа діапазону МАЄ БУТИ МЕНШОЮ за крок запусків
# (cron-job.org, зараз ~7-8 хв) — інакше перша ж перевірка після посту часто
# не встигає (число з діапазону випадає БІЛЬШИМ за крок), і реальний
# інтервал стрибає одразу до наступного кроку (напр. 30 хв замість задуманих
# 10-20). NIGHT_* — виняток: там діапазони й так набагато ширші за крок,
# тому квантування на кроці запусків не помітне.
DAY_INTERVAL_HOT = (5, 10)       # вдень, якщо новина гаряча
DAY_INTERVAL_NORMAL = (10, 20)   # вдень, звичайна новина
NIGHT_INTERVAL_HOT = (60, 85)    # вночі, гаряча
NIGHT_INTERVAL_NORMAL = (90, 130)  # вночі, звичайна

# "Гаряча" новина = стільки або більше пов'язаних публікацій на Укрнеті
HOT_THRESHOLD = 25
# Додатковий пост за один запуск — лише для дуже термінових новин (було 100,
# тепер ближче до "гарячої": частіше спрацьовує, дає більше постів за день)
SECOND_POST_THRESHOLD = 55
# Пауза між постами в межах одного запуску: випадкова, хвилин (від-до).
# Звужено (було 5-10): при частішому crontab довгий запуск (кілька постів +
# паузи) ризикує не встигнути до наступного тригера й утворити чергу
# (workflow має concurrency: cancel-in-progress: false — черга, не паралель,
# але вона накопичується, якщо запуски довші за крок cron).
POSTS_GAP_MINUTES = (2, 4)
# "Живий" розклад: випадкова затримка перед постом, щоб час не був рівно :00/:30
# Звужено (було 8 хв) — з тієї ж причини, що й POSTS_GAP_MINUTES: довгий сон
# усередині запуску при частому crontab ризикує утворити чергу запусків.
JITTER_MAX_SECONDS = 2 * 60

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
ALBUM_SOURCE_TRIES = 10   # у скількох статей кластера шукати фото для альбому
# У скількох джерелах кластера шукати пряме відео (відео цінніше за фото)
VIDEO_SOURCE_TRIES = 4
# Скільки джерел кластера завантажувати з Укрнету (буфер над ALBUM_SOURCE_TRIES —
# частина статей відсіється як заглушки/биті фото)
SOURCE_FETCH_MAX = 12
# Цільова частка постів з відео за день. Якщо фактична нижча — агент свідомо
# бере відео-сюжет з великих TG-каналів, навіть коли Укрнет має новини.
VIDEO_TARGET_SHARE = 0.15
VIDEO_QUOTA_MIN_POSTS = 4   # квота вмикається лише після стількох постів за день
# М'якші пороги для відео-сюжетів квоти (свіжі відео не встигають набрати перегляди)
VIDEO_TREND_MAX_AGE_HOURS = 6
VIDEO_TREND_MIN_VIEWS = 5_000
# Добірка коротких відео однієї теми (media group з відео, як у УС)
VIDEO_ALBUM_MAX = 6       # максимум відео в добірці (ліміт Telegram — 10)
VIDEO_ALBUM_MIN = 2       # від скількох відео постити добіркою
# Мінімум пов'язаних публікацій, щоб новина взагалі розглядалась
MIN_RELATED = 2
# Не постити новини, старші за стільки годин
MAX_AGE_HOURS = 3
# Максимум постів за один запуск (лише якщо кандидати досить гарячі,
# SECOND_POST_THRESHOLD; кожен додатковий — з паузою POSTS_GAP_MINUTES)
MAX_POSTS_DAY = 3
MAX_POSTS_NIGHT = 2

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
# Скільки вірусних (не про Україну/війну) трендів дозволено постити за день —
# для живості каналу, понад звичайні новини. Далі такі кандидати пропускаються.
VIRAL_QUOTA_MAX = 3

# Пошук фото/відео "тієї самої події" в каналах-конкурентах (build_post, крок
# "власного фото немає") — ширше вікно за TREND_MAX_AGE_HOURS: новина може бути
# ретроспективною аналітикою про подію кількаденної давнини (напр. огляд BBC про
# наслідки удару, опублікований через кілька днів після самого удару), тоді як
# канал-конкурент фотографував саму подію одразу. tgtrends.fetch_channel_history
# гортає ?before= сторінки t.me/s/ лише якщо справді потрібно глибше.
RETRO_MEDIA_MAX_AGE_HOURS = 72

# --- Консенсус каналів-гігантів: синхронна новина = термінова важлива подія ---
# Якщо та сама новина є одночасно в кількох із цих каналів — постимо невідкладно.
CONSENSUS_CHANNELS = ["truexanewsua", "u_now", "oko_ua"]  # Труха, УС, Всевидяще ОКО
CONSENSUS_MIN = 2           # мінімум каналів з однаковою новиною (з 3)
CONSENSUS_AGE_MIN = 20      # вікно синхронності, хвилин

# --- Терміновий алерт: обстріл/повітряна загроза Києва або по всій Україні ---
# Обов'язково й невідкладно, без жодних лімітів на кількість постів (лише не дубль).
KYIV_ALERT_AGE_MIN = 30     # свіжість поста-алерту, хвилин
ALERT_ALLCLEAR_MAX_AGE_MIN = 240  # доки чекаємо "відбій" для дописування в той самий пост

# --- Статистика (newsbot/stats.py) ---
STATS_STATE_FILE = ROOT / "stats_state.json"
STATS_REPORT_HOUR = 21  # київська година надсилання щоденного зведення

# --- Стан ---
STATE_FILE = ROOT / "state.json"
MAX_REMEMBERED_IDS = 500
MAX_REMEMBERED_TITLES = 60
# Поріг схожості заголовків (Жаккар за словами), щоб вважати новину дублем
TITLE_SIMILARITY = 0.55
# Скільки останніх РЕАЛЬНИХ фото (не generic/logo-фолбек) пам'ятаємо для
# перевірки дублів за хешем — Укрнет мутує cluster_id новини, що розвивається,
# AI пише щоразу новий заголовок (текстовий Jaccard це не завжди ловить), але
# фото походить з того самого джерела-статті — лишається ідентичним.
MAX_REMEMBERED_IMAGE_HASHES = 80

# --- Пости ---
CAPTION_LIMIT = 950  # ліміт Telegram для підпису до фото — 1024
MIN_IMAGE_BYTES = 8_000  # менші картинки вважаємо битими/заглушками
MIN_IMAGE_WIDTH = 550    # відсіюємо неякісні фото (кадри з відео, прев'юшки)
MIN_IMAGE_HEIGHT = 320
IMAGE_SOURCE_TRIES = 10  # у скількох статей кластера шукати пристойне фото
                          # (перед тим, як здатись і генерувати AI-ілюстрацію)
# Макс. частка 3 домінантних кольорів: вище — це текстова заглушка видання,
# а не фото (вивірено на живих даних: заглушки ≥0.65, фото ≤0.51)
IMAGE_FLAT_TOP3 = 0.62
MIN_VIDEO_BYTES = 100_000
MAX_VIDEO_BYTES = 45_000_000  # ліміт завантаження для ботів Telegram — 50 МБ
