import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    VFS_CENTER_URL = os.getenv("VFS_CENTER_URL", "https://visa.vfsglobal.com")
    VFS_SOURCE_COUNTRY = os.getenv("VFS_SOURCE_COUNTRY", "rus")
    VFS_DESTINATION_COUNTRY = os.getenv("VFS_DESTINATION_COUNTRY", "fra")
    VFS_CITY = os.getenv("VFS_CITY", "Moscow")
    VFS_VISA_CATEGORY = os.getenv("VFS_VISA_CATEGORY", "Schengen Visa")
    VFS_VISA_SUBCATEGORY = os.getenv("VFS_VISA_SUBCATEGORY", "Tourism")
    VFS_EMAIL = os.getenv("VFS_EMAIL", "")
    VFS_PASSWORD = os.getenv("VFS_PASSWORD", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "120"))
    CHECK_INTERVAL_MAX = int(os.getenv("CHECK_INTERVAL_MAX", "300"))

    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "22"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "6"))

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]
