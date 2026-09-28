import logging
import requests

from config import Config

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен — пропускаю отправку")
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
        logger.info("Telegram уведомление отправлено")
        return True
    except requests.RequestException as e:
        logger.error("Ошибка отправки в Telegram: %s", e)
        return False


def notify_slots_found(slots: list[dict], booking_url: str) -> None:
    lines = [
        "🔥 <b>СЛОТЫ VFS GLOBAL НАЙДЕНЫ!</b> 🔥",
        "",
        f"📍 {Config.VFS_CITY}",
        f"🌍 {Config.VFS_SOURCE_COUNTRY.upper()} → {Config.VFS_DESTINATION_COUNTRY.upper()}",
        f"📋 {Config.VFS_VISA_CATEGORY} / {Config.VFS_VISA_SUBCATEGORY}",
        "",
    ]

    for slot in slots[:10]:
        date = slot.get("date", "?")
        time = slot.get("time", "")
        lines.append(f"📅 <b>{date}</b> {time}")

    if len(slots) > 10:
        lines.append(f"... и ещё {len(slots) - 10} слотов")

    lines.extend([
        "",
        f"👉 <a href=\"{booking_url}\">ЗАПИСАТЬСЯ СЕЙЧАС</a>",
        "",
        "⚡ Беги бронируй, слоты разлетаются за минуты!",
    ])

    send_telegram("\n".join(lines))


def notify_error(error_msg: str) -> None:
    send_telegram(f"⚠️ VFS Monitor ошибка:\n<code>{error_msg}</code>")


def notify_status(message: str) -> None:
    send_telegram(f"ℹ️ VFS Monitor: {message}")
