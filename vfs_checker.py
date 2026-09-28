"""
VFS Global Slot Checker — точно под visa.vfsglobal.com/uzb/en/lva

Реальный флоу сайта (из скриншотов):
1. Login page → вводим email/password → Cloudflare/Turnstile
2. Dashboard → "Active application(s)" + кнопка "Start New Booking"
3. Click "Start New Booking" → Appointment Details (Step 1)
4. Dropdown "Choose your Application Centre" → "VFS GLOBAL SERVICES UBKN"
5. Dropdown "Choose your appointment category" → "Latvia Long Stay/Visa D"
6. Dropdown "Choose your sub-category" → "Work (Visa D) Uzbek, Turkmen"
   или "Cargo Drivers (Visa D) Uzbek, Turkmen"
7. Если слотов нет → голубой банер:
   "We are sorry but no appointment slots are currently available.
    New slots open at regular intervals, please try again later"
   Кнопка "Continue" — disabled/greyed out
8. Если слоты ЕСТЬ → банера нет, "Continue" активна,
   возможно показывает дату/время

Наша задача: пройти шаги 1-6, проверить шаг 7 vs 8,
если слоты — мгновенный Telegram push.
"""

import asyncio
import logging
import os
import random
import time

import nodriver as uc

from antidetect import get_chrome_args, inject_stealth, setup_stealth_on_new_page
from config import Config
from network_interceptor import NetworkInterceptor

logger = logging.getLogger(__name__)

# Точный текст банера "нет слотов" с сайта VFS
NO_SLOTS_TEXT = "we are sorry but no appointment slots are currently available"

# Маркеры
CLOUDFLARE_MARKERS = [
    "checking your browser",
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "verifying you are human",
]
CAPTCHA_MARKERS = [
    "cf-turnstile",
    "turnstile-wrapper",
    "challenges.cloudflare.com",
    "hcaptcha",
    "g-recaptcha",
]
BLOCKED_MARKERS = [
    "access denied",
    "too many requests",
    "rate limit",
    "account has been",
    "suspended",
    "temporarily blocked",
]


class VFSBrowser:
    def __init__(self, captcha_solver=None):
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None
        self.captcha_solver = captcha_solver
        self.interceptor = NetworkInterceptor()
        self.logged_in = False
        self.on_dashboard = False
        self.last_login_time: float = 0

    # ── Browser lifecycle ──────────────────────────────────────────

    async def start_browser(self) -> None:
        os.makedirs(Config.BROWSER_DATA_DIR, exist_ok=True)
        os.makedirs(Config.SCREENSHOT_DIR, exist_ok=True)
        args = get_chrome_args()
        if Config.PROXY_URL:
            args.append(f"--proxy-server={Config.PROXY_URL}")
            logger.info("Proxy: %s", Config.PROXY_URL.split("@")[-1] if "@" in Config.PROXY_URL else Config.PROXY_URL)
        self.browser = await uc.start(
            user_data_dir=Config.BROWSER_DATA_DIR,
            headless=False,
            lang="en-US",
            browser_args=args,
        )
        logger.info("Chrome запущен (nodriver)")

    async def close_browser(self) -> None:
        await self.interceptor.detach()
        if self.browser:
            try:
                self.browser.stop()
            except Exception:
                pass
        self.browser = None
        self.page = None
        self.logged_in = False
        self.on_dashboard = False

    # ── Human-like helpers ─────────────────────────────────────────

    async def _delay(self, lo: float = 0.5, hi: float = 2.0) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    async def _human_type(self, element, text: str) -> None:
        await element.clear_input()
        await self._delay(0.2, 0.4)
        for char in text:
            await element.send_keys(char)
            d = random.uniform(0.04, 0.12)
            if random.random() < 0.07:
                d += random.uniform(0.2, 0.5)
            if char in ("@", "."):
                d += random.uniform(0.1, 0.3)
            await asyncio.sleep(d)

    # ── Page inspection ────────────────────────────────────────────

    async def _text(self) -> str:
        try:
            return ((await self.page.evaluate("document.body.innerText")) or "").lower()
        except Exception:
            return ""

    async def _html(self) -> str:
        try:
            return ((await self.page.evaluate("document.documentElement.outerHTML")) or "").lower()
        except Exception:
            return ""

    async def _screenshot(self, tag: str) -> str:
        path = os.path.join(Config.SCREENSHOT_DIR, f"{tag}_{int(time.time())}.png")
        try:
            await self.page.save_screenshot(path)
        except Exception:
            pass
        return path

    async def _url(self) -> str:
        try:
            return (await self.page.evaluate("window.location.href")) or ""
        except Exception:
            return ""

    async def _page_state(self) -> str:
        """
        Определяем где мы на сайте VFS.
        Приоритет: URL → DOM-элементы → текст.
        Так надёжнее чем только текст — если VFS поменяет фразу, URL и DOM останутся.
        """
        url = (await self._url()).lower()
        html = await self._html()
        text = await self._text()
        both = html + " " + text

        # Blocked — проверяем первым
        if any(m in both for m in BLOCKED_MARKERS):
            return "blocked"

        # Cloudflare challenge — по URL и DOM
        if "challenges.cloudflare.com" in url or "/cdn-cgi/" in url:
            return "cloudflare"
        if any(m in both for m in CLOUDFLARE_MARKERS):
            return "cloudflare"

        # Captcha — по iframe/виджету
        has_captcha = await self.page.evaluate("""
            (() => {
                return !!(
                    document.querySelector('.cf-turnstile') ||
                    document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
                    document.querySelector('[data-sitekey]') ||
                    document.querySelector('.h-captcha') ||
                    document.querySelector('.g-recaptcha')
                );
            })()
        """)
        if has_captcha:
            return "captcha"

        # Login — по URL + наличие password input
        if "/login" in url:
            has_pwd = await self.page.evaluate(
                "!!document.querySelector('input[type=\"password\"]')"
            )
            if has_pwd:
                return "login"

        # Dashboard — по URL + кнопка Start New Booking
        if "/dashboard" in url or "start new booking" in both:
            has_btn = await self.page.evaluate("""
                (() => {
                    const els = document.querySelectorAll('button, a');
                    return [...els].some(e =>
                        e.textContent.toLowerCase().includes('start new booking')
                    );
                })()
            """)
            if has_btn:
                return "dashboard"

        # Appointment form — по URL + наличие select/dropdown'ов
        if "/appointment" in url or "/book" in url:
            has_selects = await self.page.evaluate(
                "document.querySelectorAll('select, mat-select, [role=\"combobox\"]').length >= 2"
            )
            if has_selects:
                return "appointment_form"

        # Fallback текстовые маркеры
        if "appointment details" in both and "choose your" in both:
            return "appointment_form"
        if "start new booking" in both:
            return "dashboard"
        if ("sign in" in both or "log in" in both) and "password" in both:
            return "login"
        if "sign out" in both or "my account" in both:
            return "logged_in"

        return "unknown"

    # ── Cloudflare / CAPTCHA ───────────────────────────────────────

    async def _wait_cloudflare(self, timeout: int = 90) -> bool:
        logger.info("Cloudflare challenge — ждём до %dс...", timeout)
        t0 = time.time()
        while time.time() - t0 < timeout:
            await asyncio.sleep(3)
            if await self._page_state() != "cloudflare":
                logger.info("Cloudflare пройден (%.0fс)", time.time() - t0)
                return True
        logger.error("Cloudflare timeout")
        await self._screenshot("cf_fail")
        return False

    async def _extract_sitekey(self) -> str | None:
        return await self.page.evaluate("""
            (() => {
                let el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                el = document.querySelector('.cf-turnstile[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (iframe) {
                    const m = iframe.src.match(/[?&]k=([^&]+)/);
                    if (m) return m[1];
                }
                for (const s of document.querySelectorAll('script')) {
                    if (!s.textContent) continue;
                    const m = s.textContent.match(/sitekey['"]?\\s*[:=]\\s*['"]([0-9a-zA-Z_-]{20,})['"]/);
                    if (m) return m[1];
                }
                return null;
            })()
        """)

    async def _solve_turnstile(self) -> bool:
        sitekey = await self._extract_sitekey()
        if not sitekey:
            logger.error("sitekey не найден")
            await self._screenshot("no_sitekey")
            return False

        page_url = await self.page.evaluate("window.location.href")
        logger.info("2Captcha: решаю Turnstile (sitekey=%s...)", sitekey[:16])
        token = self.captcha_solver.solve_turnstile(sitekey, page_url)
        if not token:
            return False

        safe = token.replace("\\", "\\\\").replace("'", "\\'")
        await self.page.evaluate(f"""
            (() => {{
                for (const n of ['cf-turnstile-response','g-recaptcha-response']) {{
                    const el = document.querySelector('[name="'+n+'"]') || document.getElementById(n);
                    if (el) {{
                        el.value = '{safe}';
                        el.dispatchEvent(new Event('input', {{bubbles:true}}));
                        el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                }}
                for (const cb of ['tsCallback','turnstileCallback','onTurnstileSuccess','cfCallback','captchaCallback']) {{
                    if (typeof window[cb]==='function') try {{ window[cb]('{safe}'); }} catch(e) {{}}
                }}
                const widgets = document.querySelectorAll('.cf-turnstile[data-callback]');
                widgets.forEach(w => {{
                    const fn = window[w.getAttribute('data-callback')];
                    if (typeof fn==='function') try {{ fn('{safe}'); }} catch(e) {{}}
                }});
            }})()
        """)
        logger.info("Turnstile token инжектирован")
        await self._delay(1.5, 3.0)
        return True

    async def _handle_obstacle(self) -> bool:
        """Обработка любого препятствия (CF/captcha/blocked)."""
        state = await self._page_state()
        if state == "blocked":
            logger.error("BLOCKED!")
            await self._screenshot("blocked")
            return False
        if state == "cloudflare":
            if not await self._wait_cloudflare():
                return False
            await inject_stealth(self.page)
            state = await self._page_state()
        if state == "captcha":
            if not await self._solve_turnstile():
                return False
            await self._delay(2, 4)
        return True

    # ── Select dropdown by visible text ────────────────────────────

    async def _select_dropdown_option(self, dropdown_label: str, option_text: str) -> bool:
        """
        Находим dropdown по тексту label'а и выбираем option по тексту.
        VFS использует обычные HTML <select> элементы.
        """
        logger.info("Выбираю '%s' в '%s'...", option_text, dropdown_label)

        # Ищем select, связанный с label
        selected = await self.page.evaluate(f"""
            (() => {{
                const optText = '{option_text.replace("'", "\\'")}';
                const labelText = '{dropdown_label.replace("'", "\\'")}';

                // Все select'ы на странице
                const selects = document.querySelectorAll('select');
                for (const sel of selects) {{
                    // Проверяем label
                    let labelEl = null;
                    if (sel.id) labelEl = document.querySelector('label[for="'+sel.id+'"]');
                    if (!labelEl) labelEl = sel.closest('div,fieldset')?.querySelector('label');
                    const lText = (labelEl?.textContent || '').toLowerCase();
                    const sLabel = labelText.toLowerCase();

                    // Ищем select, чей label содержит нужный текст
                    if (!lText.includes(sLabel) && !sLabel.includes('centre') && !sLabel.includes('center')) {{
                        // Также проверяем по предшествующему тексту
                        const prev = sel.previousElementSibling;
                        const prevText = (prev?.textContent || '').toLowerCase();
                        if (!prevText.includes(sLabel)) continue;
                    }}

                    // Ищем option с нужным текстом
                    for (const opt of sel.options) {{
                        if (opt.text.toLowerCase().includes(optText.toLowerCase())) {{
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            sel.dispatchEvent(new Event('input', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                }}

                // Fallback: ищем по всем select'ам на странице
                for (const sel of selects) {{
                    for (const opt of sel.options) {{
                        if (opt.text.toLowerCase().includes(optText.toLowerCase())) {{
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            sel.dispatchEvent(new Event('input', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                }}

                return false;
            }})()
        """)

        if selected:
            logger.info("Выбрано: '%s'", option_text)
            await self._delay(1.0, 2.5)
            return True

        # Fallback: может это Angular Material dropdown (mat-select)
        logger.info("HTML <select> не сработал — пробую Angular mat-select...")
        clicked = await self._try_mat_select(dropdown_label, option_text)
        if clicked:
            await self._delay(1.0, 2.5)
            return True

        logger.warning("Не удалось выбрать '%s'", option_text)
        await self._screenshot(f"dropdown_fail_{dropdown_label[:10]}")
        return False

    async def _try_mat_select(self, label_text: str, option_text: str) -> bool:
        """Для Angular Material dropdowns (mat-select)."""
        return await self.page.evaluate(f"""
            (() => {{
                const label = '{label_text.replace("'", "\\'")}';
                const option = '{option_text.replace("'", "\\'")}';

                // Ищем mat-select или div[role=listbox] рядом с label
                const all = document.querySelectorAll('mat-select, [role="combobox"], .dropdown-toggle, select');
                for (const el of all) {{
                    // Кликаем чтобы открыть
                    el.click();
                }}

                // Ждём появления option list
                return new Promise(resolve => {{
                    setTimeout(() => {{
                        const opts = document.querySelectorAll(
                            'mat-option, [role="option"], .dropdown-item, li.option'
                        );
                        for (const o of opts) {{
                            if (o.textContent.toLowerCase().includes(option.toLowerCase())) {{
                                o.click();
                                resolve(true);
                                return;
                            }}
                        }}
                        resolve(false);
                    }}, 500);
                }});
            }})()
        """)

    # ── Login flow ─────────────────────────────────────────────────

    async def login(self) -> bool:
        if not self.browser:
            await self.start_browser()

        self.page = await self.browser.get("about:blank")
        await setup_stealth_on_new_page(self.page)
        await self._delay(0.5, 1.0)

        logger.info("Открываю %s", Config.VFS_URL)
        await self.page.get(Config.VFS_URL)
        await self._delay(3, 5)
        await inject_stealth(self.page)

        for attempt in range(4):
            state = await self._page_state()
            logger.info("State: %s (attempt %d)", state, attempt + 1)

            if state == "blocked":
                await self._screenshot("blocked")
                return False

            if state == "cloudflare":
                if not await self._wait_cloudflare():
                    return False
                await inject_stealth(self.page)
                continue

            if state == "captcha":
                if not await self._solve_turnstile():
                    return False
                await self._delay(2, 4)
                continue

            if state == "login":
                # Заполняем форму
                email_el = await self._find_input([
                    "input[type='email']", "input[name='email']", "input[id='email']",
                    "input[name='username']", "input[id='username']",
                    "input[placeholder*='mail']", "input[autocomplete='email']",
                ])
                if not email_el:
                    await self._screenshot("no_email")
                    return False

                await email_el.click()
                await self._delay(0.3, 0.6)
                await self._human_type(email_el, Config.VFS_EMAIL)
                await self._delay(0.6, 1.2)

                pwd_el = await self._find_input([
                    "input[type='password']", "input[name='password']", "input[id='password']",
                ])
                if not pwd_el:
                    await self._screenshot("no_pwd")
                    return False

                await pwd_el.click()
                await self._delay(0.3, 0.6)
                await self._human_type(pwd_el, Config.VFS_PASSWORD)
                await self._delay(0.6, 1.2)

                # Может быть captcha на форме логина
                html = await self._html()
                if any(m in html for m in CAPTCHA_MARKERS):
                    logger.info("CAPTCHA на форме логина")
                    await self._solve_turnstile()
                    await self._delay(1, 2)

                # Submit — кнопка "Sign In"
                if not await self._click([
                    "button[type='submit']", "input[type='submit']",
                    "button.btn-primary", "button.mat-raised-button",
                ]):
                    # Fallback: ищем кнопку по тексту "Sign In"
                    clicked = await self.page.evaluate("""
                        (() => {
                            const btns = document.querySelectorAll('button');
                            for (const b of btns) {
                                if (b.textContent.trim().toLowerCase() === 'sign in') {
                                    b.click(); return true;
                                }
                            }
                            return false;
                        })()
                    """)
                    if not clicked:
                        await pwd_el.send_keys("\r")

                await self._delay(4, 8)
                state = await self._page_state()

                if state == "login":
                    text = await self._text()
                    if any(w in text for w in ["incorrect", "invalid", "wrong", "failed"]):
                        logger.error("Неверный логин/пароль!")
                        return False
                    continue

                if state in ("cloudflare", "captcha"):
                    continue

            if state in ("dashboard", "logged_in", "appointment_form", "unknown"):
                break

        self.logged_in = True
        self.on_dashboard = (await self._page_state()) == "dashboard"
        self.last_login_time = time.time()
        # Подключаем перехват API после успешного логина
        try:
            await self.interceptor.attach(self.page)
        except Exception as e:
            logger.warning("Interceptor attach failed: %s", e)
        logger.info("Login OK (state=%s)", await self._page_state())
        return True

    # ── Core: check slots ──────────────────────────────────────────

    async def check_slots(self) -> tuple[bool, str, str | None]:
        """
        Полный флоу проверки слотов:
        Dashboard → Start New Booking → выбираем dropdown'ы → читаем результат.
        Проверяем ОБЕ подкатегории.
        """
        if not self.browser or not self.logged_in:
            if not await self.login():
                return False, "Логин не удался", None

        results = []
        for subcategory in Config.VFS_SUBCATEGORIES:
            found, info, screenshot = await self._check_single_subcategory(subcategory)
            if found:
                return True, f"[{subcategory}]\n{info}", screenshot
            results.append(f"{subcategory}: {info}")

        return False, " | ".join(results), None

    async def _check_single_subcategory(self, subcategory: str) -> tuple[bool, str, str | None]:
        """Проверяем конкретную подкатегорию."""
        logger.info("Проверяю: %s", subcategory)

        # Шаг 1: Убеждаемся что мы на dashboard
        if not await self._ensure_dashboard():
            return False, "Не удалось попасть на dashboard", None

        # Шаг 2: Кликаем "Start New Booking"
        await self._delay(1, 2)
        clicked = await self._click([
            "button:has-text('Start New Booking')",
            "a:has-text('Start New Booking')",
        ])
        if not clicked:
            # Fallback: ищем кнопку по тексту через JS
            clicked = await self.page.evaluate("""
                (() => {
                    const els = document.querySelectorAll('button, a, input[type="button"]');
                    for (const el of els) {
                        if (el.textContent.toLowerCase().includes('start new booking')) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)
        if not clicked:
            await self._screenshot("no_start_booking")
            return False, "Кнопка 'Start New Booking' не найдена", None

        await self._delay(2, 4)

        # Обработка возможных препятствий после клика
        if not await self._handle_obstacle():
            return False, "Препятствие после Start New Booking", None

        # Шаг 3: Ждём загрузки формы Appointment Details
        state = await self._page_state()
        if state != "appointment_form":
            await self._delay(2, 3)
            state = await self._page_state()

        if state != "appointment_form":
            logger.warning("Не на форме appointment (state=%s)", state)
            if state == "login":
                self.logged_in = False
                return False, "Сессия истекла", None
            await self._screenshot("not_appt_form")
            # Попробуем всё равно продолжить
            text = await self._text()
            if "choose your" not in text:
                return False, f"Неожиданная страница: {text[:200]}", None

        # Шаг 4: Выбираем Centre
        if not await self._select_dropdown_option("Application Centre", Config.VFS_CENTRE):
            return False, "Не удалось выбрать Centre", None

        await self._delay(1.5, 3.0)

        # Шаг 5: Выбираем Category
        if not await self._select_dropdown_option("appointment category", Config.VFS_CATEGORY):
            return False, "Не удалось выбрать Category", None

        await self._delay(1.5, 3.0)

        # Шаг 6: Выбираем Sub-category
        if not await self._select_dropdown_option("sub-category", subcategory):
            return False, f"Не удалось выбрать Sub-category: {subcategory}", None

        await self._delay(2.0, 4.0)

        # Шаг 7: Читаем результат — двойная проверка (API + DOM)
        # Сначала проверяем перехваченные API-ответы
        if self.interceptor.has_data:
            api_result = await self.interceptor.check_api_slots()
            if api_result.available:
                screenshot = await self._screenshot("slots_found")
                info = f"API: слоты найдены!"
                if api_result.earliest_date:
                    info += f" Ближайшая дата: {api_result.earliest_date}"
                if api_result.dates:
                    info += f" Даты: {', '.join(api_result.dates[:5])}"
                logger.info("СЛОТЫ (API): %s", info)
                return True, info, screenshot

        text = await self._text()

        # Проверяем банер "нет слотов"
        if NO_SLOTS_TEXT in text:
            logger.info("Нет слотов для '%s'", subcategory)
            return False, "Нет слотов", None

        # Проверяем активность кнопки Continue
        continue_disabled = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button, input[type="submit"]');
                for (const b of btns) {
                    if (b.textContent.toLowerCase().includes('continue')) {
                        return b.disabled || b.classList.contains('disabled') ||
                               b.getAttribute('aria-disabled') === 'true';
                    }
                }
                return null;
            })()
        """)

        if continue_disabled is False:
            # Continue АКТИВНА = слоты ЕСТЬ!
            screenshot = await self._screenshot("slots_found")
            logger.info("СЛОТЫ НАЙДЕНЫ для '%s'!", subcategory)

            # Пытаемся вытащить дату/время если видно
            date_info = await self.page.evaluate("""
                (() => {
                    const text = document.body.innerText;
                    const lines = text.split('\\n').filter(l => l.trim());
                    const dateLines = lines.filter(l => {
                        const low = l.toLowerCase();
                        return low.includes('date') || low.includes('time') ||
                               low.includes('slot') || low.includes('available') ||
                               /\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{2,4}/.test(l) ||
                               /\\d{1,2}\\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(l);
                    });
                    return dateLines.slice(0, 10).join('\\n');
                })()
            """)

            info = date_info if date_info else "Слоты доступны — кнопка Continue активна!"
            return True, info, screenshot

        if continue_disabled is True:
            # Continue ЗАБЛОКИРОВАНА но банера нет — странно, но нет слотов
            logger.info("Continue disabled, банера нет — нет слотов для '%s'", subcategory)
            return False, "Continue disabled", None

        # Continue не найдена — проверяем текст
        if "no appointment" in text or "sorry" in text:
            return False, "Нет слотов (текст)", None

        # Неясно — скриншот для анализа
        screenshot = await self._screenshot("unclear")
        return False, f"Неясный результат: {text[:200]}", screenshot

    async def _ensure_dashboard(self) -> bool:
        """Убеждаемся что мы на dashboard с кнопкой Start New Booking."""
        state = await self._page_state()

        if state == "dashboard":
            return True

        if state in ("appointment_form",):
            # Уже на форме — идём назад на dashboard
            base = Config.VFS_URL.replace("/login", "")
            await self.page.get(f"{base}/dashboard")
            await self._delay(2, 4)
            if not await self._handle_obstacle():
                return False
            return (await self._page_state()) == "dashboard"

        if state == "login":
            self.logged_in = False
            if not await self.login():
                return False
            return await self._ensure_dashboard()

        if state in ("cloudflare", "captcha"):
            if not await self._handle_obstacle():
                return False
            return await self._ensure_dashboard()

        # logged_in или unknown — пробуем перейти на dashboard
        base = Config.VFS_URL.replace("/login", "")
        await self.page.get(f"{base}/dashboard")
        await self._delay(2, 4)
        if not await self._handle_obstacle():
            return False
        state = await self._page_state()
        if state == "dashboard":
            return True

        # Последняя попытка
        logger.warning("Не могу попасть на dashboard (state=%s)", state)
        await self._screenshot("no_dashboard")
        return False

    # ── Utility ────────────────────────────────────────────────────

    async def _find_input(self, selectors: list[str]):
        for s in selectors:
            try:
                el = await self.page.query_selector(s)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _click(self, selectors: list[str]) -> bool:
        for s in selectors:
            try:
                el = await self.page.query_selector(s)
                if el:
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    def session_alive(self) -> bool:
        if not self.logged_in:
            return False
        return (time.time() - self.last_login_time) < 1800
