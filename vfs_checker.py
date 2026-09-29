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
import hashlib
import json
import logging
import os
import random
import shutil
import subprocess
import time

import nodriver as uc

from antidetect import get_chrome_args, setup_stealth_on_new_page
from config import Config
from dom_dumper import dump_page
from human_clicker import HumanClicker
from network_interceptor import NetworkInterceptor
from local_proxy import start_local_proxy, get_local_proxy_url, DEFAULT_PORT
from notifier import send_telegram_photo
from otp_reader import get_vfs_otp
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
    "access restricted",
    "permission issues",
    "too many requests",
    "rate limit",
    "temporarily blocked",
    "account has been suspended",
    "account has been blocked",
    "restricted for user id",
]


class VFSBrowser:
    def __init__(self, captcha_solver=None, email: str = "", password: str = "",
                 proxy_url: str = "", worker_id: int = 0):
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None
        self.captcha_solver = captcha_solver
        self.email = email or Config.VFS_EMAIL
        self.password = password or Config.VFS_PASSWORD
        self.proxy_url = proxy_url or Config.PROXY_URL
        self._assign_sticky_session()
        self.worker_id = worker_id
        self._browser_data_dir = os.path.join(
            os.path.dirname(__file__), f"browser_data_{worker_id}")
        self._cookies_path = os.path.join(
            os.path.dirname(__file__), f"cookies_{worker_id}.json")
        self._profile_path = os.path.join(
            os.path.dirname(__file__), f"browser_profile_{worker_id}.json")
        self.interceptor = NetworkInterceptor()
        self._local_proxy_server: asyncio.Server | None = None
        self.hc: HumanClicker | None = None
        self.warmer: SessionWarmer | None = None
        self.logged_in = False
        self.on_dashboard = False
        self._on_appointment_form = False
        self.last_login_time: float = 0
        self._session_ttl: float = random.uniform(1500, 2400)
        self.cf_fail_count: int = 0
        self.banned: bool = False
        self.ban_reason: str = ""

    def _assign_sticky_session(self) -> None:
        """Assign a unique Bright Data sticky session based on email hash.
        Each account gets its own persistent IP."""
        if not self.proxy_url:
            return
        if "brd.superproxy.io" not in self.proxy_url and "brightdata" not in self.proxy_url:
            return
        import re
        base = re.sub(r'-session-[^:@]+', '', self.proxy_url)
        session_id = hashlib.md5(self.email.encode()).hexdigest()[:12]
        at_idx = base.find('@')
        if at_idx == -1:
            return
        colon_idx = base.rfind(':', 0, at_idx)
        self.proxy_url = base[:colon_idx] + f"-session-{session_id}" + base[colon_idx:]

    def set_credentials(self, email: str, password: str) -> None:
        self.email = email
        self.password = password

    # ── Proxy health ────────────────────────────────────────────────

    async def _check_proxy_health(self) -> bool:
        """Verify proxy works — test against VFS domain directly."""
        if not self.proxy_url:
            return True
        try:
            import subprocess
            proxy_for_curl = self.proxy_url
            if "://" not in proxy_for_curl:
                proxy_for_curl = f"http://{proxy_for_curl}"
            result = subprocess.run(
                ["curl", "-x", proxy_for_curl, "-s", "--max-time", "15",
                 "-o", "/dev/null", "-w", "%{http_code}",
                 "https://visa.vfsglobal.com/uzb/en/lva"],
                capture_output=True, text=True, timeout=20,
            )
            code = result.stdout.strip()
            if result.returncode == 0 and code and code != "000":
                logger.info("[W%d] Proxy OK (VFS HTTP %s)", self.worker_id, code)
                return True
            logger.warning("[W%d] Proxy check failed (rc=%d, code=%s): %s",
                          self.worker_id, result.returncode, code, result.stderr[:100])
            return False
        except Exception as e:
            logger.warning("[W%d] Proxy check error: %s", self.worker_id, e)
            return False

    def _rotate_proxy_session(self) -> None:
        """Rotate Bright Data session by changing session ID in proxy URL."""
        if "brd.superproxy.io" not in self.proxy_url and "brightdata" not in self.proxy_url:
            return
        import re
        base = re.sub(r'-session-[^:@]+', '', self.proxy_url)
        session_id = f"sess{random.randint(100000, 999999)}"
        at_idx = base.find('@')
        if at_idx == -1:
            return
        colon_idx = base.rfind(':', 0, at_idx)
        self.proxy_url = base[:colon_idx] + f"-session-{session_id}" + base[colon_idx:]
        logger.info("[W%d] Bright Data session rotated → %s", self.worker_id, session_id)

    async def ensure_proxy_alive(self) -> bool:
        """Check proxy, rotate session if dead. Returns True if proxy is usable."""
        if not self.proxy_url:
            return True
        if await self._check_proxy_health():
            return True
        logger.warning("[W%d] Proxy dead — rotating session...", self.worker_id)
        self._rotate_proxy_session()
        await asyncio.sleep(3)
        if await self._check_proxy_health():
            return True
        logger.error("[W%d] Proxy still dead after session rotation", self.worker_id)
        return False

    # ── Browser lifecycle ──────────────────────────────────────────

    @staticmethod
    def _ensure_virtual_display() -> bool:
        """Start Xvfb virtual display if no DISPLAY is set.
        Cloudflare detects headless Chrome and blocks Turnstile rendering."""
        if os.environ.get("DISPLAY"):
            return True
        if not shutil.which("Xvfb"):
            logger.warning("Xvfb not found — installing...")
            try:
                subprocess.run(["apt-get", "install", "-y", "xvfb"],
                               capture_output=True, timeout=60)
            except Exception:
                pass
        if not shutil.which("Xvfb"):
            logger.error("Xvfb unavailable — Chrome will run headless (CF may block)")
            return False
        display_num = 99
        os.environ["DISPLAY"] = f":{display_num}"
        try:
            subprocess.Popen(
                ["Xvfb", f":{display_num}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            logger.info("Xvfb started on :%d — Chrome will run in headed mode", display_num)
            return True
        except Exception as e:
            logger.error("Xvfb start failed: %s — Chrome will run headless", e)
            del os.environ["DISPLAY"]
            return False

    async def start_browser(self) -> None:
        os.makedirs(self._browser_data_dir, exist_ok=True)
        os.makedirs(Config.SCREENSHOT_DIR, exist_ok=True)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = os.path.join(self._browser_data_dir, lock)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
        args = get_chrome_args(self._profile_path)
        self._proxy_auth = None
        if self.proxy_url:
            proxy_for_chrome, self._proxy_auth = self._parse_proxy_url(self.proxy_url)
            if self._proxy_auth:
                local_port = DEFAULT_PORT + self.worker_id
                if not self._local_proxy_server:
                    self._local_proxy_server = await start_local_proxy(self.proxy_url, local_port)
                chrome_proxy = f"http://127.0.0.1:{local_port}"
                args.append(f"--proxy-server={chrome_proxy}")
                logger.info("[W%d] Proxy via local bridge: %s → %s",
                            self.worker_id, chrome_proxy, proxy_for_chrome)
            else:
                args.append(f"--proxy-server={proxy_for_chrome}")
                logger.info("[W%d] Proxy (direct): %s", self.worker_id, proxy_for_chrome)
        has_display = bool(os.environ.get("DISPLAY"))
        if not has_display:
            has_display = self._ensure_virtual_display()
        use_headless = not has_display
        if use_headless:
            logger.warning("No virtual display — headless mode (CF may block Turnstile)")
        self.browser = await uc.start(
            user_data_dir=self._browser_data_dir,
            headless="new" if use_headless else False,
            lang="en-US",
            browser_args=args,
            no_sandbox=True,
        )
        logger.info("[W%d] Chrome запущен (%s)", self.worker_id, self.email)

    @staticmethod
    def _parse_proxy_url(url: str) -> tuple[str, tuple[str, str] | None]:
        """Extract auth from proxy URL. Chrome --proxy-server ignores credentials."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        auth = None
        if parsed.username:
            auth = (parsed.username, parsed.password or "")
            clean = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        else:
            clean = url
        return clean, auth

    async def _setup_proxy_auth(self, page, username: str, password: str) -> None:
        """No-op — proxy auth handled by local proxy bridge."""
        pass

    async def _save_cookies(self) -> None:
        if not self.page:
            return
        try:
            import nodriver.cdp.network as net_cdp
            cookies = await self.page.send(net_cdp.get_cookies())
            serializable = []
            for c in cookies:
                serializable.append({
                    "name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path, "expires": c.expires, "httpOnly": c.http_only,
                    "secure": c.secure, "sameSite": c.same_site.value if c.same_site else None,
                })
            with open(self._cookies_path, "w") as f:
                json.dump(serializable, f)
            os.chmod(self._cookies_path, 0o600)
            logger.debug("Saved %d cookies", len(serializable))
        except Exception as e:
            logger.debug("Cookie save failed: %s", e)

    async def _restore_cookies(self) -> None:
        if not self.page or not os.path.exists(self._cookies_path):
            return
        try:
            import nodriver.cdp.network as net_cdp
            with open(self._cookies_path) as f:
                cookies = json.load(f)
            for c in cookies:
                try:
                    kwargs = dict(
                        name=c["name"], value=c["value"], domain=c.get("domain"),
                        path=c.get("path", "/"), expires=c.get("expires"),
                        http_only=c.get("httpOnly", False), secure=c.get("secure", False),
                    )
                    if c.get("sameSite"):
                        kwargs["same_site"] = c["sameSite"]
                    await self.page.send(net_cdp.set_cookie(**kwargs))
                except Exception:
                    pass
            logger.info("Restored %d cookies from disk", len(cookies))
        except Exception as e:
            logger.debug("Cookie restore failed: %s", e)

    async def close_browser(self) -> None:
        await self._save_cookies()
        await self.interceptor.detach()
        if self._local_proxy_server:
            self._local_proxy_server.close()
            self._local_proxy_server = None
        if self.browser:
            try:
                result = self.browser.stop()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
            try:
                import subprocess
                subprocess.run(
                    ["pkill", "-f", f"chrome.*{self._browser_data_dir}"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        self.browser = None
        self.page = None
        self.logged_in = False
        self.on_dashboard = False
        self._on_appointment_form = False

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

    async def _screenshot(self, tag: str, send_tg: bool = False) -> str:
        path = os.path.join(Config.SCREENSHOT_DIR, f"{tag}_{int(time.time())}.png")
        try:
            await self.page.save_screenshot(path)
            logger.debug("Screenshot: %s", path)
            if send_tg and os.path.exists(path):
                send_telegram_photo(path, f"[W{self.worker_id}] {tag}")
        except Exception as e:
            logger.debug("Screenshot failed (%s): %s", tag, e)
        self._cleanup_screenshots()
        return path

    @staticmethod
    def _cleanup_screenshots(max_files: int = 50) -> None:
        """Keep only the latest screenshots to avoid filling disk."""
        try:
            sdir = Config.SCREENSHOT_DIR
            files = sorted(
                (os.path.join(sdir, f) for f in os.listdir(sdir) if f.endswith(".png")),
                key=os.path.getmtime,
            )
            for old in files[:-max_files]:
                os.remove(old)
        except Exception:
            pass

    async def _url(self) -> str:
        try:
            return (await self.page.evaluate("window.location.href")) or ""
        except Exception:
            return ""

    async def _dismiss_cookie_banner(self) -> None:
        """Click 'Accept All Cookies' if the cookie consent banner is present."""
        try:
            dismissed = await self.page.evaluate("""
                (() => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        const t = b.textContent.trim().toLowerCase();
                        if (t.includes('accept all') || t.includes('accept cookies') ||
                            t === 'accept' || t.includes('agree')) {
                            b.click();
                            return true;
                        }
                    }
                    // Also try OneTrust / CookiePro patterns
                    const accept = document.querySelector('#onetrust-accept-btn-handler, .accept-all-btn, [data-testid="accept-all"]');
                    if (accept) { accept.click(); return true; }
                    return false;
                })()
            """)
            if dismissed:
                logger.info("Cookie banner dismissed")
                await self._delay(0.5, 1.0)
        except Exception:
            pass

    async def _page_state(self) -> str:
        """Single CDP round-trip to determine current page state."""
        blocked_json = json.dumps(BLOCKED_MARKERS)
        cf_json = json.dumps(CLOUDFLARE_MARKERS)
        try:
            result = await self.page.evaluate("""
                (() => {
                    const url = window.location.href.toLowerCase();
                    const html = (document.documentElement.outerHTML || '').toLowerCase();
                    const text = (document.body.innerText || '').toLowerCase();
                    const both = html + ' ' + text;
                    const blocked = """ + blocked_json + """;
                    const cfMarkers = """ + cf_json + """;
                    if (blocked.some(m => both.includes(m))) return 'blocked';
                    if (both.includes('session expired') || both.includes('session invalid') ||
                        both.includes('session has expired'))
                        return 'session_expired';
                    // Login page with embedded Turnstile — detect as 'login', not 'cloudflare'
                    const hasLoginForm = url.includes('/login') &&
                        document.querySelector('input[type="password"]');
                    if (hasLoginForm) return 'login';
                    const hasSNB = [...document.querySelectorAll('button, a')]
                        .some(e => e.textContent.toLowerCase().includes('start new booking'));
                    if (hasSNB) return 'dashboard';
                    if (url.includes('/dashboard')) return 'dashboard';
                    if (url.includes('/appointment') || url.includes('/book')) {
                        const n = document.querySelectorAll(
                            'select, mat-select, [role="combobox"]'
                        ).length;
                        if (n >= 2) return 'appointment_form';
                    }
                    if (both.includes('appointment details') && both.includes('choose your'))
                        return 'appointment_form';
                    if (both.includes('start new booking')) return 'dashboard';
                    if ((both.includes('sign in') || both.includes('log in')) &&
                        both.includes('password')) return 'login';
                    if (both.includes('sign out') || both.includes('my account'))
                        return 'logged_in';
                    // CF interstitial (no login form, no dashboard — pure challenge page)
                    if (url.includes('challenges.cloudflare.com') || url.includes('/cdn-cgi/'))
                        return 'cloudflare';
                    if (cfMarkers.some(m => both.includes(m))) return 'cloudflare';
                    const hasCaptcha = !!(
                        document.querySelector('.cf-turnstile') ||
                        document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
                        document.querySelector('[data-sitekey]') ||
                        document.querySelector('.h-captcha') ||
                        document.querySelector('.g-recaptcha')
                    );
                    if (hasCaptcha) return 'captcha';
                    return 'unknown';
                })()
            """)
            return result or "unknown"
        except Exception:
            return "unknown"

    # ── Cloudflare / CAPTCHA ───────────────────────────────────────

    async def _wait_cloudflare(self, timeout: int = 120) -> bool:
        logger.info("Cloudflare challenge — ждём до %dс...", timeout)
        t0 = time.time()
        click_count = 0
        max_clicks = 5
        widget_ready = False
        solve_attempted = False
        while time.time() - t0 < timeout:
            if self.hc:
                await self.hc.idle_drift(random.uniform(2.0, 4.0))
            else:
                await asyncio.sleep(3)
            state = await self._page_state()
            if state not in ("cloudflare", "captcha"):
                logger.info("Cloudflare пройден (%.0fс)", time.time() - t0)
                await self._screenshot("cf_passed")
                return True
            elapsed = time.time() - t0
            # Wait for Turnstile widget to become visible (CF shows "please wait" first)
            if not widget_ready:
                widget_ready = await self._is_turnstile_visible()
                if widget_ready:
                    logger.info("CF: Turnstile widget visible after %.0fс", elapsed)
                elif elapsed > 10 and int(elapsed) % 10 == 0:
                    logger.debug("CF: waiting for widget to render (%.0fс)...", elapsed)
                continue
            # Widget is visible — try clicking
            if click_count < max_clicks:
                click_count += 1
                logger.info("CF: trying Turnstile click (%d/%d)...", click_count, max_clicks)
                if await self._try_turnstile_click():
                    if await self._page_state() not in ("cloudflare", "captcha"):
                        logger.info("Cloudflare пройден кликом (%.0fс)", time.time() - t0)
                        return True
                # Wait before next click
                extra_wait = random.uniform(5, 10)
                await asyncio.sleep(extra_wait)
            elif not solve_attempted:
                solve_attempted = True
                logger.info("CF: trying Turnstile API solve...")
                if await self._solve_turnstile():
                    if await self._page_state() not in ("cloudflare", "captcha"):
                        logger.info("Cloudflare пройден через API (%.0fс)", time.time() - t0)
                        return True
        logger.error("Cloudflare timeout")
        await self._screenshot("cf_fail")
        return False

    async def _is_turnstile_visible(self) -> bool:
        """Check if Turnstile widget has rendered (iframe OR container with visible size)."""
        try:
            return await self.page.evaluate("""
                (() => {
                    // 1. Cloudflare iframe with non-zero size
                    for (const f of document.querySelectorAll('iframe')) {
                        if (f.src && (f.src.includes('challenges.cloudflare.com') || f.src.includes('turnstile'))) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) return true;
                        }
                    }
                    // 2. Container with visible size (VFS Angular wraps Turnstile)
                    const containers = document.querySelectorAll(
                        '.cf-turnstile, [appcloudflarerecaptcha], app-cloudflare-captcha-container, [data-sitekey]'
                    );
                    for (const c of containers) {
                        const r = c.getBoundingClientRect();
                        if (r.width > 50 && r.height > 30) return true;
                        const iframe = c.querySelector('iframe');
                        if (iframe) {
                            const ir = iframe.getBoundingClientRect();
                            if (ir.width > 10 && ir.height > 10) return true;
                        }
                    }
                    return false;
                })()
            """) or False
        except Exception:
            return False

    async def _extract_sitekey(self) -> str | None:
        return await self.page.evaluate("""
            (() => {
                // 1. data-sitekey attribute
                let el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                el = document.querySelector('.cf-turnstile[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // 2. VFS Angular wrapper
                el = document.querySelector('[appcloudflarerecaptcha] [data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // Also check app-cloudflare-captcha-container
                el = document.querySelector('app-cloudflare-captcha-container [data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                // 3. iframe URL param
                for (const iframe of document.querySelectorAll('iframe')) {
                    if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) {
                        const m = iframe.src.match(/[?&]k=([^&]+)/);
                        if (m) return m[1];
                    }
                }
                // 4. window.turnstile state (if Turnstile JS is loaded)
                try {
                    if (window.turnstile && window.turnstile._widgets) {
                        for (const [k, v] of Object.entries(window.turnstile._widgets)) {
                            if (v && v.sitekey) return v.sitekey;
                        }
                    }
                } catch(e) {}
                // 5. Inline scripts — sitekey assignment
                for (const s of document.querySelectorAll('script')) {
                    if (!s.textContent) continue;
                    const m = s.textContent.match(/sitekey['"]?\\s*[:=]\\s*['"]([0-9a-zA-Z_-]{20,})['"]/);
                    if (m) return m[1];
                }
                // 6. Turnstile.render call
                for (const s of document.querySelectorAll('script')) {
                    if (!s.textContent) continue;
                    const m = s.textContent.match(/turnstile\\.render[^}]*sitekey['"]?\\s*:\\s*['"]([^'"]+)['"]/);
                    if (m) return m[1];
                }
                // 7. Check all elements with any sitekey-like attribute
                for (const el of document.querySelectorAll('*')) {
                    for (const attr of el.attributes || []) {
                        if (attr.name.toLowerCase().includes('sitekey') && attr.value.length > 15) {
                            return attr.value;
                        }
                    }
                }
                return null;
            })()
        """)

    async def _try_turnstile_click(self) -> bool:
        """Try clicking the Turnstile checkbox — iframe or container."""
        try:
            widget_info = await self.page.evaluate("""
                (() => {
                    // 1. Try visible iframe first
                    for (const f of document.querySelectorAll('iframe')) {
                        if (f.src && (f.src.includes('challenges.cloudflare.com') || f.src.includes('turnstile'))) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10)
                                return {x: r.x, y: r.y, w: r.width, h: r.height, type: 'iframe'};
                        }
                    }
                    // 2. Try Turnstile container (VFS wraps it in Angular component)
                    const containers = [
                        ...document.querySelectorAll('app-cloudflare-captcha-container'),
                        ...document.querySelectorAll('.cf-turnstile'),
                        ...document.querySelectorAll('[appcloudflarerecaptcha]'),
                        ...document.querySelectorAll('[data-sitekey]'),
                    ];
                    for (const c of containers) {
                        const r = c.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20)
                            return {x: r.x, y: r.y, w: r.width, h: r.height, type: 'container'};
                        // Check child elements
                        const inner = c.querySelector('div, iframe');
                        if (inner) {
                            const ir = inner.getBoundingClientRect();
                            if (ir.width > 20 && ir.height > 20)
                                return {x: ir.x, y: ir.y, w: ir.width, h: ir.height, type: 'inner'};
                        }
                    }
                    // 3. Any iframe at all (even without matching src)
                    for (const f of document.querySelectorAll('iframe')) {
                        const r = f.getBoundingClientRect();
                        if (r.width > 30 && r.height > 30) {
                            const parent = f.closest('app-cloudflare-captcha-container, .cf-turnstile, [appcloudflarerecaptcha]');
                            if (parent) return {x: r.x, y: r.y, w: r.width, h: r.height, type: 'parent-iframe'};
                        }
                    }
                    return null;
                })()
            """)
            if not widget_info or not isinstance(widget_info, dict):
                logger.debug("Turnstile widget not found in DOM")
                return False

            wtype = widget_info.get("type", "unknown")
            # Click the checkbox area (left side of the widget)
            cx = widget_info["x"] + min(widget_info["w"] * 0.15, 30)
            cy = widget_info["y"] + widget_info["h"] * 0.5
            logger.info("Turnstile %s at (%.0f,%.0f) size %dx%d, clicking (%.0f,%.0f)",
                        wtype, widget_info["x"], widget_info["y"],
                        widget_info["w"], widget_info["h"], cx, cy)

            if self.hc:
                await self.hc._move_to(cx, cy)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await self.hc._mouse_down(cx, cy)
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await self.hc._mouse_up(cx, cy)
            else:
                # Fallback: CDP Input.dispatchMouseEvent
                import nodriver.cdp.input_ as cdp_input
                await self.page.send(cdp_input.dispatch_mouse_event(
                    type_="mousePressed", x=cx, y=cy, button=cdp_input.MouseButton.LEFT,
                    click_count=1))
                await asyncio.sleep(0.08)
                await self.page.send(cdp_input.dispatch_mouse_event(
                    type_="mouseReleased", x=cx, y=cy, button=cdp_input.MouseButton.LEFT,
                    click_count=1))

            for _ in range(6):
                await asyncio.sleep(2)
                state = await self._page_state()
                if state not in ("captcha", "cloudflare"):
                    logger.info("Turnstile solved by click")
                    return True
                token_filled = await self.page.evaluate("""
                    (() => {
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 20;
                    })()
                """)
                if token_filled:
                    logger.info("Turnstile token filled after click")
                    return True
            return False
        except Exception as e:
            logger.debug("Turnstile click attempt failed: %s", e)
            return False

    async def _solve_turnstile(self) -> bool:
        if not self.captcha_solver:
            logger.error("captcha_solver не настроен — не могу решить Turnstile")
            return False
        # Check if Turnstile already solved itself (managed/invisible mode)
        token_exists = await self.page.evaluate("""
            (() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                return el && el.value && el.value.length > 20;
            })()
        """)
        if token_exists:
            logger.info("Turnstile токен уже заполнен — пропускаем решение")
            return True

        if await self._try_turnstile_click():
            return True

        sitekey = await self._extract_sitekey()
        if not sitekey:
            cf_debug = await self.page.evaluate("""
                (() => {
                    const iframes = [...document.querySelectorAll('iframe')].map(
                        f => ({src: f.src?.substring(0, 120), w: f.offsetWidth, h: f.offsetHeight,
                               cw: f.clientWidth, ch: f.clientHeight}));
                    const containers = document.querySelectorAll(
                        '.cf-turnstile, [appcloudflarerecaptcha], app-cloudflare-captcha-container, [data-sitekey]'
                    );
                    const cfs = [...containers].map(e => {
                        const r = e.getBoundingClientRect();
                        const children = [...e.children].map(c => c.tagName + '.' + c.className).slice(0, 5);
                        return {tag: e.tagName, cls: e.className, w: Math.round(r.width), h: Math.round(r.height),
                                children: children, innerHTML: e.innerHTML?.substring(0, 200)};
                    });
                    return JSON.stringify({iframes: iframes.slice(0, 5), containers: cfs.slice(0, 3)});
                })()
            """)
            logger.error("sitekey не найден — CF debug: %s", cf_debug)
            await self._screenshot("no_sitekey")
            return False

        page_url = await self.page.evaluate("window.location.href")

        token = None
        for attempt in range(3):
            logger.info("2Captcha: attempt %d/3 (sitekey=%s...)", attempt + 1, sitekey[:16])
            try:
                token = await asyncio.wait_for(
                    asyncio.to_thread(self.captcha_solver.solve_turnstile, sitekey, page_url),
                    timeout=150,
                )
            except asyncio.TimeoutError:
                logger.warning("2Captcha timeout on attempt %d", attempt + 1)
                token = None
            except Exception as e:
                logger.warning("2Captcha error on attempt %d: %s", attempt + 1, e)
                token = None
            if token:
                break
            if attempt < 2:
                backoff = (attempt + 1) * 5
                logger.info("Retry in %ds...", backoff)
                await asyncio.sleep(backoff)

        if not token:
            return False

        safe = (token
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("</", "<\\/"))
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
        """Обработка любого препятствия (CF/captcha/blocked/session_expired) с fail-forward."""
        state = await self._page_state()
        if state == "blocked":
            text = await self._text()
            logger.error("BLOCKED! %s", text[:200])
            await self._screenshot("blocked", send_tg=True)
            await dump_page(self.page, "blocked")
            self.cf_fail_count += 3
            # Detect VFS account ban
            if any(m in text for m in [
                "access restricted", "restricted for user id",
                "permission issues", "temporarily restricted",
            ]):
                self.banned = True
                self.ban_reason = text[:150].strip()
            return False
        if state == "session_expired":
            logger.warning("Session expired — clearing cookies and restarting")
            try:
                if os.path.exists(self._cookies_path):
                    os.remove(self._cookies_path)
                    logger.info("Stale cookies deleted: %s", self._cookies_path)
            except OSError:
                pass
            self.last_login_time = 0
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
            state = await self._page_state()
        if state == "captcha":
            if not await self._solve_turnstile():
                self.cf_fail_count += 1
                return False
            self.cf_fail_count = 0
            await self._delay(2, 4)
        return True

    async def _ensure_proxy_auth(self) -> None:
        """No-op — Fetch stays enabled for the entire session now."""
        pass

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
        el = await self.page.select(f'mat-select[formcontrolname="{safe_fcn}"]')
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
        safe_text = option_text.replace("\\", "\\\\").replace("'", "\\'")
        return await self.page.evaluate(f"""
            (() => {{
                const target = '{safe_text}';
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

        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        try:
            self.page = await self.browser.get("about:blank")
        except Exception as e:
            logger.warning("Browser connection lost (%s) — restarting", e)
            await self.close_browser()
            await self.start_browser()
            self.page = await self.browser.get("about:blank")
        await setup_stealth_on_new_page(self.page, self._profile_path)
        try:
            import nodriver.cdp.emulation as emu_cdp
            await self.page.send(emu_cdp.set_timezone_override(timezone_id="Asia/Tashkent"))
        except Exception as e:
            logger.debug("Timezone override failed: %s", e)
        if self._proxy_auth:
            await self._setup_proxy_auth(self.page, *self._proxy_auth)
        self.hc = HumanClicker(self.page)
        self.warmer = SessionWarmer(self.page, self.hc)
        await self._restore_cookies()
        await self._delay(0.5, 1.0)

        # Check proxy health before wasting time on navigation
        if not await self.ensure_proxy_alive():
            logger.error("[W%d] Proxy unusable — aborting login", self.worker_id)
            return False

        # Session warming: homepage → country → login (не прямой /login)
        logger.info("Session warming → %s", Config.VFS_URL)
        warmed = await self.warmer.warm()
        if not warmed:
            logger.warning("Warming failed, прямой заход")
            await self.page.get(Config.VFS_URL)
        await self._delay(5, 10)
        await self._screenshot("after_warming")

        # Check for proxy/network failure after warming
        url = await self._url()
        if "chrome-error" in url or "err_" in (await self._text()).lower():
            logger.error("[W%d] Page failed to load (proxy issue): %s", self.worker_id, url[:100])
            self._rotate_proxy_session()
            await asyncio.sleep(3)
            # Restart browser with new proxy session
            await self.close_browser()
            await self.start_browser()
            self.page = await self.browser.get("about:blank")
            await setup_stealth_on_new_page(self.page, self._profile_path)
            if self._proxy_auth:
                await self._setup_proxy_auth(self.page, *self._proxy_auth)
            self.hc = HumanClicker(self.page)
            self.warmer = SessionWarmer(self.page, self.hc)
            await self.page.get(Config.VFS_URL)
            await self._delay(3, 5)
            url = await self._url()
            if "chrome-error" in url:
                logger.error("[W%d] Still can't load after session rotation", self.worker_id)
                return False

        for attempt in range(4):
            state = await self._page_state()
            logger.info("State: %s (attempt %d)", state, attempt + 1)
            await self._screenshot(f"step_{attempt+1}_{state}")

            if state == "unknown" and attempt == 0:
                url = await self._url()
                text = await self._text()
                logger.info("DEBUG unknown — URL: %s", url[:200])
                logger.info("DEBUG unknown — text: %s", text[:300])

            if state == "blocked":
                return False

            if state == "session_expired":
                logger.warning("Session expired — удаляю куки и перезахожу")
                try:
                    if os.path.exists(self._cookies_path):
                        os.remove(self._cookies_path)
                except OSError:
                    pass
                await self.page.get(Config.VFS_URL)
                await self._delay(3, 5)
                continue

            if state == "cloudflare":
                if not await self._wait_cloudflare():
                    return False
                continue

            if state == "captcha":
                if not await self._solve_turnstile():
                    return False
                await self._delay(2, 4)
                continue

            if state == "login":
                # Check if account is inactive BEFORE trying to log in
                text = await self._text()
                if "currently inactive" in text or "resend the activation" in text:
                    logger.error("[W%d] Account %s is INACTIVE on VFS", self.worker_id, self.email)
                    await self._screenshot("account_inactive")
                    return False

                # Dismiss cookie consent banner if present
                await self._dismiss_cookie_banner()
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
                await self._human_type(email_el, self.email)
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
                await self._human_type(pwd_el, self.password)
                await self._delay(0.6, 1.2)

                # Может быть captcha на форме логина — но часто Turnstile решается сам
                token_ready = await self.page.evaluate("""
                    (() => {
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 20;
                    })()
                """)
                if token_ready:
                    logger.info("Turnstile уже решён автоматически — токен заполнен")
                else:
                    html = await self._html()
                    if any(m in html for m in CAPTCHA_MARKERS):
                        logger.info("CAPTCHA на форме логина — решаем...")
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

                await self._delay(6, 12)
                await self._screenshot("after_signin_click")
                state = await self._page_state()

                if state == "login":
                    text = await self._text()
                    if "currently inactive" in text or "resend the activation" in text:
                        logger.error("[W%d] Account %s is INACTIVE", self.worker_id, self.email)
                        await self._screenshot("account_inactive_after_login")
                        return False
                    if any(w in text for w in ["incorrect", "invalid", "wrong", "failed"]):
                        logger.error("Неверный логин/пароль!")
                        return False
                    continue

                if state in ("cloudflare", "captcha"):
                    continue

            if state in ("dashboard", "logged_in", "appointment_form"):
                break

        # Ждём пока Angular отрендерит dashboard после логина
        final_state = await self._page_state()
        if final_state == "unknown":
            for _ in range(5):
                await self._delay(1.5, 2.5)
                final_state = await self._page_state()
                if final_state != "unknown":
                    break

        if final_state in ("login", "cloudflare", "captcha", "blocked", "session_expired"):
            logger.error("Логин не удался (final_state=%s)", final_state)
            await self._screenshot("login_failed")
            if final_state == "session_expired":
                try:
                    if os.path.exists(self._cookies_path):
                        os.remove(self._cookies_path)
                        logger.info("Stale cookies deleted")
                except OSError:
                    pass
            return False

        self.logged_in = True
        self.on_dashboard = final_state == "dashboard"
        self.last_login_time = time.time()
        self._session_ttl = random.uniform(1500, 2400)
        try:
            await self.interceptor.attach(self.page)
        except Exception as e:
            logger.warning("Interceptor attach failed: %s", e)
        logger.info("Login OK (state=%s)", final_state)
        return True

    # ── Core: check slots ──────────────────────────────────────────

    async def check_slots(self) -> tuple[bool, str, str | None]:
        """
        Проверка слотов. Первый раз — полный флоу (login → dashboard → form).
        Повторно — просто перевыбираем sub-category на той же странице
        (без рефреша/навигации, чтобы не терять сессию).
        """
        if not self.browser or not self.logged_in:
            if not await self.login():
                return False, "Логин не удался", None

        await self._ensure_proxy_auth()

        # Если мы уже на форме — быстрая проверка без навигации
        if self._on_appointment_form:
            return await self._recheck_slots_on_form()

        results = []
        for subcategory in Config.VFS_SUBCATEGORIES:
            found, info, screenshot = await self._check_single_subcategory(subcategory)
            if found:
                return True, f"[{subcategory}]\n{info}", screenshot
            results.append(f"{subcategory}: {info}")

        return False, " | ".join(results), None

    async def _recheck_slots_on_form(self) -> tuple[bool, str, str | None]:
        """Быстрая перепроверка — перевыбираем sub-category без навигации."""
        # Проверяем что страница ещё жива и мы на форме
        try:
            state = await self._page_state()
        except Exception:
            self._on_appointment_form = False
            return await self.check_slots()

        if state != "appointment_form":
            logger.info("Страница сменилась (state=%s) — полный цикл", state)
            self._on_appointment_form = False
            if state in ("login", "session_expired"):
                self.logged_in = False
            return await self.check_slots()

        results = []
        for subcategory in Config.VFS_SUBCATEGORIES:
            logger.info("Re-check (on form): %s", subcategory)
            self.interceptor.clear()

            # Перевыбираем sub-category — триггерит новый CheckIsSlotAvailable
            if not await self._reselect_subcategory(subcategory):
                logger.warning("Re-select failed — полный цикл")
                self._on_appointment_form = False
                return await self.check_slots()

            found, info, screenshot = await self._read_slot_result(subcategory)
            if found:
                return True, f"[{subcategory}]\n{info}", screenshot
            results.append(f"{subcategory}: {info}")

        return False, " | ".join(results), None

    async def _wait_please_wait_timer(self, max_wait: int = 60) -> None:
        """Ждём пока таймер 'Please wait N seconds' исчезнет."""
        for i in range(max_wait // 2):
            remaining = await self.page.evaluate("""
                (() => {
                    const text = document.body.innerText || '';
                    const m = text.match(/please wait (\\d+) second/i);
                    return m ? parseInt(m[1]) : 0;
                })()
            """)
            if not remaining or remaining <= 0:
                if i > 0:
                    logger.info("Таймер 'Please wait' завершился")
                return
            if i == 0:
                logger.info("Обнаружен таймер: Please wait %d seconds", remaining)
            await asyncio.sleep(2)
        logger.warning("Таймер 'Please wait' не исчез за %dс", max_wait)

    async def _read_slot_result(self, subcategory: str) -> tuple[bool, str, str | None]:
        """Ждём результат проверки слотов и читаем его (API + DOM + Continue button)."""
        logger.info("Ждём результат проверки слотов...")

        # Сначала дождёмся таймера "Please wait N seconds" если он появился
        await self._wait_please_wait_timer()

        for wait_i in range(10):
            await self._delay(1.5, 2.5)
            ready = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    // Если таймер ещё на экране — ещё рано проверять
                    if (/please wait \\d+ second/i.test(document.body.innerText || ''))
                        return null;
                    if (text.includes('no appointment slots')) return 'no_slots';
                    const alert = document.querySelector('.Information [role="alert"], [role="alert"]');
                    if (alert && alert.textContent.toLowerCase().includes('no appointment')) return 'no_slots_dom';
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.textContent.toLowerCase().includes('continue') && !b.disabled &&
                            !b.classList.contains('mat-mdc-button-disabled'))
                            return 'slots_available';
                    }
                    for (const b of btns) {
                        if (b.textContent.toLowerCase().includes('continue') &&
                            (b.disabled || b.classList.contains('mat-mdc-button-disabled')))
                            return 'no_slots_btn';
                    }
                    const spinner = document.querySelector('.mat-mdc-progress-spinner, mat-spinner, .loading, .spinner');
                    if (spinner) return null;
                    return null;
                })()
            """)
            if ready:
                logger.info("Результат готов после %.0fс: %s", (wait_i + 1) * 2, ready)
                break
            if self.interceptor.has_data:
                logger.info("API ответил после %.0fс", (wait_i + 1) * 2)
                break
        await self._screenshot("after_subcategory_select")

        # Проверяем перехваченные API-ответы
        if self.interceptor.has_data:
            api_result = await self.interceptor.check_api_slots()
            if api_result.available:
                screenshot = await self._screenshot("slots_found", send_tg=True)
                info = f"API: слоты найдены!"
                if api_result.earliest_date:
                    info += f" Ближайшая дата: {api_result.earliest_date}"
                if api_result.dates:
                    info += f" Даты: {', '.join(api_result.dates[:5])}"
                logger.info("СЛОТЫ (API): %s", info)
                return True, info, screenshot

        text = await self._text()

        if NO_SLOTS_TEXT in text:
            logger.info("Нет слотов для '%s'", subcategory)
            return False, "Нет слотов", None

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

        # Проверяем что таймер "Please wait" не активен
        timer_active = await self.page.evaluate("""
            (() => {
                const text = document.body.innerText || '';
                return /please wait \\d+ second/i.test(text);
            })()
        """)
        if timer_active:
            logger.info("Таймер 'Please wait' ещё активен — ждём")
            await self._wait_please_wait_timer()

        continue_disabled = await self.page.evaluate("""
            (() => {
                // Ещё раз проверяем таймер — если он есть, кнопка Continue не значит что слоты есть
                if (/please wait \\d+ second/i.test(document.body.innerText || ''))
                    return null;
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
            screenshot = await self._screenshot("slots_found", send_tg=True)
            logger.info("СЛОТЫ НАЙДЕНЫ для '%s'!", subcategory)
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
            logger.info("Continue disabled, банера нет — нет слотов для '%s'", subcategory)
            return False, "Continue disabled", None

        if "no appointment" in text or "sorry" in text:
            return False, "Нет слотов (текст)", None

        screenshot = await self._screenshot("unclear")
        await dump_page(self.page, "unclear")
        return False, f"Неясный результат: {text[:200]}", screenshot

    async def _advance_to_step2(self) -> None:
        """After slots found: click Continue, wait for 'Please wait N seconds' countdown,
        then screenshot the Your Details (passport) page and send to Telegram."""
        try:
            logger.info("Слоты найдены — нажимаю Continue для перехода на страницу паспортов...")

            # Click the Continue button
            clicked = await self.page.evaluate("""
                (() => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.textContent.toLowerCase().includes('continue') && !b.disabled &&
                            !b.classList.contains('mat-mdc-button-disabled')) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)
            if not clicked:
                logger.warning("Continue button not found or disabled")
                return

            # Wait for "Please wait N seconds" countdown (up to 60s)
            logger.info("Жду окончания таймера 'Please wait...'")
            for _ in range(40):
                await asyncio.sleep(2)
                wait_text = await self.page.evaluate("""
                    (() => {
                        const text = document.body.innerText || '';
                        const m = text.match(/please wait (\\d+) second/i);
                        return m ? parseInt(m[1]) : 0;
                    })()
                """)
                if wait_text and wait_text > 0:
                    logger.info("Таймер: %d секунд осталось...", wait_text)
                    continue

                # Check if we're on step 2 (Your Details)
                on_step2 = await self.page.evaluate("""
                    (() => {
                        const text = (document.body.innerText || '').toLowerCase();
                        return text.includes('your details') || text.includes('passport') ||
                               text.includes('first name') || text.includes('applicant') ||
                               text.includes('date of birth') || text.includes('nationality');
                    })()
                """)
                if on_step2:
                    break

            await self._delay(1.5, 3.0)
            screenshot_path = await self._screenshot("step2_your_details", send_tg=True)
            logger.info("Скриншот страницы паспортов отправлен в Telegram")

        except Exception as e:
            logger.error("Ошибка при переходе на step 2: %s", e)

    async def _reselect_subcategory(self, subcategory: str) -> bool:
        """Перевыбрать sub-category dropdown чтобы триггерить новую проверку слотов."""
        fcn = self.DROPDOWN_MAP["subcategory"]
        safe_fcn = fcn.replace("'", "\\'")

        # Сначала сбросим — выберем другую опцию или просто переоткроем
        el = await self.page.select(f'mat-select[formcontrolname="{safe_fcn}"]')
        if not el:
            return False

        if self.hc:
            await self.hc.click(el)
        else:
            await el.click()
        await self._delay(0.3, 0.6)

        # Закроем dropdown (клик вне его, Escape)
        await self.page.evaluate("document.querySelector('.cdk-overlay-backdrop')?.click()")
        await self._delay(0.3, 0.5)

        # Теперь выберем нужную sub-category заново
        return await self._select_dropdown_option("sub-category", subcategory)

    async def _check_single_subcategory(self, subcategory: str) -> tuple[bool, str, str | None]:
        """Проверяем конкретную подкатегорию."""
        logger.info("Проверяю: %s", subcategory)
        self.interceptor.clear()

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
        await self._screenshot("after_start_new_booking")

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

        await self._screenshot("appointment_form_loaded")

        # Шаг 4: Выбираем Centre (formcontrolname="centerCode")
        if self.hc:
            await self.hc.micro_scroll()
        if not await self._select_dropdown_option("Application Centre", Config.VFS_CENTRE):
            return False, "Не удалось выбрать Centre", None

        await self._delay(1.5, 3.0)
        await self._screenshot("after_centre_select")

        # Шаг 5: Выбираем Category (formcontrolname="selectedSubvisaCategory")
        if self.hc:
            await self.hc.idle_drift(random.uniform(0.3, 0.8))
        if not await self._select_dropdown_option("appointment category", Config.VFS_CATEGORY):
            return False, "Не удалось выбрать Category", None

        await self._delay(1.5, 3.0)
        await self._screenshot("after_category_select")

        # Шаг 6: Выбираем Sub-category (formcontrolname="visaCategoryCode")
        if self.hc:
            await self.hc.idle_drift(random.uniform(0.3, 0.8))
        if not await self._select_dropdown_option("sub-category", subcategory):
            return False, f"Не удалось выбрать Sub-category: {subcategory}", None

        found, info, screenshot = await self._read_slot_result(subcategory)
        # Мы успешно дошли до формы — ставим флаг чтобы следующие проверки
        # не делали полный цикл (login → dashboard → form), а просто
        # перевыбирали sub-category
        self._on_appointment_form = True
        return found, info, screenshot

    VFS_BASE = "https://visa.vfsglobal.com/uzb/en/lva"

    async def _ensure_dashboard(self, depth: int = 0) -> bool:
        """Убеждаемся что мы на dashboard с кнопкой Start New Booking."""
        if depth >= 3:
            logger.error("_ensure_dashboard: max depth reached")
            await self._screenshot("dashboard_loop")
            return False

        state = await self._page_state()

        if state == "dashboard":
            await self._screenshot("on_dashboard")
            return True

        if state == "session_expired":
            logger.warning("Session expired on dashboard — re-login")
            try:
                if os.path.exists(self._cookies_path):
                    os.remove(self._cookies_path)
            except OSError:
                pass
            self.logged_in = False
            if not await self.login():
                return False
            return await self._ensure_dashboard(depth + 1)

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
        await self._delay(3, 5)

        # Check for proxy/network failure
        url = await self._url()
        if "chrome-error" in url:
            logger.warning("Dashboard load failed — proxy issue, rotating session")
            self._rotate_proxy_session()
            await asyncio.sleep(3)
            await self.page.get(f"{self.VFS_BASE}/dashboard")
            await self._delay(3, 5)
            url = await self._url()
            if "chrome-error" in url:
                return False

        if not await self._handle_obstacle():
            return False

        # Angular SPA может рендериться медленно — retry несколько раз
        for wait_i in range(5):
            state = await self._page_state()
            if state == "dashboard":
                return True
            if state == "login":
                self.logged_in = False
                return await self._ensure_dashboard(depth + 1)
            await self._delay(1.5, 2.5)

        logger.warning("Не могу попасть на dashboard (state=%s)", state)
        await self._screenshot("no_dashboard")
        return False

    # ── Utility ────────────────────────────────────────────────────

    async def _find_input(self, selectors: list[str]):
        for s in selectors:
            try:
                el = await self.page.select(s)
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _find_visible_input(self, selectors: list[str]):
        """Находит input, пропуская honeypot'ы (d-none, aria-hidden, offsetParent null)."""
        for s in selectors:
            try:
                el = await self.page.select(s)
                if not el:
                    continue
                safe_s = s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
                visible = await self.page.evaluate(f"""
                    (() => {{
                        const el = document.querySelector('{safe_s}');
                        if (!el) return false;
                        if (el.classList.contains('d-none')) return false;
                        if (el.getAttribute('aria-hidden') === 'true') return false;
                        if (el.tabIndex === -1 && el.type === 'hidden') return false;
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                        if (parseFloat(cs.opacity) < 0.1) return false;
                        if (el.offsetParent === null && cs.position !== 'fixed') return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) return false;
                        if (rect.right < 0 || rect.bottom < 0 || rect.left > window.innerWidth) return false;
                        return true;
                    }})()
                """)
                if visible:
                    return el
            except Exception:
                continue
        return None

    async def _click(self, selectors: list[str]) -> bool:
        for s in selectors:
            try:
                el = await self.page.select(s)
                if el:
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    def session_alive(self) -> bool:
        if not self.logged_in:
            return False
        return (time.time() - self.last_login_time) < self._session_ttl

    # ══════════════════════════════════════════════════════════════
    #  FIGHTER AUTO-BOOKING FLOW (Steps 1-5)
    # ══════════════════════════════════════════════════════════════

    async def auto_book(self, passport_path: str, applicant_name: str,
                        mail_password: str = "", subcategory: str = "") -> dict:
        """
        Full auto-booking flow for fighter accounts.
        Returns dict: {success: bool, step: str, error: str, booked_date: str, booked_time: str}
        """
        result = {"success": False, "step": "", "error": "", "booked_date": "", "booked_time": ""}
        sub = subcategory or Config.VFS_SUBCATEGORIES[0]
        tag = f"F{self.worker_id}:{self.email.split('@')[0]}→{applicant_name}"

        try:
            # Step 0: Login
            result["step"] = "login"
            if not self.logged_in:
                if not await self.login():
                    result["error"] = "Login failed"
                    await self._screenshot(f"fighter_login_fail_{tag}", send_tg=True)
                    return result
            await self._screenshot(f"fighter_login_ok_{tag}", send_tg=True)
            logger.info("[F%d] Login OK for %s, starting booking for %s",
                        self.worker_id, self.email, applicant_name)

            # Step 1: Navigate to appointment form and select dropdowns
            result["step"] = "step1_appointment"
            if not await self._ensure_dashboard():
                result["error"] = "Cannot reach dashboard"
                return result

            # Click Start New Booking
            await self._delay(1, 2)
            snb_el = await self._find_input([
                "button.btn-brand-orange.mat-mdc-raised-button",
                "button.btn-brand-orange",
            ])
            if snb_el:
                if self.hc:
                    await self.hc.click(snb_el)
                else:
                    await snb_el.click()
            else:
                await self.page.evaluate("""
                    (() => {
                        for (const b of document.querySelectorAll('button, a')) {
                            if (b.textContent.toLowerCase().includes('start new booking')) {
                                b.click(); return true;
                            }
                        }
                        return false;
                    })()
                """)
            await self._delay(3, 5)
            if not await self._handle_obstacle():
                result["error"] = "Obstacle after Start New Booking"
                return result

            # Select Centre → Category → Sub-category
            if not await self._select_dropdown_option("Application Centre", Config.VFS_CENTRE):
                result["error"] = "Cannot select Centre"
                return result
            await self._delay(1.5, 3)

            if not await self._select_dropdown_option("appointment category", Config.VFS_CATEGORY):
                result["error"] = "Cannot select Category"
                return result
            await self._delay(1.5, 3)

            if not await self._select_dropdown_option("sub-category", sub):
                result["error"] = "Cannot select Sub-category"
                return result

            # Wait for slot check result
            found, info, _ = await self._read_slot_result(sub)
            if not found:
                result["error"] = f"No slots available: {info}"
                await self._screenshot(f"fighter_no_slots_{tag}", send_tg=True)
                return result
            await self._screenshot(f"fighter_slots_ok_{tag}", send_tg=True)
            logger.info("[F%d] Slots confirmed! Advancing to step 2...", self.worker_id)

            # Click Continue (step 1 → step 2) + wait timer
            result["step"] = "step1_continue"
            if not await self._fighter_click_continue_step1():
                result["error"] = "Cannot click Continue on step 1"
                return result

            # Step 2: Upload passport + OCR + Save + OTP
            result["step"] = "step2_passport"
            if not await self._fighter_upload_passport(passport_path):
                result["error"] = "Passport upload failed"
                return result

            result["step"] = "step2_form"
            if not await self._fighter_wait_ocr_and_save():
                result["error"] = "OCR/form save failed"
                return result

            result["step"] = "step2_otp"
            if not await self._fighter_verify_otp(mail_password):
                result["error"] = "OTP verification failed"
                return result

            # Step 3: Select date and time
            result["step"] = "step3_date"
            date_str, time_str = await self._fighter_select_date_time()
            if not date_str:
                result["error"] = "Cannot select date/time"
                return result
            result["booked_date"] = date_str
            result["booked_time"] = time_str
            logger.info("[F%d] Selected %s %s", self.worker_id, date_str, time_str)

            # Step 4: Services — skip (just Continue)
            result["step"] = "step4_services"
            if not await self._fighter_skip_services():
                result["error"] = "Cannot skip services"
                return result

            # Step 5: Review — Confirm
            result["step"] = "step5_confirm"
            if not await self._fighter_confirm():
                result["error"] = "Confirmation failed"
                return result

            result["success"] = True
            logger.info("[F%d] BOOKING CONFIRMED for %s! Date: %s Time: %s",
                        self.worker_id, applicant_name, date_str, time_str)
            await self._screenshot("booking_success", send_tg=True)
            return result

        except Exception as e:
            result["error"] = str(e)
            logger.error("[F%d] Auto-book error at %s: %s",
                         self.worker_id, result["step"], e, exc_info=True)
            await self._screenshot(f"fighter_error_{result['step']}")
            return result

    async def _fighter_click_continue_step1(self) -> bool:
        """Click Continue on step 1, wait through 'Please wait N seconds' timer."""
        clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.toLowerCase().includes('continue') && !b.disabled &&
                        !b.classList.contains('mat-mdc-button-disabled')) {
                        b.click(); return true;
                    }
                }
                return false;
            })()
        """)
        if not clicked:
            logger.warning("[F%d] Continue button not clickable on step 1", self.worker_id)
            return False

        logger.info("[F%d] Waiting for 'Please wait' timer...", self.worker_id)
        for _ in range(40):
            await asyncio.sleep(2)
            timer_val = await self.page.evaluate("""
                (() => {
                    const text = document.body.innerText || '';
                    const m = text.match(/please wait (\\d+) second/i);
                    return m ? parseInt(m[1]) : 0;
                })()
            """)
            if timer_val and timer_val > 0:
                logger.info("[F%d] Timer: %ds remaining...", self.worker_id, timer_val)
                continue

            on_step2 = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('your details') || text.includes('browse files') ||
                           text.includes('passport') || text.includes('upload');
                })()
            """)
            if on_step2:
                logger.info("[F%d] On step 2 (Your Details)", self.worker_id)
                return True

        await self._screenshot("step1_timer_timeout")
        return False

    async def _fighter_upload_passport(self, passport_path: str) -> bool:
        """Upload passport file via hidden input[type=file], then click Continue."""
        if not os.path.exists(passport_path):
            logger.error("[F%d] Passport file not found: %s", self.worker_id, passport_path)
            return False

        await self._delay(1, 2)

        # Set the file on the hidden input[type=file]
        file_input = await self.page.select('input[type="file"]')
        if not file_input:
            logger.error("[F%d] File input not found", self.worker_id)
            await self._screenshot("no_file_input")
            return False

        await file_input.send_file(passport_path)
        logger.info("[F%d] Passport file sent: %s", self.worker_id, os.path.basename(passport_path))
        await self._delay(3, 5)

        # Wait for file to appear + Click "Continue" button in the upload area
        uploaded = False
        for attempt in range(10):
            uploaded = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('replace') || text.includes('uploaded') ||
                           text.includes('.pdf') || text.includes('.jpg') || text.includes('.png');
                })()
            """)
            if uploaded:
                break
            await asyncio.sleep(2)

        if not uploaded:
            logger.error("[F%d] Passport upload not confirmed", self.worker_id)
            await self._screenshot("passport_upload_timeout", send_tg=True)
            return False

        # Click "Continue" in upload area (button.file-browse.fs-22 with text "Continue")
        clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button.file-browse, button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'continue' && (b.classList.contains('file-browse') ||
                        b.classList.contains('fs-22') ||
                        b.closest('.upload-container, .file-upload, .document-upload'))) {
                        b.click(); return 'upload_continue';
                    }
                }
                // Fallback: any Continue button that's not the main step Continue
                for (const b of btns) {
                    if (b.textContent.trim().toLowerCase() === 'continue' &&
                        !b.classList.contains('btn-brand-orange') &&
                        !b.classList.contains('mat-mdc-raised-button')) {
                        b.click(); return 'fallback_continue';
                    }
                }
                return null;
            })()
        """)
        logger.info("[F%d] Upload continue click: %s", self.worker_id, clicked)
        await self._delay(3, 5)
        await self._screenshot("fighter_passport_uploaded", send_tg=True)
        return bool(clicked)

    async def _fighter_wait_ocr_and_save(self) -> bool:
        """Wait for VFS OCR to fill form fields, then click Save, then Continue on summary."""
        # Wait for OCR to process and fill fields (fields become disabled)
        logger.info("[F%d] Waiting for OCR to fill form...", self.worker_id)
        for _ in range(20):
            await asyncio.sleep(2)
            ocr_done = await self.page.evaluate("""
                (() => {
                    // Check if form fields are filled and disabled (OCR completed)
                    const inputs = document.querySelectorAll(
                        'input[formcontrolname], input[matinput]'
                    );
                    let filled = 0;
                    let disabled = 0;
                    for (const inp of inputs) {
                        if (inp.value && inp.value.trim()) filled++;
                        if (inp.disabled || inp.readOnly) disabled++;
                    }
                    // OCR fills at least first name, last name, passport number
                    return filled >= 3 && disabled >= 3;
                })()
            """)
            if ocr_done:
                logger.info("[F%d] OCR completed, fields filled", self.worker_id)
                break
        else:
            logger.error("[F%d] OCR timed out — form fields not filled", self.worker_id)
            await self._screenshot("fighter_ocr_timeout", send_tg=True)
            return False

        await self._delay(1, 2)
        await self._screenshot("fighter_ocr_done", send_tg=True)

        # Check if contact number fields need filling (they might not be auto-filled)
        await self.page.evaluate("""
            (() => {
                // Country code field — set to +998 (Uzbekistan) if empty
                const codeInputs = document.querySelectorAll(
                    'input[maxlength="3"], input[formcontrolname*="code"], input[placeholder*="code"]'
                );
                for (const inp of codeInputs) {
                    if (!inp.value || !inp.value.trim()) {
                        const nv = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value');
                        nv.set.call(inp, '998');
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('blur', {bubbles: true}));
                    }
                }
                // Phone number — set dummy if empty (VFS requires it but doesn't verify)
                const phoneInputs = document.querySelectorAll(
                    'input[maxlength="15"], input[formcontrolname*="contact"], input[formcontrolname*="phone"]'
                );
                for (const inp of phoneInputs) {
                    if (!inp.value || !inp.value.trim()) {
                        const nv = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value');
                        nv.set.call(inp, '901234567');
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('blur', {bubbles: true}));
                    }
                }
            })()
        """)
        await self._delay(0.5, 1)

        # Click Save button
        save_clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'save' || t === 'save details' || t === 'save & continue') {
                        if (!b.disabled) { b.click(); return true; }
                    }
                }
                // Fallback: mat-raised-button with save text
                for (const b of document.querySelectorAll('button[mat-raised-button], button.mat-mdc-raised-button')) {
                    if (b.textContent.trim().toLowerCase().includes('save')) {
                        b.click(); return true;
                    }
                }
                return false;
            })()
        """)
        if not save_clicked:
            logger.warning("[F%d] Save button not found", self.worker_id)
            await self._screenshot("no_save_button")
            return False

        logger.info("[F%d] Save clicked", self.worker_id)
        await self._delay(3, 5)

        # Wait for "Your Details Summary" page with Continue button
        for _ in range(10):
            on_summary = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('your details summary') || text.includes('applicant 1') ||
                           text.includes('summary');
                })()
            """)
            if on_summary:
                break
            await asyncio.sleep(2)

        await self._screenshot("fighter_details_summary", send_tg=True)

        # Click Continue on summary page
        clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'continue' && !b.disabled) {
                        b.click(); return true;
                    }
                }
                // Orange continue button
                const orange = document.querySelector('button.btn-brand-orange:not([disabled])');
                if (orange && orange.textContent.toLowerCase().includes('continue')) {
                    orange.click(); return true;
                }
                return false;
            })()
        """)
        if not clicked:
            logger.warning("[F%d] Summary Continue not found", self.worker_id)
            return False

        logger.info("[F%d] Summary Continue clicked, heading to OTP", self.worker_id)
        await self._delay(3, 5)
        return True

    async def _fighter_verify_otp(self, mail_password: str = "") -> bool:
        """Generate OTP, read from email, verify, click Continue."""
        # Wait for OTP page
        for _ in range(10):
            on_otp = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('one-time password') || text.includes('otp') ||
                           text.includes('generate otp');
                })()
            """)
            if on_otp:
                break
            await asyncio.sleep(2)

        await self._screenshot("fighter_otp_page", send_tg=True)

        # Click "Generate OTP" button
        gen_clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t.includes('generate') && t.includes('otp')) {
                        if (!b.disabled) { b.click(); return true; }
                    }
                }
                return false;
            })()
        """)
        if not gen_clicked:
            logger.error("[F%d] Generate OTP button not found", self.worker_id)
            await self._screenshot("no_generate_otp")
            return False

        logger.info("[F%d] Generate OTP clicked, polling email for %s...",
                    self.worker_id, self.email)
        await self._delay(2, 3)

        # Poll email for OTP (3 min expiry — we have 90s timeout)
        otp = await get_vfs_otp(self.email, mail_password=mail_password, timeout=90)
        if not otp:
            logger.error("[F%d] OTP not received from email", self.worker_id)
            await self._screenshot("otp_timeout")
            return False

        otp = otp.strip()
        if not otp.isdigit():
            logger.error("[F%d] Invalid OTP format: %r", self.worker_id, otp)
            return False
        logger.info("[F%d] Got OTP: %s", self.worker_id, otp)

        # Enter OTP in input field
        otp_entered = await self.page.evaluate(f"""
            (() => {{
                // Find OTP input — usually near "Enter One-time Password" text
                const inputs = document.querySelectorAll('input[type="text"], input[type="number"], input:not([type])');
                for (const inp of inputs) {{
                    const parent = inp.closest('.otp, [class*="otp"], [class*="OTP"]');
                    const label = inp.closest('mat-form-field, .form-group');
                    const nearby = (parent || label || inp.parentElement)?.textContent?.toLowerCase() || '';
                    if (nearby.includes('otp') || nearby.includes('one-time') || nearby.includes('password')) {{
                        const nv = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value');
                        nv.set.call(inp, '{otp}');
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
                        return true;
                    }}
                }}
                // Fallback: any visible text input that's empty
                for (const inp of inputs) {{
                    if (!inp.value && inp.offsetParent !== null && !inp.disabled) {{
                        const nv = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value');
                        nv.set.call(inp, '{otp}');
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                }}
                return false;
            }})()
        """)
        if not otp_entered:
            logger.error("[F%d] Cannot enter OTP into input", self.worker_id)
            return False

        await self._delay(0.5, 1)

        # Click Verify button
        verify_clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t.includes('verify') && !b.disabled) {
                        b.click(); return true;
                    }
                }
                return false;
            })()
        """)
        if not verify_clicked:
            logger.error("[F%d] Verify button not found", self.worker_id)
            return False

        logger.info("[F%d] OTP Verify clicked", self.worker_id)
        await self._delay(3, 5)

        # Check verification success
        verified_ok = False
        for _ in range(10):
            verified = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    if (text.includes('otp verification successful') ||
                        text.includes('verified successfully') ||
                        text.includes('verification successful'))
                        return 'ok';
                    if (text.includes('invalid otp') || text.includes('otp expired') ||
                        text.includes('incorrect otp'))
                        return 'failed';
                    return null;
                })()
            """)
            if verified == 'ok':
                logger.info("[F%d] OTP verified successfully!", self.worker_id)
                verified_ok = True
                break
            if verified == 'failed':
                logger.error("[F%d] OTP verification FAILED", self.worker_id)
                await self._screenshot("otp_verify_failed", send_tg=True)
                return False
            await asyncio.sleep(2)

        if not verified_ok:
            logger.error("[F%d] OTP verification not confirmed in time", self.worker_id)
            await self._screenshot("otp_verify_timeout", send_tg=True)
            return False

        await self._screenshot("fighter_otp_verified", send_tg=True)

        # Click Continue after OTP (orange button)
        clicked = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'continue' && !b.disabled) {
                        b.click(); return true;
                    }
                }
                const orange = document.querySelector('button.btn-brand-orange:not([disabled])');
                if (orange) { orange.click(); return true; }
                return false;
            })()
        """)
        if not clicked:
            logger.warning("[F%d] Continue after OTP not found", self.worker_id)
            return False

        logger.info("[F%d] Continue after OTP → Step 3 (Book Appointment)", self.worker_id)
        await self._delay(3, 5)
        return True

    async def _fighter_select_date_time(self) -> tuple[str, str]:
        """Select first available date from calendar, then earliest time slot."""
        # Wait for calendar to load
        for _ in range(15):
            has_calendar = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('book appointment') || text.includes('choose an appointment') ||
                           text.includes('available') || text.includes('unavailable') ||
                           !!document.querySelector('.fc-daygrid, .calendar, [class*="calendar"]');
                })()
            """)
            if has_calendar:
                break
            await asyncio.sleep(2)

        await self._screenshot("fighter_calendar", send_tg=True)
        await self._delay(1, 2)

        # Click first AVAILABLE date (green background, not grey/unavailable)
        date_clicked = await self.page.evaluate("""
            (() => {
                // FullCalendar: look for day cells with availability indicator
                const dayCells = document.querySelectorAll('td.fc-daygrid-day');
                for (const td of dayCells) {
                    const classes = td.className || '';
                    if (classes.includes('fc-day-disabled') || classes.includes('fc-day-past') ||
                        classes.includes('fc-day-other')) continue;
                    // Check for green background (available) vs grey/white (unavailable)
                    const bg = window.getComputedStyle(td).backgroundColor;
                    const hasBgEvent = td.querySelector('.fc-bg-event, .fc-event');
                    const isAvailable = classes.includes('fc-day-available') ||
                        classes.includes('available') || hasBgEvent ||
                        (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent' &&
                         bg !== 'rgb(255, 255, 255)' && bg !== 'rgb(245, 245, 245)' &&
                         bg !== 'rgb(238, 238, 238)');
                    if (!isAvailable) continue;
                    const link = td.querySelector('a.fc-daygrid-day-number');
                    if (link) { link.click(); }
                    else { td.click(); }
                    return td.getAttribute('data-date') || td.textContent.trim();
                }
                // Fallback: look for cells with green-ish background event overlays
                for (const ev of document.querySelectorAll('.fc-bg-event, .fc-event')) {
                    const td = ev.closest('td.fc-daygrid-day');
                    if (td) {
                        const link = td.querySelector('a');
                        if (link) link.click(); else td.click();
                        return td.getAttribute('data-date') || td.textContent.trim();
                    }
                }
                // Alternative: date buttons/links explicitly marked available
                for (const b of document.querySelectorAll('[class*="available"], [class*="active-date"]')) {
                    if (!b.disabled) {
                        b.click();
                        return b.textContent.trim();
                    }
                }
                return null;
            })()
        """)

        if not date_clicked:
            logger.error("[F%d] No available date found in calendar", self.worker_id)
            await self._screenshot("no_dates")
            return ("", "")

        logger.info("[F%d] Date clicked: %s", self.worker_id, date_clicked)
        await self._delay(2, 4)
        await self._screenshot("after_date_select")

        # Wait for time slots to appear
        for _ in range(10):
            has_times = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('select') && (text.includes('time') ||
                           text.includes('08:') || text.includes('09:') || text.includes('10:'));
                })()
            """)
            if has_times:
                break
            await asyncio.sleep(2)

        # Select earliest time slot (click first "Select" button)
        time_info = await self.page.evaluate("""
            (() => {
                // Time slots are rows with time text + "Select" button
                const rows = document.querySelectorAll('tr, div[class*="slot"], div[class*="time"]');
                for (const row of rows) {
                    const selectBtn = row.querySelector('button');
                    if (selectBtn && selectBtn.textContent.trim().toLowerCase() === 'select') {
                        // Get the time from this row
                        const timeText = row.textContent.replace('Select', '').trim();
                        selectBtn.click();
                        return timeText;
                    }
                }
                // Fallback: find any "Select" button in the time section
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.trim().toLowerCase() === 'select' && !b.disabled) {
                        const parent = b.closest('tr, div');
                        const timeText = parent ? parent.textContent.replace('Select', '').trim() : '';
                        b.click();
                        return timeText || 'earliest';
                    }
                }
                return null;
            })()
        """)

        if not time_info:
            logger.error("[F%d] No time slot found", self.worker_id)
            await self._screenshot("no_times")
            return ("", "")

        logger.info("[F%d] Time selected: %s", self.worker_id, time_info)
        await self._delay(2, 3)
        await self._screenshot("fighter_time_selected", send_tg=True)

        return (str(date_clicked), str(time_info))

    async def _fighter_skip_services(self) -> bool:
        """Step 4: Services page — just click Continue without adding anything."""
        # Wait for services page
        for _ in range(10):
            on_services = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('services') && (text.includes('premium lounge') ||
                           text.includes('courier') || text.includes('add') ||
                           text.includes('unit cost'));
                })()
            """)
            if on_services:
                break
            await asyncio.sleep(2)

        await self._screenshot("fighter_services", send_tg=True)
        await self._delay(1, 2)

        # Click Continue (don't add any services)
        clicked = await self.page.evaluate("""
            (() => {
                // Orange Continue button at bottom
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'continue' && !b.disabled &&
                        (b.classList.contains('btn-brand-orange') ||
                         b.classList.contains('mat-mdc-raised-button'))) {
                        b.click(); return true;
                    }
                }
                // Any Continue button
                for (const b of btns) {
                    if (b.textContent.trim().toLowerCase() === 'continue' && !b.disabled) {
                        b.click(); return true;
                    }
                }
                return false;
            })()
        """)
        if not clicked:
            logger.warning("[F%d] Services Continue not found", self.worker_id)
            return False

        logger.info("[F%d] Services skipped → Step 5 (Review)", self.worker_id)
        await self._delay(3, 5)
        return True

    async def _fighter_confirm(self) -> bool:
        """Step 5: Review page — click Confirm to finalize booking."""
        # Wait for Review page
        for _ in range(10):
            on_review = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('review') && (text.includes('applicant details') ||
                           text.includes('appointment details') || text.includes('confirm'));
                })()
            """)
            if on_review:
                break
            await asyncio.sleep(2)

        await self._screenshot("fighter_review", send_tg=True)
        await self._delay(1, 2)

        # Click Confirm button — check text to avoid clicking cookie/consent buttons
        confirmed = await self.page.evaluate("""
            (() => {
                const btns = document.querySelectorAll('button');
                // Primary: orange Confirm button
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'confirm' && !b.disabled &&
                        (b.classList.contains('btn-brand-orange') ||
                         b.classList.contains('mat-mdc-raised-button'))) {
                        b.click(); return 'orange-confirm';
                    }
                }
                // Secondary: button#trigger but only if its text says confirm
                const trigger = document.querySelector('button#trigger');
                if (trigger && !trigger.disabled &&
                    trigger.textContent.trim().toLowerCase().includes('confirm')) {
                    trigger.click(); return 'trigger';
                }
                // Tertiary: any button with confirm text
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'confirm' && !b.disabled) {
                        b.click(); return 'text-match';
                    }
                }
                return null;
            })()
        """)

        if not confirmed:
            logger.error("[F%d] Confirm button not found!", self.worker_id)
            await self._screenshot("no_confirm_button")
            return False

        logger.info("[F%d] Confirm clicked (%s)! Waiting for success...", self.worker_id, confirmed)
        await self._delay(5, 10)

        # Verify booking success
        for _ in range(15):
            success = await self.page.evaluate("""
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    return text.includes('thank you for booking') ||
                           text.includes('booking confirmed') ||
                           text.includes('appointment has been booked') ||
                           text.includes('transaction summary');
                })()
            """)
            if success:
                logger.info("[F%d] BOOKING SUCCESS confirmed on page!", self.worker_id)
                await self._screenshot("booking_confirmed", send_tg=True)
                return True
            await asyncio.sleep(2)

        # Check for errors
        error_text = await self.page.evaluate("""
            (() => {
                const text = (document.body.innerText || '').toLowerCase();
                if (text.includes('error') || text.includes('failed') || text.includes('sorry'))
                    return text.substring(0, 300);
                return null;
            })()
        """)
        if error_text:
            logger.error("[F%d] Booking error: %s", self.worker_id, error_text)
        await self._screenshot("confirm_unclear")
        return False
