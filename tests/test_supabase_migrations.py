"""Миграции Supabase-демо: имена совпадают с историей проекта, SQL применим.

1. Файлы ``supabase/migrations`` названы как у Supabase CLI
   (``<14 цифр>_<имя>.sql``), а три уже применённые в проекте ``chek.base``
   версии присутствуют под своими номерами: репозиторий и история миграций
   проекта не расходятся (issue #8).
2. Opt-in (нужен ``UNIFIED_POSTGRES_TEST_URL``; в CI — сервис postgres:17):
   миграции применяются по порядку на чистой отдельной базе PostgreSQL, и
   результат ``supabase/verify.sql`` совпадает с ожиданием из этого файла.
   Затем каждый файл запускается ещё раз, как при ошибке оператора: запуск
   либо проходит, либо откатывается целиком, и ``verify.sql`` по-прежнему
   даёт то же ожидание.

Рабочая БД и проект Supabase не используются. Тест создаёт и удаляет свою
базу; роли Supabase (anon, authenticated, service_role) создаются в
одноразовом кластере теста, если их нет.
"""
from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
VERIFY_SQL = ROOT / "supabase" / "verify.sql"

# Версии, применённые в проекте Supabase chek.base (supabase_migrations.schema_migrations)
APPLIED_IN_PROJECT = ("20261101154331", "20261101155225", "20261101164303")

# Ожидание из supabase/verify.sql: компании, участники, чеки, позиции, сумма,
# расхождения позиций, затем права anon/authenticated.
EXPECTED = (2, 3, 12, 24, Decimal("15130.50"), 0,
            False, False, False, True, False, False, True)


def test_migration_files_follow_supabase_cli_naming():
    assert MIGRATIONS, "нет файлов supabase/migrations"
    names = [p.name for p in MIGRATIONS]
    bad = [n for n in names if not re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", n)]
    assert not bad, f"имена не в формате Supabase CLI: {bad}"
    versions = [n.split("_", 1)[0] for n in names]
    assert len(versions) == len(set(versions)), "повтор номера версии"
    missing = [v for v in APPLIED_IN_PROJECT if v not in versions]
    assert not missing, f"применённые в проекте версии без файла: {missing}"


def test_verify_expectation_matches_documented_line():
    text = VERIFY_SQL.read_text(encoding="utf-8")
    line = re.search(r"Ожидание:\s*(.+?)\.?\s*$", text, re.M).group(1)
    documented = [part.strip() for part in line.split("/")]
    rendered = [str(v).lower() if isinstance(v, bool) else str(v) for v in EXPECTED]
    assert documented == rendered


def _ensure_roles(conn, roles):
    """Создать роли, если их нет, — без гонки между параллельными тестами.

    Роли общие на весь кластер PostgreSQL, а pytest-xdist гоняет файлы
    параллельно. Схема «проверить, потом создать» проигрывает гонку: оба
    процесса видят, что роли нет, и второй падает (воспроизведено: 87 ошибок
    из 160 одновременных попыток). Перехват duplicate_object и unique_violation
    делает создание атомарным для любого числа процессов.
    """
    for role in roles:
        conn.exec_driver_sql(
            f"do $$ begin create role {role} nologin; "
            "exception when duplicate_object or unique_violation then null; end $$")


def _verify(engine):
    with engine.connect() as conn:
        return tuple(conn.exec_driver_sql(VERIFY_SQL.read_text(encoding="utf-8")).one())


def _apply(engine, path):
    with engine.begin() as conn:                 # один файл = одна транзакция
        conn.exec_driver_sql(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="dedicated PostgreSQL test URL not configured")
def test_migrations_apply_on_clean_postgres_and_reruns_are_harmless():
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError

    url = make_url(os.environ["UNIFIED_POSTGRES_TEST_URL"])
    if url.get_backend_name() != "postgresql":
        pytest.fail("UNIFIED_POSTGRES_TEST_URL must point to PostgreSQL")
    dbname = "qa_supabase_" + uuid.uuid4().hex[:12]
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            _ensure_roles(conn, ("anon", "authenticated", "service_role"))
            conn.exec_driver_sql(f'create database "{dbname}"')
        engine = create_engine(url.set(database=dbname))
        try:
            for path in MIGRATIONS:              # штатно: по порядку, по одному разу
                _apply(engine, path)
            assert _verify(engine) == EXPECTED

            outcomes = {}
            for path in MIGRATIONS:              # ошибочный повторный запуск
                try:
                    _apply(engine, path)
                    outcomes[path.name] = "applied again"
                except DBAPIError as exc:
                    outcomes[path.name] = f"rolled back: {type(exc.orig).__name__}"
                assert _verify(engine) == EXPECTED, outcomes
        finally:
            engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'drop database if exists "{dbname}" with (force)')
        admin.dispose()
