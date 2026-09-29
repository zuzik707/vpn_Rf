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

from accounts_db import get_enabled_accounts, import_from_env, count_accounts, ban_account
from budget_guard import BudgetGuard
from config import Config
from notifier import notify_error, notify_slots_found, notify_status
from session_manager import SessionStats, save_session_state, load_session_state
from tg_bot import TelegramBot
from vfs_checker import VFSBrowser


class AccountRotator:
    """Rotates VFS accounts on consecutive failures/bans."""

    MAX_FAILS = 3
    COOLDOWN = 1800  # 30 min cooldown for banned accounts

    def __init__(self, accounts: list[dict]):
        self.accounts = accounts
        self.index = 0
        self.fails: dict[str, int] = {}
        self.cooldowns: dict[str, float] = {}

    @property
    def current(self) -> dict:
        return self.accounts[self.index]

    def report_fail(self, email: str) -> None:
        self.fails[email] = self.fails.get(email, 0) + 1

    def report_success(self, email: str) -> None:
        self.fails[email] = 0

    def should_rotate(self, email: str) -> bool:
        return self.fails.get(email, 0) >= self.MAX_FAILS

    def rotate(self) -> dict | None:
        if len(self.accounts) <= 1:
            return None
        self.cooldowns[self.current["email"]] = time.time()
        original = self.index
        for _ in range(len(self.accounts)):
            self.index = (self.index + 1) % len(self.accounts)
            acct = self.accounts[self.index]
            cd = self.cooldowns.get(acct["email"], 0)
            if time.time() - cd >= self.COOLDOWN:
                logging.getLogger(__name__).info(
                    "Account rotation: %s → %s",
                    self.accounts[original]["email"],
                    acct["email"],
                )
                return acct
        self.index = (original + 1) % len(self.accounts)
        logging.getLogger(__name__).warning("All accounts on cooldown, using %s", self.current["email"])
        return self.current

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("vfs_monitor.log", maxBytes=10*1024*1024, backupCount=3, encoding="utf-8"),
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
    # VFS_EMAIL/VFS_PASSWORD no longer required — accounts come from DB
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not getattr(Config, var, ""):
            missing.append(var)
    return missing


def get_sleep_interval(consecutive_errors: int, hot_mode: bool,
                       cf_backoff: bool = False, total_workers: int = 1) -> float:
    if hot_mode:
        base = random.uniform(Config.CHECK_INTERVAL_HOT_MIN, Config.CHECK_INTERVAL_HOT_MAX)
    elif is_daytime():
        base = random.uniform(Config.CHECK_INTERVAL_DAY_MIN, Config.CHECK_INTERVAL_DAY_MAX)
    else:
        base = random.uniform(Config.CHECK_INTERVAL_NIGHT_MIN, Config.CHECK_INTERVAL_NIGHT_MAX)

    # With many workers each individual needs to check less often
    # 25 workers × 600s each = one check every 24s across all accounts
    if total_workers > 3:
        base *= max(total_workers / 3, 1.0)

    if consecutive_errors > 0:
        base *= 1.5 ** min(consecutive_errors, 5)
        base = min(base, 3600)

    if cf_backoff:
        base = max(base, 120)

    # Jitter ±40%
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


async def run_worker(worker_id: int, acct: dict, solver, budget: "BudgetGuard",
                     stats: SessionStats, hot_mode_until_shared: dict,
                     total_workers_ref: dict | None = None):
    """Single account worker — runs in its own Chrome instance."""
    email = acct["email"]
    proxy_url = acct.get("proxy", "")
    tag = f"[W{worker_id}:{email.split('@')[0]}]"

    checker = VFSBrowser(
        captcha_solver=solver, email=email, password=acct["password"],
        proxy_url=proxy_url, worker_id=worker_id,
    )
    consecutive_errors = 0
    checks_since_login = 0

    logger.info("%s Старт воркера (proxy=%s)", tag,
                proxy_url.split("@")[-1] if "@" in proxy_url else (proxy_url[:30] or "none"))

    # Stagger start: spread workers over time to avoid simultaneous launches
    if worker_id > 0:
        stagger = worker_id * random.uniform(30, 60)
        logger.info("%s Stagger start: %.0fс", tag, stagger)
        await asyncio.sleep(stagger)

    # Cooldown for freshly added accounts — wait 10-15 min before first login
    added_at = acct.get("added_at", 0)
    if added_at and (time.time() - added_at) < 900:
        cooldown = 900 - (time.time() - added_at) + random.uniform(60, 300)
        logger.info("%s Свежий аккаунт — пауза %.0fс перед первым логином", tag, cooldown)
        await asyncio.sleep(cooldown)

    try:
        stats.logins_total += 1
        login_ok = False
        for login_attempt in range(3):
            if await checker.login():
                login_ok = True
                logger.info("%s Логин OK", tag)
                break
            if checker.banned:
                break
            logger.warning("%s Логин не удался (попытка %d/3)", tag, login_attempt + 1)
            await checker.close_browser()
            if login_attempt < 2:
                await asyncio.sleep(random.uniform(10, 20))

        if checker.banned:
            ban_account(email, checker.ban_reason)
            logger.error("%s ЗАБАНЕН VFS: %s", tag, checker.ban_reason[:100])
            await asyncio.to_thread(notify_error,
                f"🚫 {tag} ЗАБАНЕН VFS!\n"
                f"Причина: {checker.ban_reason[:100]}\n"
                f"Аккаунт отключён. Бан обычно 12-24ч.\n"
                f"Создай новый: /reg")
            await checker.close_browser()
            return

        if not login_ok:
            stats.logins_failed += 1
            logger.error("%s Логин не удался после 3 попыток", tag)
            await asyncio.to_thread(notify_error, f"{tag} Логин не удался после 3 попыток. Проверь proxy/credentials.")
        else:
            await asyncio.to_thread(notify_status, f"{tag} залогинился, начинаю проверку слотов")
            post_login_pause = random.uniform(30, 90)
            logger.info("%s Пауза %.0fс после логина", tag, post_login_pause)
            await asyncio.sleep(post_login_pause)

        while running:
            if is_quiet_hours():
                await asyncio.sleep(600)
                continue

            if should_random_skip() and not hot_mode_until_shared.get("active"):
                await asyncio.sleep(random.uniform(20, 60))
                continue

            stats.checks_total += 1
            checks_since_login += 1
            hot_now = hot_mode_until_shared.get("active", False)

            logger.info("%s Проверка #%d | %s", tag, stats.checks_total,
                        "HOT" if hot_now else ("день" if is_daytime() else "ночь"))

            try:
                if should_sign_out_cycle(checks_since_login) and not hot_now:
                    logger.info("%s Sign out/in цикл", tag)
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(30, 90))
                    checks_since_login = 0

                if not checker.session_alive():
                    logger.info("%s Сессия протухла — рестарт", tag)
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(5, 15))
                    stats.logins_total += 1
                    if not await checker.login():
                        stats.logins_failed += 1
                        raise RuntimeError(f"{tag} Перелогин не удался")
                    checks_since_login = 0

                found, info, screenshot = await checker.check_slots()

                if checker.banned:
                    ban_account(email, checker.ban_reason)
                    logger.error("%s ЗАБАНЕН VFS: %s", tag, checker.ban_reason[:100])
                    await asyncio.to_thread(notify_error,
                        f"🚫 {tag} ЗАБАНЕН VFS!\n"
                        f"Причина: {checker.ban_reason[:100]}\n"
                        f"Аккаунт отключён. Бан обычно 12-24ч.\n"
                        f"Создай новый: /reg")
                    await checker.close_browser()
                    return

                if found:
                    stats.checks_success += 1
                    stats.slots_found_count += 1
                    logger.info("%s СЛОТЫ: %s", tag, info)
                    await asyncio.to_thread(notify_slots_found, f"{tag}\n{info}", screenshot)
                    hot_mode_until_shared["until"] = time.time() + Config.HOT_MODE_DURATION
                    hot_mode_until_shared["active"] = True
                    consecutive_errors = 0
                else:
                    stats.checks_success += 1
                    consecutive_errors = 0

            except Exception as e:
                stats.checks_failed += 1
                consecutive_errors += 1
                logger.error("%s Ошибка #%d: %s", tag, consecutive_errors, e, exc_info=True)

                if consecutive_errors == 3:
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(30, 60))

                if consecutive_errors >= 5:
                    await asyncio.to_thread(notify_error,
                        f"{tag} 5 ошибок подряд: {e}")
                    consecutive_errors = 0
                    await checker.close_browser()
                    await asyncio.sleep(random.uniform(300, 600))
                    continue

            n_workers = total_workers_ref.get("count", 1) if total_workers_ref else 1
            sleep_time = get_sleep_interval(
                consecutive_errors,
                hot_mode_until_shared.get("active", False),
                cf_backoff=checker.should_backoff,
                total_workers=n_workers,
            )
            # Soft start: first 5 checks use 2-3x longer intervals
            if checks_since_login <= 5:
                multiplier = 3.0 - (checks_since_login * 0.4)  # 3.0, 2.6, 2.2, 1.8, 1.4, 1.0
                sleep_time *= max(multiplier, 1.0)
                logger.info("%s Мягкий старт (проверка %d/5): пауза %.0f сек",
                           tag, checks_since_login, sleep_time)
            else:
                logger.info("%s Сон %.0f сек", tag, sleep_time)
            await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("%s Worker crash: %s", tag, e, exc_info=True)
    finally:
        await checker.close_browser()
        logger.info("%s Worker остановлен", tag)


async def run_monitor():
    missing = validate_config()
    if missing:
        print("Не заполнены в .env: " + ", ".join(missing))
        print("cp .env.example .env && nano .env")
        sys.exit(1)

    budget = BudgetGuard()

    # Proxy setup
    if Config.PROXY_URL:
        safe = Config.PROXY_URL.split("@")[-1] if "@" in Config.PROXY_URL else "configured"
        logger.info("Proxy: %s", safe)
    else:
        logger.warning("Proxy не настроен — риск бана!")

    from captcha_solver import CaptchaSolver
    try:
        solver = CaptchaSolver(budget)
        balance = await asyncio.to_thread(solver.get_balance)
        if balance >= 0:
            logger.info("2Captcha баланс: $%.2f", balance)
    except Exception as e:
        logger.warning("Captcha check: %s", e)
        solver = CaptchaSolver(budget)

    saved = load_session_state()
    stats = SessionStats()
    if saved:
        stats.checks_total = saved.get("checks_total", 0)
        stats.slots_found_count = saved.get("slots_found_count", 0)
        logger.info("Восстановлена статистика: %d проверок, %d слотов",
                     stats.checks_total, stats.slots_found_count)

    # Импортируем аккаунты из .env в SQLite (если ещё нет)
    if Config.VFS_ACCOUNTS:
        imported = import_from_env(Config.VFS_ACCOUNTS)
        if imported:
            logger.info("Импортировано %d аккаунтов из .env в БД", imported)

    # Загружаем аккаунты из БД
    accounts = get_enabled_accounts()
    if not accounts and Config.VFS_ACCOUNTS:
        accounts = Config.VFS_ACCOUNTS

    # Дополняем proxy из Config если не указан в БД
    for acct in accounts:
        if not acct.get("proxy"):
            acct["proxy"] = Config.PROXY_URL

    n = len(accounts)
    mode = "parallel" if n > 1 else ("single" if n == 1 else "waiting")

    hot_mode_shared = {"until": 0.0, "active": False}
    worker_tasks: dict[str, asyncio.Task] = {}
    workers_count = {"count": n}

    # Telegram бот для управления аккаунтами
    tg_bot = None
    if Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID:
        def status_callback():
            enabled = count_accounts()
            total = count_accounts(enabled_only=False)
            return (
                f"<b>VFS Monitor Status</b>\n\n"
                f"{stats.summary()}\n"
                f"{budget.stats_text()}\n\n"
                f"Аккаунтов: {enabled} активных / {total} всего\n"
                f"Режим: sequential rotation (1 Chrome)\n"
                f"{'HOT MODE' if hot_mode_shared.get('active') else ('День' if is_daytime() else 'Ночь')}\n"
                f"Proxy: {Config.PROXY_URL.split('@')[-1] if Config.PROXY_URL else 'нет'}"
            )

        tg_bot = TelegramBot(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID, status_cb=status_callback)
        tg_bot.start()
        logger.info("Telegram бот запущен — управляй аккаунтами через /add, /list, /status")

    acct_list = ", ".join(a["email"] for a in accounts) if accounts else "нет — добавь через /add или /reg"
    logger.info("Старт мониторинга: %s", Config.VFS_URL)
    proxy_safe = Config.PROXY_URL.split("@")[-1] if "@" in Config.PROXY_URL else (Config.PROXY_URL[:30] or "none")
    await asyncio.to_thread(notify_status,
        f"Мониторинг запущен v6.0 (sequential rotation)\n"
        f"URL: {Config.VFS_URL}\n"
        f"Аккаунтов: {n} | {acct_list}\n"
        f"Proxy: {proxy_safe}\n"
        f"Режим: 1 Chrome, случайный аккаунт каждый цикл\n"
        f"День: {Config.CHECK_INTERVAL_DAY_MIN}-{Config.CHECK_INTERVAL_DAY_MAX}с | "
        f"Ночь: {Config.CHECK_INTERVAL_NIGHT_MIN}-{Config.CHECK_INTERVAL_NIGHT_MAX}с\n"
        f"TG бот: {'ON' if tg_bot else 'OFF'} | /add /list /reg /status"
    )

    # Heartbeat + new account watcher
    async def heartbeat_loop():
        last_hb = time.time()
        while running:
            await asyncio.sleep(30)

            if time.time() - last_hb >= Config.HEARTBEAT_INTERVAL_HOURS * 3600:
                if hot_mode_shared["active"] and time.time() > hot_mode_shared["until"]:
                    hot_mode_shared["active"] = False
                    await asyncio.to_thread(notify_status, "Hot mode закончился — слоты разобрали")

                workers_alive = sum(1 for t in worker_tasks.values() if not t.done())
                proxy_info = f"Proxy: {Config.PROXY_URL.split('@')[-1]}" if Config.PROXY_URL else "Proxy: не настроен"
                hb = (
                    f"Heartbeat | {stats.summary()}\n"
                    f"{budget.stats_text()}\n"
                    f"{proxy_info}\n"
                    f"Workers: {workers_alive} | Режим: {'день' if is_daytime() else 'ночь'} | "
                    f"Hot: {'да' if hot_mode_shared['active'] else 'нет'}"
                )
                await asyncio.to_thread(notify_status, hb)
                last_hb = time.time()
                cleanup_screenshots()
                try:
                    from dom_dumper import cleanup_dumps
                    cleanup_dumps()
                except Exception:
                    pass
                await asyncio.to_thread(save_session_state, {
                    "checks_total": stats.checks_total,
                    "slots_found_count": stats.slots_found_count,
                    "logins_total": stats.logins_total,
                })

    # Two relay workers: each logs in with a random account, checks both
    # subcategories twice (4 checks total, ~40-60s), then exits.
    # The other worker picks up immediately with a different account.
    # With 20 accounts: each account visits ~once per 15-20 min (natural),
    # but combined coverage = one check every ~30-60s across all accounts.
    banned_emails: set[str] = set()
    relay_events = [asyncio.Event(), asyncio.Event()]
    relay_events[0].set()  # Worker 0 starts immediately

    async def relay_worker(wid: int):
        consecutive_errors = 0
        my_event = relay_events[wid]
        other_event = relay_events[1 - wid]

        while running:
            await my_event.wait()
            my_event.clear()

            db_accounts = get_enabled_accounts()
            available = [a for a in db_accounts if a["email"] not in banned_emails]

            if not available:
                logger.warning("R%d: Нет доступных аккаунтов, жду 60с...", wid)
                other_event.set()
                await asyncio.sleep(60)
                continue

            if is_quiet_hours():
                logger.info("R%d: Тихие часы — сплю 10 мин", wid)
                other_event.set()
                await asyncio.sleep(600)
                continue

            acct = random.choice(available)
            email = acct["email"]
            proxy_url = acct.get("proxy") or Config.PROXY_URL
            tag = f"[R{wid}:{email.split('@')[0]}]"

            checker = VFSBrowser(
                captcha_solver=solver, email=email, password=acct["password"],
                proxy_url=proxy_url, worker_id=wid,
            )

            try:
                stats.logins_total += 1
                login_ok = await checker.login()

                if checker.banned:
                    ban_account(email, checker.ban_reason)
                    banned_emails.add(email)
                    logger.error("%s ЗАБАНЕН: %s", tag, checker.ban_reason[:100])
                    await asyncio.to_thread(notify_error,
                        f"{tag} ЗАБАНЕН VFS!\n{checker.ban_reason[:100]}\nАккаунт отключён.")
                    await checker.close_browser()
                    other_event.set()
                    continue

                if not login_ok:
                    stats.logins_failed += 1
                    logger.warning("%s Логин не удался", tag)
                    consecutive_errors += 1
                    await checker.close_browser()
                    other_event.set()
                    await asyncio.sleep(random.uniform(15, 30))
                    continue

                await asyncio.to_thread(notify_status, f"{tag} залогинился, проверяю слоты")
                consecutive_errors = 0

                # Check both subcategories, twice each (4 checks total)
                # Random pauses 10-15s between each — no timing pattern
                # Signal other worker before last check so it starts login in parallel
                n_subs = len(Config.VFS_SUBCATEGORIES)
                total_checks = 2 * n_subs  # 2 rounds × 2 subcategories = 4
                check_num = 0
                slot_found = False
                signaled = False

                for round_num in range(2):
                    if slot_found:
                        break
                    for _sub_idx in range(n_subs):
                        check_num += 1
                        stats.checks_total += 1

                        # Signal other worker before last check — overlap login
                        if check_num == total_checks - 1 and not signaled:
                            other_event.set()
                            signaled = True

                        try:
                            found, info, screenshot = await checker.check_slots()

                            if checker.banned:
                                ban_account(email, checker.ban_reason)
                                banned_emails.add(email)
                                logger.error("%s ЗАБАНЕН: %s", tag, checker.ban_reason[:100])
                                await asyncio.to_thread(notify_error,
                                    f"{tag} ЗАБАНЕН VFS!\n{checker.ban_reason[:100]}")
                                slot_found = True
                                break

                            if found:
                                stats.checks_success += 1
                                stats.slots_found_count += 1
                                logger.info("%s СЛОТЫ НАЙДЕНЫ: %s", tag, info)
                                await asyncio.to_thread(notify_slots_found,
                                    f"{tag}\n{info}", screenshot)
                                hot_mode_shared["until"] = time.time() + Config.HOT_MODE_DURATION
                                hot_mode_shared["active"] = True
                                slot_found = True
                                break
                            else:
                                stats.checks_success += 1

                        except Exception as e:
                            stats.checks_failed += 1
                            consecutive_errors += 1
                            logger.error("%s Ошибка: %s", tag, e)
                            break

                        # Random pause between checks — no pattern
                        await asyncio.sleep(random.uniform(10, 15))

                logger.info("%s Проверка завершена (%d checks).", tag, check_num)

            except Exception as e:
                stats.checks_failed += 1
                consecutive_errors += 1
                logger.error("%s Ошибка: %s", tag, e, exc_info=True)
            finally:
                await checker.close_browser()

            # Signal other worker if not yet (error/ban path)
            if not signaled:
                other_event.set()

            # Short random pause before next turn
            pause = random.uniform(15, 40)
            logger.info("%s Пауза %.0fс", tag, pause)
            await asyncio.sleep(pause)

    tasks = []
    try:
        tasks.append(asyncio.create_task(heartbeat_loop()))
        r0 = asyncio.create_task(relay_worker(0))
        r1 = asyncio.create_task(relay_worker(1))
        tasks.extend([r0, r1])
        worker_tasks["__relay_0__"] = r0
        worker_tasks["__relay_1__"] = r1

        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                logger.error("Task died: %s", t.exception())
    except KeyboardInterrupt:
        pass
    finally:
        if tg_bot:
            tg_bot.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Стоп. %s", stats.summary())
        await asyncio.to_thread(notify_status,
            f"Мониторинг остановлен\n{stats.summary()}\n{budget.stats_text()}")


def main():
    print("""
 ╔═══════════════════════════════════════════════════════╗
 ║   VFS Global Slot Detector v6.0                       ║
 ║   UZB → LVA | Sequential Rotation + HumanClicker     ║
 ╚═══════════════════════════════════════════════════════╝
    """)
    asyncio.run(run_monitor())


if __name__ == "__main__":
    main()
