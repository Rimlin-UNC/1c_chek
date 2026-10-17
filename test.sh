#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — быстрый прогон тестов. ООО «Ямастер» | ymaster.ru
#   ./test.sh            → критичный набор (~30–60 с)
#   ./test.sh ui         → UI-харнесс + парсинг бандла
#   ./test.sh full       → ВСЕ тесты параллельно
#   ./test.sh full -j2   → своим числом воркеров
# Воркеров не больше 4: параллелизм ускоряет, но перегружать машину
# разработки нельзя. Если pytest-xdist не установлен — последовательный
# прогон (медленнее, результат тот же).
# ======================================================================
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${1:-quick}"
ARG2="${2:-}"
case "$ARG2" in
  -j*) JOBS="${ARG2#-j}" ;;
  "")  JOBS="$(nproc 2>/dev/null || echo 2)"; [ "$JOBS" -gt 4 ] && JOBS=4 ;;
  *)   JOBS="$ARG2" ;;
esac
[ "$JOBS" -ge 1 ] || JOBS=1

if python3 -c "import xdist" 2>/dev/null; then
  XD=(-n "$JOBS" --dist loadfile -p no:cacheprovider)
else
  echo "ℹ pytest-xdist не найден — последовательный прогон (pip install pytest-xdist ускорит)"
  XD=()
fi

case "$PROFILE" in
  quick)
    echo "▶ Критичный набор (воркеров: $JOBS)"
    python3 -m pytest -q "${XD[@]}" \
      tests/test_version.py tests/test_blocks_smoke.py tests/test_dependencies.py \
      tests/test_external.py tests/test_roles.py tests/test_qr.py \
      tests/test_secrets_hygiene.py tests/test_a11y_structure.py \
      tests/test_v1584.py tests/test_v1583.py tests/test_v1582.py tests/test_v1581.py \
      tests/test_v1580.py tests/test_v1579.py tests/test_v1578.py tests/test_v1577.py \
      tests/test_v2520.py ;;
  ui)
    echo "▶ UI-харнесс (воркеров: $JOBS)"
    python3 -m pytest -q "${XD[@]}" \
      tests/test_v1121.py tests/test_v1130.py tests/test_v1140.py tests/test_v1583.py ;;
  full)
    echo "▶ Полный прогон (воркеров: $JOBS)"
    # test_v1310 (Чек-Пул) держит состояние базы между тестами —
    # гонится отдельным процессом после основной партии
    python3 -m pytest tests/ -q "${XD[@]}" --ignore tests/test_v1310.py &&
    python3 -m pytest tests/test_v1310.py -q ;;
  *)
    echo "использование: ./test.sh [quick|ui|full] [-jN]"; exit 1 ;;
esac
