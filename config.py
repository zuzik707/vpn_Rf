import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    VFS_URL = os.getenv("VFS_URL", "https://visa.vfsglobal.com/uzb/en/lva/login")
    VFS_EMAIL = os.getenv("VFS_EMAIL", "")
    VFS_PASSWORD = os.getenv("VFS_PASSWORD", "")

    CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "180"))
    CHECK_INTERVAL_MAX = int(os.getenv("CHECK_INTERVAL_MAX", "420"))

    QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "23"))
    QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "5"))

    BROWSER_DATA_DIR = os.path.join(os.path.dirname(__file__), "browser_data")
    SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
