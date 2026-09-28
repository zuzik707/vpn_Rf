#!/usr/bin/env python3
"""
VFS Global Slot Detector v4.0
UZB → LVA | visa.vfsglobal.com/uzb/en/lva

v4 changelog:
- HumanClicker (bezier mouse, hover, dwell)
- SessionWarmer (homepage → country → login)
- Fail-forward (CF backoff)
- Traffic patterns (random skip, sign out/in, jitter ±40%)
- Адаптивные интервалы (день/ночь/hot mode)
- Timezone fix (Ташкент UTC+5)
- Budget guard (circuit breaker для captcha)
- Fallback captcha (2Captcha → CapSolver)
- Heartbeat каждые N часов
- Screenshot retention (auto-cleanup)
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


def get_sleep_interval(consecutive_errors: int, hot_mode: bool, cf_backoff: bool = False) -> float:
    if hot_mode:
        base = random.uniform(Config.CHECK_INTERVAL_HOT_MIN, Config.CHECK_INTERVAL_HOT_MAX)
    elif is_daytime():
        base = random.uniform(Config.CHECK_INTERVAL_DAY_MIN, Config.CHECK_INTERVAL_DAY_MAX)
    else:
        base = random.uniform(Config.CHECK_INTERVAL_NIGHT_MIN, Config.CHECK_INTERVAL_NIGHT_MAX)

    if consecutive_errors > 0:
        base *= 1.5 ** min(consecutive_errors, 5)
        base = min(base, 1200)

    if cf_backoff:
        base = max(base, 120)

    # Jitter ±40% (друг рекомендовал, а не ±15%)
    jitter = base * random.uniform(-0.4, 0.4)
    return base + jitter


def should_random_skip() -> bool:
    """Случайный пропуск проверки — имитация нерегулярного пользователя."""
    return random.random() < 0.05


def should_sign_out_cycle(checks_since_login: int) -> bool:
    """Периодический sign out/in — как реальный пользователь."""
    if checks_since_login < 10:
        return False
    return random.random() < 0.03


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

    # AstroProxy — автоматическое управление прокси
    from proxy_manager import ProxyManager
    proxy = ProxyManager()
    if proxy.is_configured:
        try:
            proxy_url = proxy.setup_best_proxy()
            Config.PROXY_URL = proxy_url
            logger.info("AstroProxy: %s", proxy.stats_text())
        except Exception as e:
            logger.warning("AstroProxy setup failed: %s", e)
    elif Config.PROXY_URL:
        logger.info("Proxy (manual): %s", Config.PROXY_URL.split("@")[-1] if "@" in Config.PROXY_URL else "configured")
    else:
        logger.warning("Proxy не настроен — риск бана!")

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
    checks_since_login = 0

    logger.info("Старт мониторинга: %s", Config.VFS_URL)
    notify_status(
        "Мониторинг запущен v4.0\n"
        f"URL: {Config.VFS_URL}\n"
        f"День: {Config.CHECK_INTERVAL_DAY_MIN}-{Config.CHECK_INTERVAL_DAY_MAX}с | "
        f"Ночь: {Config.CHECK_INTERVAL_NIGHT_MIN}-{Config.CHECK_INTERVAL_NIGHT_MAX}с\n"
        f"Timezone: UTC+{Config.TIMEZONE_OFFSET}\n"
        "Stack: nodriver + HumanClicker + SessionWarmer + budget guard"
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
                proxy_info = proxy.stats_text() if proxy.is_configured else "Proxy: не настроен"
                heartbeat_msg = (
                    f"Heartbeat | {stats.summary()}\n"
                    f"{budget.stats_text()}\n"
                    f"{proxy_info}\n"
                    f"Режим: {'день' if is_daytime() else 'ночь'} | "
                    f"Hot: {'да' if time.time() < hot_mode_until else 'нет'}"
                )
                notify_status(heartbeat_msg)
                last_heartbeat = time.time()
                cleanup_screenshots()

            # Random skip — как нерегулярный пользователь
            if should_random_skip() and not (time.time() < hot_mode_until):
                logger.info("Random skip — пропускаем проверку")
                await asyncio.sleep(random.uniform(20, 60))
                continue

            stats.checks_total += 1
            checks_since_login += 1
            was_hot = hot_mode_until > 0
            hot_now = time.time() < hot_mode_until
            if was_hot and not hot_now:
                notify_status("Hot mode закончился — слоты разобрали")
                hot_mode_until = 0.0
            logger.info("--- Проверка #%d | %s | %s ---",
                        stats.checks_total,
                        "HOT" if hot_now else ("день" if is_daytime() else "ночь"),
                        stats.summary())

            try:
                # Периодический sign out/in цикл
                if should_sign_out_cycle(checks_since_login) and not hot_now:
                    logger.info("Sign out/in цикл (checks_since_login=%d)", checks_since_login)
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(30, 90))
                    checks_since_login = 0

                if not checker.session_alive():
                    logger.info("Сессия протухла — рестарт")
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(5, 15))
                    stats.logins_total += 1
                    if not await checker.login():
                        stats.logins_failed += 1
                        raise RuntimeError("Перелогин не удался")
                    checks_since_login = 0

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
                    consecutive_errors = 0

            except Exception as e:
                stats.checks_failed += 1
                consecutive_errors += 1
                logger.error("Ошибка #%d: %s", consecutive_errors, e, exc_info=True)

                if consecutive_errors == 3:
                    logger.info("3 ошибки подряд — полный рестарт")
                    # Ротация IP при повторных ошибках
                    if proxy.is_configured:
                        new_ip = proxy.rotate_ip()
                        if new_ip:
                            logger.info("IP ротация после ошибок: %s", new_ip)
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

            sleep_time = get_sleep_interval(
                consecutive_errors,
                time.time() < hot_mode_until,
                cf_backoff=checker.should_backoff,
            )
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
 ║   VFS Global Slot Detector v4.0               ║
 ║   UZB → LVA | HumanClicker + SessionWarmer   ║
 ╚═══════════════════════════════════════════════╝
    """)
    asyncio.run(run_monitor())


if __name__ == "__main__":
    main()
