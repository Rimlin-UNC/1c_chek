#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — автоматическая установка на Ubuntu 24.04
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# Запуск:  sudo bash install.sh [--with-nginx] [--with-ssl yourdomain.ru]
#
# Скрипт:
#   1) ставит системные зависимости (python3, venv);
#   2) создаёт пользователя ymaster и каталог /opt/ymaster-check;
#   3) разворачивает приложение в venv;
#   4) инициализирует БД и администратора;
#   5) ставит systemd-сервис (автозапуск);
#   6) опционально: Nginx + SSL (Let's Encrypt).
# ======================================================================
set -euo pipefail

APP_DIR="/opt/ymaster-check"
APP_USER="ymaster"
SERVICE_NAME="ymaster-check"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_NGINX=0
DOMAIN=""
for arg in "$@"; do
  case "$arg" in
    --with-nginx) WITH_NGINX=1 ;;
    --with-ssl) DOMAIN="pending" ;;
    --with-ssl=*) DOMAIN="${arg#*=}" ;;
    *) echo "Неизвестный аргумент: $arg"; exit 1 ;;
  esac
done

if [[ "$DOMAIN" == "pending" && $# -lt 2 ]]; then
  echo "Укажите домен: --with-ssl=check.example.ru"; exit 1
fi

bold() { echo -e "\e[1m$*\e[0m"; }
ok()   { echo "  ✔ $*"; }

echo "=============================================================="
echo "  Ямастер Чек — установка (ООО «Ямастер», ymaster.ru)"
echo "=============================================================="

[[ $EUID -ne 0 ]] && { echo "Запустите через sudo: sudo bash install.sh"; exit 1; }

bold "1/6 Системные зависимости"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
ok "python3, venv, curl"

bold "2/6 Пользователь и каталоги"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$APP_DIR" "$APP_DIR/data"
rsync -a --exclude '.git' --exclude 'data/*.db*' "$SRC_DIR"/ "$APP_DIR"/
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
ok "$APP_DIR"

bold "3/6 Виртуальное окружение Python"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "зависимости установлены"

bold "4/6 Секреты (.env)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  ONETOK=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET
FNS_PROVIDER=mock
ONEC_API_TOKEN=$ONETOK
EOF
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  ok "сгенерированы SECRET_KEY и токен 1С"
else
  ok ".env уже существует — не трогаем"
fi

bold "5/6 Инициализация БД и администратор"
sudo -u "$APP_USER" sh -c "cd $APP_DIR && ./venv/bin/python -m app.seed"
ok "администратор: admin / admin123 (смените после входа!)"

bold "6/6 Сервис systemd"
cp "$APP_DIR/deploy/systemd/$SERVICE_NAME.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1 || systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active --quiet "$SERVICE_NAME" && ok "сервис $SERVICE_NAME запущен" || {
  echo "  ✘ сервис не запустился, журнал:"; journalctl -u "$SERVICE_NAME" -n 20 --no-pager; exit 1; }

if [[ $WITH_NGINX -eq 1 ]]; then
  bold "Бонус: Nginx"
  apt-get install -y -qq nginx >/dev/null
  cp "$APP_DIR/deploy/nginx/ymaster-check.conf" /etc/nginx/sites-available/ymaster-check
  [[ -n "$DOMAIN" && "$DOMAIN" != "pending" ]] && \
    sed -i "s/check.example.ru/$DOMAIN/g" /etc/nginx/sites-available/ymaster-check
  ln -sf /etc/nginx/sites-available/ymaster-check /etc/nginx/sites-enabled/
  nginx -t && systemctl reload nginx
  ok "Nginx настроен"
  if [[ -n "$DOMAIN" && "$DOMAIN" != "pending" ]]; then
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m info@ymaster.ru || \
      echo "  ⚠ certbot не смог выпустить сертификат — запустите вручную"
  fi
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "=============================================================="
echo "  Готово! Веб-клиент:  http://$IP:8000"
echo "  API-документация:    http://$IP:8000/api/docs"
echo "  Логин: admin / admin123  (смените пароль!)"
echo "  Статус: systemctl status $SERVICE_NAME"
echo "  Данные: $APP_DIR/data"
echo ""
echo "  Поддержка: ООО «Ямастер» — https://ymaster.ru · info@ymaster.ru"
echo "=============================================================="
