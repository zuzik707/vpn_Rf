#!/bin/bash
# Скрипт установки на VPS (Ubuntu 22.04+)
# Запусти: chmod +x setup_vps.sh && ./setup_vps.sh

set -e

echo "=== VFS Slot Detector — Установка на VPS ==="

# 1. Системные зависимости
echo "[1/4] Устанавливаю системные пакеты..."
sudo apt update -qq
sudo apt install -y -qq python3 python3-pip python3-venv xvfb wget gnupg curl

# 2. Google Chrome (nodriver использует реальный Chrome)
if ! command -v google-chrome &> /dev/null; then
    echo "[2/4] Устанавливаю Google Chrome..."
    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo dpkg -i /tmp/chrome.deb || sudo apt-get -f install -y -qq
    rm /tmp/chrome.deb
else
    echo "[2/4] Google Chrome уже установлен"
fi

# 3. Python зависимости
echo "[3/4] Устанавливаю Python зависимости..."
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 4. Конфигурация
if [ ! -f .env ]; then
    echo "[4/4] Создаю .env из шаблона..."
    cp .env.example .env
    echo ""
    echo "============================================="
    echo "  ВАЖНО: Заполни .env файл своими данными!"
    echo "  nano .env"
    echo "============================================="
else
    echo "[4/4] .env уже существует"
fi

# Создаём директории
mkdir -p browser_data screenshots

echo ""
echo "=== Установка завершена ==="
echo ""
echo "Запуск:"
echo "  source venv/bin/activate"
echo "  xvfb-run python main.py"
echo ""
echo "Или через systemd:"
echo "  sudo cp vfs-monitor.service /etc/systemd/system/"
echo "  sudo systemctl enable vfs-monitor"
echo "  sudo systemctl start vfs-monitor"
echo "  sudo journalctl -u vfs-monitor -f"
