#!/usr/bin/env bash
# Ямастер Чек v1.70.68 — проверка связности цепочки
# «локальный прокси → Cloudflare Worker → api.telegram.org» (ТЗ §6,
# мониторинг). Запуск:
#   ./check_proxy.sh [домен-workers.dev] [адрес-локального-прокси]
# Код возврата 0 — всё зелено; 1 — есть отказ (удобно для cron).
set -u

WORKER_HOST="${1:-}"
PROXY="${2:-http://127.0.0.1:8080}"
fail=0

echo "== 1. Служба tgwsapiproxy =="
if systemctl is-active --quiet tgwsapiproxy; then
  echo "OK: служба активна"
else
  echo "FAIL: служба не активна — смотрите systemctl status tgwsapiproxy"
  fail=1
fi

echo "== 2. Слушатели только на локальном интерфейсе =="
if command -v ss >/dev/null 2>&1; then
  if ss -tln 2>/dev/null | grep -E '127\.0\.0\.1:(1080|8080)' >/dev/null; then
    echo "OK: порты 1080/8080 слушают на 127.0.0.1"
  else
    echo "FAIL: порты 1080/8080 не слушают на 127.0.0.1"
    fail=1
  fi
  if ss -tln 2>/dev/null | grep -E '0\.0\.0\.0:(1080|8080)' >/dev/null; then
    echo "FAIL: прокси открыт наружу — так быть не должно (ТЗ §5.3)"
    fail=1
  fi
fi

echo "== 3. api.telegram.org через локальный прокси =="
CODE=$(curl -s -m 15 -x "$PROXY" -o /dev/null -w '%{http_code}' \
       https://api.telegram.org/ || echo 000)
case "$CODE" in
  2*|3*|4*) echo "OK: связь есть (HTTP $CODE)" ;;
  *) echo "FAIL: прокси не отвечает (HTTP $CODE)"; fail=1 ;;
esac

if [ -n "$WORKER_HOST" ]; then
  echo "== 4. Worker: healthz =="
  HZ=$(curl -s -m 15 -o /dev/null -w '%{http_code}' \
       "https://${WORKER_HOST}/healthz" || echo 000)
  if [ "$HZ" = "200" ]; then
    echo "OK: healthz 200"
  else
    echo "FAIL: healthz $HZ"
    fail=1
  fi
  echo "== 5. Worker: диагностика /diag =="
  curl -s -m 25 "https://${WORKER_HOST}/diag" | head -24
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "ИТОГ: цепочка доступна."
else
  echo "ИТОГ: есть отказы — см. шаги выше."
fi
exit "$fail"
