"""
Клиент mail.tm — бесплатная временная почта с API.
Создаёт email, получает письма, извлекает ссылки активации.
"""

import logging
import random
import re
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mail.tm"

_FIRST_NAMES = [
    "anna", "maria", "elena", "olga", "nina", "diana", "alina", "daria",
    "ivan", "alex", "dmitry", "artem", "nikita", "sergey", "pavel", "denis",
    "kamila", "aziza", "nodira", "dilnoza", "jasur", "bobur", "sardor", "timur",
    "kate", "julia", "lena", "max", "daniel", "mark", "lucas", "emma",
]
_LAST_NAMES = [
    "kim", "lee", "park", "chen", "wang", "khan", "ali", "ahmed",
    "smith", "jones", "miller", "davis", "wilson", "taylor", "moore", "clark",
    "karimov", "aliev", "umarov", "nazarov", "rashidov", "sultanov",
]


def _random_human_prefix() -> str:
    first = random.choice(_FIRST_NAMES)
    last = random.choice(_LAST_NAMES)
    sep = random.choice([".", "_", ""])
    num = random.randint(1, 99)
    style = random.randint(0, 3)
    if style == 0:
        return f"{first}{sep}{last}{num}"
    elif style == 1:
        return f"{last}{sep}{first}{num}"
    elif style == 2:
        return f"{first}{num}{sep}{last}"
    else:
        return f"{first}{sep}{last}"


class TempMailClient:
    def __init__(self):
        self.address: str = ""
        self.password: str = ""
        self.token: str = ""
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"

    def create_account(self, prefix: str = "") -> str:
        """Создаёт временный email. Возвращает адрес."""
        # Получаем доступный домен
        resp = self._session.get(f"{BASE_URL}/domains", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # API может вернуть список напрямую или объект с hydra:member
        if isinstance(data, list):
            domains = data
        else:
            domains = data.get("hydra:member", [])
        active = [d for d in domains if isinstance(d, dict) and d.get("isActive")]
        if not active:
            raise RuntimeError("Нет доступных доменов на mail.tm")

        domain = random.choice(active)["domain"]

        if not prefix:
            prefix = _random_human_prefix()
        # Убираем спецсимволы из prefix
        prefix = re.sub(r'[^a-z0-9._-]', '', prefix.lower())

        self.address = f"{prefix}@{domain}"
        self.password = f"TmpPass_{int(time.time())}!"

        # Создаём аккаунт (до 5 попыток с backoff на 429)
        created = False
        for attempt in range(5):
            resp = self._session.post(
                f"{BASE_URL}/accounts",
                json={"address": self.address, "password": self.password},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                created = True
                break
            if resp.status_code == 422:
                prefix = _random_human_prefix()
                self.address = f"{prefix}@{domain}"
                continue
            if resp.status_code == 429:
                wait = (attempt + 1) * 15
                logger.warning("mail.tm 429 — waiting %ds (attempt %d/5)", wait, attempt + 1)
                time.sleep(wait)
                prefix = _random_human_prefix()
                self.address = f"{prefix}@{domain}"
                self.password = f"TmpPass_{int(time.time())}!"
                continue
            resp.raise_for_status()

        if not created:
            raise RuntimeError(f"Не удалось создать аккаунт за 5 попыток (последний: {self.address})")

        # Получаем токен (retry на 401, таймаут, сетевые ошибки)
        token_resp = None
        for t_attempt in range(5):
            try:
                token_resp = self._session.post(
                    f"{BASE_URL}/token",
                    json={"address": self.address, "password": self.password},
                    timeout=15,
                )
            except (requests.ConnectionError, requests.Timeout, requests.ReadTimeout) as e:
                wait = (t_attempt + 1) * 3
                logger.warning("mail.tm token network error: %s — retry in %ds (%d/5)",
                               e, wait, t_attempt + 1)
                time.sleep(wait)
                continue
            if token_resp.status_code == 200:
                break
            if token_resp.status_code == 401:
                logger.debug("Token 401 — retrying in 3s... (%d/5)", t_attempt + 1)
                time.sleep(3)
                continue
            if token_resp.status_code == 429:
                wait = (t_attempt + 1) * 10
                logger.warning("mail.tm token 429 — waiting %ds (%d/5)", wait, t_attempt + 1)
                time.sleep(wait)
                continue
            logger.warning("mail.tm token unexpected %d", token_resp.status_code)
            time.sleep(3)

        if not token_resp or token_resp.status_code != 200:
            raise RuntimeError(
                f"mail.tm token failed ({token_resp.status_code if token_resp else '?'}): "
                f"{token_resp.text[:200] if token_resp else 'no response'}")

        self.token = token_resp.json()["token"]
        self._session.headers["Authorization"] = f"Bearer {self.token}"

        logger.info("Temp email created: %s", self.address)
        return self.address

    def wait_for_email(self, from_contains: str = "vfsglobal",
                       timeout_sec: int = 300, poll_sec: int = 5) -> dict | None:
        """Ждёт письмо от указанного отправителя. Возвращает сообщение или None."""
        deadline = time.time() + timeout_sec
        logger.info("Ожидаю письмо от *%s* на %s (до %dс)...",
                     from_contains, self.address, timeout_sec)

        while time.time() < deadline:
            try:
                resp = self._session.get(f"{BASE_URL}/messages", timeout=10)
                if resp.ok:
                    data = resp.json()
                    messages = data if isinstance(data, list) else data.get("hydra:member", [])
                    for msg in messages:
                        sender = msg.get("from", {}).get("address", "").lower()
                        if from_contains.lower() in sender:
                            # Получаем полное тело
                            msg_id = msg["id"]
                            full = self._session.get(
                                f"{BASE_URL}/messages/{msg_id}", timeout=10)
                            if full.ok:
                                logger.info("Письмо получено: %s", msg.get("subject"))
                                return full.json()
            except Exception as e:
                logger.debug("Mail poll error: %s", e)

            time.sleep(poll_sec)

        logger.warning("Письмо не получено за %dс", timeout_sec)
        return None

    def extract_activation_link(self, message: dict) -> str | None:
        """Извлекает ссылку активации из письма VFS."""
        # Проверяем HTML тело
        html_parts = message.get("html", [])
        html_body = "".join(html_parts) if isinstance(html_parts, list) else str(html_parts)

        # Ищем ссылку активации VFS
        patterns = [
            r'https?://visa\.vfsglobal\.com[^\s"\'<>]+activat[^\s"\'<>]+',
            r'https?://[^\s"\'<>]*vfsglobal[^\s"\'<>]*activat[^\s"\'<>]+',
            r'href=["\']?(https?://[^\s"\'<>]*activat[^\s"\'<>]+)',
        ]
        for pat in patterns:
            match = re.search(pat, html_body, re.IGNORECASE)
            if match:
                link = match.group(1) if match.lastindex else match.group(0)
                link = link.rstrip('"\'>')
                logger.info("Activation link found: %s", link[:80])
                return link

        # Fallback — проверяем текстовое тело
        text_body = message.get("text", "")
        for pat in patterns:
            match = re.search(pat, text_body, re.IGNORECASE)
            if match:
                link = match.group(1) if match.lastindex else match.group(0)
                return link.rstrip('"\'>')

        logger.warning("Activation link not found in email")
        return None

    def get_all_links(self, message: dict) -> list[str]:
        """Извлекает все ссылки из письма."""
        html_parts = message.get("html", [])
        html_body = "".join(html_parts) if isinstance(html_parts, list) else str(html_parts)
        text_body = message.get("text", "")
        combined = html_body + " " + text_body
        links = re.findall(r'https?://[^\s"\'<>]+', combined)
        return list(set(links))
