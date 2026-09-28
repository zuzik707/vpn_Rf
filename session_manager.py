"""
Управление браузерной сессией.

Ключевой принцип: мы используем РЕАЛЬНЫЙ Chrome через nodriver.
Это значит:
- TLS fingerprint = настоящий Chrome (не подделка)
- Cookie jar = нативный Chrome cookie store
- localStorage/sessionStorage = настоящие
- WebCrypto / SubtleCrypto = реальная реализация
- cf_clearance cookie = валидный, привязан к нашему IP + UA

Наша задача — НЕ МЕНЯТЬ то что Chrome делает правильно,
а только убрать следы automation (webdriver flag, CDP traces).

Persistent user_data_dir означает:
- Cookies сохраняются между перезапусками
- Cloudflare cf_clearance переиспользуется (пока не протухнет)
- Login session может пережить рестарт скрипта
- Browser fingerprint остаётся стабильным
"""

import json
import logging
import os
import time

from config import Config

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(Config.BROWSER_DATA_DIR, "session_state.json")


def save_session_state(data: dict) -> None:
    os.makedirs(Config.BROWSER_DATA_DIR, exist_ok=True)
    data["saved_at"] = time.time()
    data["saved_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.warning("Не удалось сохранить session state: %s", e)


def load_session_state() -> dict | None:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        saved_at = data.get("saved_at", 0)
        # Сессия валидна не больше 25 мин (cf_clearance ~15-30 мин)
        if time.time() - saved_at > 1500:
            logger.info("Сохранённая сессия протухла (%.0f мин назад)", (time.time() - saved_at) / 60)
            return None
        logger.info("Загружена сессия от %s", data.get("saved_at_human", "?"))
        return data
    except (OSError, json.JSONDecodeError):
        return None


def clear_session_state() -> None:
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


class SessionStats:
    """Статистика сессии для мониторинга здоровья."""

    def __init__(self):
        self.checks_total = 0
        self.checks_success = 0
        self.checks_failed = 0
        self.slots_found_count = 0
        self.logins_total = 0
        self.logins_failed = 0
        self.captchas_solved = 0
        self.captchas_failed = 0
        self.cloudflare_passed = 0
        self.cloudflare_failed = 0
        self.start_time = time.time()

    def summary(self) -> str:
        uptime = (time.time() - self.start_time) / 3600
        return (
            f"Uptime: {uptime:.1f}ч | "
            f"Проверок: {self.checks_total} (OK: {self.checks_success}, ERR: {self.checks_failed}) | "
            f"Слотов найдено: {self.slots_found_count} | "
            f"Логинов: {self.logins_total} (fail: {self.logins_failed}) | "
            f"CAPTCHA: {self.captchas_solved}/{self.captchas_solved + self.captchas_failed}"
        )
