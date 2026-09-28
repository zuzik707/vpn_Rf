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
    VFS_SUBCATEGORIES = [
        "Work (Visa D) Uzbek, Turkmen",
        "Cargo Drivers (Visa D) Uzbek, Turkmen",
    ]

    # 2Captcha
    CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Интервалы (секунды)
    CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "180"))
    CHECK_INTERVAL_MAX = int(os.getenv("CHECK_INTERVAL_MAX", "420"))

    # Тихие часы UTC
    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "23"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "5"))

    BROWSER_DATA_DIR = os.path.join(os.path.dirname(__file__), "browser_data")
    SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
