"""Обновление не трогает старые данные на одноразовой базе PostgreSQL."""
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


@pytest.mark.skipif(not os.getenv('UNIFIED_POSTGRES_TEST_URL'), reason='нужен тестовый PostgreSQL')
def test_upgrade_keeps_legacy_tables_on_postgres():
    url = make_url(os.environ['UNIFIED_POSTGRES_TEST_URL'])
    assert url.get_backend_name() == 'postgresql'
    name = 'qa_retired_monitor_' + uuid.uuid4().hex[:12]
    admin = create_engine(url, isolation_level='AUTOCOMMIT')
    engine = None
    try:
        with admin.connect() as con:
            con.exec_driver_sql(f'CREATE DATABASE "{name}"')
        target = url.set(database=name)
        engine = create_engine(target)
        names = ('openstack_settings', 'openstack_metrics', 'openstack_sync_log')
        with engine.begin() as con:
            for table in names:
                con.exec_driver_sql(f'CREATE TABLE {table} (id text PRIMARY KEY, payload text)')
                con.exec_driver_sql(f"INSERT INTO {table} VALUES ('synthetic-id', 'сохранить')")
        before = {table: inspect(engine).get_columns(table) for table in names}
        env = dict(os.environ, DATABASE_URL=target.render_as_string(hide_password=False), YM_FETCHQ_OFF='1')
        run = subprocess.run([sys.executable, '-c', 'from app.database import init_db; init_db()'],
                             env=env, capture_output=True, text=True, timeout=120, check=False)
        assert run.returncode == 0, run.stderr
        with engine.connect() as con:
            for table in names:
                assert con.exec_driver_sql(f'SELECT id, payload FROM {table}').all() == [('synthetic-id', 'сохранить')]
                assert [col['name'] for col in inspect(engine).get_columns(table)] == [col['name'] for col in before[table]]
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as con:
            con.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()
