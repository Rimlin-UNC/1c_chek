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
# Фазы: preflight | install | login | socks | units | autopilot |
#       version | status | rights | install-rule | remove-rule
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
INSTALL_CMD="curl -fsSL https://raw.githubusercontent.com/AdguardTeam/AdGuardVPNCLI/HEAD/scripts/release/install.sh | sh -s -- -v"
# Фазы, доступные из-под приложения (в них и состоит sudoers-правило).
# install-rule/remove-rule здесь НЕТ: их владелец выполняет вручную.
ALLOWED_PHASES="preflight rights install login socks units autopilot version status"

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
      say "CLI уже установлен: $("$cli" --version 2>&1 | head -n1)"
      return 0
    fi
  fi
  say "Ставим AdGuard VPN CLI официальным установщиком (1–2 минуты)…"
  # -v: без вопросов; установщик сам отвечает «y» на ссылку /usr/local/bin
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
  if [ -x "$CLI_REAL" ] && [ ! -x "$CLI_LINK" ]; then
    ln -sf "$CLI_REAL" "$CLI_LINK" && say "создана ссылка $CLI_LINK"
  fi
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

phase_socks() {
  need_root "socks"
  local cli; cli=$(cli_path) || die "CLI не найден"
  "$cli" config set-mode SOCKS || die "set-mode SOCKS не удался"
  say "SOCKS5 включён: socks5://127.0.0.1:1080"
}

phase_units() {
  need_root "units"
  local f
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
    socks)        phase_socks ;;
    units)        phase_units ;;
    autopilot)    phase_autopilot ;;
    install-rule) phase_install_rule "${1:-$DEFAULT_APP_DIR}" ;;
    remove-rule)  phase_remove_rule ;;
    *)            die "неизвестная фаза: $phase" ;;
  esac
}

main "$@"
