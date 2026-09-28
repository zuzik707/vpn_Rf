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
from dom_dumper import dump_page
from human_clicker import HumanClicker
from network_interceptor import NetworkInterceptor
from session_warmer import SessionWarmer

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
        self.hc: HumanClicker | None = None
        self.warmer: SessionWarmer | None = None
        self.logged_in = False
        self.on_dashboard = False
        self.last_login_time: float = 0
        self.cf_fail_count: int = 0

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
                // 1. data-sitekey на виджете
                let el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                el = document.querySelector('.cf-turnstile[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // 2. VFS оборачивает в <app-cloudflare-captcha-container> → <div appcloudflarerecaptcha>
                el = document.querySelector('[appcloudflarerecaptcha] [data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // 3. iframe URL
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (iframe) {
                    const m = iframe.src.match(/[?&]k=([^&]+)/);
                    if (m) return m[1];
                }
                // 4. Inline script
                for (const s of document.querySelectorAll('script')) {
                    if (!s.textContent) continue;
                    const m = s.textContent.match(/sitekey['"]?\\s*[:=]\\s*['"]([0-9a-zA-Z_-]{20,})['"]/);
                    if (m) return m[1];
                }
                // 5. Turnstile render call
                for (const s of document.querySelectorAll('script')) {
                    if (!s.textContent) continue;
                    const m = s.textContent.match(/turnstile\\.render[^}]*sitekey['"]?\\s*:\\s*['"]([^'"]+)['"]/);
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
        token = await asyncio.to_thread(self.captcha_solver.solve_turnstile, sitekey, page_url)
        if not token:
            return False

        safe = token.replace("\\", "\\\\").replace("'", "\\'")
        await self.page.evaluate(f"""
            (() => {{
                // VFS: hidden input name="cf-turnstile-response" внутри
                // <app-cloudflare-captcha-container>
                const names = ['cf-turnstile-response','g-recaptcha-response'];
                for (const n of names) {{
                    const el = document.querySelector('[name="'+n+'"]') || document.getElementById(n);
                    if (el) {{
                        el.value = '{safe}';
                        el.dispatchEvent(new Event('input', {{bubbles:true}}));
                        el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                }}
                // Также VFS может иметь id вида cf-chl-widget-XXXX_response
                document.querySelectorAll('input[id*="cf-chl-widget"]').forEach(el => {{
                    el.value = '{safe}';
                    el.dispatchEvent(new Event('input', {{bubbles:true}}));
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                }});
                // Вызываем callback'и
                const callbacks = [
                    'tsCallback','turnstileCallback','onTurnstileSuccess',
                    'cfCallback','captchaCallback'
                ];
                for (const cb of callbacks) {{
                    if (typeof window[cb]==='function') try {{ window[cb]('{safe}'); }} catch(e) {{}}
                }}
                const widgets = document.querySelectorAll('.cf-turnstile[data-callback], [appcloudflarerecaptcha] [data-callback]');
                widgets.forEach(w => {{
                    const fn = window[w.getAttribute('data-callback')];
                    if (typeof fn==='function') try {{ fn('{safe}'); }} catch(e) {{}}
                }});
                // Angular: trigger change detection
                if (window.ng) {{
                    try {{
                        const appRef = window.ng.getComponent(document.querySelector('app-root'));
                        if (appRef) appRef.detectChanges?.();
                    }} catch(e) {{}}
                }}
            }})()
        """)
        logger.info("Turnstile token инжектирован")
        await self._delay(1.5, 3.0)
        return True

    async def _handle_obstacle(self) -> bool:
        """Обработка любого препятствия (CF/captcha/blocked) с fail-forward."""
        state = await self._page_state()
        if state == "blocked":
            logger.error("BLOCKED!")
            await self._screenshot("blocked")
            await dump_page(self.page, "blocked")
            self.cf_fail_count += 3
            return False
        if state == "cloudflare":
            if not await self._wait_cloudflare():
                self.cf_fail_count += 1
                if self.cf_fail_count >= 3:
                    backoff = min(60 * self.cf_fail_count, 600)
                    logger.warning("CF fail-forward: %d fails, backoff %ds", self.cf_fail_count, backoff)
                    await asyncio.sleep(backoff)
                return False
            self.cf_fail_count = 0
            await inject_stealth(self.page)
            state = await self._page_state()
        if state == "captcha":
            if not await self._solve_turnstile():
                self.cf_fail_count += 1
                return False
            self.cf_fail_count = 0
            await self._delay(2, 4)
        return True

    @property
    def should_backoff(self) -> bool:
        """True если много CF fails — main.py должен увеличить интервал."""
        return self.cf_fail_count >= 3

    # ── Select dropdown by visible text ────────────────────────────

    # formcontrolname → mat-select ID (из реального F12 HTML)
    DROPDOWN_MAP = {
        "centre": "centerCode",
        "category": "selectedSubvisaCategory",
        "subcategory": "visaCategoryCode",
    }

    async def _select_dropdown_option(self, dropdown_label: str, option_text: str) -> bool:
        """
        Выбираем option в mat-select dropdown.
        VFS использует Angular Material — подтверждено из F12.
        Точные formcontrolname: centerCode, selectedSubvisaCategory, visaCategoryCode.
        """
        logger.info("Выбираю '%s' в '%s'...", option_text, dropdown_label)

        # Определяем formcontrolname по label
        fcn = None
        label_low = dropdown_label.lower()
        if "centre" in label_low or "center" in label_low:
            fcn = self.DROPDOWN_MAP["centre"]
        elif "sub" in label_low:
            fcn = self.DROPDOWN_MAP["subcategory"]
        elif "category" in label_low:
            fcn = self.DROPDOWN_MAP["category"]

        # PRIMARY: прямой доступ по formcontrolname (самый надёжный)
        if fcn:
            ok = await self._select_mat_by_fcn(fcn, option_text)
            if ok:
                logger.info("mat-select[%s] OK: '%s'", fcn, option_text)
                await self._delay(1.0, 2.5)
                return True

        # FALLBACK 1: поиск по тексту label'а
        mat_ok = await self._select_mat_by_label(dropdown_label, option_text)
        if mat_ok:
            logger.info("mat-select (label) OK: '%s'", option_text)
            await self._delay(1.0, 2.5)
            return True

        # FALLBACK 2: обычные HTML <select>
        logger.info("mat-select не сработал — пробую HTML <select>...")
        html_ok = await self._select_html_dropdown(option_text)
        if html_ok:
            logger.info("HTML select OK: '%s'", option_text)
            await self._delay(1.0, 2.5)
            return True

        logger.warning("Не удалось выбрать '%s'", option_text)
        await self._screenshot(f"dropdown_fail_{dropdown_label[:10]}")
        await dump_page(self.page, f"dropdown_fail_{dropdown_label[:10]}")
        return False

    async def _select_mat_by_fcn(self, formcontrolname: str, option_text: str) -> bool:
        """Точный путь: находим mat-select по formcontrolname, кликаем, выбираем option."""
        safe_fcn = formcontrolname.replace("'", "\\'")
        safe_option = option_text.replace("'", "\\'")

        # Проверяем — может уже выбрано нужное значение?
        already = await self.page.evaluate(f"""
            (() => {{
                const sel = document.querySelector('mat-select[formcontrolname="{safe_fcn}"]');
                if (!sel) return null;
                const val = sel.querySelector('.mat-mdc-select-min-line');
                return val ? val.textContent.trim() : null;
            }})()
        """)
        if already and already.lower().strip() == option_text.lower().strip():
            logger.info("Уже выбрано: '%s'", already)
            return True

        # Кликаем mat-select чтобы открыть overlay
        el = await self.page.query_selector(f'mat-select[formcontrolname="{safe_fcn}"]')
        if not el:
            logger.debug("mat-select[formcontrolname=%s] не найден", formcontrolname)
            return False

        if self.hc:
            await self.hc.scroll_into_view(el)
            await self.hc.click(el)
        else:
            await el.click()
        await self._delay(0.4, 0.8)

        # Выбираем нужную опцию из overlay
        selected = await self.page.evaluate(f"""
            (() => {{
                const target = '{safe_option}'.toLowerCase();
                const opts = document.querySelectorAll(
                    'mat-option, [role="option"], .mat-mdc-option'
                );
                for (const o of opts) {{
                    if (o.textContent.trim().toLowerCase().includes(target)) {{
                        o.click();
                        return true;
                    }}
                }}
                // overlay panel fallback
                const panels = document.querySelectorAll(
                    '.cdk-overlay-pane, .mat-mdc-select-panel'
                );
                for (const p of panels) {{
                    for (const item of p.querySelectorAll('mat-option, [role="option"]')) {{
                        if (item.textContent.trim().toLowerCase().includes(target)) {{
                            item.click();
                            return true;
                        }}
                    }}
                }}
                return false;
            }})()
        """)

        if not selected:
            await self.page.evaluate("document.body.click()")
            await self._delay(0.2, 0.4)

        return bool(selected)

    async def _select_mat_by_label(self, label_text: str, option_text: str) -> bool:
        """Fallback: поиск mat-select по тексту label'а рядом."""
        safe_label = label_text.replace("'", "\\'")
        safe_option = option_text.replace("'", "\\'")

        found = await self.page.evaluate(f"""
            (() => {{
                const label = '{safe_label}'.toLowerCase();
                const allText = document.querySelectorAll('label, span, div, p, mat-label');
                let targetContainer = null;
                for (const el of allText) {{
                    if (el.textContent.toLowerCase().includes(label)) {{
                        targetContainer = el.closest('.form-group, .mat-mdc-form-field');
                        if (targetContainer) break;
                    }}
                }}
                if (!targetContainer) return 'no_container';
                const dropdown = targetContainer.querySelector(
                    'mat-select, [role="combobox"]'
                );
                if (!dropdown) return 'no_dropdown';
                dropdown.click();
                return 'clicked';
            }})()
        """)
        if found != "clicked":
            return False

        await self._delay(0.4, 0.8)

        selected = await self.page.evaluate(f"""
            (() => {{
                const target = '{safe_option}'.toLowerCase();
                const opts = document.querySelectorAll(
                    'mat-option, [role="option"], .mat-mdc-option'
                );
                for (const o of opts) {{
                    if (o.textContent.trim().toLowerCase().includes(target)) {{
                        o.click();
                        return true;
                    }}
                }}
                return false;
            }})()
        """)

        if not selected:
            await self.page.evaluate("document.body.click()")
            await self._delay(0.2, 0.4)

        return bool(selected)

    async def _select_html_dropdown(self, option_text: str) -> bool:
        """Fallback: обычные HTML <select> элементы."""
        return await self.page.evaluate(f"""
            (() => {{
                const target = '{option_text.replace("'", "\\'")}';
                const selects = document.querySelectorAll('select');
                for (const sel of selects) {{
                    for (const opt of sel.options) {{
                        if (opt.text.toLowerCase().includes(target.toLowerCase())) {{
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

    # ── Login flow ─────────────────────────────────────────────────

    async def login(self) -> bool:
        if not self.browser:
            await self.start_browser()

        self.page = await self.browser.get("about:blank")
        await setup_stealth_on_new_page(self.page)
        self.hc = HumanClicker(self.page)
        self.warmer = SessionWarmer(self.page, self.hc)
        await self._delay(0.5, 1.0)

        # Session warming: homepage → country → login (не прямой /login)
        logger.info("Session warming → %s", Config.VFS_URL)
        warmed = await self.warmer.warm()
        if not warmed:
            logger.warning("Warming failed, прямой заход")
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
                # HONEYPOT ALERT: VFS имеет скрытые input'ы с class="d-none"
                # #username, #username1, #password1 — ловушки!
                # Реальные поля: #email (formcontrolname="username"), #password
                email_el = await self._find_visible_input([
                    "input#email:not(.d-none)",
                    "input[formcontrolname='username']:not(.d-none)",
                    "input[formcontrolname='username']:not([aria-hidden='true'])",
                ])
                if not email_el:
                    # Ultra-fallback: ищем видимый email input через JS
                    email_el = await self.page.evaluate("""
                        (() => {
                            const inputs = document.querySelectorAll('input');
                            for (const inp of inputs) {
                                if (inp.classList.contains('d-none') ||
                                    inp.getAttribute('aria-hidden') === 'true' ||
                                    inp.offsetParent === null) continue;
                                if (inp.type === 'email' || inp.id === 'email' ||
                                    inp.getAttribute('formcontrolname') === 'username' ||
                                    (inp.placeholder && inp.placeholder.includes('@'))) {
                                    return true;
                                }
                            }
                            return false;
                        })()
                    """)
                    if email_el:
                        email_el = await self._find_visible_input([
                            "input#email:not(.d-none)",
                            "input[formcontrolname='username']:not(.d-none)",
                        ])
                if not email_el:
                    await self._screenshot("no_email")
                    return False

                if self.hc:
                    await self.hc.scroll_into_view(email_el)
                    await self.hc.click(email_el)
                else:
                    await email_el.click()
                await self._delay(0.3, 0.6)
                await self._human_type(email_el, Config.VFS_EMAIL)
                await self._delay(0.6, 1.2)

                # Реальный password: #password (не #password1 — honeypot!)
                pwd_el = await self._find_visible_input([
                    "input#password:not(.d-none)",
                    "input[formcontrolname='password']:not(.d-none)",
                    "input[formcontrolname='password'][type='password']:not([aria-hidden='true'])",
                ])
                if not pwd_el:
                    await self._screenshot("no_pwd")
                    return False

                if self.hc:
                    await self.hc.click(pwd_el)
                else:
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

                # Submit — кнопка "Sign In" (mat-stroked-button btn-brand-orange)
                if not await self._click([
                    "button.btn-brand-orange",
                    "button[mat-stroked-button]",
                    "button.mat-mdc-outlined-button",
                ]):
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

            if state in ("dashboard", "logged_in", "appointment_form"):
                break

        # Проверяем что реально залогинились
        final_state = await self._page_state()
        if final_state in ("login", "cloudflare", "captcha", "blocked"):
            logger.error("Логин не удался (final_state=%s)", final_state)
            await self._screenshot("login_failed")
            return False

        self.logged_in = True
        self.on_dashboard = final_state == "dashboard"
        self.last_login_time = time.time()
        try:
            await self.interceptor.attach(self.page)
        except Exception as e:
            logger.warning("Interceptor attach failed: %s", e)
        logger.info("Login OK (state=%s)", final_state)
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
        # Из реального F12: button.btn-brand-orange.mat-mdc-raised-button
        await self._delay(1, 2)

        # PRIMARY: HumanClicker на реальную кнопку
        snb_el = await self._find_input([
            "button.btn-brand-orange.mat-mdc-raised-button",
            "button.btn-brand-orange",
            "button[mat-raised-button].btn-brand-orange",
        ])
        if snb_el and self.hc:
            await self.hc.micro_scroll()
            await self.hc.click(snb_el, dwell=random.uniform(0.1, 0.3))
            clicked = True
        elif snb_el:
            await snb_el.click()
            clicked = True
        else:
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

        # Шаг 4: Выбираем Centre (formcontrolname="centerCode")
        if self.hc:
            await self.hc.micro_scroll()
        if not await self._select_dropdown_option("Application Centre", Config.VFS_CENTRE):
            return False, "Не удалось выбрать Centre", None

        await self._delay(1.5, 3.0)

        # Шаг 5: Выбираем Category (formcontrolname="selectedSubvisaCategory")
        if self.hc:
            await self.hc.idle_drift(random.uniform(0.3, 0.8))
        if not await self._select_dropdown_option("appointment category", Config.VFS_CATEGORY):
            return False, "Не удалось выбрать Category", None

        await self._delay(1.5, 3.0)

        # Шаг 6: Выбираем Sub-category (formcontrolname="visaCategoryCode")
        if self.hc:
            await self.hc.idle_drift(random.uniform(0.3, 0.8))
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

        # Проверяем банер "нет слотов" — div.alert с role="alert"
        if NO_SLOTS_TEXT in text:
            logger.info("Нет слотов для '%s'", subcategory)
            return False, "Нет слотов", None

        # Также проверяем по DOM (div[role="alert"] внутри .Information)
        no_slots_dom = await self.page.evaluate("""
            (() => {
                const alert = document.querySelector('.Information [role="alert"]');
                if (alert && alert.textContent.toLowerCase().includes('no appointment slots')) return true;
                return false;
            })()
        """)
        if no_slots_dom:
            logger.info("Нет слотов (DOM alert) для '%s'", subcategory)
            return False, "Нет слотов", None

        # Проверяем активность кнопки Continue
        # Из F12: button.btn-brand-orange.mat-mdc-raised-button с disabled="true"
        # и class mat-mdc-button-disabled когда нет слотов
        continue_disabled = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button.btn-brand-orange, button.mat-mdc-raised-button');
                for (const b of btns) {
                    if (b.textContent.toLowerCase().includes('continue')) {
                        return b.disabled || b.classList.contains('mat-mdc-button-disabled') ||
                               b.classList.contains('disabled') ||
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

        # Неясно — скриншот + DOM dump для анализа
        screenshot = await self._screenshot("unclear")
        await dump_page(self.page, "unclear")
        return False, f"Неясный результат: {text[:200]}", screenshot

    VFS_BASE = "https://visa.vfsglobal.com/uzb/en/lva"

    async def _ensure_dashboard(self, depth: int = 0) -> bool:
        """Убеждаемся что мы на dashboard с кнопкой Start New Booking."""
        if depth >= 3:
            logger.error("_ensure_dashboard: max depth reached")
            await self._screenshot("dashboard_loop")
            return False

        state = await self._page_state()

        if state == "dashboard":
            return True

        if state in ("appointment_form",):
            await self.page.get(f"{self.VFS_BASE}/dashboard")
            await self._delay(2, 4)
            if not await self._handle_obstacle():
                return False
            return (await self._page_state()) == "dashboard"

        if state == "login":
            self.logged_in = False
            if not await self.login():
                return False
            return await self._ensure_dashboard(depth + 1)

        if state in ("cloudflare", "captcha"):
            if not await self._handle_obstacle():
                return False
            return await self._ensure_dashboard(depth + 1)

        # logged_in или unknown — пробуем перейти на dashboard
        await self.page.get(f"{self.VFS_BASE}/dashboard")
        await self._delay(2, 4)
        if not await self._handle_obstacle():
            return False
        state = await self._page_state()
        if state == "dashboard":
            return True

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

    async def _find_visible_input(self, selectors: list[str]):
        """Находит input, пропуская honeypot'ы (d-none, aria-hidden, offsetParent null)."""
        for s in selectors:
            try:
                el = await self.page.query_selector(s)
                if not el:
                    continue
                # Дополнительная проверка видимости через JS
                visible = await self.page.evaluate("""
                    (sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        if (el.classList.contains('d-none')) return false;
                        if (el.getAttribute('aria-hidden') === 'true') return false;
                        if (el.offsetParent === null && el.style.position !== 'fixed') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }
                """, s)
                if visible:
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
