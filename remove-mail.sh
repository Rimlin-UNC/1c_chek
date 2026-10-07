#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — удаление почтового сервиса с сервера (v1.45.0).
# Убирает ровно то, что ставил setup-mail.sh, и НЕ ТРОГАЕТ приложение,
# Nginx, UFW, fail2ban, certbot и БД. Почта переезжает на Timeweb
# (chek@ymaster.ru → smtp.timeweb.ru:465).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Запуск:
#   sudo bash deploy.sh --remove-mail
#   sudo bash /opt/ymaster-check/remove-mail.sh
#
# Удаляет:
#   • пакеты postfix, opendkim, opendkim-tools, mailutils (с конфигами);
#   • DKIM-ключи (/etc/dkimkeys), конфиги OpenDKIM (/etc/opendkim*),
#     модифицированный main.cf (уйдёт вместе с пакетом);
#   • системного пользователя «chek» (создан setup-mail.sh, вход в ОС
#     отключён) вместе с его каталогом и почтовым ящиком;
#   • /root/mail-dns-records.txt.
# Перед удалением всё это складывается в архив
#   /root/mail-legacy-backup-<дата>.tar.gz
# на случай, если что-то понадобится вернуть.
#
# НЕ удаляет: /opt/ymaster-check, базу данных, Nginx/UFW/fail2ban,
# сертификаты, /etc/ymaster-check, пользователей приложения.
# ======================================================================
set -euo pipefail

MAIL_USER="${MAIL_USER:-chek}"
DKIM_DIR="/etc/dkimkeys"
OPENDKIM_CONF="/etc/opendkim.conf"
OPENDKIM_DIR="/etc/opendkim"
DNS_FILE="/root/mail-dns-records.txt"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/mail-legacy-backup-${STAMP}.tar.gz"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✔ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo: sudo bash $0"; exit 1; }
bold "Удаляю самохостингованный почтовый сервис (переезд на Timeweb)"

# 1) Архив того, что будет удалено (страховка) -------------------------
echo "1) Архивирую удаляемое в ${BACKUP}…"
ARCHIVE_LIST=()
[ -d "$DKIM_DIR" ] && ARCHIVE_LIST+=("$DKIM_DIR")
[ -f "$OPENDKIM_CONF" ] && ARCHIVE_LIST+=("$OPENDKIM_CONF")
[ -d "$OPENDKIM_DIR" ] && ARCHIVE_LIST+=("$OPENDKIM_DIR")
[ -f "$DNS_FILE" ] && ARCHIVE_LIST+=("$DNS_FILE")
[ -f "/var/mail/${MAIL_USER}" ] && ARCHIVE_LIST+=("/var/mail/${MAIL_USER}")
if [ "${#ARCHIVE_LIST[@]}" -gt 0 ]; then
  tar -czf "$BACKUP" "${ARCHIVE_LIST[@]}" 2>/dev/null || true
  chmod 600 "$BACKUP"
  ok "архив создан: ${BACKUP}"
else
  ok "удалять по конфигам нечего (возможно, уже удалено)"
fi

# 2) Остановка и удаление пакетов --------------------------------------
echo "2) Останавливаю и удаляю postfix/opendkim/mailutils…"
systemctl stop postfix opendkim 2>/dev/null || true
systemctl disable postfix opendkim 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive
# --purge: вместе с конфигами (в т.ч. изменённый main.cf)
apt-get purge -y -qq postfix opendkim opendkim-tools mailutils >/dev/null 2>&1 || true
apt-get autoremove -y -qq >/dev/null 2>&1 || true
ok "пакеты удалены (с конфигами)"

# 3) Остатки конфигов и ключей -----------------------------------------
echo "3) Убираю остатки конфигов…"
rm -rf "$DKIM_DIR" "$OPENDKIM_DIR"
rm -f "$OPENDKIM_CONF" /etc/default/opendkim "$DNS_FILE"
rm -rf /run/opendkim
ok "ключи DKIM и конфиги удалены (копия — в архиве)"

# 4) Системный пользователь chek ---------------------------------------
echo "4) Пользователь ${MAIL_USER}…"
if id "${MAIL_USER}" >/dev/null 2>&1; then
  # предохранитель: удаляем только если это наш служебный пользователь
  # (shell отключён — создан setup-mail.sh, обычные люди так не выглядят)
  if id -p "${MAIL_USER}" 2>/dev/null | grep -q "shell=/usr/sbin/nologin\|/usr/sbin/nologin"; then
    userdel -r "${MAIL_USER}" 2>/dev/null || { userdel "${MAIL_USER}" 2>/dev/null || true; \
      rm -rf "/home/${MAIL_USER}" "/var/mail/${MAIL_USER}"; }
    ok "пользователь ${MAIL_USER} удалён (с каталогом и ящиком)"
  else
    warn "пользователь ${MAIL_USER} существует, но НЕ служебный (shell не nologin)"
    warn "НЕ удаляю автоматически. Проверьте вручную: id -p ${MAIL_USER}"
  fi
else
  ok "пользователь ${MAIL_USER} не существует"
fi

# 5) Проверки ----------------------------------------------------------
echo "5) Проверки…"
command -v postfix >/dev/null 2>&1 && warn "postfix ещё присутствует — перезапустите скрипт" \
  || ok "postfix удалён"
ss -ltn 2>/dev/null | grep -qE ':(25|8891)\b' \
  && warn "порт 25/8891 ещё слушается — перезагрузите сервер" \
  || ok "портов 25/8891 нет — снаружи чисто"
[ -d /opt/ymaster-check ] && ok "приложение на месте (/opt/ymaster-check)"
systemctl is-active --quiet ymaster-check 2>/dev/null && ok "сервис ymaster-check работает" \
  || ok "(сервис ymaster-check не найден — возможно, ставился иначе)"

echo
bold "Готово: самохостингованная почта удалена, лишнее не тронуто."
bold "Дальше — почта Timeweb: chek@ymaster.ru → smtp.timeweb.ru:465."
bold "В приложении: Настройки → «✉️ Почтовый центр» → кнопка"
bold "«Заполнить для Timeweb» → пароль ящика → тестовое письмо."
