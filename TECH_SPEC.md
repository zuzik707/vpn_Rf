# ТЗ: VFS Global Slot Detector
## Узбекистан → Латвия | visa.vfsglobal.com/uzb/en/lva

---

## 1. Разведка: Защита VFS Global (актуально сентябрь 2026)

### Уровень 1: Cloudflare
| Что | Детали |
|-----|--------|
| **Cloudflare Waiting Room** | Стоит перед логином когда слоты открыты. Без реального браузера — не пройти. |
| **Cloudflare Turnstile CAPTCHA** | На странице логина. Заменили reCAPTCHA. Невидимая или интерактивная. |
| **cf_clearance cookie** | Выдаётся после прохождения challenge. Живёт 15-30 мин. Привязан к IP + User-Agent. |
| **TLS fingerprinting** | Cloudflare проверяет TLS-отпечаток клиента. Стандартный requests/urllib палится. |

### Уровень 2: Anti-Bot Detection
| Что | Детали |
|-----|--------|
| **navigator.webdriver** | Стандартный Playwright/Selenium ставит `true` — сразу детект. |
| **Canvas fingerprinting** | Сравнивают рендер canvas — headless отличается от реального. |
| **CDP leak detection** | Cloudflare проверяет наличие Chrome DevTools Protocol. |
| **Поведенческий анализ** | Скорость навигации, движения мыши, паттерны кликов. |

### Уровень 3: Rate Limiting & Account Bans
| Что | Детали |
|-----|--------|
| **IP блокировка** | Частые запросы → временный бан IP. |
| **Account ban** | Новое в 2025-2026. Паттерн автоматизации → бан аккаунта. |
| **Login throttling** | Частые логины → "Sorry, you have either..." lockout. |

### Что НЕ работает в 2026
- ❌ Чистый `requests` / `urllib` — Cloudflare блокирует
- ❌ Стандартный Playwright/Selenium — детектится через webdriver flag
- ❌ `undetected-chromedriver` — не обновляется с 2024
- ❌ `playwright-extra stealth` на VPS IP — блокируется
- ❌ FlareSolverr — устарел
- ❌ curl-impersonate соло — недостаточно

---

## 2. Выбранный стек (на основе бенчмарков 2026)

### Браузерная автоматизация: nodriver
**Почему nodriver:**
- Официальный наследник `undetected-chromedriver` от того же автора
- CDP-direct, async, без WebDriver flag
- В бенчмарке 2026: **0 блокировок из 31 Cloudflare-таргета**
- Есть плагин `nodriver-cf-bypass` для автоматического обхода Turnstile
- Python, активно поддерживается

**Fallback: Camoufox**
- Firefox-форк с C-level спуфингом canvas/WebGL/navigator
- 0% detection rate на тестах
- Если nodriver не пройдёт VFS — переключаемся на Camoufox

### CAPTCHA: 2Captcha API
**Как работает с Turnstile:**
1. Извлекаем `sitekey` со страницы VFS
2. Отправляем в 2Captcha API: `solver.turnstile(sitekey=..., url=...)`
3. 2Captcha решает challenge на своей ферме → возвращает token
4. Инжектим token в `cf-turnstile-response` input
5. Вызываем callback `window.tsCallback(token)`
6. Форма логина проходит

**Стоимость:** ~$3 за 1000 решений. При проверке каждые 5 мин = ~288/день = ~$0.86/день.

### Уведомления: Telegram Bot API
- Push приходит мгновенно на телефон
- Со ссылкой на бронирование

---

## 3. Архитектура

```
┌──────────────────────────────────────────────────┐
│                    main.py                        │
│              (scheduling loop)                    │
│         рандомный интервал 3-7 мин                │
└────────┬──────────────────────┬──────────────────┘
         │                      │
   ┌─────▼──────────┐    ┌────▼──────────┐
   │  browser.py     │    │  notifier.py   │
   │  (nodriver)     │    │  (telegram)    │
   │                 │    └────────────────┘
   │  - запуск Chrome│
   │  - stealth mode │
   │  - keep session │
   └────────┬────────┘
            │
   ┌────────▼────────┐
   │  captcha.py      │
   │  (2captcha API)  │
   │                  │
   │  - solve turnstile│
   │  - inject token  │
   └────────┬─────────┘
            │
   ┌────────▼─────────┐
   │  vfs_checker.py   │
   │                   │
   │  Два режима:      │
   │  1. DOM scraping  │
   │     (читаем текст │
   │      на странице)  │
   │  2. API intercept │
   │     (ловим ответ   │
   │      lift-api)     │
   └────────┬──────────┘
            │
   ┌────────▼──────────┐
   │  session.py        │
   │                    │
   │  - persistent      │
   │    browser profile │
   │  - cookie mgmt     │
   │  - JWT tracking    │
   │  - auto re-auth    │
   └────────────────────┘
```

## 4. Логика работы (пошагово)

```
1. Запуск nodriver (реальный Chrome, headed через Xvfb)
2. Открываем visa.vfsglobal.com/uzb/en/lva/login
3. Если Cloudflare challenge:
   a. nodriver-cf-bypass пытается пройти автоматом
   b. Если не удалось → ждём + retry
4. Если Turnstile CAPTCHA на логине:
   a. Извлекаем sitekey
   b. Отправляем в 2Captcha
   c. Получаем token → инжектим в форму
5. Вводим email/password, submit
6. Если залогинились → сохраняем сессию (cookies, profile)
7. Переходим на страницу бронирования
8. Проверяем наличие слотов:
   - Читаем DOM: ищем даты/текст "no appointments"
   - ИЛИ перехватываем API-ответ от lift-api
9. Если слоты найдены → Telegram уведомление
10. Ждём рандомный интервал (3-7 мин)
11. Повтор с шага 7 (не перелогиниваемся!)
12. Если сессия протухла → повтор с шага 2
```

## 5. Анти-бан меры

| Мера | Реализация |
|------|------------|
| Рандомные интервалы | 3-7 мин + джиттер ±20% |
| Один логин | Держим сессию, не перелогиниваемся каждый раз |
| Человекоподобное поведение | Рандомные задержки между действиями (0.5-2 сек) |
| Реальный Chrome | Через nodriver — не headless, а headed через Xvfb |
| User-Agent consistency | Не ротируем UA в рамках сессии (привязан к cf_clearance) |
| Тихие часы | Ночью не проверяем |
| Backoff при ошибках | Экспоненциальный рост интервала при 429/403 |
| Один аккаунт | Не параллелим запросы |

## 6. Зависимости

```
nodriver              # anti-detect браузер
2captcha-python       # решение Turnstile CAPTCHA
python-dotenv         # конфигурация из .env
requests              # HTTP для Telegram и 2Captcha
```

## 7. Конфигурация (.env)

```
# VFS Global
VFS_URL=https://visa.vfsglobal.com/uzb/en/lva/login
VFS_EMAIL=...
VFS_PASSWORD=...

# 2Captcha
CAPTCHA_API_KEY=...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Интервалы
CHECK_INTERVAL_MIN=180
CHECK_INTERVAL_MAX=420
QUIET_HOURS_START=23
QUIET_HOURS_END=6
```

## 8. Деплой на VPS

```bash
# Ubuntu 22.04+ VPS
sudo apt update
sudo apt install -y xvfb google-chrome-stable python3 python3-pip

# Запуск
pip install nodriver 2captcha-python python-dotenv requests
xvfb-run python main.py

# Или через systemd для автозапуска
```
