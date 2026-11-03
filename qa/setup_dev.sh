#!/usr/bin/env bash
# Воспроизводимая подготовка окружения разработки и QA.
# Никогда не трогает данные приложения, .env, проект Supabase и секреты.
set -euo pipefail
cd "$(dirname "$0")/.."

# Версии валидаторов — те же, что в .github/workflows/ci.yml
STYLELINT_VERSION="17.16.0"
HTMLVALIDATE_VERSION="11.18.0"

mode=setup
postgres=0
full=0
for arg in "$@"; do
  case "$arg" in
    --check) mode=check ;;
    --with-postgres) postgres=1 ;;
    --full) full=1; postgres=1 ;;
    -h|--help)
      echo 'Использование: qa/setup_dev.sh [--with-postgres] [--full] [--check]'
      echo 'Создаёт .venv, ставит зависимости разработки и Node-инструменты, если их нет.'
      echo '--with-postgres  драйвер psycopg 3 для тестов PostgreSQL.'
      echo '--full           всё для прогона «как в CI»: --with-postgres, pgserver'
      echo '                 (локальный PostgreSQL, qa/pg_local.py) и валидаторы'
      echo '                 stylelint и html-validate в /tmp, как в CI; если браузеров'
      echo '                 Playwright нет — Chromium из npm (qa/local_chromium.sh).'
      echo '--check          только проверить установленное, ничего не ставить.'
      exit 0 ;;
    *) echo "Неизвестный параметр: $arg" >&2; exit 2 ;;
  esac
done

for cmd in python3 node npm; do
  command -v "$cmd" >/dev/null || { echo "Не найдена обязательная команда: $cmd" >&2; exit 1; }
done
if [[ "$mode" == setup ]]; then
  if [[ ! -e .venv ]]; then python3 -m venv .venv; fi
  [[ -x .venv/bin/python ]] || { echo 'Существующий .venv непригоден; он не изменён' >&2; exit 1; }
  .venv/bin/python -m pip install -r requirements-dev.txt
  if [[ "$postgres" == 1 ]]; then .venv/bin/python -m pip install 'psycopg[binary]>=3.1,<4'; fi
  if [[ ! -x node_modules/.bin/vitest ]]; then
    if [[ -e node_modules ]]; then
      echo 'node_modules есть, но Vitest в нём нет; заменять автоматически не буду' >&2
      exit 1
    fi
    npm ci --no-audit --no-fund
  fi
  if [[ "$full" == 1 ]]; then
    .venv/bin/python -m pip install pgserver
    npm install --prefix /tmp/csslint --no-audit --no-fund "stylelint@${STYLELINT_VERSION}"
    npm install --prefix /tmp/htmlval --no-audit --no-fund "html-validate@${HTMLVALIDATE_VERSION}"
    # v1.70.86: браузер для Playwright. В CI его ставит `npx playwright install`; без
    # доступа к CDN браузеров — сборка из npm. Шаг необязательный: без браузера
    # браузерные сценарии в полном прогоне пропускаются, как раньше.
    if ! compgen -G "$HOME/.cache/ms-playwright/*" >/dev/null; then
      bash qa/local_chromium.sh || echo 'Chromium для Playwright не подготовлен — браузерные сценарии будут пропущены' >&2
    fi
  fi
fi
[[ -x .venv/bin/python ]] || { echo 'Нет .venv: сначала запустите qa/setup_dev.sh' >&2; exit 1; }
[[ -x node_modules/.bin/vitest ]] || { echo 'Нет Node-инструментов: сначала запустите qa/setup_dev.sh' >&2; exit 1; }
.venv/bin/python -m pip check
.venv/bin/python -c 'import pytest, xdist, hypothesis; print("Python QA: импорты в порядке")'
.venv/bin/ruff --version
node --version
npm --version
if [[ "$postgres" == 1 ]]; then
  .venv/bin/python -c 'import psycopg; print("Драйвер PostgreSQL:", psycopg.__version__)'
fi
if [[ "$full" == 1 ]]; then
  .venv/bin/python -c 'import importlib.metadata as m; print("Локальный PostgreSQL (pgserver):", m.version("pgserver"))'
  [[ -x /tmp/csslint/node_modules/.bin/stylelint ]] || { echo 'Нет stylelint: запустите qa/setup_dev.sh --full' >&2; exit 1; }
  [[ -x /tmp/htmlval/node_modules/.bin/html-validate ]] || { echo 'Нет html-validate: запустите qa/setup_dev.sh --full' >&2; exit 1; }
  echo "stylelint: $(/tmp/csslint/node_modules/.bin/stylelint --version)"
  echo "html-validate: $(/tmp/htmlval/node_modules/.bin/html-validate --version)"
  if [[ -x /tmp/ym-chromium/chrome.sh ]]; then
    echo 'Playwright: YM_PLAYWRIGHT_CHROMIUM=/tmp/ym-chromium/chrome.sh (qa/local_chromium.sh)'
  fi
fi
printf '%s\n' 'Готово. База приложения, проект Supabase и секреты не изменялись.'
