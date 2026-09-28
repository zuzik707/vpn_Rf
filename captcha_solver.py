import logging
from twocaptcha import TwoCaptcha
from config import Config

logger = logging.getLogger(__name__)


class CaptchaSolver:
    def __init__(self):
        if not Config.CAPTCHA_API_KEY:
            raise ValueError("CAPTCHA_API_KEY не задан в .env")
        self.solver = TwoCaptcha(Config.CAPTCHA_API_KEY)

    def solve_turnstile(self, sitekey: str, page_url: str) -> str | None:
        logger.info("Отправляю Turnstile в 2Captcha (sitekey=%s)...", sitekey[:20])
        try:
            result = self.solver.turnstile(sitekey=sitekey, url=page_url)
            token = result.get("code") if isinstance(result, dict) else str(result)
            logger.info("Turnstile решён!")
            return token
        except Exception as e:
            logger.error("2Captcha ошибка: %s", e)
            return None

    def get_balance(self) -> float:
        try:
            return float(self.solver.balance())
        except Exception:
            return -1.0
