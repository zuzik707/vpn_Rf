#!/usr/bin/env python3
"""
VFS Global Slot Detector
Мониторит доступность слотов для записи на визу и шлёт уведомления в Telegram.
"""

import logging
import random
import signal
import sys
import time
from datetime import datetime, timezone

from config import Config
from notifier import notify_error, notify_slots_found, notify_status
from vfs_checker import VFSChecker

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
    logger.info("Получен сигнал остановки — завершаю")
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


def validate_config() -> bool:
    issues = []
    if not Config.VFS_EMAIL:
        issues.append("VFS_EMAIL не задан")
    if not Config.VFS_PASSWORD:
        issues.append("VFS_PASSWORD не задан")
    if not Config.TELEGRAM_BOT_TOKEN:
        issues.append("TELEGRAM_BOT_TOKEN не задан")
    if not Config.TELEGRAM_CHAT_ID:
        issues.append("TELEGRAM_CHAT_ID не задан")

    if issues:
        print("=" * 50)
        print("ОШИБКИ КОНФИГУРАЦИИ:")
        for issue in issues:
            print(f"  ❌ {issue}")
        print()
        print("Скопируй .env.example в .env и заполни настройки:")
        print("  cp .env.example .env")
        print("  nano .env")
        print("=" * 50)
        return False
    return True


def main():
    print("""
╔══════════════════════════════════════════╗
║     VFS Global Slot Detector v1.0        ║
║     Мониторинг слотов для визы           ║
╚══════════════════════════════════════════╝
    """)

    if not validate_config():
        sys.exit(1)

    checker = VFSChecker()
    consecutive_errors = 0
    last_slots_found = False
    check_count = 0

    logger.info(
        "Старт мониторинга: %s -> %s, город: %s",
        Config.VFS_SOURCE_COUNTRY.upper(),
        Config.VFS_DESTINATION_COUNTRY.upper(),
        Config.VFS_CITY,
    )
    notify_status(
        f"Мониторинг запущен\n"
        f"📍 {Config.VFS_CITY}\n"
        f"🌍 {Config.VFS_SOURCE_COUNTRY.upper()} → {Config.VFS_DESTINATION_COUNTRY.upper()}\n"
        f"📋 {Config.VFS_VISA_CATEGORY}"
    )

    while running:
        if is_quiet_hours():
            logger.debug("Тихие часы — пропускаю проверку")
            time.sleep(600)
            continue

        check_count += 1
        logger.info("Проверка #%d...", check_count)

        try:
            slots = checker.check_slots()

            if not slots:
                slots = checker.get_available_dates()

            if slots:
                logger.info("НАЙДЕНО %d слотов!", len(slots))
                notify_slots_found(slots, checker.get_booking_url())
                last_slots_found = True
                consecutive_errors = 0
                # После нахождения слотов — короткая пауза и продолжаем
                # (слоты могут быть заняты пока юзер дойдёт)
                time.sleep(30)
                continue
            else:
                if last_slots_found:
                    notify_status("Слоты закончились — продолжаю мониторинг")
                last_slots_found = False
                consecutive_errors = 0
                logger.info("Слотов нет — жду следующей проверки")

        except Exception as e:
            consecutive_errors += 1
            logger.error("Ошибка проверки: %s", e, exc_info=True)

            if consecutive_errors >= 5:
                notify_error(
                    f"5 ошибок подряд. Последняя: {e}\n"
                    "Возможно VFS заблокировал IP или сменил API."
                )
                consecutive_errors = 0

        # Рандомный интервал между проверками
        base_interval = random.uniform(
            Config.CHECK_INTERVAL_MIN,
            Config.CHECK_INTERVAL_MAX,
        )
        # При ошибках увеличиваем интервал (backoff)
        if consecutive_errors > 0:
            base_interval *= (1.5 ** consecutive_errors)
            base_interval = min(base_interval, 900)

        # Добавляем джиттер ±15%
        jitter = base_interval * random.uniform(-0.15, 0.15)
        sleep_time = base_interval + jitter

        logger.info("Следующая проверка через %.0f сек", sleep_time)
        time.sleep(sleep_time)

    logger.info("Мониторинг остановлен")
    notify_status("Мониторинг остановлен")


if __name__ == "__main__":
    main()
