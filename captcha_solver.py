"""
Captcha solver с fallback: 2Captcha (primary) → CapSolver (secondary).

При 2 провалах подряд на primary — автопереключение на secondary на 30 мин.
Budget guard контролирует расходы и ставит паузу при перерасходе.
"""

import logging
import time

import requests
from twocaptcha import TwoCaptcha

from budget_guard import BudgetGuard
from config import Config

logger = logging.getLogger(__name__)


class CaptchaSolver:
    FALLBACK_COOLDOWN = 30 * 60  # 30 мин на secondary перед возвратом

    def __init__(self, budget: BudgetGuard | None = None):
        if not Config.CAPTCHA_API_KEY:
            raise ValueError("CAPTCHA_API_KEY не задан в .env")
        self.primary = TwoCaptcha(Config.CAPTCHA_API_KEY)
        self.has_secondary = bool(Config.CAPSOLVER_API_KEY)
        self.budget = budget or BudgetGuard()
        self._primary_fails = 0
        self._on_secondary_until = 0.0

    @property
    def _use_secondary(self) -> bool:
        return self.has_secondary and time.time() < self._on_secondary_until

    def solve_turnstile(self, sitekey: str, page_url: str) -> str | None:
        if not self.budget.can_solve():
            logger.warning("Budget guard: captcha заблокирована")
            return None

        if self._use_secondary:
            token = self._solve_capsolver(sitekey, page_url)
            if token:
                self.budget.record_success()
                return token
            # secondary тоже упал — пробуем primary
            logger.warning("CapSolver fail, пробую 2Captcha...")

        token = self._solve_2captcha(sitekey, page_url)
        if token:
            self._primary_fails = 0
            self.budget.record_success()
            return token

        self._primary_fails += 1
        self.budget.record_failure()

        if self._primary_fails >= 2 and self.has_secondary:
            logger.info("2 провала 2Captcha → переключаюсь на CapSolver на 30 мин")
            self._on_secondary_until = time.time() + self.FALLBACK_COOLDOWN
            self._primary_fails = 0
            # Сразу пробуем secondary
            token = self._solve_capsolver(sitekey, page_url)
            if token:
                self.budget.record_success()
                return token
            self.budget.record_failure()

        return None

    def _solve_2captcha(self, sitekey: str, page_url: str) -> str | None:
        logger.info("2Captcha: Turnstile (sitekey=%s...)", sitekey[:16])
        try:
            result = self.primary.turnstile(sitekey=sitekey, url=page_url)
            token = result.get("code") if isinstance(result, dict) else str(result)
            logger.info("2Captcha OK")
            return token
        except Exception as e:
            logger.error("2Captcha ошибка: %s", e)
            return None

    def _solve_capsolver(self, sitekey: str, page_url: str) -> str | None:
        logger.info("CapSolver: Turnstile (sitekey=%s...)", sitekey[:16])
        try:
            # CapSolver API — createTask + getTaskResult
            resp = requests.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": Config.CAPSOLVER_API_KEY,
                    "task": {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                },
                timeout=15,
            )
            data = resp.json()
            task_id = data.get("taskId")
            if not task_id:
                logger.error("CapSolver createTask: %s", data.get("errorDescription", data))
                return None

            # Poll result (max 120s)
            for _ in range(40):
                time.sleep(3)
                resp = requests.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": Config.CAPSOLVER_API_KEY, "taskId": task_id},
                    timeout=15,
                )
                result = resp.json()
                status = result.get("status")
                if status == "ready":
                    token = result.get("solution", {}).get("token")
                    if token:
                        logger.info("CapSolver OK")
                        return token
                elif status == "failed":
                    logger.error("CapSolver failed: %s", result.get("errorDescription"))
                    return None
            logger.error("CapSolver timeout")
            return None
        except Exception as e:
            logger.error("CapSolver ошибка: %s", e)
            return None

    def get_balance(self) -> float:
        try:
            bal = float(self.primary.balance())
            self.budget.check_balance(bal)
            return bal
        except Exception:
            return -1.0
