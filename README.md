# VFS Global Slot Detector v2.0

Мониторинг слотов для визы **Узбекистан → Латвия** через VFS Global с обходом всех защит.

## Stack

| Компонент | Зачем |
|-----------|-------|
| **nodriver** | Anti-detect Chrome — реальный браузер без `webdriver` flag, нативный TLS/cookies |
| **2Captcha** | Решает Cloudflare Turnstile CAPTCHA через API |
| **Stealth модуль** | 10+ патчей: plugins, WebGL, canvas, CDP leaks, permissions, connection rtt |
| **Persistent session** | Cookies/profile сохраняются — не перелогиниваемся каждый раз |
| **Telegram** | Мгновенные push-уведомления + скриншоты |

## Что обходим

- Cloudflare Waiting Room
- Cloudflare Turnstile CAPTCHA
- `navigator.webdriver` детект
- Canvas/WebGL fingerprinting
- CDP leak detection
- Headless browser detection
- Rate limiting (рандомные интервалы + backoff)
- Поведенческий анализ (human-like typing/delays)

## Быстрый старт

```bash
# 1. Установка
chmod +x setup_vps.sh
./setup_vps.sh

# 2. Конфигурация
nano .env

# 3. Запуск
source venv/bin/activate
xvfb-run python main.py
```

## Ручная установка

```bash
pip install nodriver 2captcha-python requests python-dotenv
cp .env.example .env
nano .env
xvfb-run python main.py
```

## .env — что заполнить

| Переменная | Откуда взять |
|-----------|-------------|
| `VFS_EMAIL` | Твой email на visa.vfsglobal.com |
| `VFS_PASSWORD` | Пароль от аккаунта VFS |
| `CAPTCHA_API_KEY` | [2captcha.com](https://2captcha.com) — пополнить $3 |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → /newbot |
| `TELEGRAM_CHAT_ID` | [@userinfobot](https://t.me/userinfobot) |

## Запуск на VPS как сервис

```bash
# Отредактируй WorkingDirectory и пути в vfs-monitor.service
sudo cp vfs-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vfs-monitor
sudo systemctl start vfs-monitor

# Логи
sudo journalctl -u vfs-monitor -f
```

## Структура

```
main.py              — точка входа, scheduling loop
vfs_checker.py       — браузер + логин + проверка слотов
antidetect.py        — 10+ stealth патчей, chrome args
captcha_solver.py    — 2Captcha Turnstile solver
notifier.py          — Telegram уведомления + скриншоты
session_manager.py   — persistent session + статистика
config.py            — загрузка .env
setup_vps.sh         — скрипт установки на VPS
vfs-monitor.service  — systemd сервис
```

## Как это работает

1. Запускает реальный Chrome через nodriver (anti-detect)
2. Инжектит stealth патчи ДО загрузки страницы
3. Открывает VFS login → ждёт Cloudflare challenge
4. nodriver проходит Cloudflare автоматически (реальный Chrome)
5. Если Turnstile CAPTCHA → отправляет в 2Captcha → инжектит token
6. Логинится с human-like задержками
7. Переходит на страницу бронирования
8. Определяет наличие слотов по DOM-маркерам
9. Если слоты есть → Telegram push + скриншот
10. Спит рандомный интервал → повтор с шага 7
11. Если сессия протухла → рестарт с шага 1
