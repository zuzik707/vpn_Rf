"""
Telegram бот для управления VFS аккаунтами и мониторингом.

Пошаговый диалог:
  /add → бот спрашивает email → пользователь пишет email →
  бот спрашивает пароль → пользователь пишет пароль → сохранено в БД.
  /reg email — автоматическая регистрация на VFS + сохранение в БД.
  Каждому аккаунту автоматически назначается уникальная Bright Data сессия.

Команды:
  /reg email     — зарегистрировать аккаунт на VFS автоматически
  /add           — добавить существующий аккаунт (пошагово)
  /remove email  — удалить аккаунт
  /list          — список всех аккаунтов
  /enable email  — включить аккаунт
  /disable email — выключить аккаунт
  /status        — статус мониторинга
  /help          — справка
"""

import asyncio
import hashlib
import logging
import random
import re
import threading
import time

import requests

from config import Config
from accounts_db import (
    add_account, remove_account, toggle_account,
    get_all_accounts, count_accounts, get_banned_accounts,
)

logger = logging.getLogger(__name__)


def _make_proxy_for_account(email: str) -> str:
    """Генерирует уникальный Bright Data proxy URL для аккаунта.
    Каждый email получает свою sticky session — свой IP."""
    base_proxy = Config.PROXY_URL
    if not base_proxy:
        return ""
    if "brd.superproxy.io" not in base_proxy and "brightdata" not in base_proxy:
        return base_proxy

    # Убираем старую session если есть
    clean = re.sub(r'-session-[^:@]+', '', base_proxy)

    # Генерируем стабильный session ID из email (один email = один IP)
    session_hash = hashlib.md5(email.encode()).hexdigest()[:8]
    session_id = f"acct_{session_hash}"

    at_idx = clean.find('@')
    if at_idx == -1:
        return base_proxy
    colon_idx = clean.rfind(':', 0, at_idx)
    if colon_idx == -1:
        return base_proxy

    return clean[:colon_idx] + f"-session-{session_id}" + clean[colon_idx:]


async def _reactivate_account(email: str, vfs_password: str,
                               mail_password: str, proxy: str,
                               progress_cb=None) -> str:
    """Try to reactivate: check VFS login, if inactive → go to mail.tm → activate."""
    from vfs_checker import VFSBrowser
    from vfs_register import VFSRegistrar

    # First check if already active via VFS login
    checker = VFSBrowser(email, vfs_password, proxy)
    try:
        await checker.start_browser()
        await checker.warm_session()
        login_ok = await checker.login()
        if login_ok:
            return "already_active"
        if checker.banned:
            return "banned"
        # Check page for inactive message
        text = ""
        if checker.page:
            text = (await checker.page.evaluate(
                "document.body?.innerText || ''") or "").lower()
        if "currently inactive" not in text:
            return f"login failed (not inactive): {text[:80]}"
    except Exception as e:
        return f"login check error: {e}"
    finally:
        await checker.close_browser()

    # Account is inactive — go to mail.tm to get activation link
    if progress_cb:
        progress_cb(f"Аккаунт неактивен. Захожу в mail.tm...")

    from temp_mail import TempMailClient
    mail_client = TempMailClient()
    mail_client.address = email
    mail_client.password = mail_password

    # Login to mail.tm
    try:
        import requests as req
        token_resp = mail_client._session.post(
            f"https://api.mail.tm/token",
            json={"address": email, "password": mail_password},
            timeout=15,
        )
        if token_resp.status_code != 200:
            return f"mail.tm login failed ({token_resp.status_code})"
        mail_client.token = token_resp.json()["token"]
        mail_client._session.headers["Authorization"] = f"Bearer {mail_client.token}"
    except Exception as e:
        return f"mail.tm login error: {e}"

    # Look for activation email
    message = mail_client.wait_for_email(
        from_contains="vfsglobal", timeout_sec=10, poll_sec=2)
    if not message:
        # No email in inbox — try resend via VFS login
        if progress_cb:
            progress_cb("Письмо не найдено. Запрашиваю повторную отправку...")
        from vfs_register import _try_resend_activation
        resend_ok = await _try_resend_activation(
            email, proxy, mail_client, progress_cb, password=vfs_password)
        if resend_ok:
            return "activated"
        return "no activation email + resend failed"

    activation_link = mail_client.extract_activation_link(message)
    if not activation_link:
        return "email found but no activation link"

    if progress_cb:
        progress_cb("Ссылка найдена! Активирую...")

    activator = VFSRegistrar(proxy_url=proxy)
    act_result = await activator.activate_account(activation_link)
    if act_result.get("success"):
        return "activated"

    # Try resend as fallback
    if progress_cb:
        progress_cb("Прямая активация не сработала. Пробую resend...")
    from vfs_register import _try_resend_activation
    resend_ok = await _try_resend_activation(
        email, proxy, mail_client, progress_cb, password=vfs_password)
    if resend_ok:
        return "activated"
    return f"activation failed: {act_result.get('page_text', '?')[:80]}"


class TelegramBot:
    def __init__(self, token: str, chat_id: str, status_cb=None):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self.status_cb = status_cb
        self._on_accounts_changed: list = []

        # Состояние пошагового диалога: {chat_id: {"step": ..., "email": ...}}
        self._pending: dict[str, dict] = {}

    def on_accounts_changed(self, cb):
        self._on_accounts_changed.append(cb)

    def _notify_accounts_changed(self):
        for cb in self._on_accounts_changed:
            try:
                cb()
            except Exception as e:
                logger.error("accounts_changed callback error: %s", e)

    def send(self, text: str) -> bool:
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            return resp.ok
        except Exception as e:
            logger.error("TG send error: %s", e)
            return False

    def _get_updates(self) -> list:
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._offset, "timeout": 30},
                timeout=35,
            )
            if resp.ok:
                data = resp.json()
                return data.get("result", [])
        except Exception:
            pass
        return []

    def _handle_message(self, msg: dict):
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self.chat_id:
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        # Если есть pending диалог — обрабатываем ответ
        if chat_id in self._pending:
            # Но если пользователь прислал другую команду — отменяем pending
            if text.startswith("/"):
                del self._pending[chat_id]
                # И обрабатываем как команду ниже
            else:
                self._handle_pending(chat_id, text)
                return

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/reg" or cmd == "/register":
            self._cmd_reg_start(chat_id, arg)
        elif cmd == "/add":
            self._cmd_add_start(chat_id, arg)
        elif cmd in ("/remove", "/del", "/delete"):
            self._cmd_remove(arg)
        elif cmd == "/deleteall":
            self._cmd_deleteall(chat_id)
        elif cmd in ("/list", "/accounts"):
            self._cmd_list()
        elif cmd == "/enable":
            self._cmd_toggle(arg, True)
        elif cmd == "/disable":
            self._cmd_toggle(arg, False)
        elif cmd == "/status":
            self._cmd_status()
        elif cmd in ("/bans", "/banned"):
            self._cmd_bans()
        elif cmd == "/verify":
            self._cmd_verify()
        elif cmd == "/reactivate":
            self._cmd_reactivate()
        elif cmd == "/cleanbans":
            self._cmd_cleanbans()
        elif cmd in ("/help", "/start"):
            self._cmd_help()
        elif cmd == "/cancel":
            self.send("Нет активного действия для отмены")

    # ── Пошаговый диалог /add ──────────────────────────────────────

    def _cmd_add_start(self, chat_id: str, arg: str):
        # Если сразу передали email:password — быстрый путь
        if ":" in arg:
            parts = arg.split(":", 1)
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                self._save_account(email, password)
                return

        if arg and "@" in arg:
            # Передали только email — спрашиваем пароль
            self._pending[chat_id] = {"step": "password", "email": arg.strip()}
            self.send(f"Email: <b>{arg.strip()}</b>\n\nТеперь введи пароль:")
            return

        # Ничего не передали — начинаем диалог
        self._pending[chat_id] = {"step": "email"}
        self.send("Введи email аккаунта VFS:")

    def _handle_pending(self, chat_id: str, text: str):
        state = self._pending[chat_id]
        step = state["step"]

        if step == "email":
            email = text.strip()
            if "@" not in email or "." not in email:
                self.send("Это не похоже на email. Попробуй ещё раз или /cancel:")
                return
            state["email"] = email
            state["step"] = "password"
            self.send(f"Email: <b>{email}</b>\n\nТеперь введи пароль:")

        elif step == "password":
            password = text.strip()
            if len(password) < 3:
                self.send("Пароль слишком короткий. Попробуй ещё раз или /cancel:")
                return
            email = state["email"]
            del self._pending[chat_id]
            self._save_account(email, password)

        elif step == "reg_email":
            email = text.strip()
            if "@" not in email or "." not in email:
                self.send("Это не похоже на email. Попробуй ещё раз или /cancel:")
                return
            del self._pending[chat_id]
            self._run_registration(email)

        elif step == "confirm_deleteall":
            if text.strip().upper() == "ДА":
                del self._pending[chat_id]
                self._do_deleteall()
            else:
                del self._pending[chat_id]
                self.send("Отменено")

    def _save_account(self, email: str, password: str):
        proxy = _make_proxy_for_account(email)
        if add_account(email, password, proxy):
            total = count_accounts()
            proxy_info = ""
            if proxy:
                # Показываем только session ID, не весь proxy с паролями
                match = re.search(r'-session-([^:@]+)', proxy)
                if match:
                    proxy_info = f"\nProxy session: <code>{match.group(1)}</code>"
            self.send(
                f"Аккаунт добавлен!\n\n"
                f"Email: <b>{email}</b>\n"
                f"Пароль: <code>{'*' * len(password)}</code>{proxy_info}\n"
                f"Всего активных: {total}\n\n"
                f"Воркер запустится автоматически"
            )
            self._notify_accounts_changed()
        else:
            self.send(f"Ошибка добавления {email}")

    # ── Авто-регистрация /reg ─────────────────────────────────────

    def _cmd_reg_start(self, chat_id: str, arg: str):
        if arg and "@" in arg:
            # Юзер передал свой email
            self._run_registration(arg.strip())
        elif arg:
            # Юзер передал количество или prefix
            try:
                count = int(arg)
                self._run_batch_registration(count)
            except ValueError:
                self._run_registration("", prefix=arg.strip())
        else:
            # Без аргументов — создаём temp email автоматически
            self._run_registration("")

    def _do_single_registration(self, email: str, prefix: str = ""):
        """Synchronous registration — blocks until done. Call from a thread."""
        if email:
            proxy = _make_proxy_for_account(email)
        else:
            proxy = _make_proxy_for_account(prefix or f"auto{int(time.time())}")
            display = "авто (mail.tm)"

        self.send(
            f"Регистрирую аккаунт на VFS...\n"
            f"Email: <b>{email or display}</b>\n"
            f"Полный цикл: регистрация → письмо → активация\n"
            f"Это займёт 1-3 минуты"
        )

        try:
            from vfs_register import register_account
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                register_account(
                    email=email,
                    proxy_url=proxy,
                    auto_activate=True,
                    progress_cb=self.send,
                ))
            loop.close()

            if result.get("success"):
                final_email = result.get("email", email)
                password = result["password"]
                final_proxy = _make_proxy_for_account(final_email)
                mail_pwd = result.get("mail_password", "")

                if add_account(final_email, password, final_proxy,
                               mail_password=mail_pwd):
                    self._notify_accounts_changed()

                activated = result.get("activated", False)
                status = "АКТИВИРОВАН" if activated else "Нужна активация"

                self.send(
                    f"Аккаунт готов!\n\n"
                    f"Email: <code>{final_email}</code>\n"
                    f"Пароль: <code>{password}</code>\n"
                    f"Телефон: {result.get('phone', '?')}\n"
                    f"Статус: <b>{status}</b>\n\n"
                    f"{result.get('message', '')}\n\n"
                    f"Сохранено в БД. Воркер запустится автоматически."
                )
                for key in ("screenshot", "activation_screenshot"):
                    if result.get(key):
                        try:
                            send_telegram_photo(result[key], f"Рег. {final_email}")
                        except Exception:
                            pass
            else:
                error = result.get("error", "Неизвестная ошибка")
                self.send(f"Регистрация не удалась:\n<code>{error}</code>")
                if result.get("screenshot"):
                    try:
                        send_telegram_photo(result["screenshot"], "Ошибка регистрации")
                    except Exception:
                        pass

        except Exception as e:
            logger.error("Registration error: %s", e, exc_info=True)
            self.send(f"Ошибка регистрации: <code>{e}</code>")

    def _run_registration(self, email: str, prefix: str = ""):
        """Start registration in a background thread (for single /reg)."""
        t = threading.Thread(
            target=self._do_single_registration,
            args=(email, prefix),
            daemon=True, name=f"reg-{email or 'auto'}",
        )
        t.start()

    def _run_batch_registration(self, count: int):
        if count < 1 or count > 30:
            self.send("Количество: от 1 до 30")
            return
        self.send(f"Запускаю регистрацию {count} аккаунтов последовательно...\n"
                  f"Каждый ждёт завершения предыдущего + пауза 30-60с")

        bot = self
        def do_batch():
            ok = 0
            fail = 0
            for i in range(count):
                bot.send(f"Регистрация {i+1}/{count}...")
                before = count_accounts(enabled_only=True)
                bot._do_single_registration("")
                after = count_accounts(enabled_only=True)
                if after > before:
                    ok += 1
                else:
                    fail += 1
                if i < count - 1:
                    pause = random.uniform(30, 60)
                    bot.send(f"[{ok} ok / {fail} fail] Пауза {pause:.0f}с...")
                    time.sleep(pause)
            bot.send(f"Batch завершена!\n"
                     f"Успешно: {ok}\n"
                     f"Неудачно: {fail}\n"
                     f"Всего аккаунтов: {count_accounts(enabled_only=True)}")

        t = threading.Thread(target=do_batch, daemon=True, name="batch-reg")
        t.start()

    # ── Остальные команды ──────────────────────────────────────────

    def _cmd_remove(self, arg: str):
        if not arg:
            self.send("Формат:\n"
                      "/delete email — удалить один\n"
                      "/delete email1 email2 — несколько\n"
                      "/delete 1 3 5 — по номерам из /list\n"
                      "/deleteall — удалить ВСЕ")
            return

        # Support multiple: /delete email1 email2 email3
        # Support by number: /delete 1 3 5
        parts = arg.strip().split()
        accounts = get_all_accounts()

        to_delete = []
        for p in parts:
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(accounts):
                    to_delete.append(accounts[idx]["email"])
                else:
                    self.send(f"Номер {p} не существует (всего {len(accounts)})")
            else:
                to_delete.append(p.lower().strip())

        if not to_delete:
            self.send("Нечего удалять")
            return

        deleted = 0
        for email in to_delete:
            if remove_account(email):
                deleted += 1

        total = count_accounts()
        self.send(f"Удалено: {deleted}/{len(to_delete)}\nВсего активных: {total}")
        if deleted:
            self._notify_accounts_changed()

    def _cmd_deleteall(self, chat_id: str):
        accounts = get_all_accounts()
        if not accounts:
            self.send("Аккаунтов нет")
            return
        self._pending[chat_id] = {"step": "confirm_deleteall", "count": len(accounts)}
        self.send(f"Удалить ВСЕ {len(accounts)} аккаунтов?\n\n"
                  f"Напиши <b>ДА</b> для подтверждения или /cancel для отмены")

    def _do_deleteall(self):
        accounts = get_all_accounts()
        count = 0
        for a in accounts:
            if remove_account(a["email"]):
                count += 1
        self.send(f"Удалено ВСЕ аккаунты: {count}")
        if count:
            self._notify_accounts_changed()

    def _cmd_list(self):
        accounts = get_all_accounts()
        if not accounts:
            self.send("Аккаунтов нет. Добавь: /add")
            return

        lines = ["<b>Аккаунты VFS:</b>\n"]
        for i, a in enumerate(accounts, 1):
            status = "ON" if a["enabled"] else "OFF"
            fails = f" | {a['fail_count']} fails" if a["fail_count"] else ""
            # Показываем session ID прокси
            proxy_tag = ""
            if a.get("proxy"):
                match = re.search(r'-session-([^:@]+)', a["proxy"])
                if match:
                    proxy_tag = f" | IP:{match.group(1)[:8]}"
                else:
                    proxy_tag = " | proxy"
            lines.append(f"{i}. <code>{a['email']}</code> [{status}]{fails}{proxy_tag}")

        enabled = sum(1 for a in accounts if a["enabled"])
        lines.append(f"\nАктивных: {enabled}/{len(accounts)}")
        self.send("\n".join(lines))

    def _cmd_toggle(self, arg: str, enabled: bool):
        if not arg:
            self.send(f"Формат: /{'enable' if enabled else 'disable'} email")
            return

        email = arg.strip()
        action = "включён" if enabled else "выключен"
        if toggle_account(email, enabled):
            self.send(f"Аккаунт <b>{email}</b> {action}")
            self._notify_accounts_changed()
        else:
            self.send(f"Аккаунт {email} не найден")

    def _cmd_status(self):
        if self.status_cb:
            info = self.status_cb()
            self.send(info)
        else:
            enabled = count_accounts()
            total = count_accounts(enabled_only=False)
            self.send(
                f"<b>VFS Monitor</b>\n"
                f"Аккаунтов: {enabled}/{total}\n"
                f"Бот работает"
            )

    def _cmd_bans(self):
        banned = get_banned_accounts()
        if not banned:
            self.send("Забаненных аккаунтов нет")
            return
        lines = ["<b>Забаненные аккаунты:</b>\n"]
        for a in banned:
            notes = a.get("notes", "")
            lines.append(f"• <code>{a['email']}</code>\n  {notes}")
        lines.append(f"\nВсего: {len(banned)}")
        lines.append("\n/delete email — удалить из базы")
        lines.append("/cleanbans — удалить ВСЕ забаненные")
        self.send("\n".join(lines))

    def _cmd_verify(self):
        """Check each account via VFS login — report active vs inactive."""
        from accounts_db import get_enabled_accounts
        accounts = get_enabled_accounts()
        if not accounts:
            self.send("Нет активных аккаунтов для проверки")
            return
        self.send(f"Проверяю {len(accounts)} аккаунтов через VFS логин...\n"
                  f"Это займёт ~1-2 мин на каждый")

        bot = self
        def do_verify():
            active = []
            inactive = []
            errors = []
            for i, acct in enumerate(accounts):
                email = acct["email"]
                password = acct["password"]
                proxy = acct.get("proxy", "")
                bot.send(f"Проверка {i+1}/{len(accounts)}: {email}...")
                try:
                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(
                        VFSBot._verify_account_vfs(email, password, proxy))
                    loop.close()
                    if result == "active":
                        active.append(email)
                    elif result == "inactive":
                        inactive.append(email)
                    else:
                        errors.append(f"{email}: {result}")
                except Exception as e:
                    errors.append(f"{email}: {e}")

            msg = f"<b>Проверка завершена:</b>\n\n"
            msg += f"Активных: {len(active)}\n"
            msg += f"Неактивных: {len(inactive)}\n"
            if errors:
                msg += f"Ошибки: {len(errors)}\n"
            if inactive:
                msg += f"\n<b>Неактивные:</b>\n"
                for e in inactive:
                    msg += f"• <code>{e}</code>\n"
            if errors:
                msg += f"\n<b>Ошибки:</b>\n"
                for e in errors[:10]:
                    msg += f"• {e}\n"
            bot.send(msg)

        t = threading.Thread(target=do_verify, daemon=True, name="verify")
        t.start()

    def _cmd_reactivate(self):
        """Re-activate accounts by going back to mail.tm inbox."""
        from accounts_db import get_enabled_accounts
        accounts = get_enabled_accounts()
        with_mail = [a for a in accounts if a.get("mail_password")]
        if not with_mail:
            self.send("Нет аккаунтов с сохранённым mail.tm паролем.\n"
                      "Новые аккаунты через /reg будут сохранять пароль автоматически.")
            return

        self.send(f"Проверяю и реактивирую {len(with_mail)} аккаунтов...\n"
                  f"Захожу в mail.tm → ищу письмо → активирую")

        bot = self
        def do_reactivate():
            ok = 0
            fail = 0
            already = 0
            for i, acct in enumerate(with_mail):
                email = acct["email"]
                vfs_pwd = acct["password"]
                mail_pwd = acct["mail_password"]
                proxy = acct.get("proxy", "")
                bot.send(f"Реактивация {i+1}/{len(with_mail)}: {email}...")

                try:
                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(
                        _reactivate_account(email, vfs_pwd, mail_pwd, proxy,
                                          progress_cb=bot.send))
                    loop.close()

                    if result == "already_active":
                        already += 1
                        bot.send(f"{email} — уже активен")
                    elif result == "activated":
                        ok += 1
                        bot.send(f"{email} — АКТИВИРОВАН!")
                    else:
                        fail += 1
                        bot.send(f"{email} — не удалось: {result}")
                except Exception as e:
                    fail += 1
                    bot.send(f"{email} — ошибка: {e}")

                if i < len(with_mail) - 1:
                    time.sleep(random.uniform(5, 10))

            bot.send(f"<b>Реактивация завершена:</b>\n"
                     f"Уже активны: {already}\n"
                     f"Активированы: {ok}\n"
                     f"Не удалось: {fail}")

        t = threading.Thread(target=do_reactivate, daemon=True, name="reactivate")
        t.start()

    def _cmd_cleanbans(self):
        banned = get_banned_accounts()
        if not banned:
            self.send("Забаненных аккаунтов нет")
            return
        count = 0
        for a in banned:
            if remove_account(a["email"]):
                count += 1
        self.send(f"Удалено забаненных аккаунтов: {count}")
        if count:
            self._notify_accounts_changed()

    def _cmd_help(self):
        self.send(
            "<b>VFS Monitor Bot</b>\n\n"
            "<b>Регистрация:</b>\n"
            "/reg — создать 1 аккаунт (авто email + регистрация + активация)\n"
            "/reg 5 — создать 5 аккаунтов разом\n"
            "/reg user@mail.com — зарегать с конкретным email\n\n"
            "<b>Управление:</b>\n"
            "/add — добавить существующий аккаунт\n"
            "/remove email — удалить\n"
            "/list — все аккаунты\n"
            "/enable email — включить\n"
            "/disable email — выключить\n"
            "/bans — забаненные аккаунты\n"
            "/delete email — удалить аккаунт\n"
            "/cleanbans — удалить все забаненные\n"
            "/verify — проверить все аккаунты (логин на VFS)\n"
            "/status — статус мониторинга\n"
            "/cancel — отменить\n\n"
            "Каждому аккаунту свой IP через Bright Data"
        )

    # ── Polling loop ───────────────────────────────────────────────

    @staticmethod
    async def _verify_account_vfs(email: str, password: str, proxy: str) -> str:
        """Try VFS login. Returns 'active', 'inactive', 'banned', or error string."""
        from vfs_checker import VFSBrowser
        checker = VFSBrowser(email, password, proxy)
        try:
            await checker.start_browser()
            await checker.warm_session()
            login_ok = await checker.login()
            if checker.banned:
                return "banned"
            if login_ok:
                return "active"
            # Check page text for "inactive"
            text = ""
            if checker.page:
                text = (await checker.page.evaluate(
                    "document.body?.innerText || ''") or "").lower()
            if "currently inactive" in text or "inactive" in text:
                return "inactive"
            return f"login failed: {text[:100]}"
        except Exception as e:
            return f"error: {e}"
        finally:
            await checker.close_browser()

    # ── Polling loop ───────────────────────────────────────────────

    def _poll_loop(self):
        logger.info("TG bot polling started")
        while self._running:
            try:
                updates = self._get_updates()
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    if "message" in upd:
                        self._handle_message(upd["message"])
            except Exception as e:
                logger.error("TG bot poll error: %s", e)
                time.sleep(5)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-bot")
        self._thread.start()
        logger.info("TG bot started (chat_id=%s)", self.chat_id)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("TG bot stopped")
