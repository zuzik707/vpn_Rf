#!/usr/bin/env python3
"""
VFS Global Slot Detector v2.0
UZB → LVA | visa.vfsglobal.com/uzb/en/lva

Stack:
- nodriver: anti-detect Chrome (реальный браузер, нет webdriver flag)
- 2Captcha: решает Cloudflare Turnstile
- Stealth: 10+ anti-fingerprinting патчей
- Persistent session: cookies/profile живут между рестартами
- Telegram: мгновенные push-уведомления
"""

import asyncio
import logging
import random
import signal
import sys
import time
from datetime import datetime, timezone

from config import Config
from notifier import notify_error, notify_slots_found, notify_status
from session_manager import SessionStats
from vfs_checker import VFSBrowser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("vfs_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

running = True


def signal_handler(sig, frame):
    global running
    logger.info("Сигнал остановки")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def is_quiet_hours() -> bool:
    hour = datetime.now(timezone.utc).hour
    start = Config.QUIET_HOURS_START
    end = Config.QUIET_HOURS_END
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def validate_config() -> list[str]:
    missing = []
    for var in ("VFS_EMAIL", "VFS_PASSWORD", "CAPTCHA_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not getattr(Config, var, ""):
            missing.append(var)
    return missing


def get_sleep_interval(consecutive_errors: int, slots_just_found: bool) -> float:
    if slots_just_found:
        return random.uniform(25, 45)

    base = random.uniform(Config.CHECK_INTERVAL_MIN, Config.CHECK_INTERVAL_MAX)
    if consecutive_errors > 0:
        base *= 1.5 ** min(consecutive_errors, 5)
        base = min(base, 1200)
    jitter = base * random.uniform(-0.2, 0.2)
    return base + jitter


async def run_monitor():
    missing = validate_config()
    if missing:
        print("Не заполнены в .env: " + ", ".join(missing))
        print("cp .env.example .env && nano .env")
        sys.exit(1)

    # Проверяем баланс 2Captcha
    from captcha_solver import CaptchaSolver
    try:
        solver = CaptchaSolver()
        balance = solver.get_balance()
        if balance >= 0:
            logger.info("2Captcha баланс: $%.2f", balance)
            if balance < 1.0:
                notify_status(f"Внимание: баланс 2Captcha низкий — ${balance:.2f}")
    except Exception as e:
        logger.warning("2Captcha check: %s", e)

    stats = SessionStats()
    checker = VFSBrowser()
    consecutive_errors = 0
    last_slots_found = False

    logger.info("Старт мониторинга: %s", Config.VFS_URL)
    notify_status(
        "Мониторинг запущен v2.0\n"
        f"URL: {Config.VFS_URL}\n"
        f"Интервал: {Config.CHECK_INTERVAL_MIN}-{Config.CHECK_INTERVAL_MAX}с\n"
        "Stack: nodriver + 2Captcha + stealth"
    )

    try:
        # Первый логин
        stats.logins_total += 1
        if not await checker.login():
            stats.logins_failed += 1
            logger.error("Первый логин не удался — пробую дальше")
            notify_error("Первый логин не удался. Проверь данные.")
        else:
            logger.info("Первый логин ОК")

        while running:
            if is_quiet_hours():
                logger.debug("Тихие часы")
                await asyncio.sleep(600)
                continue

            stats.checks_total += 1
            logger.info("--- Проверка #%d | %s ---", stats.checks_total, stats.summary())

            try:
                if not checker.session_alive():
                    logger.info("Сессия протухла — рестарт браузера")
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(5, 15))
                    stats.logins_total += 1
                    if not await checker.login():
                        stats.logins_failed += 1
                        raise RuntimeError("Перелогин не удался")

                found, info, screenshot = await checker.check_slots()

                if found:
                    stats.checks_success += 1
                    stats.slots_found_count += 1
                    logger.info("СЛОТЫ: %s", info)
                    notify_slots_found(info, screenshot)
                    last_slots_found = True
                    consecutive_errors = 0
                else:
                    stats.checks_success += 1
                    if last_slots_found:
                        notify_status("Слоты разобрали — продолжаю")
                    last_slots_found = False
                    consecutive_errors = 0

            except Exception as e:
                stats.checks_failed += 1
                consecutive_errors += 1
                logger.error("Ошибка #%d: %s", consecutive_errors, e, exc_info=True)

                if consecutive_errors == 3:
                    logger.info("3 ошибки подряд — полный рестарт браузера")
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(30, 60))

                if consecutive_errors >= 5:
                    notify_error(f"5 ошибок подряд: {e}\n{stats.summary()}")
                    consecutive_errors = 0
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(300, 600))
                    continue

            sleep_time = get_sleep_interval(consecutive_errors, last_slots_found)
            logger.info("Сон %.0f сек", sleep_time)
            await asyncio.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        await checker.close_browser()
        logger.info("Стоп. %s", stats.summary())
        notify_status(f"Мониторинг остановлен\n{stats.summary()}")


def main():
    print("""
 ╔═══════════════════════════════════════════════╗
 ║   VFS Global Slot Detector v2.0               ║
 ║   UZB → LVA | nodriver + 2Captcha + stealth   ║
 ╚═══════════════════════════════════════════════╝
    """)
    asyncio.run(run_monitor())


if __name__ == "__main__":
    main()
