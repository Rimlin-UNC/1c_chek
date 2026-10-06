#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — настройка почтового сервиса на своём сервере (v1.44.1).
# Домен почты: chek.ymaster.ru, ящик отправителя: chek@chek.ymaster.ru.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Запуск (на сервере, одним из способов):
#   sudo bash deploy.sh --setup-mail            # скачает и запустит этот скрипт
#   sudo bash /opt/ymaster-check/setup-mail.sh  # если репозиторий уже клонирован
#
# Что делает:
#   1) ставит Postfix (только отправка/локальная доставка), OpenDKIM, mailutils;
#   2) создаёт системного получателя «chek» (ящик chek@chek.ymaster.ru);
#   3) настраивает Postfix БЕЗ открытых портов снаружи (только 127.0.0.1 —
#      приложение «Ямастер Чек» отправляет письма через локальный сервер,
#      снаружи нечего атаковать: ни паролей, ни релея);
#   4) включает DKIM-подпись писем (RSA 2048, selector «mail»);
#   5) генерирует файл со всеми DNS-записями: /root/mail-dns-records.txt
#      (SPF, DKIM, DMARC; MX — опционально) и печатает их в консоль;
#   6) проверяет доставку локального письма и доступность исходящего
#      порта 25 (некоторые провайдеры его закрывают — подскажет обход).
#
# Скрипт можно запускать повторно — он идемпотентен.
# ======================================================================
set -euo pipefail

MAIL_DOMAIN="${MAIL_DOMAIN:-chek.ymaster.ru}"
MAIL_USER="${MAIL_USER:-chek}"
FROM_ADDR="${MAIL_USER}@${MAIL_DOMAIN}"
# IP сервера: автоопределение, иначе боевой 94.183.236.179
SERVER_IP="${SERVER_IP:-$(ip -4 -o addr show scope global 2>/dev/null \
  | awk '{print $4}' | cut -d/ -f1 | head -1 || true)}"
SERVER_IP="${SERVER_IP:-94.183.236.179}"
DKIM_DIR="/etc/dkimkeys/keys/${MAIL_DOMAIN}"
DNS_FILE="/root/mail-dns-records.txt"
KEYDIR="/etc/opendkim/keys"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✔ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }

# ----------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo: sudo bash $0"; exit 1; }
bold "Почтовый сервис «Ямастер Чек»: домен ${MAIL_DOMAIN}, ящик ${FROM_ADDR}"

# 1) Пакеты ------------------------------------------------------------
if ! dpkg -s postfix >/dev/null 2>&1 || ! dpkg -s opendkim >/dev/null 2>&1; then
  echo "1) Устанавливаю пакеты (postfix, opendkim, mailutils)…"
  export DEBIAN_FRONTEND=noninteractive
  echo "postfix postfix/main_mailer_type select Local only" | debconf-set-selections
  echo "postfix postfix/mailname string ${MAIL_DOMAIN}" | debconf-set-selections
  apt-get update -qq
  apt-get install -y -qq postfix opendkim opendkim-tools mailutils >/dev/null
else
  echo "1) Пакеты postfix/opendkim уже установлены"
fi
ok "пакеты на месте"

# 2) Системный получатель chek -----------------------------------------
if ! id "${MAIL_USER}" >/dev/null 2>&1; then
  useradd -r -m -d "/home/${MAIL_USER}" -s /usr/sbin/nologin "${MAIL_USER}"
  ok "создан системный получатель ${MAIL_USER} (вход отключён)"
else
  ok "получатель ${MAIL_USER} уже существует"
fi

# 3) Postfix: только отправка, только с локальной машины ---------------
echo "3) Настраиваю Postfix (только 127.0.0.1, без открытых портов)…"
postconf -e "myhostname = ${MAIL_DOMAIN}"
postconf -e "myorigin = ${MAIL_DOMAIN}"
postconf -e "mydestination = localhost.\$mydomain, localhost, \$myhostname, ${MAIL_DOMAIN}"
postconf -e "inet_interfaces = loopback-only"
postconf -e "mynetworks = 127.0.0.0/8, [::1]/128"
postconf -e "smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, defer_unauth_destination"
postconf -e "smtp_tls_security_level = may"
postconf -e "smtp_tls_CApath = /etc/ssl/certs"
postconf -e "smtpd_milters = inet:127.0.0.1:8891"
postconf -e "non_smtpd_milters = inet:127.0.0.1:8891"
postconf -e "milter_default_action = accept"
postconf -e "message_size_limit = 20971520"
postconf -e "compatibility_level = 3.6"
ok "Postfix настроен (main.cf)"

# 4) OpenDKIM: подпись писем -------------------------------------------
echo "4) Настраиваю DKIM-подпись (RSA 2048, selector «mail»)…"
if [ ! -s "${DKIM_DIR}/mail.private" ]; then
  mkdir -p "${DKIM_DIR}"
  opendkim-genkey -b 2048 -d "${MAIL_DOMAIN}" -s mail -D "${DKIM_DIR}" -v >/dev/null
  chown -R opendkim:opendkim /etc/dkimkeys
  chmod 600 "${DKIM_DIR}/mail.private"
  ok "ключ DKIM создан: ${DKIM_DIR}/mail.private"
else
  ok "ключ DKIM уже есть: ${DKIM_DIR}/mail.private"
fi

mkdir -p "${KEYDIR}"
cat > "${KEYDIR}/KeyTable" <<EOF
mail._domainkey.${MAIL_DOMAIN} ${MAIL_DOMAIN}:mail:${DKIM_DIR}/mail.private
EOF
cat > "${KEYDIR}/SigningTable" <<EOF
*@${MAIL_DOMAIN} mail._domainkey.${MAIL_DOMAIN}
EOF
cat > "${KEYDIR}/TrustedHosts" <<EOF
127.0.0.1
localhost
${SERVER_IP}
*.${MAIL_DOMAIN}
EOF

cat > /etc/opendkim.conf <<EOF
Syslog          yes
UMask           002
Canonicalization relaxed/simple
Mode            sv
SubDomains      no
AutoRestart     yes
AutoRestartRate 10/1M
Background      yes
PIDFile         /run/opendkim/opendkim.pid
SignatureAlgorithm rsa-sha256
ExternalIgnoreList refile:${KEYDIR}/TrustedHosts
InternalHosts      refile:${KEYDIR}/TrustedHosts
KeyTable        refile:${KEYDIR}/KeyTable
SigningTable    refile:${KEYDIR}/SigningTable
Socket          inet:8891@127.0.0.1
UserID          opendkim:opendkim
EOF

mkdir -p /run/opendkim
chown -R opendkim:opendkim /run/opendkim "${KEYDIR}"
systemctl enable --quiet opendkim 2>/dev/null || true
systemctl restart opendkim
systemctl enable --quiet postfix 2>/dev/null || true
systemctl restart postfix
ok "Postfix и OpenDKIM перезапущены"

# 5) DNS-записи --------------------------------------------------------
echo "5) Готовлю DNS-записи…"
apt-get install -y -qq dnsutils >/dev/null 2>&1 || true   # dig для проверок
# >>DKIM_EXTRACT (конвейер проверяется тестом tests/test_v1441.py)
# opendkim-genkey пишет: mail._domainkey IN TXT ( "v=…; " "p=…" ) ; ----- DKIM key …
# Извлекаем ЧИСТОЕ значение: без имён, кавычек, скобок и хвостового комментария.
DKIM_VALUE="$(tr '\n' ' ' < "${DKIM_DIR}/mail.txt" \
  | grep -o 'v=DKIM1;[^)]*' \
  | tr -d '"' \
  | sed 's/  */ /g; s/ $//')"
# <<DKIM_EXTRACT
if [ -z "${DKIM_VALUE}" ] || [[ "${DKIM_VALUE}" != v=DKIM1* ]]; then
  warn "не удалось извлечь DKIM из ${DKIM_DIR}/mail.txt — перенесите значение"
  warn "руками из этого файла (только v=DKIM1; … p=…, без кавычек и скобок)"
fi
# разбивка по 240 символов для старых BIND-панелей (обычно не требуется)
DKIM_SPLIT="$(printf '%s' "${DKIM_VALUE}" | fold -w 240 \
  | sed 's/^/"&/; s/$/&"/' | tr '\n' ' ' | sed 's/ $//')"

DNS_NOTE="Важно: значения ниже вставляйте БЕЗ кавычек — панель DNS добавляет их сама. Если в значение попадёт кавычка или скобка, opendkim-testkey верёт ошибку «syntax error in key data (ASCII 0x22…)»."
cat > "${DNS_FILE}" <<EOF
=== DNS-записи для почты chek@${MAIL_DOMAIN} (Ямастер Чек) ===
Добавьте у держателя DNS зоны ymaster.ru (записи ДЛЯ ПОДДОМЕНА ${MAIL_DOMAIN}).
${DNS_NOTE}

1) SPF (TXT, имя: ${MAIL_DOMAIN}, значение):
   v=spf1 a ip4:${SERVER_IP} ~all

2) DKIM (TXT, имя: mail._domainkey.${MAIL_DOMAIN}, значение ЦЕЛИКОМ):
   ${DKIM_VALUE}
   (Если панель требует разбивку на части по 255 символов — ставьте
    части в кавычках друг за другом: ${DKIM_SPLIT})

3) DMARC (TXT, имя: _dmarc.${MAIL_DOMAIN}, значение):
   v=DMARC1; p=quarantine; rua=mailto:info@ymaster.ru; adkim=s; aspf=s

4) MX (необязательно — только если хотите ПРИНИМАТЬ письма на
   ${FROM_ADDR}; для отправки MX не нужен):
   ${MAIL_DOMAIN}.  IN  MX  10  ${MAIL_DOMAIN}.

После добавления записей проверка (на сервере, 5–30 минут на DNS):
   dig +short TXT ${MAIL_DOMAIN}
   dig +short TXT mail._domainkey.${MAIL_DOMAIN}
   opendkim-testkey -d ${MAIL_DOMAIN} -s mail -vvv

   opendkim-testkey говорит:
   «key OK» — всё готово;
   «syntax error in key data (ASCII 0x22 …)» — в значение записи
     попала кавычка (0x22 = ") или скобка/комментарий:
     исправьте TXT-запись — значение из п.2 выше, без кавычек;
   «no key for signature» — запись ещё не видна: подождите DNS
     или проверьте имя (mail._domainkey.${MAIL_DOMAIN}).

Значения в «Почтовый центр» приложения (Настройки):
   SMTP-сервер: 127.0.0.1, порт: 25, логин и пароль — ПУСТО,
   STARTTLS — выключен, ящик From: ${FROM_ADDR},
   адрес сайта для ссылок: https://chek.ymaster.ru
EOF
ok "записи сохранены: ${DNS_FILE}"

# 6) Самопроверки ------------------------------------------------------
echo "6) Проверки…"
postfix check >/dev/null 2>&1 && ok "конфигурация Postfix корректна" \
  || warn "postfix check вернул замечания — см. journalctl -u postfix"
systemctl is-active --quiet postfix && ok "postfix запущен" || warn "postfix не запущен"
systemctl is-active --quiet opendkim && ok "opendkim запущен (порт 8891)" || warn "opendkim не запущен"

printf 'To: %s\nFrom: %s\nSubject: Проверка доставки (setup-mail)\n\nПисьмо доставлено локально — сервис работает.\n' \
  "${FROM_ADDR}" "${FROM_ADDR}" \
  | /usr/sbin/sendmail -t && sleep 1
if [ -s "/var/mail/${MAIL_USER}" ]; then
  ok "локальное письмо доставлено в /var/mail/${MAIL_USER}"
else
  warn "письмо не найдено в /var/mail/${MAIL_USER} — см. /var/log/mail.log"
fi

# Исходящий порт 25 у провайдера
if timeout 6 bash -c 'cat < /dev/null > /dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
  ok "исходящий порт 25 открыт — письма уйдут напрямую"
else
  warn "исходящий порт 25 ЗАКРЫТ провайдером (частый случай для домашних IP)."
  warn "Обход: релей через SMTP провайдера или Яндекса — пошагово в"
  warn "docs/knowledge/mail_setup.md, раздел «Если порт 25 закрыт»."
fi

echo
bold "Готово. Добавьте DNS-записи из ${DNS_FILE} — и подпишите письма начнёт"
bold "сам домен. Затем в приложении: Настройки → «✉️ Почтовый центр» →"
bold "сохраните значения из файла и отправьте тестовое письмо."
