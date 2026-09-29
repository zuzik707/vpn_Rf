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

    def _run_registration(self, email: str, prefix: str = ""):
        if email:
            proxy = _make_proxy_for_account(email)
            display = email
        else:
            proxy = _make_proxy_for_account(prefix or f"auto{int(time.time())}")
            display = "авто (mail.tm)"

        self.send(
            f"Регистрирую аккаунт на VFS...\n"
            f"Email: <b>{display}</b>\n"
            f"Полный цикл: регистрация → письмо → активация\n"
            f"Это займёт 1-3 минуты"
        )

        bot = self  # capture for closure

        def do_reg():
            try:
                from vfs_register import register_account
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(
                    register_account(
                        email=email,
                        proxy_url=proxy,
                        auto_activate=True,
                        progress_cb=bot.send,
                    ))
                loop.close()

                if result.get("success"):
                    final_email = result.get("email", email)
                    password = result["password"]
                    final_proxy = _make_proxy_for_account(final_email)

                    # Сохраняем в БД
                    if add_account(final_email, password, final_proxy):
                        bot._notify_accounts_changed()

                    activated = result.get("activated", False)
                    status = "АКТИВИРОВАН" if activated else "Нужна активация"

                    bot.send(
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
                    bot.send(f"Регистрация не удалась:\n<code>{error}</code>")
                    if result.get("screenshot"):
                        try:
                            send_telegram_photo(result["screenshot"], "Ошибка регистрации")
                        except Exception:
                            pass

            except Exception as e:
                logger.error("Registration thread error: %s", e, exc_info=True)
                bot.send(f"Ошибка регистрации: <code>{e}</code>")

        t = threading.Thread(target=do_reg, daemon=True, name=f"reg-{email or 'auto'}")
        t.start()

    def _run_batch_registration(self, count: int):
        if count < 1 or count > 30:
            self.send("Количество: от 1 до 30")
            return
        self.send(f"Запускаю регистрацию {count} аккаунтов последовательно...\n"
                  f"Пауза 30-60с между каждым чтобы не словить 429")

        bot = self
        def do_batch():
            for i in range(count):
                bot.send(f"Регистрация {i+1}/{count}...")
                bot._run_registration("")
                if i < count - 1:
                    pause = random.uniform(30, 60)
                    time.sleep(pause)

        t = threading.Thread(target=do_batch, daemon=True, name="batch-reg")
        t.start()

    # ── Остальные команды ──────────────────────────────────────────

    def _cmd_remove(self, arg: str):
        if not arg:
            self.send("Формат: /remove email")
            return

        email = arg.strip()
        if remove_account(email):
            total = count_accounts()
            self.send(f"Аккаунт <b>{email}</b> удалён\nВсего активных: {total}")
            self._notify_accounts_changed()
        else:
            self.send(f"Аккаунт {email} не найден")

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
            "/status — статус мониторинга\n"
            "/cancel — отменить\n\n"
            "Каждому аккаунту свой IP через Bright Data"
        )

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
