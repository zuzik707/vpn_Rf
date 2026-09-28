"""
AstroProxy API интеграция.

Управление прокси: создание портов, ротация IP, проверка баланса,
sticky sessions, автовыбор страны (UZ > KZ > любой residential).
"""

import logging
import os
import time

import requests

from config import Config

logger = logging.getLogger(__name__)

API_BASE = "https://astroproxy.com/api/v1"


class ProxyManager:
    def __init__(self, api_token: str = ""):
        self.token = api_token or os.getenv("ASTROPROXY_TOKEN", "")
        self.port_id: int | None = None
        self.proxy_url: str = ""
        self.last_rotate: float = 0
        self._port_info: dict = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    # ── API helpers ───────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        p = {"token": self.token}
        if params:
            p.update(params)
        r = requests.get(f"{API_BASE}/{path}", params=p, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            raise RuntimeError(f"AstroProxy: {data.get('message', 'unknown error')}")
        return data

    def _post(self, path: str, body: dict = None) -> dict:
        r = requests.post(
            f"{API_BASE}/{path}",
            params={"token": self.token},
            data=body or {},
            timeout=15,
        )
        data = r.json()
        if data.get("status") == "error":
            raise RuntimeError(f"AstroProxy: {data.get('message', 'unknown error')}")
        return data

    def _patch(self, path: str, body: dict = None) -> dict:
        r = requests.patch(
            f"{API_BASE}/{path}",
            params={"token": self.token},
            data=body or {},
            timeout=15,
        )
        data = r.json()
        if data.get("status") == "error":
            raise RuntimeError(f"AstroProxy: {data.get('message', 'unknown error')}")
        return data

    # ── Public API ────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._get("balance")
        balance = data.get("data", {}).get("balance", 0)
        logger.info("AstroProxy баланс: $%.2f", balance)
        return float(balance)

    def list_countries(self, network: str = "Residential") -> list[str]:
        data = self._get("countries", {"network": network})
        return [c["name"] for c in data.get("data", [])]

    def list_ports(self, status: str = "active") -> list[dict]:
        data = self._get("ports", {"status": status})
        return data.get("data", {}).get("ports", [])

    def create_port(
        self,
        country: str = "UZ",
        network: str = "Residential",
        volume_mb: int = 1024,
        rotation_by: str = "link",
        name: str = "vfs-monitor",
    ) -> dict:
        """
        Создаёт новый порт.
        rotation_by="link" → sticky session, IP меняется только через API.
        """
        body = {
            "country": country,
            "network": network,
            "volume": volume_mb,
            "is_unlimited": False,
            "rotation_by": rotation_by,
            "name": name,
        }
        data = self._post("ports", body)
        ports = data.get("data", [])
        if not ports:
            raise RuntimeError("Порт не создан")
        port = ports[0]
        self._port_info = port
        self.port_id = port["id"]
        self.proxy_url = self._build_proxy_url(port)
        logger.info("Порт создан: id=%s, proxy=%s", self.port_id, self._safe_proxy_url())
        return port

    def rotate_ip(self) -> str | None:
        """Принудительная ротация IP. Минимум 30 сек между ротациями."""
        if not self.port_id:
            logger.warning("Нет активного порта для ротации")
            return None
        if time.time() - self.last_rotate < 30:
            logger.debug("Ротация: cooldown 30с")
            return None
        try:
            data = self._get(f"ports/{self.port_id}/newip")
            new_ip = data.get("data", {}).get("ip")
            self.last_rotate = time.time()
            logger.info("IP ротация: %s", new_ip)
            return new_ip
        except RuntimeError as e:
            logger.warning("Ротация не удалась: %s", e)
            return None

    def set_sticky_session(self, minutes: int = 30) -> None:
        """Sticky session: один IP на N минут."""
        if not self.port_id:
            return
        self._patch(f"ports/{self.port_id}", {
            "rotation_by": "time",
            "rotation_time_type": "minutes",
            "rotation_time": minutes,
        })
        logger.info("Sticky session: %d мин", minutes)

    def set_manual_rotation(self) -> None:
        """Ручная ротация — IP не меняется пока не вызовешь rotate_ip()."""
        if not self.port_id:
            return
        self._patch(f"ports/{self.port_id}", {"rotation_by": "link"})
        logger.info("Ручная ротация включена")

    def get_traffic_left(self) -> float:
        """Сколько МБ осталось на порту."""
        if not self.port_id:
            return 0
        ports = self.list_ports()
        for p in ports:
            if p.get("id") == self.port_id:
                return p.get("traffic", {}).get("left_mb", 0)
        return 0

    def setup_best_proxy(self, preferred_countries: list[str] = None) -> str:
        """
        Автоматически создаёт порт с лучшей доступной локацией.
        Приоритет: UZ > KZ > LT > LV > любая.
        Возвращает proxy URL.
        """
        if preferred_countries is None:
            preferred_countries = ["UZ", "KZ", "LT", "LV", "DE", "PL"]

        # Проверяем существующие порты
        existing = self.list_ports()
        for p in existing:
            if p.get("name") == "vfs-monitor" and p.get("network") == "Residential":
                self._port_info = p
                self.port_id = p["id"]
                self.proxy_url = self._build_proxy_url(p)
                traffic = p.get("traffic", {}).get("left_mb", 0)
                logger.info("Используем существующий порт: id=%s (%s), трафик: %.0f MB",
                            self.port_id, p.get("country"), traffic)
                return self.proxy_url

        # Ищем лучшую страну
        available = self.list_countries("Residential")
        chosen = None
        for country in preferred_countries:
            if country in available:
                chosen = country
                break
        if not chosen:
            chosen = available[0] if available else "US"

        logger.info("Создаю порт: страна=%s", chosen)
        self.create_port(country=chosen, rotation_by="link")
        return self.proxy_url

    # ── Internal ──────────────────────────────────────────────

    @staticmethod
    def _build_proxy_url(port: dict) -> str:
        node = port.get("node", {})
        access = port.get("access", {})
        ports = port.get("ports", {})
        socks_port = ports.get("socks")
        http_port = ports.get("http")
        host = node.get("ip") or node.get("address", "")
        login = access.get("login", "")
        password = access.get("password", "")

        if socks_port:
            return f"socks5://{login}:{password}@{host}:{socks_port}"
        return f"http://{login}:{password}@{host}:{http_port}"

    def _safe_proxy_url(self) -> str:
        """Proxy URL с замаскированным паролем для логов."""
        if "@" in self.proxy_url:
            parts = self.proxy_url.split("@")
            return f"***@{parts[-1]}"
        return self.proxy_url

    def stats_text(self) -> str:
        if not self.port_id:
            return "Proxy: не настроен"
        country = self._port_info.get("country", "?")
        try:
            traffic = self.get_traffic_left()
            balance = self.get_balance()
            return (
                f"Proxy: {country} | Трафик: {traffic:.0f} MB | "
                f"Баланс: ${balance:.2f}"
            )
        except Exception as e:
            logger.debug("stats_text API error: %s", e)
            return f"Proxy: {country} | stats unavailable"
