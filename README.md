# VFS Global Slot Detector

Мониторинг доступных слотов для записи на визу через VFS Global с уведомлениями в Telegram.

## Быстрый старт

```bash
# 1. Установи зависимости
pip install -r requirements.txt

# 2. Создай файл конфигурации
cp .env.example .env

# 3. Заполни .env своими данными (см. ниже)
nano .env

# 4. Запусти
python main.py
```

## Настройка .env

### VFS Global
- `VFS_SOURCE_COUNTRY` — откуда подаёшь (например `rus`)
- `VFS_DESTINATION_COUNTRY` — куда виза (например `fra`, `deu`, `ita`, `esp`)
- `VFS_CITY` — город визового центра
- `VFS_VISA_CATEGORY` — категория визы
- `VFS_EMAIL` / `VFS_PASSWORD` — логин/пароль от аккаунта VFS Global

### Telegram бот
1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен
2. Напиши [@userinfobot](https://t.me/userinfobot) → получи свой `chat_id`
3. Впиши оба значения в `.env`

### Интервалы
- `CHECK_INTERVAL_MIN` / `CHECK_INTERVAL_MAX` — диапазон в секундах между проверками (рандомится)
- `QUIET_HOURS_START` / `QUIET_HOURS_END` — тихие часы (UTC), ночью не проверяет

## Как это работает

1. Скрипт авторизуется в API VFS Global
2. Каждые 2-5 минут (настраивается) проверяет доступные слоты
3. При нахождении слота — шлёт уведомление в Telegram со ссылкой на бронирование
4. Автоматически обновляет токен авторизации
5. Рандомизирует интервалы и user-agent чтобы снизить риск блокировки
6. Backoff при ошибках, уведомление при 5 ошибках подряд

## Запуск на сервере (фоновый режим)

```bash
# Через nohup
nohup python main.py &

# Или через systemd (создай сервис)
# Или через screen/tmux
screen -S vfs
python main.py
# Ctrl+A, D для отключения
```

## Важно

- Не ставь слишком маленький интервал проверки — VFS может забанить
- Рекомендуемый минимум: 120 секунд
- Скрипт для личного использования — не злоупотребляй
