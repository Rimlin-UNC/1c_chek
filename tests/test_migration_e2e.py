"""Сквозной тест миграции рабочей базы (#20).

1.70.80 → текущая версия → перенос архива → сверка инвариантов → откат на
1.70.80 → повторное обновление. Сценарий, фазы и проверки — qa/migration_e2e.py;
здесь — запуск на каждом бэкенде и понятное сообщение о каждом нарушении.

На SQLite тест выполняется всегда, на PostgreSQL 17 — при UNIFIED_POSTGRES_TEST_URL
(в CI — сервис postgres:17; локально: python3 qa/pg_local.py pytest
tests/test_migration_e2e.py). В CI пропуски запрещены воротами qa/junit_gate.py.
"""
from __future__ import annotations

import os

import pytest

from qa import migration_e2e as e2e

PG_URL = os.getenv("UNIFIED_POSTGRES_TEST_URL", "")
NEED_PG = pytest.mark.skipif(
    not PG_URL, reason="нужен UNIFIED_POSTGRES_TEST_URL: в CI — сервис PostgreSQL 17, локально — "
                       "python3 qa/pg_local.py pytest tests/test_migration_e2e.py")


@pytest.fixture(scope="module")
def code_dirs(tmp_path_factory):
    """Код 1.70.80 (git archive) и копия текущего — один раз на модуль."""
    try:
        return e2e.prepare_code(tmp_path_factory.mktemp("migration-e2e"))
    except e2e.OldCodeUnavailable as exc:
        pytest.skip(f"сквозной тест миграции: {exc}")


def _assert_all(checks: list[dict], backend: str) -> None:
    failed = [c for c in checks if not c["ok"]]
    assert checks and not failed, (
        f"Сквозной тест миграции ({backend}): нарушено {len(failed)} из {len(checks)} проверок\n"
        + e2e.format_checks(checks))


def test_migration_e2e_sqlite(code_dirs, tmp_path):
    checks, _ = e2e.run_scenario(e2e.sqlite_url(tmp_path), *code_dirs, postgres=False)
    _assert_all(checks, "SQLite")


@NEED_PG
def test_migration_e2e_postgres(code_dirs):
    if not PG_URL.startswith("postgresql"):
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    with e2e.postgres_database(PG_URL) as url:
        checks, _ = e2e.run_scenario(url, *code_dirs, postgres=True)
    _assert_all(checks, "PostgreSQL")


@NEED_PG
def test_fresh_install_postgres(code_dirs):
    """Чистая установка текущей версии на PostgreSQL — как deploy.sh: python -m app.seed.

    1.70.80 так не ставится: init_db() вставлял компанию по умолчанию с
    is_active = 1 — целое число в булев столбец. Для перехода рабочей базы на
    PostgreSQL (#11) текущая версия обязана устанавливаться с нуля."""
    with e2e.postgres_database(PG_URL) as url:
        out = e2e.fresh_install(url, code_dirs[1])
    assert out.get("seed_code") == 0, f"python -m app.seed на чистом PostgreSQL упал:\n{out.get('error')}"
    assert out["admins"] == 1 and out["companies"], f"после установки нет администратора или компании: {out}"
    assert all(c["is_active"] is True for c in out["companies"]), f"компания по умолчанию не активна: {out}"
