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
#   sudo bash rollback.sh                        # СПАСАТЕЛЬ: список рабочих версий
#   sudo bash rollback.sh 1.44.2                 # откат к рабочей версии (когда
#                                                #   приложение не запускается)
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
FIXSSL=0
WITH_NGINX=1
CERTBOT_BIN="$(command -v certbot || echo /snap/bin/certbot)"
CONF_DIR="/etc/ymaster-check"
CONF_FILE="$CONF_DIR/deploy.conf"

for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    --rollback) ROLLBACK="list" ;;                    # v1.46.0
    --rollback=*) ROLLBACK="${arg#*=}" ;;             # v1.46.0: к версии
    --diagnose) DIAGNOSE=1 ;;
    --fix-ssl) FIXSSL=1 ;;
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
# ------------------------------------------------------------------
# v1.55.1: пути данных — для страховых копий и диагностики
BACKUP_ROOT="/var/backups/ymaster-check"
DB_FILE="$APP_DIR/data/ymaster_check.db"

# Режим ремонта SSL: deploy.sh --fix-ssl — diagnos + выпуск/продление с полным выводом
if [[ $FIXSSL -eq 1 ]]; then
  echo "======== Починка SSL — Ямастер Чек ========"
  D="${SSL_DOMAIN:-$DOMAIN}"
  echo "-- сертификаты на сервере:"
  ls /etc/letsencrypt/live 2>/dev/null || echo "   (папка letsencrypt пуста/отсутствует)"
  if [[ -n "$D" && -f "/etc/letsencrypt/live/$D/fullchain.pem" ]]; then
    echo "-- срок текущего сертификата ($D):"
    openssl x509 -in "/etc/letsencrypt/live/$D/fullchain.pem" -noout -dates 2>/dev/null | sed 's/^/   /'
    echo "-- пробую продлить (полный вывод):"
    "$CERTBOT_BIN" renew --cert-name "$D" ; RC=$?
  else
    [[ -z "$D" ]] && { echo "   Домен не задан. Запустите: sudo bash deploy.sh --update --with-ssl=ваш-домен.ru"; exit 1; }
    echo "-- сертификата нет, выпускаю новый для $D (полный вывод):"
    SERVER_IP=$(curl -4 -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
    DOMAIN_IP=$(getent ahostsv4 "$D" | awk '{print $1; exit}')
    echo "   DNS: $D -> ${DOMAIN_IP:-не найден} | этот сервер: $SERVER_IP"
    [[ "$DOMAIN_IP" != "$SERVER_IP" ]] && echo "   ⚠ DNS указывает не на этот сервер — Let's Encrypt не пройдёт!"
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1 || true
    "$CERTBOT_BIN" certonly --nginx -d "$D" --non-interactive --agree-tos \
      -m info@ymaster.ru ; RC=$?
  fi
  if [[ ${RC:-1} -eq 0 ]]; then
    ok "SSL готов. Перезапускаю nginx и собираю HTTPS-конфиг: sudo bash deploy.sh --update"
    systemctl reload nginx 2>/dev/null || true
  else
    warn "certbot завершился с ошибкой ${RC} — текст выше содержит точную причину"
    echo "   Лимиты Let's Encrypt: 5 одинаковых сертификатов в неделю / 5 неудач проверки в час."
  fi
  exit ${RC:-1}
fi

# Режим диагностики: deploy.sh --diagnose — быстро понять, почему сайт недоступен
if [[ $DIAGNOSE -eq 1 ]]; then
  echo "======== Диагностика Ямастер Чек ========"
  echo "-- сервис:"; systemctl is-active "$SERVICE" || true
  journalctl -u "$SERVICE" -n 12 --no-pager 2>/dev/null | tail -12 || true
  echo "-- приложение напрямую (health):"
  curl -s -o /dev/null -w "   127.0.0.1:8000/health -> HTTP %{http_code}\n" --max-time 5 http://127.0.0.1:8000/health || echo "   127.0.0.1:8000 -> НЕ ОТВЕЧАЕТ"
  echo "-- база данных:"
  if [[ -f "$DB_FILE" ]]; then
    ls -l "$DB_FILE" | awk '{print "   файл: " $9 " (" $5 " байт)"}'
    sqlite3 "$DB_FILE" "SELECT '   users: '||COUNT(*) FROM users; SELECT '   receipts: '||COUNT(*) FROM receipts;" 2>/dev/null || true
  else
    echo "   файл базы НЕ НАЙДЕН: $DB_FILE"
  fi
  ls -1t "$BACKUP_ROOT"/ymaster_check-*.db 2>/dev/null | head -3 | sed 's/^/   копия: /' || true
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
  echo "-- certbot (автопродление):"
  systemctl is-active certbot.timer 2>/dev/null && echo "   certbot.timer: активен" \
    || { systemctl is-active snap.certbot.renew.timer >/dev/null 2>&1 && echo "   snap.certbot.renew.timer: активен" \
         || echo "   таймер не найден (см.: systemctl list-timers | grep -i cert)"; }
  ls /etc/letsencrypt/live 2>/dev/null | sed 's/^/   cert: /' || echo "   сертификатов нет"
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
# v1.55.1: СТРАХОВКА ДАННЫХ — копия базы и .env до любых операций.
# Копии живут ВНЕ каталога приложения: переустановка каталога больше
# не может потерять чеки и настройки (инцидент v1.55.0).
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_ROOT"
if [[ -f "$DB_FILE" ]]; then
  cp -a "$DB_FILE" "$BACKUP_ROOT/ymaster_check-$TS.db"
  ls -1t "$BACKUP_ROOT"/ymaster_check-*.db 2>/dev/null | tail -n +31 | xargs -r rm -f
  echo "  ✔ страховая копия базы (вне каталога приложения): $BACKUP_ROOT/ymaster_check-$TS.db"
fi
if [[ -f "$APP_DIR/.env" ]]; then
  cp -a "$APP_DIR/.env" "$BACKUP_ROOT/env-$TS.bak"
  ls -1t "$BACKUP_ROOT"/env-*.bak 2>/dev/null | tail -n +31 | xargs -r rm -f
fi

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
# v1.55.1: каталог страховых копий доступен приложению (зеркалирование бэкапов)
chown "$APP_USER:$APP_USER" "$BACKUP_ROOT" 2>/dev/null || true
# ручные git-команды root здесь не должны падать с «dubious ownership»
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

# v1.46.0: откат к рабочей версии — отдельный спасательный сценарий
# (работает, даже если приложение не запускается: git + systemctl)
if [[ -n "${ROLLBACK:-}" ]]; then
  RB_SCRIPT="$APP_DIR/rollback.sh"
  [[ -f "$RB_SCRIPT" ]] || RB_SCRIPT="$(cd "$(dirname "$0")" && pwd)/rollback.sh"
  [[ -f "$RB_SCRIPT" ]] || { echo "rollback.sh не найден — сначала выполните deploy.sh --update"; exit 1; }
  exec bash "$RB_SCRIPT" $ROLLBACK
fi

# ------------------------------------------------------------------
bold "3/9 Код с GitHub ($REPO_URL, ветка $BRANCH)"
# v1.55.1: ЛЮБАЯ существующая установка (есть .git) обновляется НА МЕСТЕ —
# повторный `sudo bash deploy.sh` без --update больше не удаляет код и
# не вынуждает к «чистой» переустановке с потерей данных.
if [[ -d "$APP_DIR/.git" ]]; then
  OLD_SCRIPT_MD5=$(md5sum "$APP_DIR/deploy.sh" 2>/dev/null | cut -d" " -f1 || true)
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
  ok "обновлено из GitHub (данные не тронуты)"
  # v1.6.2: deploy.sh обновил сам себя — bash уже загрузил СТАРЫЙ текст в
  # память, поэтому перезапускаемся новой версией с теми же аргументами
  NEW_SCRIPT_MD5=$(md5sum "$APP_DIR/deploy.sh" 2>/dev/null | cut -d" " -f1 || true)
  if [[ -n "$OLD_SCRIPT_MD5" && "$OLD_SCRIPT_MD5" != "$NEW_SCRIPT_MD5" ]]; then
    bold "deploy.sh обновился — перезапускаюсь новой версией…"
    exec bash "$APP_DIR/deploy.sh" "$@"
  fi
elif sudo -u "$APP_USER" git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR" 2>/dev/null; then
  ok "репозиторий склонирован"
elif [[ -d "$APP_DIR" && -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
  # Каталог занят без git (после сбоя старой установки): инициализируем
  # репозиторий НА МЕСТЕ — data/, .env, venv и база остаются на месте.
  warn "каталог занят без .git — инициализирую репозиторий на месте (данные сохраняются)"
  sudo -u "$APP_USER" git -C "$APP_DIR" init -q
  sudo -u "$APP_USER" git -C "$APP_DIR" remote remove origin 2>/dev/null || true
  sudo -u "$APP_USER" git -C "$APP_DIR" remote add origin "$REPO_URL"
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch -q --depth 1 origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard FETCH_HEAD
  sudo -u "$APP_USER" git -C "$APP_DIR" clean -fd \
    -e data -e .env -e venv -e '*.db' -e '*.db-*' -e backups >/dev/null || true
  ok "код получен; data/, .env, venv и база не тронуты"
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
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ------------------------------------------------------------------
bold "4/9 Python-окружение"
if [[ ! -d "$APP_DIR/venv" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
# v1.10.0: самопроверка целостности зависимостей (конфликты версий = откат по git)
if sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" check >/dev/null 2>&1; then
  ok "зависимости установлены и согласованы (pip check ok)"
else
  warn "pip check: конфликты зависимостей! Откат: sudo git -C $APP_DIR revert <коммит> && sudo bash $APP_DIR/deploy.sh --update"
  sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" check | sed 's/^/     /' | head -5
fi

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
# v1.55.1: файла базы нет, но страховые копии есть — восстанавливаем
# последнюю целую (защита от «слетевшей базы» при переустановке каталога)
if [[ ! -s "$DB_FILE" ]] && ls "$BACKUP_ROOT"/ymaster_check-*.db >/dev/null 2>&1; then
  NEWEST_DB="$(ls -1t "$BACKUP_ROOT"/ymaster_check-*.db | head -1)"
  mkdir -p "$APP_DIR/data"
  cp -a "$NEWEST_DB" "$DB_FILE"
  chown "$APP_USER:$APP_USER" "$DB_FILE"
  warn "файл базы отсутствовал — восстановлен из копии: $NEWEST_DB"
fi
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

# v1.8.3: проверяем ЖИВОСТЬ приложения по HTTP — «systemctl is-active» не
# видит падение импорта (например, забыли зависимость), а nginx тогда отдаёт 502
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8; do
  if curl -fs --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    HEALTH_OK=1; break
  fi
  sleep 1
done
if [[ $HEALTH_OK -eq 1 ]]; then
  ok "приложение отвечает (/health ok)"
else
  echo "  ✘ Приложение НЕ отвечает после рестарта (nginx покажет 502). Последние строки журнала:"
  journalctl -u "$SERVICE" -n 20 --no-pager | tail -20
  echo "     Частая причина: не хватает зависимости. Лечение:"
  echo "       sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt"
  echo "       sudo systemctl restart $SERVICE"
  exit 1
fi

# v1.8.1: разрешаем приложению (пользователь ymaster) перезапускать свой сервис
# — без этого «Обновить из приложения» обновляло файлы, но не могло применить их
# v1.8.3: блок защищён — любая ошибка здесь НЕ должна ронять весь деплой
SUDOERS_FILE=/etc/sudoers.d/ymaster-check
if (
  mkdir -p /etc/sudoers.d
  echo "ymaster ALL=(root) NOPASSWD: /usr/bin/systemctl restart ymaster-check, /usr/bin/systemctl status ymaster-check" > "$SUDOERS_FILE"
  chmod 440 "$SUDOERS_FILE"
  visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1
) >/dev/null 2>&1; then
  ok "sudoers: приложение может перезапускать себя (обновление из приложения)"
else
  rm -f "$SUDOERS_FILE" 2>/dev/null || true
  warn "sudoers-правило не установлено — обновление из приложения потребует ручного рестарта"
fi

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

# HTTP -> HTTPS — восстанавливается deploy.sh при каждом обновлении.
# Перенаправляем ВСЕ http-запросы (включая заход по IP) на https-домен:
# сертификат выпущен на домен, редирект на https://IP дал бы ошибку браузера.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $D _;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$D\$request_uri; }
}
NGXEOF
    ok "HTTPS восстановлен из существующего сертификата ($D)"
  fi
  rm -f /etc/nginx/sites-enabled/default
  ln -sf /etc/nginx/sites-available/ymaster-check /etc/nginx/sites-enabled/
  # v1.8.3: ошибка конфига не роняет деплой — показываем её и продолжаем
  if nginx -t 2>/tmp/ymaster-nginx-test.log; then
    systemctl reload nginx || systemctl restart nginx || true
    ok "Nginx настроен (rate-limit, headers, служебные пути закрыты)"
  else
    warn "Nginx: конфиг не прошёл проверку (см. ниже) — предыдущий конфиг мог остаться активным"
    sed 's/^/     /' /tmp/ymaster-nginx-test.log | tail -5
  fi
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
  # v1.8.3: следим за сроком сертификата и продлеваем автоматически.
  # Таймер бывает systemd (apt) или snap — пробуем оба, ошибка не фатальна.
  if systemctl enable --now certbot.timer >/dev/null 2>&1 \
     || systemctl enable --now snap.certbot.renew.timer >/dev/null 2>&1; then
    ok "таймер автопродления certbot активен"
  else
    warn "автотаймер certbot не найден — проверьте: systemctl list-timers | grep -i cert"
  fi
  END_STR=$(openssl x509 -in "/etc/letsencrypt/live/$SSL_DOMAIN/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)
  CERT_END_EPOCH=$(date -d "$END_STR" +%s 2>/dev/null || echo 0)
  if [[ "${CERT_END_EPOCH:-0}" -eq 0 ]]; then
    warn "не удалось прочитать срок сертификата — пропускаю автопродление (сертификат на месте)"
  else
    DAYS_LEFT=$(( (CERT_END_EPOCH - $(date +%s)) / 86400 ))
    if [[ $DAYS_LEFT -lt 25 ]]; then
      bold "Сертификат истекает через ${DAYS_LEFT} дн. — продлеваю…"
      if "$CERTBOT_BIN" renew --cert-name "$SSL_DOMAIN" 2>&1 | tail -3 \
         && systemctl reload nginx 2>/dev/null; then
        ok "сертификат продлён"
      else
        warn "автопродление не прошло (вывод выше) — вручную: sudo certbot renew && sudo systemctl reload nginx"
      fi
    else
      ok "SSL-сертификат ($SSL_DOMAIN) действителен ещё ${DAYS_LEFT} дн. — продлится автоматически"
    fi
  fi
elif [[ -n "$SSL_DOMAIN" ]]; then
  # Preflight: домен должен указывать на ЭТОТ сервер, иначе Let's Encrypt не пройдёт
  SERVER_IP=$(curl -4 -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
  DOMAIN_IP=$(getent ahostsv4 "$SSL_DOMAIN" | awk '{print $1; exit}')
  echo "  Домен $SSL_DOMAIN -> ${DOMAIN_IP:-не найден}; этот сервер: $SERVER_IP"
  if [[ -n "$DOMAIN_IP" && "$DOMAIN_IP" == "$SERVER_IP" ]]; then
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1 || true
    echo "  Выпускаю сертификат (вывод certbot ниже)…"
    if "$CERTBOT_BIN" --nginx -d "$SSL_DOMAIN" --non-interactive --agree-tos --redirect \
        -m info@ymaster.ru 2>&1 | tail -6; then
      ok "сертификат выпущен, HTTPS включён, http -> https автоматически"
    else
      warn "certbot не смог выпустить сертификат (вывод выше — там точная причина)."
      echo "     Частые причины: лимит Let's Encrypt (5 одинаковых за неделю — повторите завтра),"
      echo "     80-й порт закрыт, домен не на этот сервер."
      echo "     Повтор: sudo bash deploy.sh --update --with-ssl=$SSL_DOMAIN"
    fi
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
  URL="https://$SSL_DOMAIN"
  CERT_END=$(openssl x509 -in "/etc/letsencrypt/live/$SSL_DOMAIN/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)
  ALT="  ← открывайте этот адрес (заход по IP тоже приведёт сюда)"
elif [[ -n "$SSL_DOMAIN" ]]; then
  URL="http://$SSL_DOMAIN"; ALT=""
else
  URL="http://$IP"; ALT=""
fi
echo ""
echo "=============================================================="
echo "  ✅ Ямастер Чек v$APP_VERSION развёрнут!"
echo "  Веб-клиент:       $URL"
[[ -n "$ALT" ]] && echo "  $ALT"
[[ -n "${CERT_END:-}" ]] && echo "  Сертификат SSL:   действует до $CERT_END (продлевается автоматически)"
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
