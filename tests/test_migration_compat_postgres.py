"""Совместимость схемы приложения с проектом Supabase chek.base (PostgreSQL 17).

Вопрос владельца: «проверь, что миграция не сломает текущую структуру».
Тест собирает на одноразовой базе PostgreSQL то, что сегодня есть в проекте
Supabase, и кладёт рядом все таблицы приложения — так, как их создаст перенос
рабочей БД (решение по переходу — #11):

1. роли anon/authenticated/service_role, таблица истории миграций
   supabase_migrations.schema_migrations и копия событийного триггера
   ensure_rls (определение снято с проекта 02.11.2026: включает RLS на каждой
   новой таблице в public);
2. три миграции supabase/migrations — демо-контур chek_demo;
3. все таблицы приложения (Base.metadata) в схеме public.

Проверяется:
- демо-контур цел и после схемы приложения: supabase/verify.sql даёт прежнее
  ожидание, коллизий имён нет;
- supabase/checks/rls_fk_audit.sql не находит ни одной ОШИБКИ, все внешние
  ключи приложения провалидированы;
- ensure_rls включил RLS на каждой таблице приложения: владелец таблиц
  работает как обычно, а роль, не владеющая таблицами, видит их пустыми.
  Поэтому при переносе подключаться владельцем таблиц или создавать их в
  отдельной схеме — это записано в docs/DATABASE.md.

Реальный проект Supabase не затрагивается. Нужен UNIFIED_POSTGRES_TEST_URL
(в CI — сервис postgres:17, пользователь — суперпользователь: событийный
триггер создаёт только он).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tests.test_supabase_migrations import EXPECTED, MIGRATIONS, _apply, _verify

ROOT = Path(__file__).resolve().parents[1]
AUDIT_SQL = ROOT / "supabase" / "checks" / "rls_fk_audit.sql"

# Копия функции и событийного триггера проекта chek.base (pg_get_functiondef,
# 02.11.2026). В миграциях проекта их нет — Q-10; здесь они воспроизводят
# поведение Supabase при создании таблиц приложения в public.
SUPABASE_ENSURE_RLS = r"""
CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
  tbl name;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        SELECT c.relname INTO tbl FROM pg_catalog.pg_class c WHERE c.oid = cmd.objid;
        EXECUTE format('alter table if exists %I.%I enable row level security', cmd.schema_name, tbl);
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     END IF;
  END LOOP;
END;
$function$;
CREATE EVENT TRIGGER ensure_rls ON ddl_command_end
  WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
  EXECUTE FUNCTION public.rls_auto_enable();
"""

SUPABASE_HISTORY = """
CREATE SCHEMA supabase_migrations;
CREATE TABLE supabase_migrations.schema_migrations (version text PRIMARY KEY, name text);
"""


def _app_metadata():
    """Все таблицы приложения — те же модули, что регистрирует init_db()."""
    from app import models  # noqa: F401
    from app.b2b import models as _b2b  # noqa: F401
    from app.database import Base
    from app.openstack_monitor import models as _os  # noqa: F401
    from app.pool import models as _pool  # noqa: F401
    from app.unified import models as _unified  # noqa: F401
    return Base.metadata


def _exec_raw(conn, sql: str):
    """Выполнить SQL как есть, курсором драйвера без параметров.

    exec_driver_sql передаёт psycopg пустые параметры, и драйвер разбирает
    каждый «%» как заполнитель. В функции PL/pgSQL (%I) и в шаблонах LIKE
    аудита знак % — часть текста, а файл аудита должен оставаться пригодным
    для SQL-редактора Supabase, поэтому экранировать его нельзя.
    """
    cur = conn.connection.cursor()
    try:
        cur.execute(sql)
        return [tuple(row) for row in cur.fetchall()] if cur.description else None
    finally:
        cur.close()


def _audit(engine):
    with engine.connect() as conn:
        return _exec_raw(conn, AUDIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="dedicated PostgreSQL test URL not configured")
def test_app_schema_fits_next_to_supabase_contour():
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["UNIFIED_POSTGRES_TEST_URL"])
    if url.get_backend_name() != "postgresql":
        pytest.fail("UNIFIED_POSTGRES_TEST_URL must point to PostgreSQL")
    dbname = "qa_compat_" + uuid.uuid4().hex[:12]
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            for role in ("anon", "authenticated", "service_role", "qa_compat_reader"):
                conn.exec_driver_sql(
                    "do $$ begin if not exists (select 1 from pg_roles where rolname = "
                    f"'{role}') then create role {role} nologin; end if; end $$")
            conn.exec_driver_sql(f'create database "{dbname}"')
        engine = create_engine(url.set(database=dbname))
        try:
            # 1–2. Что сегодня есть в проекте Supabase
            with engine.begin() as conn:
                _exec_raw(conn, SUPABASE_HISTORY)
                _exec_raw(conn, SUPABASE_ENSURE_RLS)
            for path in MIGRATIONS:
                _apply(engine, path)
                version, _, name = path.stem.partition("_")
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "insert into supabase_migrations.schema_migrations values "
                        f"('{version}', '{name}')")
            assert _verify(engine) == EXPECTED

            # 3. Все таблицы приложения рядом с демо-контуром
            metadata = _app_metadata()
            with engine.begin() as conn:
                metadata.create_all(bind=conn)
            app_tables = sorted(metadata.tables)
            assert len(app_tables) >= 40, f"подозрительно мало таблиц приложения: {len(app_tables)}"
            assert _verify(engine) == EXPECTED, "схема приложения задела демо-контур chek_demo"

            # Аудит RLS и внешних ключей: ни одной ОШИБКИ
            rows = _audit(engine)
            errors = [r for r in rows if r[3] == "ОШИБКА"]
            assert not errors, f"аудит нашёл ошибки: {errors}"
            rls_ok = {r[2] for r in rows if r[0] == "R1" and r[3] == "OK"}
            missing = [t for t in app_tables if f"public.{t}" not in rls_ok]
            assert not missing, f"ensure_rls не включил RLS на таблицах: {missing}"
            fk_rows = [r for r in rows if r[0] == "F1" and r[2].startswith(tuple(app_tables))]
            assert fk_rows, "у таблиц приложения не найдено внешних ключей — аудит выродился"
            assert {r[3] for r in rows if r[0] == "M1"} == {"OK"}

            # Последствие ensure_rls: владелец видит строки, не владелец — нет
            from app.models import Company
            from sqlalchemy.orm import Session
            with Session(engine) as db:
                db.add(Company(name="Проверка совместимости"))
                db.commit()
            with engine.begin() as conn:
                owner_rows = conn.exec_driver_sql("select count(*) from companies").scalar_one()
                conn.exec_driver_sql("grant usage on schema public to qa_compat_reader")
                conn.exec_driver_sql("grant select on companies to qa_compat_reader")
                conn.exec_driver_sql("set local role qa_compat_reader")
                reader_rows = conn.exec_driver_sql("select count(*) from companies").scalar_one()
            print(f"совместимость: таблиц приложения {len(app_tables)}, RLS включён на "
                  f"{len(rls_ok)} таблицах, внешних ключей приложения {len(fk_rows)}, "
                  f"строк аудита {len(rows)} (ОШИБОК 0); companies видит владелец: "
                  f"{owner_rows}, роль без владения: {reader_rows}")
            assert owner_rows == 1, "владелец таблиц должен видеть свои строки"
            assert reader_rows == 0, ("роль без владения при включённом RLS и без политик "
                                      "не должна видеть строк — иначе вывод в DATABASE.md неверен")
        finally:
            engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'drop database if exists "{dbname}" with (force)')
        admin.dispose()
