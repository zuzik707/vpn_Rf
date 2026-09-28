import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # VFS Global — Узбекистан → Латвия
    VFS_URL = os.getenv("VFS_URL", "https://visa.vfsglobal.com/uzb/en/lva/login")
    VFS_EMAIL = os.getenv("VFS_EMAIL", "")
    VFS_PASSWORD = os.getenv("VFS_PASSWORD", "")

    # Параметры бронирования (точные значения из dropdown'ов)
    VFS_CENTRE = os.getenv("VFS_CENTRE", "VFS GLOBAL SERVICES UBKN")
    VFS_CATEGORY = os.getenv("VFS_CATEGORY", "Latvia Long Stay/Visa D")
    _sub_env = os.getenv("VFS_SUBCATEGORIES", "")
    VFS_SUBCATEGORIES = (
        [s.strip() for s in _sub_env.split(";") if s.strip()]
        if _sub_env
        else [
            "Work (Visa D) Uzbek, Turkmen",
            "Cargo Drivers (Visa D) Uzbek, Turkmen",
        ]
    )

    # 2Captcha (primary) + CapSolver (fallback)
    CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")
    CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Адаптивные интервалы (секунды)
    # День (10:00-22:00 Ташкент) — агрессивно
    CHECK_INTERVAL_DAY_MIN = int(os.getenv("CHECK_INTERVAL_DAY_MIN", "30"))
    CHECK_INTERVAL_DAY_MAX = int(os.getenv("CHECK_INTERVAL_DAY_MAX", "60"))
    # Ночь — экономим captcha budget
    CHECK_INTERVAL_NIGHT_MIN = int(os.getenv("CHECK_INTERVAL_NIGHT_MIN", "180"))
    CHECK_INTERVAL_NIGHT_MAX = int(os.getenv("CHECK_INTERVAL_NIGHT_MAX", "300"))
    # После обнаружения слотов — мониторим часто 15 мин
    CHECK_INTERVAL_HOT_MIN = int(os.getenv("CHECK_INTERVAL_HOT_MIN", "10"))
    CHECK_INTERVAL_HOT_MAX = int(os.getenv("CHECK_INTERVAL_HOT_MAX", "20"))
    HOT_MODE_DURATION = int(os.getenv("HOT_MODE_DURATION", "900"))

    # Тихие часы — полная пауза (Ташкент UTC+5)
    TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "5"))
    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "2"))   # 02:00 local
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "6"))       # 06:00 local

    # Heartbeat — статус в Telegram каждые N часов
    HEARTBEAT_INTERVAL_HOURS = int(os.getenv("HEARTBEAT_INTERVAL_HOURS", "6"))

    # Retention — скриншоты старше N часов удаляются (кроме slots_found)
    SCREENSHOT_RETENTION_HOURS = int(os.getenv("SCREENSHOT_RETENTION_HOURS", "24"))

    # Резидентский прокси (опционально — вручную или через AstroProxy API)
    PROXY_URL = os.getenv("PROXY_URL", "")

    # AstroProxy API (автоматическое управление прокси)
    ASTROPROXY_TOKEN = os.getenv("ASTROPROXY_TOKEN", "")

    BROWSER_DATA_DIR = os.path.join(os.path.dirname(__file__), "browser_data")
    SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
