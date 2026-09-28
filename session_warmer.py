"""
Session warming — имитация реального пользователя перед логином.

Прямой заход на /login — красный флаг для Cloudflare.
Реальный путь: homepage → выбор страны → выбор посольства → login.

vfsglobal.com → /uzb/en/lva → /uzb/en/lva/login
"""

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# Этапы warming-маршрута для UZB → LVA
WARM_ROUTE = [
    {
        "url": "https://visa.vfsglobal.com/uzb/en",
        "wait": (2.0, 5.0),
        "label": "homepage-uzb",
    },
    {
        "url": "https://visa.vfsglobal.com/uzb/en/lva",
        "wait": (2.0, 4.0),
        "label": "country-lva",
    },
]

LOGIN_URL = "https://visa.vfsglobal.com/uzb/en/lva/login"


class SessionWarmer:
    def __init__(self, page, human_clicker=None):
        self.page = page
        self.hc = human_clicker

    async def warm(self) -> bool:
        """Пройти warming-маршрут до /login. True = дошли до логина."""
        try:
            for step in WARM_ROUTE:
                logger.info("Warming: %s → %s", step["label"], step["url"])
                await self.page.get(step["url"])
                await asyncio.sleep(random.uniform(*step["wait"]))

                if self.hc:
                    await self.hc.idle_drift(random.uniform(0.5, 1.5))
                    if random.random() < 0.5:
                        await self.hc.micro_scroll()

                await self._random_interaction()

            # Финальный переход на login
            logger.info("Warming: переход на login")
            await self.page.get(LOGIN_URL)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            if self.hc:
                await self.hc.idle_drift(random.uniform(0.3, 0.8))

            logger.info("Warming: маршрут завершён")
            return True

        except Exception as e:
            logger.warning("Warming failed: %s — прямой заход на /login", e)
            try:
                await self.page.get(LOGIN_URL)
                await asyncio.sleep(2)
                return True
            except Exception as e2:
                logger.error("Warming fallback failed: %s", e2)
                return False

    async def _random_interaction(self) -> None:
        """Случайные действия на странице — как реальный человек."""
        actions = random.choices(
            ["scroll", "hover_link", "idle", "nothing"],
            weights=[30, 20, 20, 30],
            k=1,
        )[0]

        if actions == "scroll" and self.hc:
            await self.hc.micro_scroll()

        elif actions == "hover_link":
            try:
                links = await self.page.select_all("a[href]")
                if links:
                    random.choice(links[:10])
                    if self.hc:
                        await self.hc._move_to(
                            random.uniform(200, 800),
                            random.uniform(200, 500),
                        )
                    await asyncio.sleep(random.uniform(0.3, 0.8))
            except Exception:
                pass

        elif actions == "idle":
            await asyncio.sleep(random.uniform(1.0, 3.0))
