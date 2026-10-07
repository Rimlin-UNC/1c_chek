#!/usr/bin/env bash
# ======================================================================
# Ямастер Чек — откат к последней рабочей версии (v1.46.0)
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
#
# Главный инструмент, когда приложение НЕ ЗАПУСКАЕТСЯ после обновления:
# работает из терминала, приложение для этого не нужно.
#
#   sudo bash rollback.sh                 — показать последние рабочие версии
#   sudo bash rollback.sh 1.44.2          — откатиться к рабочей версии
#   sudo bash rollback.sh <коммит>        — откатиться к конкретному коммиту
#
# Что делает:
#   1) страховая копия базы (data/backups/db-prerollback-…, хранятся 5);
#   2) git reset --hard на коммит рабочей версии + чистка лишних файлов
#      (данные data/, .env, venv и *.db НЕ трогаются);
#   3) зависимости, если requirements.txt изменился;
#   4) перезапуск сервиса и проверка, что приложение отвечает;
#   5) при неудаче — печать журнала и подсказка (состояние НЕ портится:
#      откатState — обычный git-коммит, повторяется в любую сторону).
# ======================================================================
set -u
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
SERVICE="ymaster-check"
REGISTRY="data/releases/known_good.json"
DB="data/ymaster_check.db"
PORT="${YM_PORT:-8000}"

say()  { printf '%s\n' "$*"; }
die()  { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[ -d .git ] || die "каталог $(pwd) — не git-репозиторий (откат невозможен)"

# ---------- список рабочих версий -----------------------------------------
list_releases() {
  if [ -f "$REGISTRY" ]; then
    python3 - "$REGISTRY" <<'PY'
import json, sys
try:
    items = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    items = []
cur = ""
try:
    sys.path.insert(0, ".")
    from app.config import settings
    cur = settings.APP_VERSION
except Exception:
    pass
if not items:
    print("Реестр пуст — рабочих версий ещё не зафиксировано.")
for it in items:
    mark = "  <- работает сейчас" if it.get("version") == cur else ""
    print(f"  v{it.get('version')}  коммит {str(it.get('commit'))[:8]}  "
          f"от {str(it.get('at'))[:10]}  {it.get('note','')}{mark}")
PY
  else
    say "Реестр рабочих версий не найден — последние коммиты ветки:"
    git log --oneline -10
  fi
}

# ---------- разбор аргумента: версия или коммит ----------------------------
resolve_commit() {
  local target="$1"
  if [ -f "$REGISTRY" ]; then
    local c
    c=$(python3 - "$REGISTRY" "$target" <<'PY'
import json, sys
try:
    items = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    items = []
for it in items:
    if str(it.get("version")) == sys.argv[2]:
        print(it.get("commit", ""))
        break
PY
)
    [ -n "$c" ] && { echo "$c"; return 0; }
  fi
  case "$target" in
    *[!0-9a-f]*|'') die "цель «$target»: ожидался номер версии или hex-коммит" ;;
  esac
  echo "$target"
}

# ---------- выполнение -----------------------------------------------------
TARGET="${1:-}"
if [ -z "$TARGET" ] || [ "$TARGET" = "--list" ] || [ "$TARGET" = "list" ]; then
  say "Последние рабочие версии ($APP_DIR):"
  list_releases
  say ""
  say "Откат: sudo bash rollback.sh <версия>   (например: sudo bash rollback.sh 1.44.2)"
  exit 0
fi

COMMIT="$(resolve_commit "$TARGET")"
git cat-file -e "${COMMIT}^{commit}" 2>/dev/null || die "коммит $COMMIT не найден в репозитории"

STAMP="$(date +%Y%m%d-%H%M%S)"
OLD_HEAD="$(git rev-parse HEAD)"

say "1/5 Страховая копия базы…"
if [ -f "$DB" ]; then
  mkdir -p data/backups
  cp "$DB" "data/backups/db-prerollback-$STAMP.db"
  ls -1t data/backups/db-prerollback-*.db 2>/dev/null | tail -n +6 | xargs -r rm -f
  say "    копия: data/backups/db-prerollback-$STAMP.db"
else
  say "    (база не найдена — пропущено)"
fi

say "2/5 Возврат файлов к рабочей версии ($COMMIT)…"
git reset --hard "$COMMIT" >/dev/null || die "git reset не удался"
# удалённые из проекта файлы не остаются; данные/секреты/venv/база целы
git clean -fd -e data -e .env -e venv -e '*.db' >/dev/null || true

say "3/5 Зависимости…"
if ! git diff --quiet "$OLD_HEAD" HEAD -- requirements.txt 2>/dev/null; then
  pip3 install -q -r requirements.txt || die "установка зависимостей не удалась"
  say "    обновлены (requirements.txt изменился)"
else
  say "    не менялись — пропущено"
fi

say "4/5 Перезапуск сервиса…"
if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE"; then
  systemctl restart "$SERVICE" || die "systemctl restart $SERVICE не удался (журнал: journalctl -u $SERVICE -n 50)"
  sleep 3
else
  say "    сервис $SERVICE не найден — запустите приложение как обычно"
fi

say "5/5 Проверка…"
HEALTH_OK=0
for i in 1 2 3 4 5; do
  if curl -sf "http://127.0.0.1:$PORT/api/v1/about" 2>/dev/null | grep -q '"version"'; then
    HEALTH_OK=1; break
  fi
  sleep 2
done
NEW_VER="$(python3 -c "import sys; sys.path.insert(0,'.'); from app.config import settings; print(settings.APP_VERSION)" 2>/dev/null || echo '?')"
if [ "$HEALTH_OK" = "1" ]; then
  say ""
  say "ГОТОВО: работает v$NEW_VER (откат к $TARGET)."
  say "Проверьте сайт; реестр рабочих версий пополнится автоматически при старте."
else
  say ""
  say "ВНИМАНИЕ: файлы откатились к v$NEW_VER, но приложение не отвечает на порту $PORT."
  say "Журнал:  journalctl -u $SERVICE -n 50"
  say "Повторите откат к другой рабочей версии: sudo bash rollback.sh"
  exit 1
fi
