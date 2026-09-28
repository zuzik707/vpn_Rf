"""
Circuit breaker для captcha-бюджета.

Предотвращает:
- Утечку денег при серии ошибок
- Случайный перерасход при зацикливании
- Бан аккаунта из-за спама captcha-запросов
"""

import logging
import time

from config import Config
from notifier import notify_error, notify_status

logger = logging.getLogger(__name__)


class BudgetGuard:
    CONSECUTIVE_FAIL_LIMIT = 5
    CONSECUTIVE_FAIL_HARD = 10
    HOURLY_SPEND_LIMIT = 2.0
    DAILY_SPEND_LIMIT = 15.0
    MIN_BALANCE_WARN = 1.0

    PAUSE_SOFT = 30 * 60       # 30 min
    PAUSE_HARD = 60 * 60       # 1 hour
    PAUSE_DAILY = 4 * 60 * 60  # 4 hours

    def __init__(self):
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self._solves: list[tuple[float, float]] = []  # (timestamp, cost)
        self.paused_until: float = 0
        self.total_solves = 0
        self.total_failures = 0
        self.total_spent = 0.0
        self._started = time.time()

    def _avg_cost(self) -> float:
        return 0.003  # ~$0.003 per Turnstile solve on 2Captcha

    def record_success(self, cost: float | None = None) -> None:
        c = cost if cost is not None else self._avg_cost()
        now = time.time()
        self._solves.append((now, c))
        self.total_solves += 1
        self.total_spent += c
        self.consecutive_failures = 0
        self.consecutive_successes += 1

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.total_failures += 1

        if self.consecutive_failures >= self.CONSECUTIVE_FAIL_HARD:
            self._pause(self.PAUSE_HARD, f"HARD STOP: {self.consecutive_failures} провалов подряд")
        elif self.consecutive_failures >= self.CONSECUTIVE_FAIL_LIMIT:
            self._pause(self.PAUSE_SOFT, f"{self.consecutive_failures} провалов подряд — пауза 30 мин")

    def can_solve(self) -> bool:
        now = time.time()
        if now < self.paused_until:
            remaining = int((self.paused_until - now) / 60)
            logger.info("Budget guard: пауза ещё %d мин", remaining)
            return False

        hourly = self._spent_since(now - 3600)
        if hourly >= self.HOURLY_SPEND_LIMIT:
            self._pause(self.PAUSE_HARD, f"Часовой лимит: ${hourly:.2f} >= ${self.HOURLY_SPEND_LIMIT}")
            return False

        daily = self._spent_since(now - 86400)
        if daily >= self.DAILY_SPEND_LIMIT:
            self._pause(self.PAUSE_DAILY, f"Дневной лимит: ${daily:.2f} >= ${self.DAILY_SPEND_LIMIT}")
            return False

        return True

    def check_balance(self, balance: float) -> None:
        if 0 <= balance < self.MIN_BALANCE_WARN:
            notify_status(f"Баланс captcha низкий: ${balance:.2f}")

    def _spent_since(self, since: float) -> float:
        return sum(c for t, c in self._solves if t >= since)

    def _pause(self, seconds: int, reason: str) -> None:
        self.paused_until = time.time() + seconds
        logger.warning("Budget guard PAUSE %d мин: %s", seconds // 60, reason)
        notify_error(f"Captcha пауза ({seconds // 60} мин): {reason}")

    def stats_text(self) -> str:
        uptime_h = (time.time() - self._started) / 3600
        hourly = self._spent_since(time.time() - 3600)
        daily = self._spent_since(time.time() - 86400)
        rate = self.total_solves / max(self.total_solves + self.total_failures, 1) * 100
        return (
            f"Captcha: {self.total_solves} OK / {self.total_failures} fail "
            f"({rate:.0f}%) | "
            f"${self.total_spent:.3f} total | "
            f"${hourly:.3f}/hr ${daily:.3f}/day | "
            f"{uptime_h:.1f}h uptime"
        )
