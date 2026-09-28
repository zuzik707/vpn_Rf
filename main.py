#!/usr/bin/env python3
"""
VFS Global Slot Detector v3.0
UZB → LVA | visa.vfsglobal.com/uzb/en/lva

v3 changelog:
- Адаптивные интервалы (день/ночь/hot mode)
- Timezone fix (Ташкент UTC+5)
- Budget guard (circuit breaker для captcha)
- Fallback captcha (2Captcha → CapSolver)
- Heartbeat каждые N часов
- Screenshot retention (auto-cleanup)
- SessionStats persistent + captcha tracking
"""

import asyncio
import glob
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

from budget_guard import BudgetGuard
from config import Config
from notifier import notify_error, notify_slots_found, notify_status
from session_manager import SessionStats, save_session_state, load_session_state
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


def local_hour() -> int:
    utc_now = datetime.now(timezone.utc)
    local = utc_now + timedelta(hours=Config.TIMEZONE_OFFSET)
    return local.hour


def is_quiet_hours() -> bool:
    hour = local_hour()
    start = Config.QUIET_HOURS_START
    end = Config.QUIET_HOURS_END
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def is_daytime() -> bool:
    hour = local_hour()
    return 10 <= hour < 22


def validate_config() -> list[str]:
    missing = []
    for var in ("VFS_EMAIL", "VFS_PASSWORD", "CAPTCHA_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not getattr(Config, var, ""):
            missing.append(var)
    return missing


def get_sleep_interval(consecutive_errors: int, hot_mode: bool) -> float:
    if hot_mode:
        base = random.uniform(Config.CHECK_INTERVAL_HOT_MIN, Config.CHECK_INTERVAL_HOT_MAX)
    elif is_daytime():
        base = random.uniform(Config.CHECK_INTERVAL_DAY_MIN, Config.CHECK_INTERVAL_DAY_MAX)
    else:
        base = random.uniform(Config.CHECK_INTERVAL_NIGHT_MIN, Config.CHECK_INTERVAL_NIGHT_MAX)

    if consecutive_errors > 0:
        base *= 1.5 ** min(consecutive_errors, 5)
        base = min(base, 1200)

    jitter = base * random.uniform(-0.15, 0.15)
    return base + jitter


def cleanup_screenshots() -> int:
    if not os.path.isdir(Config.SCREENSHOT_DIR):
        return 0
    cutoff = time.time() - Config.SCREENSHOT_RETENTION_HOURS * 3600
    removed = 0
    for f in glob.glob(os.path.join(Config.SCREENSHOT_DIR, "*.png")):
        basename = os.path.basename(f)
        # Скриншоты слотов — не трогаем
        if basename.startswith("slots_found"):
            continue
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Очистка: удалено %d старых скриншотов", removed)
    return removed


async def run_monitor():
    missing = validate_config()
    if missing:
        print("Не заполнены в .env: " + ", ".join(missing))
        print("cp .env.example .env && nano .env")
        sys.exit(1)

    budget = BudgetGuard()

    # Проверяем баланс captcha
    from captcha_solver import CaptchaSolver
    try:
        solver = CaptchaSolver(budget)
        balance = solver.get_balance()
        if balance >= 0:
            logger.info("2Captcha баланс: $%.2f", balance)
    except Exception as e:
        logger.warning("Captcha check: %s", e)
        solver = CaptchaSolver(budget)

    # Пробуем загрузить сохранённую статистику
    saved = load_session_state()
    stats = SessionStats()
    if saved:
        stats.checks_total = saved.get("checks_total", 0)
        stats.slots_found_count = saved.get("slots_found_count", 0)
        logger.info("Восстановлена статистика: %d проверок, %d слотов",
                     stats.checks_total, stats.slots_found_count)

    checker = VFSBrowser(captcha_solver=solver)
    consecutive_errors = 0
    hot_mode_until = 0.0
    last_heartbeat = time.time()

    logger.info("Старт мониторинга: %s", Config.VFS_URL)
    notify_status(
        "Мониторинг запущен v3.0\n"
        f"URL: {Config.VFS_URL}\n"
        f"День: {Config.CHECK_INTERVAL_DAY_MIN}-{Config.CHECK_INTERVAL_DAY_MAX}с | "
        f"Ночь: {Config.CHECK_INTERVAL_NIGHT_MIN}-{Config.CHECK_INTERVAL_NIGHT_MAX}с\n"
        f"Timezone: UTC+{Config.TIMEZONE_OFFSET}\n"
        "Stack: nodriver + 2Captcha/CapSolver + stealth + budget guard"
    )

    try:
        stats.logins_total += 1
        if not await checker.login():
            stats.logins_failed += 1
            logger.error("Первый логин не удался")
            notify_error("Первый логин не удался. Проверь VFS_EMAIL/VFS_PASSWORD.")
        else:
            logger.info("Первый логин OK")

        while running:
            # Тихие часы
            if is_quiet_hours():
                logger.debug("Тихие часы (%02d:00 local)", local_hour())
                await asyncio.sleep(600)
                continue

            # Heartbeat
            if time.time() - last_heartbeat >= Config.HEARTBEAT_INTERVAL_HOURS * 3600:
                heartbeat_msg = (
                    f"Heartbeat | {stats.summary()}\n"
                    f"{budget.stats_text()}\n"
                    f"Режим: {'день' if is_daytime() else 'ночь'} | "
                    f"Hot: {'да' if time.time() < hot_mode_until else 'нет'}"
                )
                notify_status(heartbeat_msg)
                last_heartbeat = time.time()
                cleanup_screenshots()

            stats.checks_total += 1
            hot_now = time.time() < hot_mode_until
            logger.info("--- Проверка #%d | %s | %s ---",
                        stats.checks_total,
                        "HOT" if hot_now else ("день" if is_daytime() else "ночь"),
                        stats.summary())

            try:
                if not checker.session_alive():
                    logger.info("Сессия протухла — рестарт")
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
                    hot_mode_until = time.time() + Config.HOT_MODE_DURATION
                    consecutive_errors = 0
                else:
                    stats.checks_success += 1
                    if hot_now and time.time() >= hot_mode_until:
                        notify_status("Hot mode закончился — слоты разобрали")
                    consecutive_errors = 0

            except Exception as e:
                stats.checks_failed += 1
                consecutive_errors += 1
                logger.error("Ошибка #%d: %s", consecutive_errors, e, exc_info=True)

                if consecutive_errors == 3:
                    logger.info("3 ошибки подряд — полный рестарт")
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(30, 60))

                if consecutive_errors >= 5:
                    notify_error(f"5 ошибок подряд: {e}\n{stats.summary()}")
                    consecutive_errors = 0
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(300, 600))
                    continue

            # Сохраняем статистику
            save_session_state({
                "checks_total": stats.checks_total,
                "slots_found_count": stats.slots_found_count,
                "logins_total": stats.logins_total,
            })

            sleep_time = get_sleep_interval(consecutive_errors, time.time() < hot_mode_until)
            logger.info("Сон %.0f сек (%s)", sleep_time,
                        "hot" if time.time() < hot_mode_until else
                        "день" if is_daytime() else "ночь")
            await asyncio.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        await checker.close_browser()
        logger.info("Стоп. %s", stats.summary())
        notify_status(f"Мониторинг остановлен\n{stats.summary()}\n{budget.stats_text()}")


def main():
    print("""
 ╔═══════════════════════════════════════════════╗
 ║   VFS Global Slot Detector v3.0               ║
 ║   UZB → LVA | nodriver + stealth + budget     ║
 ╚═══════════════════════════════════════════════╝
    """)
    asyncio.run(run_monitor())


if __name__ == "__main__":
    main()
