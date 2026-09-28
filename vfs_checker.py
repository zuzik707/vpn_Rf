import asyncio
import logging
import os
import random
import time

import nodriver as uc

from antidetect import get_chrome_args, inject_stealth, setup_stealth_on_new_page
from captcha_solver import CaptchaSolver
from config import Config

logger = logging.getLogger(__name__)

# Маркеры для определения состояния страницы
NO_SLOTS_MARKERS = [
    "no appointment",
    "no available",
    "currently no date",
    "slot not available",
    "no earlier date",
    "not available",
    "no open dates",
    "there are no open",
    "appointment is unavailable",
]

SLOTS_AVAILABLE_MARKERS = [
    "earliest available",
    "available slot",
    "select date",
    "choose a date",
    "appointment date",
    "book appointment",
    "available date",
    "open appointment",
    "select time",
    "choose time",
]

CAPTCHA_MARKERS = [
    "cf-turnstile",
    "turnstile-wrapper",
    "cf-challenge",
    "hcaptcha",
    "h-captcha",
    "g-recaptcha",
    "captcha-container",
]

CLOUDFLARE_MARKERS = [
    "checking your browser",
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "verifying you are human",
    "please wait",
    "ray id",
]

BLOCKED_MARKERS = [
    "access denied",
    "forbidden",
    "blocked",
    "temporarily unavailable",
    "too many requests",
    "rate limit",
    "try again later",
    "account has been",
    "suspended",
]

LOGIN_MARKERS = [
    "sign in",
    "log in",
    "email address",
    "email id",
]


class VFSBrowser:
    def __init__(self):
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None
        self.captcha_solver = CaptchaSolver()
        self.logged_in = False
        self.last_login_time: float = 0
        self._recursion_depth = 0

    async def start_browser(self) -> None:
        os.makedirs(Config.BROWSER_DATA_DIR, exist_ok=True)
        os.makedirs(Config.SCREENSHOT_DIR, exist_ok=True)

        chrome_args = get_chrome_args()

        self.browser = await uc.start(
            user_data_dir=Config.BROWSER_DATA_DIR,
            headless=False,
            lang="en-US",
            browser_args=chrome_args,
        )
        logger.info("Браузер запущен (nodriver + stealth args)")

    async def close_browser(self) -> None:
        if self.browser:
            try:
                self.browser.stop()
            except Exception:
                pass
            self.browser = None
            self.page = None
            self.logged_in = False
            logger.info("Браузер закрыт")

    async def _human_delay(self, min_s: float = 0.5, max_s: float = 2.0) -> None:
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _human_type(self, element, text: str) -> None:
        """Печатаем как человек — с рандомной скоростью и паузами."""
        await element.clear_input()
        await self._human_delay(0.2, 0.5)
        for i, char in enumerate(text):
            await element.send_keys(char)
            # Микро-пауза между символами
            base_delay = random.uniform(0.04, 0.12)
            # Иногда "задумываемся" подольше
            if random.random() < 0.08:
                base_delay += random.uniform(0.2, 0.5)
            # После @ или . в email — чуть длиннее
            if char in ("@", "."):
                base_delay += random.uniform(0.1, 0.3)
            await asyncio.sleep(base_delay)

    async def _get_page_text(self) -> str:
        try:
            return ((await self.page.evaluate("document.body.innerText")) or "").lower()
        except Exception:
            return ""

    async def _get_page_html(self) -> str:
        try:
            return ((await self.page.evaluate("document.documentElement.outerHTML")) or "").lower()
        except Exception:
            return ""

    async def _take_screenshot(self, name: str) -> str:
        path = os.path.join(Config.SCREENSHOT_DIR, f"{name}_{int(time.time())}.png")
        try:
            await self.page.save_screenshot(path)
        except Exception as e:
            logger.debug("Скриншот не удался: %s", e)
        return path

    async def _detect_page_state(self) -> str:
        html = await self._get_page_html()
        text = await self._get_page_text()
        combined = html + " " + text

        if any(m in combined for m in BLOCKED_MARKERS):
            return "blocked"
        if any(m in combined for m in CLOUDFLARE_MARKERS):
            return "cloudflare"
        if any(m in combined for m in CAPTCHA_MARKERS):
            if any(m in combined for m in LOGIN_MARKERS):
                return "login_with_captcha"
            return "captcha"
        if any(m in combined for m in SLOTS_AVAILABLE_MARKERS):
            return "slots_found"
        if any(m in combined for m in NO_SLOTS_MARKERS):
            return "no_slots"
        if any(m in combined for m in LOGIN_MARKERS) and "password" in combined:
            return "login_page"
        return "unknown"

    async def _wait_for_cloudflare(self, timeout: int = 60) -> bool:
        """
        Ждём пока nodriver пройдёт Cloudflare challenge.
        nodriver проходит его автоматически благодаря anti-detect,
        но нужно дать время.
        """
        logger.info("Cloudflare challenge — ждём (до %d сек)...", timeout)
        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(3)
            state = await self._detect_page_state()
            if state not in ("cloudflare",):
                logger.info("Cloudflare пройден -> %s (%.0f сек)", state, time.time() - start)
                return True
        logger.error("Cloudflare НЕ пройден за %d сек", timeout)
        await self._take_screenshot("cf_timeout")
        return False

    async def _extract_turnstile_sitekey(self) -> str | None:
        """Извлекаем sitekey Turnstile несколькими способами."""
        sitekey = await self.page.evaluate("""
            (() => {
                // Способ 1: data-sitekey атрибут
                let el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');

                // Способ 2: cf-turnstile div
                el = document.querySelector('.cf-turnstile[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');

                // Способ 3: iframe URL
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (iframe) {
                    const m = iframe.src.match(/[?&]k=([^&]+)/);
                    if (m) return m[1];
                }

                // Способ 4: inline script с sitekey
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    if (!s.textContent) continue;
                    // turnstile.render({sitekey: '...'})
                    let m = s.textContent.match(/sitekey['":\\s]+['"]([0-9a-zA-Z_-]{20,})['"]/);
                    if (m) return m[1];
                    // data-sitekey="..."
                    m = s.textContent.match(/data-sitekey=['"]([^'"]+)['"]/);
                    if (m) return m[1];
                }

                // Способ 5: meta tag
                el = document.querySelector('meta[name="cf-turnstile-sitekey"]');
                if (el) return el.getAttribute('content');

                return null;
            })()
        """)
        return sitekey

    async def _solve_and_inject_turnstile(self) -> bool:
        """Решаем Turnstile через 2Captcha и инжектим токен."""
        sitekey = await self._extract_turnstile_sitekey()
        if not sitekey:
            logger.error("Turnstile sitekey не найден")
            await self._take_screenshot("no_sitekey")
            return False

        page_url = await self.page.evaluate("window.location.href")
        logger.info("Решаю Turnstile (sitekey=%s...)", sitekey[:16])

        token = self.captcha_solver.solve_turnstile(sitekey, page_url)
        if not token:
            logger.error("2Captcha не вернул токен")
            return False

        # Инжектим токен
        # Важно: экранируем кавычки в токене
        safe_token = token.replace("'", "\\'").replace('"', '\\"')
        result = await self.page.evaluate(f"""
            (() => {{
                let ok = false;

                // 1. Стандартные input поля
                for (const name of ['cf-turnstile-response', 'g-recaptcha-response']) {{
                    const el = document.querySelector('[name="' + name + '"]')
                             || document.getElementById(name);
                    if (el) {{
                        el.value = '{safe_token}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        ok = true;
                    }}
                }}

                // 2. Callback функции
                const callbacks = [
                    'tsCallback', 'turnstileCallback',
                    'onTurnstileSuccess', 'cfCallback',
                    'captchaCallback', 'onCaptchaSuccess',
                ];
                for (const cb of callbacks) {{
                    if (typeof window[cb] === 'function') {{
                        try {{
                            window[cb]('{safe_token}');
                            ok = true;
                        }} catch(e) {{}}
                    }}
                }}

                // 3. Через turnstile API если доступен
                if (window.turnstile) {{
                    const widgets = document.querySelectorAll('.cf-turnstile');
                    widgets.forEach(w => {{
                        const cbName = w.getAttribute('data-callback');
                        if (cbName && typeof window[cbName] === 'function') {{
                            try {{
                                window[cbName]('{safe_token}');
                                ok = true;
                            }} catch(e) {{}}
                        }}
                    }});
                }}

                return ok;
            }})()
        """)

        if result:
            logger.info("Turnstile token инжектирован и callback вызван")
        else:
            logger.warning("Token инжектирован, но callbacks не найдены — продолжаем")

        await self._human_delay(1.5, 3.0)
        return True

    async def _find_and_click(self, selectors: list[str]):
        """Ищем элемент по списку селекторов и кликаем."""
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    async def _find_input(self, selectors: list[str]):
        """Ищем input по списку селекторов."""
        for sel in selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _do_login(self) -> bool:
        """Заполняем форму логина."""
        logger.info("Заполняю форму логина...")
        await self._human_delay(1.5, 3.0)

        email_field = await self._find_input([
            "input[type='email']",
            "input[name='email']",
            "input[id='email']",
            "input[name='username']",
            "input[id='username']",
            "input[placeholder*='mail']",
            "input[placeholder*='Email']",
            "input[autocomplete='email']",
            "input[autocomplete='username']",
        ])
        if not email_field:
            logger.error("Поле email не найдено")
            await self._take_screenshot("no_email")
            return False

        await email_field.click()
        await self._human_delay(0.3, 0.7)
        await self._human_type(email_field, Config.VFS_EMAIL)
        await self._human_delay(0.8, 1.5)

        password_field = await self._find_input([
            "input[type='password']",
            "input[name='password']",
            "input[id='password']",
        ])
        if not password_field:
            logger.error("Поле пароля не найдено")
            await self._take_screenshot("no_password")
            return False

        await password_field.click()
        await self._human_delay(0.3, 0.7)
        await self._human_type(password_field, Config.VFS_PASSWORD)
        await self._human_delay(0.8, 1.5)

        # Сабмит
        submitted = await self._find_and_click([
            "button[type='submit']",
            "input[type='submit']",
            "button.mat-raised-button",
            "button.btn-primary",
            "button.login-btn",
            "button.sign-in-btn",
        ])
        if not submitted:
            logger.info("Submit кнопка не найдена — Enter")
            await password_field.send_keys("\r")

        logger.info("Логин отправлен — ждём ответ...")
        await self._human_delay(4.0, 7.0)
        return True

    async def login(self) -> bool:
        """Полный флоу логина: браузер -> cloudflare -> captcha -> логин."""
        if not self.browser:
            await self.start_browser()

        self.page = await self.browser.get("about:blank")

        # Инжектим stealth ПЕРЕД навигацией
        await setup_stealth_on_new_page(self.page)
        await self._human_delay(0.5, 1.0)

        # Навигация на страницу логина
        logger.info("Открываю %s", Config.VFS_URL)
        await self.page.get(Config.VFS_URL)
        await self._human_delay(3.0, 5.0)

        # Post-navigation stealth inject
        await inject_stealth(self.page)

        for attempt in range(3):
            state = await self._detect_page_state()
            logger.info("Состояние страницы: %s (попытка %d)", state, attempt + 1)

            if state == "blocked":
                logger.error("Доступ заблокирован! Возможно IP бан.")
                await self._take_screenshot("blocked")
                return False

            if state == "cloudflare":
                if not await self._wait_for_cloudflare(timeout=90):
                    return False
                await inject_stealth(self.page)
                continue

            if state in ("captcha", "login_with_captcha"):
                # Если есть и логин форма и captcha — сначала заполняем форму
                if state == "login_with_captcha":
                    await self._do_login()
                    await self._human_delay(1.0, 2.0)
                    # Теперь решаем captcha
                    if not await self._solve_and_inject_turnstile():
                        return False
                    await self._human_delay(2.0, 4.0)
                    # И сабмитим ещё раз если нужно
                    state = await self._detect_page_state()
                    if state == "login_page":
                        await self._find_and_click(["button[type='submit']"])
                        await self._human_delay(4.0, 7.0)
                else:
                    if not await self._solve_and_inject_turnstile():
                        return False
                    await self._human_delay(2.0, 4.0)
                continue

            if state == "login_page":
                if not await self._do_login():
                    return False
                # После отправки формы проверяем что дальше
                state = await self._detect_page_state()
                if state == "cloudflare":
                    await self._wait_for_cloudflare()
                    state = await self._detect_page_state()
                if state == "captcha":
                    if not await self._solve_and_inject_turnstile():
                        return False
                    await self._human_delay(3.0, 5.0)
                    state = await self._detect_page_state()
                if state == "login_page":
                    text = await self._get_page_text()
                    if "incorrect" in text or "invalid" in text or "wrong" in text:
                        logger.error("Неверный логин/пароль!")
                        return False
                    logger.warning("Всё ещё на странице логина после попытки")
                    await self._take_screenshot("still_login")
                    continue
                break

            if state in ("slots_found", "no_slots", "unknown"):
                logger.info("Уже залогинены! (state=%s)", state)
                break

        self.logged_in = True
        self.last_login_time = time.time()
        logger.info("Логин завершён успешно")
        return True

    async def check_slots(self) -> tuple[bool, str, str | None]:
        """
        Проверяем слоты.
        Возвращает: (found, info_text, screenshot_path)
        """
        self._recursion_depth += 1
        if self._recursion_depth > 3:
            self._recursion_depth = 0
            return False, "Слишком много редиректов", None

        if not self.browser or not self.logged_in:
            if not await self.login():
                self._recursion_depth = 0
                return False, "Логин не удался", None

        # Переход на страницу бронирования
        base = Config.VFS_URL.replace("/login", "")
        appt_url = f"{base}/book-an-appointment"
        logger.info("Проверяю слоты: %s", appt_url)

        await self.page.get(appt_url)
        await self._human_delay(3.0, 6.0)
        await inject_stealth(self.page)

        state = await self._detect_page_state()

        if state == "blocked":
            self._recursion_depth = 0
            return False, "IP заблокирован", await self._take_screenshot("blocked")

        if state == "cloudflare":
            if await self._wait_for_cloudflare():
                state = await self._detect_page_state()
            else:
                self._recursion_depth = 0
                return False, "Cloudflare не пропустил", None

        if state == "captcha":
            await self._solve_and_inject_turnstile()
            await self._human_delay(3.0, 5.0)
            state = await self._detect_page_state()

        if state == "login_page":
            self.logged_in = False
            self._recursion_depth = 0
            return False, "Сессия истекла — перелогин на следующей итерации", None

        if state == "slots_found":
            screenshot = await self._take_screenshot("slots_found")
            text = await self._get_page_text()

            # Извлекаем информацию о слотах из текста
            info_lines = []
            for line in text.split("\n"):
                line = line.strip()
                if not line or len(line) > 200:
                    continue
                low = line.lower()
                if any(kw in low for kw in ["earliest", "available", "date", "slot", "appointment",
                                             "january", "february", "march", "april", "may", "june",
                                             "july", "august", "september", "october", "november", "december",
                                             "2025", "2026", "2027"]):
                    info_lines.append(line)

            info = "\n".join(info_lines[:15]) if info_lines else "Слоты доступны!"
            self._recursion_depth = 0
            return True, info, screenshot

        if state == "no_slots":
            self._recursion_depth = 0
            return False, "Слотов нет", None

        # Неизвестное состояние — делаем скриншот для анализа
        screenshot = await self._take_screenshot("unknown")
        text = await self._get_page_text()
        self._recursion_depth = 0
        return False, f"Страница: {text[:300]}", screenshot

    def session_alive(self) -> bool:
        if not self.logged_in:
            return False
        return (time.time() - self.last_login_time) < 1800
