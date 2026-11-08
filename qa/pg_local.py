#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный PostgreSQL для тестов — как сервис postgres:17 в CI, но без Docker.

В песочнице и на машине разработчика сервера PostgreSQL обычно нет, а CI
поднимает его сервисом. Скрипт запускает временный сервер (pip-пакет pgserver,
вне зависимостей проекта: ``.venv/bin/pip install pgserver``), передаёт адрес
тестам через ``UNIFIED_POSTGRES_TEST_URL`` и останавливает сервер после прогона.

Использование:
    python qa/pg_local.py                        тесты PostgreSQL (opt-in)
    python qa/pg_local.py pytest <аргументы>     любой прогон pytest с базой
    python qa/pg_local.py full [-jN]             весь набор как в CI: ./test.sh full,
                                                 отчёты JUnit и ворота qa/junit_gate.py

База одноразовая: рабочие данные и проект Supabase не затрагиваются. Режим
``full`` печатает каталог отчётов JUnit — его берёт qa/test_matrix.py для
QA-панели (docs/RUNBOOK.md, раздел «QA-панель в Google Sheets»).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PG_TESTS = ["tests/test_postgres_identity_optin.py", "tests/test_unified_postgres.py",
            "tests/test_supabase_migrations.py", "tests/test_migration_compat_postgres.py",
            # v1.70.110 (T-41): формы чека из app.js через API на PostgreSQL
            "tests/test_ui_api_contract.py",
            # v1.70.111 (T-78): отчёт и возврат времени чеков по QR на PostgreSQL
            "tests/test_receipt_time_repair.py",
            # v1.70.112 (T-46): согласование авансовых отчётов — маршрут, версия строки, нумерация на PostgreSQL
            "tests/test_approvals.py",
            # v1.70.113 (T-79): папка с УПД по компаниям, поступления и сверка выписки с документами на PostgreSQL
            "tests/test_bank_closing.py",
            # v1.70.114 (T-62): печатная форма УПД — сохранение полей и печатная страница на PostgreSQL
            "tests/test_primary_printupd.py",
            "tests/test_counterparties.py"]


def _environment(url: str) -> dict[str, str]:
    env = dict(os.environ, UNIFIED_POSTGRES_TEST_URL=url)
    # test.sh вызывает python3 — берём интерпретатор из .venv, как в CI
    env["PATH"] = f"{ROOT / '.venv' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_full(env: dict[str, str], extra: list[str]) -> int:
    junit = Path(tempfile.mkdtemp(prefix="ym-junit-"))
    env = dict(env, YM_JUNIT_DIR=str(junit))
    code = subprocess.call(["./test.sh", "full", *(extra or ["-j4"])], cwd=ROOT, env=env)
    reports = sorted(str(p) for p in junit.glob("*.xml"))
    gate = subprocess.call([sys.executable, "qa/junit_gate.py", "--title", "локальный прогон",
                            *reports], cwd=ROOT, env=env)
    print(f"pg-local: test.sh — код {code}; ворота — код {gate}; отчёты JUnit: {junit}", flush=True)
    if code == 0 and gate != 0:
        # без браузеров Playwright локально остаётся 1 пропуск; в CI их ставят
        print("pg-local: ворота не пройдены только из-за пропусков — смотрите SKIPPED выше", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    try:
        import pgserver
    except ImportError:
        # Запуск системным python (как в документации: «python qa/pg_local.py»):
        # pgserver стоит в .venv — перезапускаемся его интерпретатором. Проверка
        # по sys.prefix, а не по пути python: .venv/bin/python — ссылка на системный.
        venv = ROOT / ".venv"
        if argv is None and (venv / "bin" / "python").exists() and Path(sys.prefix).resolve() != venv.resolve():
            os.execv(str(venv / "bin" / "python"), [str(venv / "bin" / "python"), __file__, *args])
        print("pg-local: нет пакета pgserver — установите: .venv/bin/pip install pgserver",
              file=sys.stderr)
        return 2
    mode = args[0] if args else "pg"
    if mode not in ("pg", "pytest", "full"):
        print(f"pg-local: неизвестный режим «{mode}» — см. --help", file=sys.stderr)
        return 2
    with pgserver.get_server(Path(tempfile.mkdtemp(prefix="ym-pg-")), cleanup_mode="stop") as server:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        print(f"pg-local: временный PostgreSQL запущен ({url.rsplit('@', 1)[-1]})", flush=True)
        env = _environment(url)
        if mode == "full":
            return _run_full(env, args[1:])
        pytest_args = args[1:] if mode == "pytest" else PG_TESTS
        return subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                *pytest_args], cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
