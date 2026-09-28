"""
Human-like mouse/keyboard interaction через CDP Input events.

Вместо голого element.click() — реалистичное поведение:
- Bezier-кривая движения курсора от текущей позиции к цели
- Hover 100-300ms перед кликом
- Dwell time на dropdown'ах (человек читает список)
- Микро-скроллы перед действиями
- Idle drift — лёгкое движение курсора на "пустых" страницах
"""

import asyncio
import random

import nodriver.cdp.input_ as inp


class HumanClicker:
    def __init__(self, page):
        self.page = page
        self.last_x: float = random.uniform(300, 700)
        self.last_y: float = random.uniform(200, 400)

    async def click(self, element, dwell: float = 0) -> None:
        """Человекоподобный клик: move → hover → mousedown → mouseup."""
        box = await self._get_box(element)
        if not box:
            await element.click()
            return

        tx = box["x"] + random.uniform(box["w"] * 0.2, box["w"] * 0.8)
        ty = box["y"] + random.uniform(box["h"] * 0.2, box["h"] * 0.8)

        await self._move_to(tx, ty)
        # Hover
        await asyncio.sleep(random.uniform(0.1, 0.3))
        if dwell > 0:
            await asyncio.sleep(dwell)
        # Click
        await self._mouse_down(tx, ty)
        await asyncio.sleep(random.uniform(0.05, 0.12))
        await self._mouse_up(tx, ty)
        self.last_x, self.last_y = tx, ty

    async def click_and_select(self, dropdown_el, option_el) -> None:
        """Открыть dropdown → dwell (читаем) → выбрать option."""
        await self.click(dropdown_el)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await self.click(option_el)

    async def scroll_into_view(self, element) -> None:
        """Микро-скролл к элементу, как человек."""
        try:
            await element.scroll_into_view()
        except Exception:
            try:
                await element.apply("(el) => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
            except Exception:
                pass
        await asyncio.sleep(random.uniform(0.3, 0.7))

    async def idle_drift(self, duration: float = 1.0) -> None:
        """Лёгкое движение курсора — имитация живого пользователя."""
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < duration:
            dx = random.uniform(-30, 30)
            dy = random.uniform(-20, 20)
            nx = max(50, min(1200, self.last_x + dx))
            ny = max(50, min(700, self.last_y + dy))
            await self._dispatch_mouse("mouseMoved", nx, ny)
            self.last_x, self.last_y = nx, ny
            await asyncio.sleep(random.uniform(0.1, 0.4))

    async def micro_scroll(self) -> None:
        """Небольшой скролл вверх-вниз перед действием."""
        direction = random.choice([-1, 1])
        delta = random.randint(30, 120) * direction
        await self.page.send(inp.dispatch_mouse_event(
            type_="mouseWheel",
            x=self.last_x,
            y=self.last_y,
            delta_x=0,
            delta_y=delta,
        ))
        await asyncio.sleep(random.uniform(0.2, 0.5))
        # Иногда скроллим обратно
        if random.random() < 0.4:
            await self.page.send(inp.dispatch_mouse_event(
                type_="mouseWheel",
                x=self.last_x,
                y=self.last_y,
                delta_x=0,
                delta_y=-delta * random.uniform(0.5, 1.0),
            ))
            await asyncio.sleep(random.uniform(0.15, 0.35))

    # ── Internal ───────────────────────────────────────────────

    async def _move_to(self, tx: float, ty: float) -> None:
        """Bezier-кривая от текущей позиции до цели."""
        points = self._bezier_curve(
            self.last_x, self.last_y, tx, ty,
            steps=random.randint(15, 35)
        )
        for px, py in points:
            await self._dispatch_mouse("mouseMoved", px, py)
            await asyncio.sleep(random.uniform(0.005, 0.02))
        self.last_x, self.last_y = tx, ty

    async def _mouse_down(self, x: float, y: float) -> None:
        await self.page.send(inp.dispatch_mouse_event(
            type_="mousePressed",
            x=x, y=y,
            button=inp.MouseButton("left"),
            click_count=1,
        ))

    async def _mouse_up(self, x: float, y: float) -> None:
        await self.page.send(inp.dispatch_mouse_event(
            type_="mouseReleased",
            x=x, y=y,
            button=inp.MouseButton("left"),
            click_count=1,
        ))

    async def _dispatch_mouse(self, event_type: str, x: float, y: float) -> None:
        await self.page.send(inp.dispatch_mouse_event(
            type_=event_type,
            x=x, y=y,
        ))

    async def _get_box(self, element) -> dict | None:
        try:
            result = await element.apply("""
                function() {
                    const r = this.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return null;
                    return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height});
                }
            """)
            if result and isinstance(result, str):
                import json
                return json.loads(result)
            return result
        except Exception:
            return None

    @staticmethod
    def _bezier_curve(x0, y0, x1, y1, steps=25) -> list[tuple[float, float]]:
        """Cubic bezier с 2 случайными контрольными точками."""
        # Контрольные точки со смещением для естественности
        cp1x = x0 + (x1 - x0) * random.uniform(0.1, 0.4) + random.uniform(-50, 50)
        cp1y = y0 + (y1 - y0) * random.uniform(0.0, 0.3) + random.uniform(-30, 30)
        cp2x = x0 + (x1 - x0) * random.uniform(0.6, 0.9) + random.uniform(-50, 50)
        cp2y = y0 + (y1 - y0) * random.uniform(0.7, 1.0) + random.uniform(-30, 30)

        points = []
        for i in range(steps + 1):
            t = i / steps
            # Easing — чуть быстрее в середине, медленнее к краям
            t = t * t * (3 - 2 * t)
            u = 1 - t
            px = u**3 * x0 + 3 * u**2 * t * cp1x + 3 * u * t**2 * cp2x + t**3 * x1
            py = u**3 * y0 + 3 * u**2 * t * cp1y + 3 * u * t**2 * cp2y + t**3 * y1
            # Микро-тремор (человеческая рука)
            px += random.uniform(-1.5, 1.5)
            py += random.uniform(-1.5, 1.5)
            points.append((px, py))
        return points
