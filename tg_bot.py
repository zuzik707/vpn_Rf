"""
Telegram бот для управления VFS аккаунтами и мониторингом.

Команды:
  /add email:password        — добавить аккаунт
  /add email:password:proxy  — добавить аккаунт с прокси
  /remove email              — удалить аккаунт
  /list                      — список всех аккаунтов
  /enable email              — включить аккаунт
  /disable email             — выключить аккаунт
  /status                    — статус мониторинга
  /help                      — справка

Бот работает в отдельном потоке через polling.
"""

import logging
import threading
import time

import requests

from config import Config
from accounts_db import (
    add_account, remove_account, toggle_account,
    get_all_accounts, get_enabled_accounts, count_accounts,
)

logger = logging.getLogger(__name__)


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
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/add":
            self._cmd_add(arg)
        elif cmd == "/remove" or cmd == "/del" or cmd == "/delete":
            self._cmd_remove(arg)
        elif cmd == "/list" or cmd == "/accounts":
            self._cmd_list()
        elif cmd == "/enable":
            self._cmd_toggle(arg, True)
        elif cmd == "/disable":
            self._cmd_toggle(arg, False)
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/help" or cmd == "/start":
            self._cmd_help()

    def _cmd_add(self, arg: str):
        if not arg:
            self.send("Формат: /add email:password\nИли: /add email:password:proxy")
            return

        parts = arg.split(":", 2)
        if len(parts) < 2:
            self.send("Неверный формат. Нужно: /add email:password")
            return

        email = parts[0].strip()
        password = parts[1].strip()
        proxy = parts[2].strip() if len(parts) > 2 else ""

        if not email or not password:
            self.send("Email и пароль не могут быть пустыми")
            return

        if add_account(email, password, proxy):
            total = count_accounts()
            self.send(f"Аккаунт <b>{email}</b> добавлен\nВсего активных: {total}")
            self._notify_accounts_changed()
        else:
            self.send(f"Ошибка добавления {email}")

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
            self.send("Аккаунтов нет. Добавь: /add email:password")
            return

        lines = ["<b>Аккаунты VFS:</b>\n"]
        for i, a in enumerate(accounts, 1):
            status = "ON" if a["enabled"] else "OFF"
            fails = f" [{a['fail_count']} fails]" if a["fail_count"] else ""
            proxy = f" proxy" if a["proxy"] else ""
            lines.append(f"{i}. <code>{a['email']}</code> [{status}]{fails}{proxy}")

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

    def _cmd_help(self):
        self.send(
            "<b>VFS Monitor Bot</b>\n\n"
            "/add email:password — добавить аккаунт\n"
            "/add email:pass:proxy — с прокси\n"
            "/remove email — удалить\n"
            "/list — все аккаунты\n"
            "/enable email — включить\n"
            "/disable email — выключить\n"
            "/status — статус мониторинга\n"
            "/help — эта справка"
        )

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
