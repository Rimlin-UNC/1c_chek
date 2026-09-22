#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — развёртывание на сервере Ubuntu 24.04 прямо из GitHub
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# БЫСТРЫЙ СТАРТ НА ЧИСТОМ СЕРВЕРЕ (пример: 94.183.236.179):
#   ssh root@94.183.236.179
#   curl -fsSL https://raw.githubusercontent.com/Rimlin-UNC/1c_chek/arena/01a0caaa-1c-chek/deploy.sh -o deploy.sh
#   sudo bash deploy.sh                          # установка
#   sudo bash deploy.sh --with-ssl=check.my.ru   # установка + SSL
#   sudo bash deploy.sh --update                 # обновление версии с GitHub
#
# Что делает:
#   1) ставит пакеты: nginx, ufw, fail2ban, python3-venv, git, sqlite3, certbot*;
#   2) клонирует ветку приложения с GitHub в /opt/ymaster-check;
#   3) создаёт пользователя ymaster, venv, генерирует секреты в .env;
#   4) инициализирует БД, администратора (admin/admin123 → смена при входе);
#   5) systemd-сервис с усиленной изоляцией;
#   6) Nginx: rate-limit (анти-DoS), security headers, закрытые служебные пути;
#   7) UFW: снаружи только 22/80/443, порт 8000 закрыт;
#   8) fail2ban: бан IP за перебор паролей (по access-логу Nginx);
#   9) опционально: сертификат Let's Encrypt.
# ======================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Rimlin-UNC/1c_chek.git}"
BRANCH="${BRANCH:-arena/01a0caaa-1c-chek}"
APP_DIR="/opt/ymaster-check"
APP_USER="ymaster"
SERVICE="ymaster-check"

DOMAIN=""
SSL_DOMAIN=""
UPDATE=0
WITH_NGINX=1

for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    --no-nginx) WITH_NGINX=0 ;;
    --with-ssl=*) SSL_DOMAIN="${arg#*=}"; WITH_NGINX=1 ;;
    --domain=*) DOMAIN="${arg#*=}" ;;
    --repo=*) REPO_URL="${arg#*=}" ;;
    --branch=*) BRANCH="${arg#*=}" ;;
    -h|--help)
      sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg"; exit 1 ;;
  esac
done

bold() { echo -e "\n\e[1m==> $*\e[0m"; }
ok()   { echo "  ✔ $*"; }
warn() { echo "  ⚠ $*"; }

[[ $EUID -ne 0 ]] && { echo "Запустите через sudo: sudo bash deploy.sh"; exit 1; }

# ------------------------------------------------------------------
bold "1/9 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl sqlite3 \
    nginx ufw fail2ban >/dev/null
ok "python3, git, nginx, ufw, fail2ban"

# ------------------------------------------------------------------
bold "2/9 Пользователь и каталог"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ------------------------------------------------------------------
bold "3/9 Код с GitHub ($REPO_URL, ветка $BRANCH)"
if [[ $UPDATE -eq 1 && -d "$APP_DIR/.git" ]]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
  ok "обновлено из GitHub"
else
  rm -rf "$APP_DIR/app" "$APP_DIR/docs" "$APP_DIR/deploy" 2>/dev/null || true
  if sudo -u "$APP_USER" git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR" 2>/dev/null; then
    ok "репозиторий склонирован"
  else
    warn "Публичное клонирование не удалось (приватный репозиторий?)."
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
      sudo -u "$APP_USER" git clone --depth 1 \
        "https://x-access-token:${GITHUB_TOKEN}@github.com/Rimlin-UNC/1c_chek.git" "$APP_DIR"
      sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
      ok "склонировано с GITHUB_TOKEN"
    else
      echo "  Вариант A: запустите с токеном:  GITHUB_TOKEN=ghp_xxx sudo -E bash deploy.sh"
      echo "  Вариант B: сделайте репозиторий публичным."
      exit 1
    fi
  fi
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ------------------------------------------------------------------
bold "4/9 Python-окружение"
if [[ ! -d "$APP_DIR/venv" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "зависимости установлены"

# ------------------------------------------------------------------
bold "5/9 Секреты (.env)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET
FNS_PROVIDER=mock
FNS_MASTER_TOKEN=
ONEC_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
EOF
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  ok "сгенерированы SECRET_KEY и токен 1С"
else
  ok ".env сохранён (не перезаписан)"
fi

# ------------------------------------------------------------------
bold "6/9 База данных и администратор"
sudo -u "$APP_USER" sh -c "cd $APP_DIR && ./venv/bin/python -m app.seed"
ok "администратор: admin / admin123 (сменится при первом входе — обязательно)"

# ------------------------------------------------------------------
bold "7/9 Сервис systemd"
cp "$APP_DIR/deploy/systemd/$SERVICE.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" && ok "сервис запущен" || {
  echo "  ✘ Сервис не запустился. Журнал:"; journalctl -u "$SERVICE" -n 25 --no-pager; exit 1; }

# ------------------------------------------------------------------
bold "8/9 Nginx + файрвол UFW + fail2ban"
if [[ $WITH_NGINX -eq 1 ]]; then
  cp "$APP_DIR/deploy/nginx/ymaster-check.conf" /etc/nginx/sites-available/ymaster-check
  if [[ -n "${SSL_DOMAIN:-$DOMAIN}" ]]; then
    sed -i "s/server_name _;/server_name ${SSL_DOMAIN:-$DOMAIN};/" \
      /etc/nginx/sites-available/ymaster-check
  fi
  rm -f /etc/nginx/sites-enabled/default
  ln -sf /etc/nginx/sites-available/ymaster-check /etc/nginx/sites-enabled/
  nginx -t && systemctl reload nginx
  ok "Nginx настроен (rate-limit, headers, служебные пути закрыты)"
fi

# UFW: снаружи только SSH/HTTP/HTTPS; 8000 остаётся внутренним
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw limit OpenSSH >/dev/null      # защита от брутфорса SSH
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true
cp "$APP_DIR/deploy/fail2ban/ymaster-check.conf" /etc/fail2ban/filter.d/ymaster-check.conf
cp "$APP_DIR/deploy/fail2ban/jail-ymaster.local" /etc/fail2ban/jail.d/ymaster.local
systemctl restart fail2ban
ufw status | grep -q "80/tcp" && ok "UFW: снаружи только 22 (limit), 80, 443. Порт 8000 закрыт."
fail2ban-client status ymaster-check >/dev/null 2>&1 && ok "fail2ban: бан за перебор паролей активен"

# ------------------------------------------------------------------
bold "9/9 SSL (Let's Encrypt) — опционально"
if [[ -n "$SSL_DOMAIN" ]]; then
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  certbot --nginx -d "$SSL_DOMAIN" --non-interactive --agree-tos \
    -m info@ymaster.ru && ok "сертификат выпущен, HTTPS включён" \
    || warn "certbot не смог выпустить сертификат — проверьте DNS (A-запись → этот сервер)"
else
  warn "SSL не настроен. Для камеры на телефонах нужен HTTPS:"
  echo "     sudo bash deploy.sh --update --with-ssl=ваш-домен.ru"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "=============================================================="
echo "  ✅ Ямастер Чек развёрнут!"
echo "  Веб-клиент:       http://$IP  (или http://$IP:80, домен)"
echo "  Swagger API:      http://$IP/api/docs"
echo "  Логин:            admin / admin123  → смена пароля при входе"
echo ""
echo "  Управление:  systemctl status $SERVICE"
echo "  Обновление:  sudo bash deploy.sh --update"
echo "  Логи:        journalctl -u $SERVICE -f"
echo "  Безопасность: UFW + fail2ban + rate-limit активны"
echo ""
echo "  ООО «Ямастер» — https://ymaster.ru · info@ymaster.ru"
echo "=============================================================="
