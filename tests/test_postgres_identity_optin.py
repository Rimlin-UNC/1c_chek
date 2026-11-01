"""Optional live PostgreSQL test in a new, disposable private schema.

Requires UNIFIED_POSTGRES_TEST_URL and a PostgreSQL DBAPI driver. Never use the
production database: the test creates a uniquely named schema and table and
cleans up only those objects. This is not a test of the full application.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.services.receipt_identity import lock_identity


@pytest.mark.parametrize('dialect', [postgresql.dialect(), sqlite.dialect()])
def test_identity_lock_compiles_as_upsert_with_hashed_identity(dialect):
    class Capture:
        statement = None
        def get_bind(self):
            return type('Bind', (), {'dialect': dialect})()
        def execute(self, statement):
            self.statement = statement

    db = Capture()
    lock_identity(db, 'secret-fn', 'secret-fd', 'secret-fp')
    compiled = db.statement.compile(dialect=dialect)
    assert 'ON CONFLICT' in str(compiled)
    assert 'DO UPDATE' in str(compiled)
    assert 'secret-fn' not in str(compiled.params)
    assert len(next(iter(compiled.params.values()))) == 64


@pytest.mark.skipif(not os.getenv('UNIFIED_POSTGRES_TEST_URL'), reason='dedicated PostgreSQL test URL not configured')
@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_postgres_identity_lock_serializes_transactions(finish):
    url = os.environ['UNIFIED_POSTGRES_TEST_URL']
    engine = create_engine(url, pool_size=3, max_overflow=0)
    if engine.dialect.name != 'postgresql':
        engine.dispose()
        pytest.fail('UNIFIED_POSTGRES_TEST_URL must point to PostgreSQL')
    schema = 'qa_identity_' + uuid4().hex
    created = False
    started, acquired = Event(), Event()
    worker_pid = []
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'CREATE TABLE "{schema}".receipt_identity_locks (key varchar(64) PRIMARY KEY)')
        created = True
        def other_transaction():
            with Session(engine) as db:
                db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                worker_pid.append(db.execute(text('SELECT pg_backend_pid()')).scalar_one())
                started.set()
                lock_identity(db, 'fn-test', 'fd-test', 'fp-test')
                acquired.set()
                db.commit()
        with Session(engine) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            lock_identity(db, 'fn-test', 'fd-test', 'fp-test')
            holder_pid = db.execute(text('SELECT pg_backend_pid()')).scalar_one()
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(other_transaction)
                assert started.wait(5)
                deadline = time.monotonic() + 3
                with engine.connect() as observer:
                    while time.monotonic() < deadline:
                        if holder_pid in observer.execute(text('SELECT pg_blocking_pids(:pid)'),
                                                          {'pid': worker_pid[0]}).scalar_one():
                            break
                        if future.done():
                            future.result()
                            pytest.fail('second transaction acquired the lock too early')
                        time.sleep(0.05)
                    else:
                        pytest.fail('PostgreSQL did not report the expected blocking transaction')
                assert not acquired.is_set()
                getattr(db, finish)()
                future.result(timeout=10)
                assert acquired.is_set()
        with engine.connect() as conn:
            assert conn.execute(text(f'SELECT count(*) FROM "{schema}".receipt_identity_locks')).scalar_one() == 1
    finally:
        if created:
            with engine.begin() as conn:
                conn.exec_driver_sql(f'DROP TABLE "{schema}".receipt_identity_locks')
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}"')
        engine.dispose()
