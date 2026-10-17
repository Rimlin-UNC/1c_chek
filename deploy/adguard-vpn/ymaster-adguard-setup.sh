#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — привилегированный помощник мастера AdGuard VPN (v1.70.16)
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
# ----------------------------------------------------------------------
# Зачем: мастер «🛡 AdGuard VPN» в программе делает всё сам и БЕЗ пароля
# сервера в браузере. Для этого ему нужен ровно ОДИН разрешённый sudoers-
# командой сценарий с фиксированными фазами (никаких произвольных команд,
# никакого пользовательского ввода в шелле):
#
#   sudo -n /usr/local/sbin/ymaster-adguard-setup <фаза>
#
# Фазы: preflight | install | login | socks | tun | units | autopilot |
#       version | status | rights | install-rule | remove-rule
#
# v1.70.24: фаза «socks» теперь включает режим TUN + selective-исключения
# (через VPN идёт только Telegram). Имя сохранено ради уже установленных
# sudoers-правил; «tun» принято как синоним.
#
# Одноразовая установка правила (владелец в терминале, один раз):
#   sudo bash /opt/ymaster-check/deploy/adguard-vpn/ymaster-adguard-setup.sh install-rule
# deploy.sh выполняет этот же шаг сам при установке/обновлении.
#
# Безопасность:
#   • помощник живёт в /usr/local/sbin (root:root, 0755) — служебный
#     пользователь программы может его ЗАПУСТИТЬ, но не может изменить;
#   • комплект юнитов копируется в /usr/local/share/ymaster-adguard
#     (root:root) — оттуда и раскладывается по системе, путь из-под
#     приложения не передаётся;
#   • sudoers-правило перечисляет фазы явно (install-rule/remove-rule
#     из-под приложения недоступны — только вручную из терминала);
#   • `set -u`, все переменные в кавычках, аргументы никогда не попадают
#     в интерпретатор команд; неизвестная фаза — отказ.
# ======================================================================
set -u

HELPER="/usr/local/sbin/ymaster-adguard-setup"
KIT_DIR="/usr/local/share/ymaster-adguard"
SUDOERS_FILE="/etc/sudoers.d/ymaster-adguard"
APP_UNIT="ymaster-check"
DEFAULT_APP_DIR="/opt/ymaster-check"
CLI_LINK="/usr/local/bin/adguardvpn-cli"
CLI_REAL="/opt/adguardvpn_cli/adguardvpn-cli"
TUNNEL_UNIT="adguard-vpn-telegram"
TIMER_UNIT="tg-health.timer"
# v1.70.22: двумя шагами, а не `curl … | sh`. В пайплайне код возврата — у
# последней команды (sh), поэтому отказ curl (нет сети/DNS) выглядел как успех;
# `set -o pipefail` в dash (/bin/sh в Ubuntu/Debian) недоступен.
INSTALL_URL="https://raw.githubusercontent.com/AdguardTeam/AdGuardVPNCLI/HEAD/scripts/release/install.sh"
INSTALL_TMP="/tmp/ymaster-adguard-install.sh"
INSTALL_CMD="curl -fsSL $INSTALL_URL -o $INSTALL_TMP && sh $INSTALL_TMP -a y"
# Фазы, доступные из-под приложения (в них и состоит sudoers-правило).
# install-rule/remove-rule здесь НЕТ: их владелец выполняет вручную.
# v1.70.24: «tun» добавлено как синоним «socks» (см. phase_tun). «socks»
# остаётся в списке ОБЯЗАТЕЛЬНО: уже установленные на серверах правила его
# перечисляют, а обновление из приложения install-rule не перезапускает.
ALLOWED_PHASES="preflight rights install login socks tun units autopilot version status"

# v1.70.24: домены Telegram для selective-исключений — через VPN идёт только
# они, весь прочий трафик сервера остаётся прямым. Поддомены AdGuard сам не
# наследует (AdguardTeam/AdGuardVPNCLI#148), поэтому wildcards перечислены
# явно и в кавычках (иначе «*.t.me» раскроет шелл).
TG_DOMAINS=(
  "api.telegram.org"      # Bot API — обязательно, на нём держится бот
  "core.telegram.org"
  "telegram.org"
  "*.telegram.org"
  "t.me"
  "*.t.me"
  "telegram.me"
  "telegram.dog"
)

# Комплект: имя файла -> куда класть и с какими правами
UNITS_ETC=("adguard-vpn-telegram.service" "tg-health.service" "tg-health.timer")
UNITS_SBIN=("tg-health.sh")

say() { printf '%s\n' "$*"; }
die() { say "ymaster-adguard-setup: $*"; exit 1; }

cli_path() {
  if [ -x "$CLI_LINK" ]; then printf '%s' "$CLI_LINK"; return 0; fi
  if [ -x "$CLI_REAL" ]; then printf '%s' "$CLI_REAL"; return 0; fi
  command -v adguardvpn-cli 2>/dev/null || return 1
}

# v1.70.23: ссылка $CLI_LINK обязательна — на неё ЖЁСТКО прописаны ExecStart=
# юнита adguard-vpn-telegram.service и CLI= в tg-health.sh. Официальный
# установщик её не создаёт в двух случаях: (1) нет терминала для вопроса
# «create a link in /usr/local/bin? [y/N]» — пустой ответ понимается как «нет»
# и создаётся файл .nosymlink, отключающий вопрос навсегда; (2) /usr/local/bin
# не входит в PATH — тогда create_symlink() молча делает return 0 (у юнита
# программы PATH=/opt/ymaster-check/venv/bin:/usr/bin:/bin — его там нет).
# Поэтому обеспечиваем ссылку сами и на пути «CLI уже установлен» тоже:
# cli_path() находит и $CLI_REAL, и без этой страховки фаза units падала.
ensure_link() {
  if [ -x "$CLI_REAL" ] && [ ! -e "$CLI_LINK" ]; then
    ln -sf "$CLI_REAL" "$CLI_LINK" \
      && say "создана ссылка $CLI_LINK" \
      || die "не создать ссылку $CLI_LINK (нужна службе туннеля и автопилоту)"
  fi
}

need_root() {
  [ "$(id -u)" -eq 0 ] || die "фаза «$1» выполняется только от root (sudo)"
}

# Служебный пользователь программы: User= из юнита, иначе владелец каталога
svc_user() {
  local app_dir="${1:-$DEFAULT_APP_DIR}" u=""
  if [ -f "/etc/systemd/system/${APP_UNIT}.service" ]; then
    u=$(sed -n 's/^[[:space:]]*User=[[:space:]]*//p' \
        "/etc/systemd/system/${APP_UNIT}.service" | head -n1)
  fi
  if [ -z "$u" ] && [ -d "$app_dir" ]; then
    u=$(stat -c '%U' "$app_dir" 2>/dev/null || true)
  fi
  [ -n "$u" ] || u="ymaster"
  printf '%s' "$u"
}

# ----------------------------------------------------------------------
#  install-rule — одноразовая установка помощника и sudoers-правила
#  (единственная фаза, которой нужен каталог программы: берёт комплект)
# ----------------------------------------------------------------------
phase_install_rule() {
  need_root "install-rule"
  # v1.70.20: ОБЯЗАТЕЛЬНО двумя строками. В одном `local a=… b="$a/…"` bash
  # раскрывает слова до присваиваний, поэтому под `set -u` вторая переменная
  # падала с «app_dir: unbound variable» — фаза install-rule не работала вовсе.
  local app_dir="${1:-$DEFAULT_APP_DIR}"
  local src="$app_dir/deploy/adguard-vpn"
  [ -d "$src" ] || die "каталог комплекта не найден: $src (укажите путь к программе: install-rule /путь/к/ymaster-check)"
  [ -f "$src/$(basename "$0")" ] || [ -f "$src/ymaster-adguard-setup.sh" ] \
    || die "в $src нет ymaster-adguard-setup.sh — обновите программу"

  # 1) комплект юнитов — в root-каталог (приложение его изменить не может)
  mkdir -p "$KIT_DIR" || die "не создать $KIT_DIR"
  local f
  for f in "${UNITS_ETC[@]}" "${UNITS_SBIN[@]}" "ymaster-adguard-setup.sh"; do
    [ -f "$src/$f" ] || die "в комплекте нет файла: $f"
    install -m 0644 -o root -g root "$src/$f" "$KIT_DIR/$f" \
      || die "не скопировать $f в $KIT_DIR"
  done

  # 2) сам помощник — в /usr/local/sbin (root:root, 0755)
  install -m 0755 -o root -g root "$src/ymaster-adguard-setup.sh" "$HELPER" \
    || die "не установить $HELPER"

  # 3) sudoers-правило: только фазы мастера, без install-rule/remove-rule
  local user rules ph
  user=$(svc_user "$app_dir")
  rules="$user ALL=(root) NOPASSWD:"
  for ph in $ALLOWED_PHASES; do
    rules="$rules $HELPER $ph,"
  done
  rules="${rules%,}"
  mkdir -p /etc/sudoers.d
  printf '%s\n' "$rules" > "$SUDOERS_FILE" || die "не записать $SUDOERS_FILE"
  chmod 0440 "$SUDOERS_FILE"
  chown root:root "$SUDOERS_FILE" 2>/dev/null || true
  if ! visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    rm -f "$SUDOERS_FILE"
    die "sudoers-правило не прошло visudo -c — удалено (мастер спросит пароль как раньше)"
  fi

  say "✅ Мастер AdGuard работает без пароля."
  say "   помощник : $HELPER (root:root 0755)"
  say "   комплект : $KIT_DIR"
  say "   правило  : $SUDOERS_FILE (пользователь: $user)"
  say "   проверка : sudo -u \"$user\" sudo -n $HELPER rights"
}

phase_remove_rule() {
  need_root "remove-rule"
  rm -f "$SUDOERS_FILE" && say "правило $SUDOERS_FILE удалено"
  rm -f "$HELPER" && say "помощник $HELPER удалён"
  rm -rf "$KIT_DIR" && say "комплект $KIT_DIR удалён"
}

# ----------------------------------------------------------------------
#  Фазы мастера (доступны из-под приложения через sudoers-правило)
# ----------------------------------------------------------------------
phase_rights() { say "ok"; }

phase_preflight() {
  [ "$(uname -s)" = "Linux" ] || die "автоустановка работает на Linux"
  systemctl --version >/dev/null 2>&1 || die "systemd не найден (нужен Ubuntu/Debian)"
  command -v curl >/dev/null 2>&1 || die "curl не найден: apt install -y curl"
  [ -d "$KIT_DIR" ] || die "комплект не разложен ($KIT_DIR) — выполните install-rule ещё раз"
  if cli_path >/dev/null 2>&1; then
    say "cli: $(cli_path)"
  else
    say "cli: не установлен — поставим официальным установщиком"
  fi
  say "ready"
}

phase_version() {
  local cli; cli=$(cli_path) || die "AdGuard VPN CLI не установлен"
  "$cli" --version 2>&1 | head -n1
}

phase_status() {
  systemctl is-active "$TUNNEL_UNIT" 2>/dev/null || true
  systemctl is-enabled "$TUNNEL_UNIT" 2>/dev/null || true
  systemctl is-active "$TIMER_UNIT" 2>/dev/null || true
}

phase_install() {
  need_root "install"
  local cli
  if cli=$(cli_path); then
    if "$cli" --version >/dev/null 2>&1; then
      # v1.70.23: ссылку обеспечиваем и на пути «CLI уже установлен».
      # cli_path() находит и $CLI_REAL, поэтому раньше фаза завершалась без
      # ссылки, а phase_units ставил юнит с ExecStart=$CLI_LINK.
      ensure_link
      say "CLI уже установлен: $("$cli" --version 2>&1 | head -n1)"
      return 0
    fi
  fi
  say "Ставим AdGuard VPN CLI официальным установщиком (1–2 минуты)…"
  # v1.70.23: `-a y` — автоответ «да» на вопрос установщика про ссылку
# /usr/local/bin. Стоявший здесь `-v` — это verbose (по умолчанию уже включён),
# на вопросы он не отвечает: без tty установщик получал пустой ответ, создавал
# .nosymlink и ссылку НЕ делал. Кроме того, create_symlink() сразу выходит, если
# /usr/local/bin не в PATH (у юнита программы его там нет). Ссылка обязательна:
# на неё жёстко прописаны ExecStart юнита туннеля и CLI= в tg-health.sh.
# v1.70.21: вывод установщика больше НЕ глушим. `>/dev/null 2>&1` прятал
# причину (нет сети, DNS, GitHub недоступен), и владелец видел лишь «CLI не
# появился после установки» — тот же класс дефекта, что в deploy.sh до v1.70.20.
  local install_out
  local install_rc=0
  install_out=$(sh -c "$INSTALL_CMD" </dev/null 2>&1) || install_rc=$?
  if [ "$install_rc" -ne 0 ]; then
    say "установщик завершился с ошибкой (код $install_rc) — пробую продолжить (мог остаться вопрос про ссылку)"
    if [ -n "$install_out" ]; then
      printf '%s\n' "$install_out" | tail -n 5 | sed 's/^/       причина: /'
    fi
  fi
  ensure_link
  cli=$(cli_path) || die "CLI не появился после установки"
  "$cli" --version >/dev/null 2>&1 || die "CLI установлен, но не запускается: $cli"
  say "CLI готов: $("$cli" --version 2>&1 | head -n1)"
}

phase_login() {
  need_root "login"
  local cli; cli=$(cli_path) || die "CLI не найден — сначала фаза install"
  # stdout не буферизуем: программа показывает ссылку входа сразу
  exec "$cli" login
}

# v1.70.24: режим TUN + selective-исключения — через VPN идёт ТОЛЬКО Telegram.
#
# Почему не SOCKS5 (как было до v1.70.24):
#   • httpx/socksio не поддерживают socks5h — имя api.telegram.org
#     резолвилось ЛОКАЛЬНЫМ DNS сервера. При отравлении DNS прокси не спасал:
#     бот получал подменённый адрес и не подключался вовсе.
#   • в TUN-режиме DNS уходит в туннель, поэтому эта проблема исчезает.
# Почему не SCRIPT-маршрутизация (самодельный скрипт по CIDR Telegram):
#   • AdguardTeam/AdGuardVPNCLI#69 и #190: «Routing mode SCRIPT cause
#     "Unknown error" and won't connect» — режим не работает.
#   • список подсетей AS62041 приходится поддерживать вручную, а доменные
#     исключения AdGuard обновляет сам.
# Почему AUTO, а не NONE: AUTO сам держит маршруты при реконнектах и смене
# локации; в паре с selective в туннель попадают только перечисленные домены.
#
# Имя фазы — ИСТОРИЧЕСКОЕ «socks». Именно оно записано в уже установленные
# sudoers-правила /etc/sudoers.d/ymaster-adguard (правило перечисляет фазы
# явно, а обновление из приложения install-rule не перезапускает). Поэтому
# программа по-прежнему вызывает фазу «socks», а «tun» принято как синоним.
phase_tun() {
  need_root "tun"
  [ -e /dev/net/tun ] || die "нет /dev/net/tun — TUN-режим недоступен (контейнер без NET_ADMIN?)"
  local cli; cli=$(cli_path) || die "CLI не найден"
  "$cli" config set-mode TUN || die "set-mode TUN не удался"
  "$cli" config set-tun-routing-mode AUTO \
    || die "set-tun-routing-mode AUTO не удался"
  "$cli" site-exclusions mode selective \
    || die "site-exclusions mode selective не удался"

  # Домены Telegram. api.telegram.org — обязателен (Bot API); поддомены
  # AdGuard сам не наследует (issue #148), поэтому wildcards добавлены
  # отдельно и в кавычках — иначе их раскроет шелл.
  # v1.70.24: идемпотентность обеспечиваем САМИ — уже присутствующие домены
  # не добавляем повторно, а не надеемся на поведение CLI при повторном add.
  # Вывод `show` нормализуем в один токен на строку: там могут быть отступы,
  # маркеры списка и разделители.
  local d added=0 failed="" have
  have=$("$cli" site-exclusions show 2>/dev/null | tr -s ' \t,' '\n' || true)
  for d in "${TG_DOMAINS[@]}"; do
    if printf '%s\n' "$have" | grep -qxF -- "$d"; then
      continue
    fi
    if "$cli" site-exclusions add "$d" >/dev/null 2>&1; then
      added=$((added + 1))
    else
      # не ошибка: wildcard мог не поддержаться — критичен только Bot API,
      # его наличие проверяется ниже отдельно
      failed="$failed $d"
    fi
  done
  # критичен именно Bot API: без него туннель бессмыслен
  "$cli" site-exclusions show 2>/dev/null | grep -q "api.telegram.org" \
    || die "api.telegram.org не попал в selective-исключения — трафик бота не пойдёт через VPN"
  say "TUN + selective: через VPN идёт только Telegram (добавлено исключений: $added)"
  [ -n "$failed" ] && say "уже были/не добавились:$failed"
  say "остальной трафик сервера идёт напрямую, помимо VPN"
}

phase_units() {
  need_root "units"
  local f
  # v1.70.23: юнит туннеля и tg-health.sh прописаны на $CLI_LINK жёстко —
  # без ссылки systemctl restart упадёт с «служба туннеля не запустилась».
  ensure_link
  for f in "${UNITS_ETC[@]}"; do
    [ -f "$KIT_DIR/$f" ] || die "в $KIT_DIR нет $f — выполните install-rule"
    install -m 0644 -o root -g root "$KIT_DIR/$f" "/etc/systemd/system/$f"
  done
  for f in "${UNITS_SBIN[@]}"; do
    [ -f "$KIT_DIR/$f" ] || die "в $KIT_DIR нет $f — выполните install-rule"
    install -m 0755 -o root -g root "$KIT_DIR/$f" "/usr/local/sbin/$f"
  done
  systemctl daemon-reload || die "daemon-reload не удался"
  systemctl enable "$TUNNEL_UNIT" >/dev/null 2>&1 || true
  systemctl restart "$TUNNEL_UNIT" || die "служба туннеля не запустилась (journalctl -u $TUNNEL_UNIT)"
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    [ "$(systemctl is-active "$TUNNEL_UNIT" 2>/dev/null)" = "active" ] && break
    sleep 2
  done
  [ "$(systemctl is-active "$TUNNEL_UNIT" 2>/dev/null)" = "active" ] \
    || die "туннель не поднялся за 20 с — journalctl -u $TUNNEL_UNIT"
  say "туннель активен и переживёт перезагрузку"
}

phase_autopilot() {
  need_root "autopilot"
  systemctl daemon-reload || die "daemon-reload не удался"
  systemctl enable --now "$TIMER_UNIT" || die "таймер автопилота не включился"
  say "автопилот включён (замер 15:00, смена локации 15:10)"
}

# ----------------------------------------------------------------------
main() {
  local phase="${1:-}"
  [ -n "$phase" ] || { say "использование: ymaster-adguard-setup <фаза>"; exit 2; }
  shift || true
  case "$phase" in
    rights)       phase_rights ;;
    preflight)    phase_preflight ;;
    version)      phase_version ;;
    status)       phase_status ;;
    install)      phase_install ;;
    login)        phase_login ;;
    # v1.70.24: обе фазы настраивают TUN + selective. «socks» — историческое
    # имя: оно перечислено в уже установленных на серверах sudoers-правилах,
    # а обновление из приложения install-rule не перезапускает. «tun» — новое
    # понятное имя, добавлено в правило при следующем install-rule.
    socks)        phase_tun ;;
    tun)          phase_tun ;;
    units)        phase_units ;;
    autopilot)    phase_autopilot ;;
    install-rule) phase_install_rule "${1:-$DEFAULT_APP_DIR}" ;;
    remove-rule)  phase_remove_rule ;;
    *)            die "неизвестная фаза: $phase" ;;
  esac
}

main "$@"
