"""
Перехват сетевых запросов через CDP.
Ловим ответы от lift-api.vfsglobal.com прямо из браузера —
это надёжнее чем парсить DOM, потому что мы видим сырые данные API.
"""

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SlotData:
    available: bool = False
    earliest_date: str = ""
    dates: list[str] = field(default_factory=list)
    raw_response: dict | None = None
    center: str = ""


class NetworkInterceptor:
    """Собирает ответы API из сетевого трафика браузера через CDP."""

    INTERESTING_URLS = [
        "appointment",
        "slot",
        "CheckIsSlotAvailable",
        "centerwithearliestslot",
        "GetAppointmentScheduleDate",
    ]

    def __init__(self):
        self.captured_responses: list[dict] = []
        self.last_slot_data: SlotData | None = None
        self._listener_attached = False

    async def attach_to_page(self, page) -> None:
        if self._listener_attached:
            return

        # Включаем перехват сети через CDP
        await page.send(uc_cdp_cmd("Network.enable"))

        # Слушаем ответы
        page.add_handler(
            "Network.responseReceived",
            self._on_response_received,
        )
        self._listener_attached = True
        logger.info("Network interceptor подключен")

    async def _on_response_received(self, event: dict) -> None:
        try:
            response = event.get("params", {}).get("response", {})
            url = response.get("url", "")

            if not any(marker in url.lower() for marker in self.INTERESTING_URLS):
                return

            request_id = event.get("params", {}).get("requestId")
            if not request_id:
                return

            self.captured_responses.append({
                "url": url,
                "status": response.get("status"),
                "request_id": request_id,
            })

            logger.info("Перехвачен API ответ: %s (status=%s)", url[:100], response.get("status"))
        except Exception as e:
            logger.debug("Ошибка обработки response event: %s", e)

    async def get_response_body(self, page, request_id: str) -> dict | None:
        try:
            result = await page.send(
                uc_cdp_cmd("Network.getResponseBody"),
                requestId=request_id,
            )
            body = result.get("body", "")
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            return json.loads(body)
        except Exception as e:
            logger.debug("Не удалось получить body: %s", e)
            return None

    async def extract_slot_data(self, page) -> SlotData:
        slot_data = SlotData()

        for resp in reversed(self.captured_responses):
            body = await self.get_response_body(page, resp["request_id"])
            if not body:
                continue

            slot_data.raw_response = body

            # Парсим разные форматы ответа VFS
            if isinstance(body, dict):
                is_available = (
                    body.get("IsSlotAvailable")
                    or body.get("isSlotAvailable")
                    or body.get("slotAvailable")
                    or body.get("available")
                )
                if is_available:
                    slot_data.available = True

                earliest = (
                    body.get("EarliestDate")
                    or body.get("earliestDate")
                    or body.get("earliestSlotDate")
                )
                if earliest:
                    slot_data.available = True
                    slot_data.earliest_date = str(earliest)

                center = body.get("centerName") or body.get("center") or ""
                if center:
                    slot_data.center = center

            if isinstance(body, list):
                for item in body:
                    date_val = (
                        item.get("date")
                        or item.get("Date")
                        or item.get("VisaDate")
                        or item.get("appointmentDate")
                    )
                    if date_val:
                        slot_data.dates.append(str(date_val))
                        slot_data.available = True

            if slot_data.available:
                break

        self.captured_responses.clear()
        self.last_slot_data = slot_data
        return slot_data

    def clear(self) -> None:
        self.captured_responses.clear()
        self.last_slot_data = None


def uc_cdp_cmd(method: str):
    """Хелпер для формирования CDP команды в формате nodriver."""
    class CDPCmd:
        def __init__(self, method):
            self.method = method
        def __str__(self):
            return self.method
    return CDPCmd(method)
