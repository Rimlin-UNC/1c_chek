#!/usr/bin/env bash
# Reproducible local QA setup. Never touches application data or .env files.
set -euo pipefail
cd "$(dirname "$0")/.."

mode=setup
postgres=0
for arg in "$@"; do
  case "$arg" in
    --check) mode=check ;;
    --with-postgres) postgres=1 ;;
    -h|--help)
      echo 'Usage: qa/setup_dev.sh [--with-postgres] [--check]'
      echo 'Creates .venv if absent, installs dev dependencies and Node tools if absent.'
      echo '--with-postgres installs the optional psycopg 3 binary driver.'
      echo '--check only validates already installed tools and does not install anything.'
      exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

for cmd in python3 node npm; do
  command -v "$cmd" >/dev/null || { echo "Missing required command: $cmd" >&2; exit 1; }
done
if [[ "$mode" == setup ]]; then
  if [[ ! -e .venv ]]; then python3 -m venv .venv; fi
  [[ -x .venv/bin/python ]] || { echo 'Existing .venv is not usable; it was not modified' >&2; exit 1; }
  .venv/bin/python -m pip install -r requirements-dev.txt
  if [[ "$postgres" == 1 ]]; then .venv/bin/python -m pip install 'psycopg[binary]>=3.1,<4'; fi
  if [[ ! -x node_modules/.bin/vitest ]]; then
    if [[ -e node_modules ]]; then
      echo 'node_modules exists but Vitest is missing; refusing to replace it automatically' >&2
      exit 1
    fi
    npm ci --no-audit --no-fund
  fi
fi
[[ -x .venv/bin/python ]] || { echo 'Missing .venv; run qa/setup_dev.sh first' >&2; exit 1; }
[[ -x node_modules/.bin/vitest ]] || { echo 'Missing Node tools; run qa/setup_dev.sh first' >&2; exit 1; }
.venv/bin/python -m pip check
.venv/bin/python -c 'import pytest, xdist, hypothesis; print("Python QA imports: OK")'
.venv/bin/ruff --version
node --version
npm --version
if [[ "$postgres" == 1 ]]; then
  .venv/bin/python -c 'import psycopg; print("PostgreSQL driver:", psycopg.__version__)'
fi
printf '%s\n' 'Ready. No application database, Supabase project or secrets were modified.'
