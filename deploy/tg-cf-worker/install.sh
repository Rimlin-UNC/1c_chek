#!/usr/bin/env bash
# Ямастер Чек v1.70.68 — установка локального прокси tgwsapiproxy
# (Ubuntu 24.04, ТЗ §5.2). Запуск:
#   sudo ./install.sh <ваш-домен>.workers.dev
set -euo pipefail

WORKER_HOST="${1:-}"
if [ -z "$WORKER_HOST" ]; then
  echo "Использование: sudo ./install.sh <ваш-домен>.workers.dev"
  exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "1/6. Системные пакеты (python3-venv, git)…"
apt-get update -qq
apt-get install -y -qq python3-venv git >/dev/null

echo "2/6. Служебный пользователь tgproxy…"
id -u tgproxy >/dev/null 2>&1 || \
  useradd --system --home-dir /opt/tgwsapiproxy --shell /usr/sbin/nologin tgproxy

echo "3/6. Виртуальное окружение /opt/tgwsapiproxy/venv…"
mkdir -p /opt/tgwsapiproxy
[ -d /opt/tgwsapiproxy/venv ] || python3 -m venv /opt/tgwsapiproxy/venv

echo "4/6. Установка tgwsapiproxy из Git (нулевые зависимости)…"
/opt/tgwsapiproxy/venv/bin/pip install --no-cache-dir --quiet \
  "git+https://github.com/winchanii/TgWsApiProxy"

echo "5/6. Служба systemd (Restart=always)…"
sed "s/__WORKER_HOST__/${WORKER_HOST}/" "$DIR/tgwsapiproxy.service" \
  > /etc/systemd/system/tgwsapiproxy.service
systemctl daemon-reload
systemctl enable --now tgwsapiproxy

echo "6/6. Проверка…"
sleep 2
systemctl is-active tgwsapiproxy && echo "Служба активна."
CODE=$(curl -s -m 15 -x http://127.0.0.1:8080 -o /dev/null -w '%{http_code}' \
       https://api.telegram.org/ || true)
echo "api.telegram.org через прокси: HTTP ${CODE:-000} (2xx/3xx/4xx — связь есть)"

echo
echo "Готово. В программе: Настройки → Чек-Пул и интеграции → Telegram-бот →"
echo "Прокси = http://127.0.0.1:8080  (или TELEGRAM_PROXY=http://127.0.0.1:8080 в .env)."
echo "Контроль: ./check_proxy.sh ${WORKER_HOST}"
