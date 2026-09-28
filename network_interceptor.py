"""
Перехват сетевых запросов через CDP (nodriver API).
Ловим ответы от VFS API прямо из браузера —
надёжнее чем парсить DOM, потому что видим сырые данные.

nodriver CDP: page.send(cdp.network.enable()) — не самопальный класс.
"""

import json
import logging
from dataclasses import dataclass, field

import nodriver.cdp.network as net

logger = logging.getLogger(__name__)


@dataclass
class SlotData:
    available: bool = False
    earliest_date: str = ""
    dates: list[str] = field(default_factory=list)
    raw_response: dict | None = None
    center: str = ""
    source_url: str = ""


class NetworkInterceptor:
    """Собирает ответы VFS API из сетевого трафика через CDP."""

    INTERESTING_PATTERNS = [
        "appointment",
        "slot",
        "checkisslotavailable",
        "centerwithearliestslot",
        "getappointmentscheduledate",
        "calendar",
        "availability",
    ]

    def __init__(self):
        self.captured: list[dict] = []
        self.last_slot_data: SlotData | None = None
        self._attached = False
        self._page = None

    async def attach(self, page) -> None:
        if self._attached:
            return
        self._page = page
        await page.send(net.enable())
        page.add_handler(net.ResponseReceived, self._on_response)
        self._attached = True
        logger.info("Network interceptor подключен (CDP)")

    async def detach(self) -> None:
        if self._page and self._attached:
            try:
                await self._page.send(net.disable())
            except Exception:
                pass
        self._attached = False
        self._page = None

    def _on_response(self, event: net.ResponseReceived) -> None:
        try:
            url = event.response.url.lower()
            if not any(p in url for p in self.INTERESTING_PATTERNS):
                return

            self.captured.append({
                "url": event.response.url,
                "status": event.response.status,
                "request_id": event.request_id,
            })
            logger.info("API перехвачен: %s (status=%d)",
                        event.response.url[:120], event.response.status)
        except Exception as e:
            logger.debug("Response event error: %s", e)

    async def get_body(self, request_id) -> dict | None:
        if not self._page:
            return None
        try:
            result = await self._page.send(net.get_response_body(request_id))
            body_str = result[0]  # (body, base64_encoded)
            if result[1]:  # base64
                import base64
                body_str = base64.b64decode(body_str).decode("utf-8", errors="replace")
            return json.loads(body_str)
        except Exception as e:
            logger.debug("Body fetch failed: %s", e)
            return None

    async def check_api_slots(self) -> SlotData:
        """Анализирует перехваченные API-ответы на наличие слотов."""
        slot_data = SlotData()

        for resp in reversed(self.captured):
            body = await self.get_body(resp["request_id"])
            if not body:
                continue

            slot_data.raw_response = body
            slot_data.source_url = resp["url"]

            if isinstance(body, dict):
                is_avail = (
                    body.get("IsSlotAvailable")
                    or body.get("isSlotAvailable")
                    or body.get("slotAvailable")
                    or body.get("available")
                )
                if is_avail:
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
                    if not isinstance(item, dict):
                        continue
                    date_val = (
                        item.get("date") or item.get("Date")
                        or item.get("VisaDate") or item.get("appointmentDate")
                    )
                    if date_val:
                        slot_data.dates.append(str(date_val))
                        slot_data.available = True

            if slot_data.available:
                break

        self.captured.clear()
        self.last_slot_data = slot_data
        return slot_data

    def clear(self) -> None:
        self.captured.clear()
        self.last_slot_data = None

    @property
    def has_data(self) -> bool:
        return len(self.captured) > 0
