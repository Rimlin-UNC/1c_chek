#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — развёртывание на сервере Ubuntu 24.04 прямо из GitHub
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# БЫСТРЫЙ СТАРТ НА ЧИСТОМ СЕРВЕРЕ (боевой: chek.ymaster.ru = 94.183.236.179):
#   ssh root@94.183.236.179
#   curl -fsSL https://raw.githubusercontent.com/Rimlin-UNC/1c_chek/arena/01a0caaa-1c-chek/deploy.sh -o deploy.sh
#   sudo bash deploy.sh                          # установка
#   sudo bash deploy.sh --domain=chek.ymaster.ru --with-ssl=chek.ymaster.ru  # + SSL
#   sudo bash deploy.sh --update                 # обновление (домен/SSL запомнены
#                                                #   в /etc/ymaster-check/deploy.conf
#                                                #   и применяются автоматически)
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
DIAGNOSE=0
WITH_NGINX=1
CONF_DIR="/etc/ymaster-check"
CONF_FILE="$CONF_DIR/deploy.conf"

for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    --diagnose) DIAGNOSE=1 ;;
    --no-nginx) WITH_NGINX=0 ;;
    --with-ssl=*) OPT_SSL="${arg#*=}"; WITH_NGINX=1 ;;
    --no-ssl) OPT_SSL="none" ;;
    --domain=*) OPT_DOMAIN="${arg#*=}" ;;
    --repo=*) REPO_URL="${arg#*=}" ;;
    --branch=*) BRANCH="${arg#*=}" ;;
    -h|--help)
      sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg"; exit 1 ;;
  esac
done

# v1.6.1: параметры предыдущего запуска (домен, SSL, ветка) сохраняются в
# /etc/ymaster-check/deploy.conf и автоматически применяются при --update.
# Раньше повторный запуск без аргументов затирал домен и HTTPS-конфиг Nginx.
if [[ -f "$CONF_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONF_FILE"
  echo "  ℹ Параметры прошлого запуска: домен=${DOMAIN:-нет}, ssl=${SSL_DOMAIN:-нет}, ветка=$BRANCH"
fi
DOMAIN="${OPT_DOMAIN:-$DOMAIN}"
SSL_DOMAIN="${OPT_SSL:-$SSL_DOMAIN}"
[[ "$SSL_DOMAIN" == "none" ]] && SSL_DOMAIN=""
BRANCH="${OPT_BRANCH:-$BRANCH}"

# ------------------------------------------------------------------
# Режим диагностики: deploy.sh --diagnose — быстро понять, почему сайт недоступен
if [[ $DIAGNOSE -eq 1 ]]; then
  echo "======== Диагностика Ямастер Чек ========"
  echo "-- сервис:"; systemctl is-active "$SERVICE" || true
  journalctl -u "$SERVICE" -n 12 --no-pager 2>/dev/null | tail -12 || true
  echo "-- приложение напрямую (health):"
  curl -s -o /dev/null -w "   127.0.0.1:8000/health -> HTTP %{http_code}\n" --max-time 5 http://127.0.0.1:8000/health || echo "   127.0.0.1:8000 -> НЕ ОТВЕЧАЕТ"
  echo "-- nginx:"; nginx -t 2>&1 || true
  systemctl is-active nginx || true
  echo "-- порты (80/443/8000):"
  ss -tlnp 2>/dev/null | grep -E ':(80|443|8000)\s' || echo "   ничего не слушает?!"
  echo "-- локальный сайт через nginx:"
  curl -s -o /dev/null -w "   http://127.0.0.1/ -> HTTP %{http_code}\n" --max-time 5 http://127.0.0.1/ || echo "   http://127.0.0.1 -> НЕ ОТВЕЧАЕТ"
  D="${SSL_DOMAIN:-$DOMAIN}"
  if [[ -n "$D" && -f "/etc/letsencrypt/live/$D/fullchain.pem" ]]; then
    echo "-- https ($D):"
    curl -sk -o /dev/null -w "   https://127.0.0.1/ (SNI $D) -> HTTP %{http_code}\n" --max-time 5 --resolve "$D:443:127.0.0.1" "https://$D/" || echo "   https -> НЕ ОТВЕЧАЕТ"
    openssl x509 -in "/etc/letsencrypt/live/$D/fullchain.pem" -noout -dates 2>/dev/null | sed 's/^/   /' || true
  fi
  echo "-- UFW:"; ufw status 2>/dev/null | sed -n '1,8p' || true
  echo "========================================="
  exit 0
fi

# Автопоиск домена: если сертификат уже выпускался — восстанавливаем HTTPS
if [[ -z "$SSL_DOMAIN" && -d /etc/letsencrypt/live ]]; then
  DETECTED=$(ls /etc/letsencrypt/live 2>/dev/null | grep -Ev 'README|^$' | head -1 || true)
  if [[ -n "$DETECTED" ]]; then
    SSL_DOMAIN="$DETECTED"
    echo "  ℹ Найден выпущенный сертификат: $DETECTED — HTTPS будет сохранён"
  fi
fi

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
ok "администратор: admin / admin123 (смените при первом входе; если пароль уже меняли — он сохранён)"

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
  NGX_CONF=/etc/nginx/sites-available/ymaster-check
  cp "$APP_DIR/deploy/nginx/ymaster-check.conf" "$NGX_CONF"
  if [[ -n "${SSL_DOMAIN:-$DOMAIN}" ]]; then
    sed -i "s/server_name _;/server_name ${SSL_DOMAIN:-$DOMAIN};/" "$NGX_CONF"
  fi
  # v1.6.1: если сертификат уже выпущен — сразу собираем HTTPS-блок
  # (прежде повторный --update возвращал голый HTTP-шаблон)
  D="${SSL_DOMAIN:-$DOMAIN}"
  if [[ -n "$D" && -f "/etc/letsencrypt/live/$D/fullchain.pem" ]]; then
    EXTRA_SSL=""
    [[ -f /etc/letsencrypt/options-ssl-nginx ]] && EXTRA_SSL="    include /etc/letsencrypt/options-ssl-nginx;"
    [[ -f /etc/letsencrypt/ssl-dhparams.pem ]] && EXTRA_SSL="${EXTRA_SSL}
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"
    sed -i "s|listen 80;|listen 443 ssl; listen [::]:443 ssl;|" "$NGX_CONF"
    # вставка ssl-строк после server_name через awk: sed-«a\» внутри кавычек
    # ломается — bash склеивает backslash+перевод строки
    awk -v d="$D" -v cdir="/etc/letsencrypt/live/$D" -v extra="$EXTRA_SSL" '
      { print }
      /server_name / && !ins {
        ins = 1
        print "    ssl_certificate " cdir "/fullchain.pem;"
        print "    ssl_certificate_key " cdir "/privkey.pem;"
        if (extra != "") print extra
      }' "$NGX_CONF" > "$NGX_CONF.tmp" && mv "$NGX_CONF.tmp" "$NGX_CONF"
    cat >> "$NGX_CONF" <<NGXEOF

# HTTP -> HTTPS — восстанавливается deploy.sh при каждом обновлении
server {
    listen 80;
    listen [::]:80;
    server_name $D;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
NGXEOF
    ok "HTTPS восстановлен из существующего сертификата ($D)"
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
ufw limit OpenSSH >/dev/null 2>&1 || ufw limit 22/tcp >/dev/null  # анти-брутфорс SSH
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true
cp "$APP_DIR/deploy/fail2ban/ymaster-check.conf" /etc/fail2ban/filter.d/ymaster-check.conf
cp "$APP_DIR/deploy/fail2ban/jail-ymaster.local" /etc/fail2ban/jail.d/ymaster.local
systemctl restart fail2ban 2>/dev/null || warn "fail2ban не перезапустился (проверьте: systemctl status fail2ban)"
ufw status | grep -q "80/tcp" && ok "UFW: снаружи только 22 (limit), 80, 443. Порт 8000 закрыт."
fail2ban-client status ymaster-check >/dev/null 2>&1 && ok "fail2ban: бан за перебор паролей активен"

# ------------------------------------------------------------------
bold "9/9 SSL (Let's Encrypt) — опционально"
if [[ -n "$SSL_DOMAIN" && -f "/etc/letsencrypt/live/$SSL_DOMAIN/fullchain.pem" ]]; then
  ok "SSL-сертификат на месте ($SSL_DOMAIN), HTTPS работает, продление — таймер certbot"
elif [[ -n "$SSL_DOMAIN" ]]; then
  # Preflight: домен должен указывать на ЭТОТ сервер, иначе Let's Encrypt не пройдёт
  SERVER_IP=$(curl -4 -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
  DOMAIN_IP=$(getent ahostsv4 "$SSL_DOMAIN" | awk '{print $1; exit}')
  echo "  Домен $SSL_DOMAIN -> ${DOMAIN_IP:-не найден}; этот сервер: $SERVER_IP"
  if [[ -n "$DOMAIN_IP" && "$DOMAIN_IP" == "$SERVER_IP" ]]; then
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    certbot --nginx -d "$SSL_DOMAIN" --non-interactive --agree-tos --redirect \
      -m info@ymaster.ru && ok "сертификат выпущен, HTTPS включён, http -> https автоматически" \
      || warn "certbot не смог выпустить сертификат — повторите позже: sudo bash deploy.sh --update --with-ssl=$SSL_DOMAIN"
  else
    warn "DNS $SSL_DOMAIN пока не указывает на этот сервер ($DOMAIN_IP != $SERVER_IP)."
    echo "     Сайт уже работает по http://$SERVER_IP — выпустите SSL после обновления DNS:"
    echo "     sudo bash deploy.sh --update --with-ssl=$SSL_DOMAIN"
  fi
else
  warn "SSL не настроен. Для камеры на телефонах нужен HTTPS:"
  echo "     sudo bash deploy.sh --update --with-ssl=ваш-домен.ru"
fi

# v1.6.1: самопроверка — деплой сразу говорит, отвечает ли сайт
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://127.0.0.1/ 2>/dev/null || echo 000)
if [[ "$HTTP_CODE" =~ ^(200|301|302|401)$ ]]; then
  ok "сайт отвечает (HTTP $HTTP_CODE)"
else
  warn "сайт НЕ отвечает через nginx (HTTP $HTTP_CODE) — смотрите: journalctl -u $SERVICE -n 30"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
APP_VERSION=$(grep -oP 'APP_VERSION: str = "\K[^"]+' "$APP_DIR/app/config.py" 2>/dev/null || echo "?")

# v1.6.1: запоминаем параметры — следующий --update подхватит их сам
mkdir -p "$CONF_DIR"
cat > "$CONF_FILE" <<CFGEOF
# Параметры развёртывания Ямастер Чек (используются при повторных запусках deploy.sh)
DOMAIN="$DOMAIN"
SSL_DOMAIN="$SSL_DOMAIN"
BRANCH="$BRANCH"
CFGEOF
chown -R "$APP_USER:$APP_USER" "$CONF_DIR" 2>/dev/null || true

if [[ -n "$SSL_DOMAIN" && -f "/etc/letsencrypt/live/$SSL_DOMAIN/fullchain.pem" ]]; then
  URL="https://$SSL_DOMAIN"; ALT="  (или http://$IP)"
elif [[ -n "$SSL_DOMAIN" ]]; then
  URL="http://$SSL_DOMAIN"; ALT="  (или http://$IP)"
else
  URL="http://$IP"; ALT=""
fi
echo ""
echo "=============================================================="
echo "  ✅ Ямастер Чек v$APP_VERSION развёрнут!"
echo "  Веб-клиент:       $URL$ALT"
echo "  Swagger API:      $URL/api/docs"
echo "  Логин:            admin  (пароль admin123 — только если не меняли)"
echo ""
echo "  Управление:  systemctl status $SERVICE"
echo "  Обновление:  sudo bash deploy.sh --update"
echo "  Логи:        journalctl -u $SERVICE -f"
echo "  Безопасность: UFW + fail2ban + rate-limit активны"
echo "  Диагностика: sudo bash deploy.sh --diagnose   (если сайт недоступен)"
echo ""
echo "  ООО «Ямастер» — https://ymaster.ru · info@ymaster.ru"
echo "=============================================================="
