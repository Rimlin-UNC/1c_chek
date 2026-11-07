"""Агрегированная диагностика не раскрывает записи и не изменяет источник."""
import json
import sqlite3

import pytest
from sqlalchemy import create_engine

from qa.migration_assessment import assess_connection, assess_postgres, assess_sqlite
from tests.test_receipt_copy_migration import _source


def _participants(db):
    db.execute('ALTER TABLE pool_receipts ADD COLUMN pool_user_id TEXT')
    db.execute('CREATE TABLE pool_users (id TEXT PRIMARY KEY, points INTEGER)')
    db.execute('CREATE TABLE pool_points (id TEXT PRIMARY KEY, user_id TEXT, delta INTEGER)')
    db.execute('CREATE TABLE pool_consents (id TEXT PRIMARY KEY, user_id TEXT)')


def test_assessment_counts_conflicts_without_exposing_data(tmp_path):
    path = tmp_path / 'source.db'
    _source(path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE companies (id TEXT PRIMARY KEY)')
        _participants(db)
        db.executemany('INSERT INTO companies VALUES (?)', [('company-a',), ('company-b',)])
        db.execute("UPDATE receipts SET company_id='company-a' WHERE id='r1'")
        db.execute("UPDATE pool_receipts SET assigned_company_id='company-b', total_sum=100.001 WHERE id='p1'")
        db.execute("UPDATE pool_receipts SET assigned_company_id='missing' WHERE id='p2'")
        db.execute("INSERT INTO receipt_items VALUES ('orphan-item', 'missing-receipt', 'hidden')")
        db.execute("INSERT INTO pool_users VALUES ('donor', 5)")
        db.execute("INSERT INTO pool_points VALUES ('event', 'donor', 3)")
        db.execute("INSERT INTO pool_consents VALUES ('consent', 'missing-donor')")
        db.execute("UPDATE pool_receipts SET pool_user_id='missing-donor' WHERE id='p2'")
        db.execute("INSERT INTO receipts (id,fn,fd,fp) VALUES ('duplicate', '123','4','5')")
    before = path.read_bytes()
    result = assess_sqlite(path)
    assert path.read_bytes() == before
    assert result['source_counts'] == {'registry': 3, 'pool': 2}
    assert result['incomplete_identities'] == {'registry': 1, 'pool': 0}
    assert result['duplicate_identity_groups'] == {'registry': 1, 'pool': 0}
    assert result['orphan_items'] == {'registry': 1, 'pool': 0}
    assert result['ownership'] == {'registry_without_company': 2, 'registry_unknown_company': 0,
                                   'pool_without_company': 0, 'pool_unknown_company': 1}
    assert result['participants'] == {'users': 1, 'point_events': 1, 'consents': 1,
                                      'receipts_unknown_user': 1, 'points_unknown_user': 0,
                                      'consents_unknown_user': 1, 'points_missing_user': 0, 'balance_mismatch_users': 1}
    assert result['overlap']['matched_pairs'] == 2
    assert result['overlap']['different_amount'] == 1
    assert result['overlap']['different_companies'] == 1
    encoded = json.dumps(result)
    for private in ('123', 'company-a', 'company-b', 'private item', 'missing-receipt'):
        assert private not in encoded


def test_assessment_trims_unicode_whitespace_and_does_not_merge_values(tmp_path):
    path = tmp_path / 'source.db'
    _source(path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE companies (id TEXT PRIMARY KEY)')
        _participants(db)
        db.execute("UPDATE receipts SET fn=' 123' WHERE id='r1'")
        db.execute("UPDATE pool_receipts SET fn=? WHERE id='p2'", ('\u2003\t',))
        db.execute("UPDATE receipts SET company_id=? WHERE id='r1'", ('\u2003\t',))
    result = assess_sqlite(path)
    assert result['incomplete_identities']['pool'] == 1
    assert result['ownership']['registry_without_company'] == 2
    assert result['overlap']['matched_pairs'] == 0


def test_assessment_refuses_wrong_or_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        assess_sqlite(tmp_path / 'missing.db')
    path = tmp_path / 'wrong.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE other (id TEXT)')
    with pytest.raises(ValueError, match='необходимая таблица'):
        assess_sqlite(path)
    with pytest.raises(ValueError, match='PostgreSQL'):
        assess_postgres('sqlite:///should-not-be-created.db')


def test_connection_query_only_prevents_write(tmp_path):
    path = tmp_path / 'source.db'
    _source(path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE companies (id TEXT PRIMARY KEY)')
        _participants(db)
    engine = create_engine(f'sqlite:///{path}')
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql('PRAGMA query_only=ON')
            conn.exec_driver_sql('BEGIN')
            assert assess_connection(conn)['source_counts']['pool'] == 2
            with pytest.raises(Exception, match='readonly'):
                conn.exec_driver_sql('DELETE FROM receipts')
    finally:
        engine.dispose()


def test_assessment_reads_consistent_wal_snapshot(tmp_path, monkeypatch):
    from qa import migration_assessment

    path = tmp_path / 'wal.db'
    _source(path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE companies (id TEXT PRIMARY KEY)')
        _participants(db)
        db.execute('PRAGMA journal_mode=WAL')
    original = migration_assessment.assess_connection

    def assess_after_concurrent_write(conn):
        assert conn.exec_driver_sql('SELECT count(*) FROM pool_receipts').scalar_one() == 2
        with sqlite3.connect(path) as writer:
            writer.execute("DELETE FROM pool_receipts WHERE id='p2'")
        return original(conn)

    monkeypatch.setattr(migration_assessment, 'assess_connection', assess_after_concurrent_write)
    assert migration_assessment.assess_sqlite(path)['source_counts']['pool'] == 2
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM pool_receipts').fetchone()[0] == 1
