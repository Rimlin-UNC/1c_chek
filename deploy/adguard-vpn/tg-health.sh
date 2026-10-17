#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — автопилот AdGuard VPN (v1.70.14)
# Раз в сутки (systemd-timer, 15:10 по серверному времени = МСК):
#   1) 3 коротких запроса к api.telegram.org через локальный прокси
#      AdGuard VPN; считаем среднее.
#   2) Если путь мёртв ИЛИ среднее > 30 мс — подбираем локацию:
#      Германия (минимальный её пинг) -> иначе локация с пингом <= 30 ->
#      иначе просто самая быстрая. Подключаемся и проверяем ещё раз.
# Экономия: обычный день — только 3 запроса; list-locations (пингует
# все локации) выполняется ТОЛЬКО при проблеме.
# Установка: sudo cp tg-health.sh /usr/local/sbin/ && sudo chmod +x /usr/local/sbin/tg-health.sh
# ======================================================================
set -u
CLI="${CLI:-/usr/local/bin/adguardvpn-cli}"
TARGET="${TARGET_MS:-30}"                # целевой пинг, мс
CHECK_URL="https://api.telegram.org/"

# v1.70.24: режим TUN + selective — трафик до api.telegram.org уходит в
# туннель ПРОЗРАЧНО (по доменному исключению), локального SOCKS-слушателя
# больше нет. Замер с `-x socks5h://127.0.0.1:1080` в таком режиме давал бы
# -1 («путь мёртв») и автопилот ежедневно дёргал бы локацию на рабочем
# туннеле. Поэтому режим определяем сами: порт 1080 слушается — значит
# сервер ещё в SOCKS5 (старая схема), меряем через прокси; не слушается —
# меряем напрямую, что в TUN и есть настоящий путь бота.
PROXY="${TG_PROXY:-}"
if [ -z "$PROXY" ] && (exec 3<>/dev/tcp/127.0.0.1/1080) 2>/dev/null; then
  PROXY="socks5h://127.0.0.1:1080"
  exec 3>&- 2>/dev/null || true
fi

# --- 1) замер: 3 запроса, среднее целыми мс; -1 = путь мёртв ----------
measure() {
  local t sum=0 n=0
  for _ in 1 2 3; do
    if [ -n "$PROXY" ]; then
      t=$(curl -x "$PROXY" -o /dev/null -s --max-time 10 \
                -w '%{time_total}' "$CHECK_URL" 2>/dev/null) || { sleep 0.3; continue; }
    else
      t=$(curl -o /dev/null -s --max-time 10 \
                -w '%{time_total}' "$CHECK_URL" 2>/dev/null) || { sleep 0.3; continue; }
    fi
    [ -n "$t" ] && sum=$(awk -v a="$sum" -v b="$t" 'BEGIN{print a+b}') && n=$((n+1))
    sleep 0.3
  done
  [ "$n" -eq 0 ] && { echo -1; return; }
  awk -v s="$sum" -v n="$n" 'BEGIN{printf "%d", s/n*1000}'
}

AVG=$(measure)
if [ "$AVG" -ge 0 ] && [ "$AVG" -le "$TARGET" ]; then
  echo "tg-health: пинг ${AVG} мс — в норме (порог ${TARGET}), смены не требуется"
  exit 0
fi
echo "tg-health: пинг ${AVG} мс (порог ${TARGET}) — подбираем локацию"

# --- 2) список локаций «пинг<TAB>имя» по возрастанию -------------------
MAP=$("$CLI" list-locations 2>/dev/null | awk -F'Ping:' 'NF>1 {
  p=$2; gsub(/[^0-9]/, "", p); name=$1;
  # срезаем хвост-разделитель и пробелы вокруг него (« — », «: » и т.п.),
  # иначе connect -l не совпадёт с настоящим именем локации
  gsub(/[[:space:]]+$/, "", name);
  sub(/[-—–:|]+$/, "", name);
  gsub(/[[:space:]]+$/, "", name);
  if (name != "" && p != "") print p "\t" name }' | sort -n)
if [ -z "$MAP" ]; then
  echo "tg-health: список локаций недоступен (CLI/вход?)"; exit 1
fi

DE=$(echo "$MAP"   | grep -iE 'germany|герман' | head -1 | cut -f2)
FAST=$(awk -F'\t' -v t="$TARGET" '$1 + 0 <= t {print $2; exit}' <<<"$MAP")
BEST=$(head -1 <<<"$MAP" | cut -f2)

PICK=""
[ -n "$DE" ]   && PICK="$DE"
[ -z "$PICK" ] && [ -n "$FAST" ] && PICK="$FAST"
[ -z "$PICK" ] && PICK="$BEST"

echo "tg-health: подключаем «$PICK»"
"$CLI" connect -l "$PICK" >/dev/null 2>&1
sleep 5
AVG2=$(measure)
echo "tg-health: после смены «$PICK» пинг ${AVG2} мс"

# --- 3) Германия не дала порога — самая быстрая (если это не она) -----
if { [ "$AVG2" -lt 0 ] || [ "$AVG2" -gt "$TARGET" ]; } && [ -n "$FAST" ] && [ "$FAST" != "$PICK" ]; then
  echo "tg-health: вторая попытка — «$FAST»"
  "$CLI" connect -l "$FAST" >/dev/null 2>&1
  sleep 5
  AVG3=$(measure)
  echo "tg-health: итог «$FAST» пинг ${AVG3} мс"
fi
# если туннелем владеет systemd-юнит — передаём ему подключение
# (юнит переподключится на выбранную локацию; Restart=always держит туннель)
if systemctl is-active -q adguard-vpn-telegram 2>/dev/null; then
  systemctl try-restart adguard-vpn-telegram 2>/dev/null || true
fi

echo "tg-health: готово (проверка программы — кнопка «📏 Пинг» в карточке Telegram)"
exit 0
