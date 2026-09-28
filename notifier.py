import logging
import requests
from config import Config

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен")
        return False

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram отправлено")
        return True
    except requests.RequestException as e:
        logger.error("Telegram ошибка: %s", e)
        return False


def send_telegram_photo(photo_path: str, caption: str = "") -> bool:
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": Config.TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=30,
            )
        resp.raise_for_status()
        return True
    except (requests.RequestException, OSError) as e:
        logger.error("Telegram photo ошибка: %s", e)
        return False


def notify_slots_found(slot_info: str, screenshot_path: str | None = None) -> None:
    msg = (
        "\U0001f525 <b>СЛОТЫ VFS GLOBAL НАЙДЕНЫ!</b> \U0001f525\n\n"
        f"{slot_info}\n\n"
        f"\U0001f449 <a href=\"{Config.VFS_URL}\">ЗАПИСАТЬСЯ СЕЙЧАС</a>\n\n"
        "⚡ Беги бронируй!"
    )
    send_telegram(msg)
    if screenshot_path:
        send_telegram_photo(screenshot_path, "Скриншот страницы со слотами")


def notify_error(error_msg: str) -> None:
    send_telegram(f"⚠️ VFS Monitor:\n<code>{error_msg}</code>")


def notify_status(message: str) -> None:
    send_telegram(f"ℹ️ VFS Monitor: {message}")
