"""
Автоматическая регистрация аккаунтов на VFS Global.

Форма регистрации (visa.vfsglobal.com/uzb/en/lva):
- Email*
- Password*
- Confirm Password*
- Mobile Number* (Dial Code + number)
- 3 чекбокса (Privacy, Data Transfer, Terms)
- Cloudflare Turnstile
- Кнопка Submit/Register
"""

import asyncio
import json
import logging
import os
import random
import string
import time

import nodriver as uc

from antidetect import get_chrome_args, setup_stealth_on_new_page
from config import Config
from local_proxy import start_local_proxy, DEFAULT_PORT
from notifier import send_telegram_photo

logger = logging.getLogger(__name__)

REG_URL = "https://visa.vfsglobal.com/uzb/en/lva/login"


def _js_str(value: str) -> str:
    """Safely embed a Python string as a JS string literal."""
    return json.dumps(value)


def generate_password(length: int = 12) -> str:
    """VFS требует: мин 8 символов, буквы + цифры + спецсимвол."""
    lower = random.choices(string.ascii_lowercase, k=4)
    upper = random.choices(string.ascii_uppercase, k=3)
    digits = random.choices(string.digits, k=3)
    special = random.choices("!@*", k=2)
    pwd = lower + upper + digits + special
    random.shuffle(pwd)
    return "".join(pwd)


class VFSRegistrar:
    def __init__(self, proxy_url: str = "", worker_id: int = 99):
        self.proxy_url = proxy_url
        self.worker_id = worker_id
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None
        self._browser_data_dir = os.path.join(
            os.path.dirname(__file__), f"browser_data_reg_{worker_id}")
        self._local_proxy_server: asyncio.Server | None = None

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

        args = get_chrome_args(
            os.path.join(os.path.dirname(__file__), "browser_profile_reg.json"))

        if self.proxy_url:
            from urllib.parse import urlparse
            parsed = urlparse(self.proxy_url)
            if parsed.username:
                local_port = DEFAULT_PORT + 50 + self.worker_id
                if not self._local_proxy_server:
                    self._local_proxy_server = await start_local_proxy(self.proxy_url, local_port)
                args.append(f"--proxy-server=http://127.0.0.1:{local_port}")
            else:
                args.append(f"--proxy-server={self.proxy_url}")

        import shutil
        has_display = bool(os.environ.get("DISPLAY"))
        if not has_display:
            if shutil.which("Xvfb"):
                os.environ["DISPLAY"] = ":99"
                has_display = True

        self.browser = await uc.start(
            user_data_dir=self._browser_data_dir,
            headless="new" if not has_display else False,
            lang="en-US",
            browser_args=args,
            no_sandbox=True,
        )

    async def close_browser(self) -> None:
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
        self.browser = None
        self.page = None

    async def _screenshot(self, tag: str) -> str:
        path = os.path.join(Config.SCREENSHOT_DIR, f"reg_{tag}_{int(time.time())}.png")
        try:
            await self.page.save_screenshot(path)
            logger.info("Reg screenshot: %s", path)
        except Exception as e:
            logger.debug("Screenshot failed: %s", e)
        return path

    async def _delay(self, lo: float = 0.5, hi: float = 1.5) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    async def _human_type(self, el, text: str) -> None:
        for ch in text:
            await el.send_keys(ch)
            await asyncio.sleep(random.uniform(0.05, 0.15))

    async def register(self, email: str, phone: str = "",
                       dial_code: str = "+998") -> dict:
        password = generate_password()

        if not phone:
            phone = "9" + "".join(random.choices(string.digits, k=8))

        try:
            await self.start_browser()
            self.page = await self.browser.get(REG_URL)
            await self._delay(3, 5)

            # Ждём загрузки страницы
            for _ in range(10):
                text = (await self.page.evaluate(
                    "document.body?.innerText || ''") or "").lower()
                if "sign in" in text or "register" in text:
                    break
                await self._delay(1, 2)

            await self._screenshot("reg_page_loaded")

            # Кликаем "I don't have an account"
            clicked_reg = await self.page.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a, button, span');
                    for (const el of links) {
                        const t = el.textContent.toLowerCase().trim();
                        if (t.includes("don't have an account") || t.includes('do not have an account') ||
                            t.includes('register') || t.includes('create account') ||
                            t.includes('i don\\'t have an account')) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)

            if not clicked_reg:
                await self._screenshot("reg_no_register_link")
                return {"success": False, "error": "Кнопка регистрации не найдена"}

            await self._delay(3, 5)
            await self._screenshot("reg_form_opened")

            # Ждём форму регистрации
            for _ in range(10):
                text = (await self.page.evaluate(
                    "document.body?.innerText || ''") or "").lower()
                if "confirm password" in text or "register with" in text:
                    break
                await self._delay(1, 2)

            # Angular-совместимое заполнение: используем нативный setter
            # чтобы Angular Reactive Forms увидел изменения
            email_js = _js_str(email)
            email_filled = await self.page.evaluate(f"""
                (() => {{
                    const val = {email_js};
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    const inp = document.querySelector(
                        'input[formcontrolname="emailid"], input#inputEmail, input[type="email"]');
                    if (!inp) return false;
                    inp.focus();
                    inp.dispatchEvent(new Event('focus', {{bubbles: true}}));
                    setter.call(inp, val);
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    return true;
                }})()
            """)

            if not email_filled:
                await self._screenshot("reg_no_email_field")
                return {"success": False, "error": "Поле Email не найдено"}

            await self._delay(0.5, 1.0)

            # Заполняем Password + Confirm Password
            pwd_js = _js_str(password)
            pwd_filled = await self.page.evaluate(f"""
                (() => {{
                    const pwd = {pwd_js};
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    const pwdInp = document.querySelector('input[formcontrolname="password"]');
                    const confirmInp = document.querySelector('input[formcontrolname="confirmPassword"]');
                    if (!pwdInp || !confirmInp) return false;
                    for (const inp of [pwdInp, confirmInp]) {{
                        inp.focus();
                        inp.dispatchEvent(new Event('focus', {{bubbles: true}}));
                        setter.call(inp, pwd);
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    }}
                    return true;
                }})()
            """)

            if not pwd_filled:
                await self._screenshot("reg_no_pwd_fields")
                return {"success": False, "error": "Поля пароля не найдены"}

            await self._delay(0.5, 1.0)

            # Заполняем Dial Code — кликаем mat-select чтобы открыть dropdown
            # Используем mousedown+mouseup для Angular Material
            await self.page.evaluate("""
                (() => {
                    const sel = document.querySelector('mat-select[formcontrolname="dialcode"]');
                    if (!sel) return;
                    const trigger = sel.querySelector('.mat-mdc-select-trigger') || sel;
                    trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    trigger.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    trigger.click();
                })()
            """)

            await self._delay(1.5, 2.5)

            # Ищем Uzbekistan(998) в overlay — формат: "Uzbekistan(998)", без +
            # dial_code = "+998", ищем по "998" и "Uzbekistan"
            code_num = dial_code.lstrip("+")
            code_num_js = _js_str(code_num)
            dial_selected = False
            for _attempt in range(3):
                dial_selected = await self.page.evaluate(f"""
                    (() => {{
                        const code = {code_num_js};
                        const opts = document.querySelectorAll('mat-option, .mat-mdc-option, [role="option"]');
                        for (const opt of opts) {{
                            const t = opt.textContent;
                            if (t.includes('(' + code + ')') || t.includes(code)) {{
                                opt.click();
                                return true;
                            }}
                        }}
                        return false;
                    }})()
                """)
                if dial_selected:
                    break
                await self._delay(0.5, 1.0)

            if not dial_selected:
                # Закрыть overlay если открыт
                await self.page.evaluate("""
                    (() => {
                        const backdrop = document.querySelector('.cdk-overlay-backdrop');
                        if (backdrop) backdrop.click();
                    })()
                """)
                logger.warning("Dial code +998 not found in dropdown")

            await self._delay(0.5, 0.8)

            # Заполняем Mobile Number (formcontrolname="contact")
            phone_js = _js_str(phone)
            await self.page.evaluate(f"""
                (() => {{
                    const val = {phone_js};
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    const inp = document.querySelector('input[formcontrolname="contact"]');
                    if (!inp) return false;
                    inp.focus();
                    inp.dispatchEvent(new Event('focus', {{bubbles: true}}));
                    setter.call(inp, val);
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    return true;
                }})()
            """)

            await self._delay(0.5, 1.0)

            # Ставим все 3 чекбокса — кликаем внутренний input напрямую
            for cb_name in ['processPerDataAgreed', 'intTransPerDataAgreed', 'termAndConditionAgreed']:
                await self.page.evaluate(f"""
                    (() => {{
                        const cb = document.querySelector('mat-checkbox[formcontrolname="{cb_name}"]');
                        if (!cb) return;
                        const inner = cb.querySelector('input[type="checkbox"]');
                        if (inner && !inner.checked) {{
                            inner.focus();
                            inner.click();
                            inner.dispatchEvent(new Event('change', {{bubbles: true}}));
                            cb.dispatchEvent(new Event('change', {{bubbles: true}}));
                        }}
                    }})()
                """)
                await self._delay(0.3, 0.5)

            await self._delay(1, 2)
            await self._screenshot("reg_form_filled")

            # Диагностика: какие поля ещё невалидны
            diag = await self.page.evaluate("""
                (() => {
                    const fields = ['emailid', 'password', 'confirmPassword', 'dialcode', 'contact',
                                    'processPerDataAgreed', 'intTransPerDataAgreed', 'termAndConditionAgreed'];
                    const result = {};
                    for (const f of fields) {
                        const el = document.querySelector('[formcontrolname="' + f + '"]');
                        if (!el) {
                            result[f] = 'NOT_FOUND';
                        } else if (el.classList.contains('ng-invalid')) {
                            result[f] = 'INVALID (value: ' + (el.value || el.textContent || '').substring(0, 30) + ')';
                        } else {
                            result[f] = 'OK';
                        }
                    }
                    const btn = document.querySelector('button#trigger, button[type="submit"]');
                    result['submit_disabled'] = btn ? btn.disabled : 'NO_BUTTON';
                    const ts = document.querySelector('[name="cf-turnstile-response"]');
                    result['turnstile'] = (ts && ts.value && ts.value.length > 20) ? 'OK' : 'MISSING';
                    return JSON.stringify(result);
                })()
            """)
            logger.info("Form diagnostics: %s", diag)

            # Ждём Turnstile (до 30 сек)
            for _ in range(15):
                token_ok = await self.page.evaluate("""
                    (() => {
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 20;
                    })()
                """)
                if token_ok:
                    logger.info("Turnstile resolved for registration")
                    break
                await self._delay(1, 2)

            # Кликаем Submit/Register
            submitted = await self.page.evaluate("""
                (() => {
                    const btns = document.querySelectorAll('button, input[type="submit"]');
                    for (const b of btns) {
                        const t = b.textContent.toLowerCase().trim();
                        if (t === 'submit' || t === 'register' || t === 'create account' ||
                            t === 'sign up') {
                            if (!b.disabled) {
                                b.click();
                                return 'clicked';
                            } else {
                                return 'disabled';
                            }
                        }
                    }
                    return 'not_found';
                })()
            """)

            if submitted == "disabled":
                screenshot = await self._screenshot("reg_submit_disabled")
                return {"success": False,
                        "error": f"Кнопка Submit заблокирована.\nДиагностика: {diag}",
                        "screenshot": screenshot}

            if submitted == "not_found":
                await self._screenshot("reg_no_submit")
                return {"success": False, "error": "Кнопка Submit не найдена"}

            await self._delay(5, 8)
            screenshot = await self._screenshot("reg_after_submit")

            # Проверяем результат
            text = (await self.page.evaluate(
                "document.body?.innerText || ''") or "").lower()

            if any(w in text for w in [
                "successfully", "registered", "verification", "verify your email",
                "activation", "check your email", "account created",
                "registration successful"
            ]):
                logger.info("Registration successful for %s", email)
                return {
                    "success": True,
                    "email": email,
                    "password": password,
                    "phone": f"{dial_code}{phone}",
                    "screenshot": screenshot,
                    "message": "Аккаунт создан! Проверь почту для активации.",
                }

            if any(w in text for w in [
                "already registered", "already exists", "email is already",
                "account already", "already in use"
            ]):
                return {
                    "success": False,
                    "error": f"Email {email} уже зарегистрирован",
                    "screenshot": screenshot,
                }

            if any(w in text for w in ["error", "failed", "invalid"]):
                error_text = await self.page.evaluate("""
                    (() => {
                        const errs = document.querySelectorAll(
                            '.error, .mat-error, .mat-mdc-form-field-error, [role="alert"], .alert-danger'
                        );
                        const msgs = [];
                        for (const e of errs) {
                            const t = e.textContent.trim();
                            if (t) msgs.push(t);
                        }
                        return msgs.join('; ') || '';
                    })()
                """)
                return {
                    "success": False,
                    "error": error_text or "Ошибка регистрации",
                    "screenshot": screenshot,
                }

            return {
                "success": False,
                "error": f"Неясный результат: {text[:200]}",
                "screenshot": screenshot,
            }

        except Exception as e:
            logger.error("Registration error: %s", e, exc_info=True)
            screenshot = ""
            try:
                screenshot = await self._screenshot("reg_error")
            except Exception:
                pass
            return {"success": False, "error": str(e), "screenshot": screenshot}
        finally:
            await self.close_browser()

    async def activate_account(self, activation_url: str) -> dict:
        """Открывает ссылку активации через тот же прокси."""
        try:
            await self.start_browser()
            self.page = await self.browser.get(activation_url)
            await self._delay(5, 8)

            screenshot = await self._screenshot("activation_page")
            text = (await self.page.evaluate(
                "document.body?.innerText || ''") or "").lower()

            if any(w in text for w in [
                "activated", "successfully", "account is active",
                "verification successful", "email verified",
                "you can now login", "sign in",
            ]):
                logger.info("Account activated successfully")
                return {"success": True, "screenshot": screenshot}

            # Может быть CF challenge — ждём
            for _ in range(15):
                await self._delay(2, 3)
                text = (await self.page.evaluate(
                    "document.body?.innerText || ''") or "").lower()
                if any(w in text for w in [
                    "activated", "successfully", "sign in",
                    "account is active", "verified",
                ]):
                    screenshot = await self._screenshot("activation_success")
                    return {"success": True, "screenshot": screenshot}
                if "checking" not in text and "moment" not in text:
                    break

            screenshot = await self._screenshot("activation_result")
            return {
                "success": "sign in" in text or "activated" in text or "success" in text,
                "screenshot": screenshot,
                "page_text": text[:300],
            }

        except Exception as e:
            logger.error("Activation error: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}
        finally:
            await self.close_browser()


async def register_account(email: str = "", proxy_url: str = "",
                           phone: str = "", dial_code: str = "+998",
                           auto_activate: bool = True,
                           progress_cb=None) -> dict:
    """
    Полный цикл: создать temp email → зарегать на VFS → получить письмо → активировать.
    """
    from temp_mail import TempMailClient

    mail_client = None
    created_email = email

    if not email:
        try:
            mail_client = TempMailClient()
            created_email = mail_client.create_account()
            if progress_cb:
                progress_cb(f"Email создан: <code>{created_email}</code>")
        except Exception as e:
            return {"success": False, "error": f"Не удалось создать temp email: {e}"}

    registrar = VFSRegistrar(proxy_url=proxy_url)
    result = await registrar.register(created_email, phone, dial_code)

    if not result.get("success"):
        return result

    password = result["password"]

    if progress_cb:
        progress_cb(
            f"VFS регистрация OK!\n"
            f"Email: <code>{created_email}</code>\n"
            f"Пароль: <code>{password}</code>\n"
            f"Жду письмо активации..."
        )

    if not auto_activate:
        result["email"] = created_email
        return result

    if not mail_client:
        result["email"] = created_email
        result["message"] = "Аккаунт создан! Активируй вручную по ссылке из письма."
        return result

    try:
        message = mail_client.wait_for_email(
            from_contains="vfsglobal", timeout_sec=120, poll_sec=5)
    except Exception as e:
        logger.error("Mail polling error: %s", e)
        result["email"] = created_email
        result["message"] = f"Регистрация OK, но письмо не получено: {e}. Активируй вручную."
        return result

    if not message:
        result["email"] = created_email
        result["message"] = "Регистрация OK, но письмо не пришло за 2 мин. Проверь почту вручную."
        return result

    activation_link = mail_client.extract_activation_link(message)
    if not activation_link:
        all_links = mail_client.get_all_links(message)
        result["email"] = created_email
        result["message"] = f"Письмо получено, но ссылка не найдена. Ссылки: {all_links[:3]}"
        return result

    if progress_cb:
        progress_cb("Письмо получено! Активирую аккаунт...")

    activator = VFSRegistrar(proxy_url=proxy_url)
    act_result = await activator.activate_account(activation_link)

    result["email"] = created_email
    result["activated"] = act_result.get("success", False)
    if act_result.get("screenshot"):
        result["activation_screenshot"] = act_result["screenshot"]

    if act_result.get("success"):
        result["message"] = "Аккаунт создан и активирован! Готов к работе."
    else:
        result["message"] = (
            f"Аккаунт создан, но активация неясна. "
            f"Страница: {act_result.get('page_text', '?')[:100]}"
        )

    return result
