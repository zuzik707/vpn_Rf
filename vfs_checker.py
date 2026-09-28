import logging
import random
import time

import requests

from config import Config

logger = logging.getLogger(__name__)


class VFSChecker:
    """
    Проверяет доступность слотов на VFS Global.

    VFS Global использует REST API за своим фронтендом.
    Основной флоу: авторизация -> получение токена -> запрос слотов.
    """

    BASE_API = "https://lift-api.vfsglobal.com/appointment"
    AUTH_API = "https://lift-api.vfsglobal.com/login"

    def __init__(self):
        self.session = requests.Session()
        self.token: str | None = None
        self.token_ts: float = 0
        self._setup_session()

    def _setup_session(self) -> None:
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Origin": Config.VFS_CENTER_URL,
            "Referer": f"{Config.VFS_CENTER_URL}/",
            "User-Agent": random.choice(Config.USER_AGENTS),
        })

    def _rotate_user_agent(self) -> None:
        self.session.headers["User-Agent"] = random.choice(Config.USER_AGENTS)

    def authenticate(self) -> bool:
        if not Config.VFS_EMAIL or not Config.VFS_PASSWORD:
            logger.error("VFS credentials не заданы в .env")
            return False

        payload = {
            "username": Config.VFS_EMAIL,
            "password": Config.VFS_PASSWORD,
            "countryCode": Config.VFS_SOURCE_COUNTRY,
            "missionCode": Config.VFS_DESTINATION_COUNTRY,
        }

        try:
            self._rotate_user_agent()
            resp = self.session.post(self.AUTH_API, json=payload, timeout=30)

            if resp.status_code == 401:
                logger.error("Неверные логин/пароль VFS")
                return False

            if resp.status_code == 429:
                logger.warning("Rate limit от VFS — ждём подольше")
                return False

            resp.raise_for_status()
            data = resp.json()
            self.token = data.get("accessToken") or data.get("token")
            if not self.token:
                logger.error("Токен не получен. Ответ: %s", data)
                return False

            self.token_ts = time.time()
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            logger.info("Авторизация VFS успешна")
            return True

        except requests.RequestException as e:
            logger.error("Ошибка авторизации VFS: %s", e)
            return False

    def _token_alive(self) -> bool:
        if not self.token:
            return False
        return (time.time() - self.token_ts) < 1500

    def _ensure_auth(self) -> bool:
        if self._token_alive():
            return True
        logger.info("Токен истёк или отсутствует — переавторизация")
        return self.authenticate()

    def check_slots(self) -> list[dict]:
        if not self._ensure_auth():
            return []

        params = {
            "countryCode": Config.VFS_SOURCE_COUNTRY,
            "missionCode": Config.VFS_DESTINATION_COUNTRY,
            "centerCode": Config.VFS_CITY,
            "loginUser": Config.VFS_EMAIL,
            "visaCategoryCode": Config.VFS_VISA_CATEGORY,
            "payCode": "",
        }

        try:
            self._rotate_user_agent()
            # Рандомная задержка перед запросом чтобы быть менее предсказуемым
            time.sleep(random.uniform(1.0, 3.0))

            resp = self.session.get(
                f"{self.BASE_API}/CheckIsSlotAvailable",
                params=params,
                timeout=30,
            )

            if resp.status_code == 401:
                logger.info("Токен протух — переавторизация")
                self.token = None
                if not self.authenticate():
                    return []
                resp = self.session.get(
                    f"{self.BASE_API}/CheckIsSlotAvailable",
                    params=params,
                    timeout=30,
                )

            if resp.status_code == 429:
                logger.warning("Rate limit — увеличиваю паузу")
                return []

            resp.raise_for_status()
            data = resp.json()
            return self._parse_slots(data)

        except requests.RequestException as e:
            logger.error("Ошибка проверки слотов: %s", e)
            return []

    def _parse_slots(self, data: dict | list) -> list[dict]:
        slots = []

        if isinstance(data, dict):
            is_available = data.get("IsSlotAvailable", data.get("isSlotAvailable", False))
            if not is_available:
                return []

            earliest = data.get("EarliestDate") or data.get("earliestDate")
            if earliest:
                slots.append({"date": earliest, "time": ""})

        if isinstance(data, list):
            for item in data:
                date_val = item.get("date") or item.get("Date") or item.get("VisaDate")
                time_val = item.get("time") or item.get("Time") or ""
                if date_val:
                    slots.append({"date": date_val, "time": time_val})

        return slots

    def get_booking_url(self) -> str:
        src = Config.VFS_SOURCE_COUNTRY
        dst = Config.VFS_DESTINATION_COUNTRY
        return f"{Config.VFS_CENTER_URL}/{src}/en/{dst}/book-an-appointment"

    def get_available_dates(self) -> list[dict]:
        """Альтернативный эндпоинт — запрос конкретных дат."""
        if not self._ensure_auth():
            return []

        params = {
            "countryCode": Config.VFS_SOURCE_COUNTRY,
            "missionCode": Config.VFS_DESTINATION_COUNTRY,
            "centerCode": Config.VFS_CITY,
            "loginUser": Config.VFS_EMAIL,
            "visaCategoryCode": Config.VFS_VISA_CATEGORY,
        }

        try:
            self._rotate_user_agent()
            time.sleep(random.uniform(1.0, 3.0))

            resp = self.session.get(
                f"{self.BASE_API}/slots",
                params=params,
                timeout=30,
            )

            if resp.status_code == 401:
                self.token = None
                return []

            if resp.status_code == 429:
                logger.warning("Rate limit на slots endpoint")
                return []

            resp.raise_for_status()
            data = resp.json()

            slots = []
            if isinstance(data, list):
                for entry in data:
                    date_val = entry.get("date") or entry.get("Date")
                    times = entry.get("availableTimes") or entry.get("times") or []
                    if date_val:
                        if times:
                            for t in times:
                                slots.append({"date": date_val, "time": t})
                        else:
                            slots.append({"date": date_val, "time": ""})
            return slots

        except requests.RequestException as e:
            logger.error("Ошибка получения дат: %s", e)
            return []
