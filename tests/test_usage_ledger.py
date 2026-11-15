"""Технический журнал событий: синтетика, без платных списаний и вызовов источников."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.usage import ledger


def test_retry_deduplicates_and_reports_remaining(store):
    company = str(uuid4())
    with store.session() as db:
        assert ledger.record(db, company, 'checks-example', 'package-1', 'event-1') is True
        assert ledger.record(db, company, 'checks-example', 'package-1', 'event-1') is False
        assert ledger.record(db, company, 'checks-example', 'package-1', 'event-2') is True
        db.commit()
    with store.session() as db:
        result = ledger.summary(db, company, 'checks-example', 'package-1', limit=5)
        assert (result['used'], result['limit'], result['remaining'], result['over_limit']) == (2, 5, 3, 0)
        assert result['complete'] is False
        assert ledger.summary(db, company, 'checks-example', 'package-1')['remaining'] is None
        assert ledger.summary(db, company, 'checks-example', 'package-1', limit=1)['over_limit'] == 1


def test_company_metric_and_period_do_not_mix(store):
    a, b = str(uuid4()), str(uuid4())
    with store.session() as db:
        assert ledger.record(db, a, 'example', 'period-a', 'same-key')
        assert ledger.record(db, b, 'example', 'period-a', 'same-key')
        assert ledger.record(db, a, 'different', 'period-a', 'same-key')
        assert ledger.record(db, a, 'example', 'period-b', 'different-key')
        db.commit()
        assert ledger.summary(db, a, 'example', 'period-a', limit=7)['used'] == 1
        assert ledger.summary(db, b, 'example', 'period-b', limit=7)['used'] == 0
        assert ledger.summary(db, a, 'different', 'period-a', limit=7)['used'] == 1


def test_same_business_event_cannot_move_to_another_period_by_retry(store):
    company = str(uuid4())
    with store.session() as db:
        assert ledger.record(db, company, 'example', 'period-a', 'same-key')
        db.commit()
        with pytest.raises(ledger.EventConflict):
            ledger.record(db, company, 'example', 'period-b', 'same-key')
        db.rollback()
        assert ledger.summary(db, company, 'example', 'period-a')['used'] == 1
        assert ledger.summary(db, company, 'example', 'period-b')['used'] == 0


def test_rollback_is_atomic_and_no_plain_business_key_is_saved(store):
    from sqlalchemy import select

    from app.usage.models import UsageEvent

    company = str(uuid4())
    secret_key = 'test-private-fiscal-key-' + uuid4().hex
    with store.session() as db:
        assert ledger.record(db, company, 'example', 'period-a', secret_key)
        db.rollback()
    with store.session() as db:
        assert ledger.summary(db, company, 'example', 'period-a')['used'] == 0
        assert ledger.record(db, company, 'example', 'period-a', secret_key)
        db.commit()
        row = db.scalar(select(UsageEvent).where(UsageEvent.company_id == company))
        assert secret_key not in str(row.__dict__)
        assert len(row.event_hash) == 64


def test_parallel_retries_are_idempotent(store):
    company = str(uuid4())
    def worker(_):
        with store.session() as db:
            inserted = ledger.record(db, company, 'example', 'period-a', 'same-event')
            db.commit()
            return inserted
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert results.count(True) == 1 and results.count(False) == 7
    with store.session() as db:
        assert ledger.summary(db, company, 'example', 'period-a')['used'] == 1


@pytest.mark.parametrize('values', [
    ('', 'example', 'period', 'event'), ('a' * 37, 'example', 'period', 'event'),
    ('company', '', 'period', 'event'), ('company', 'example', '', 'event'),
    ('company', 'example', 'period', ''), ('company', 'example', 'period', 'x' * 257),
    ('company', 'bad\nmetric', 'period', 'event'),
])
def test_invalid_identifiers_are_rejected_before_writing(store, values):
    with store.session() as db:
        with pytest.raises(ValueError):
            ledger.record(db, *values)
        assert not db.new


@pytest.mark.parametrize('limit', [-1, True, 1.5, '3'])
def test_bad_limit_is_rejected(store, limit):
    with store.session() as db:
        with pytest.raises(ValueError):
            ledger.summary(db, str(uuid4()), 'example', 'period', limit=limit)


def test_source_and_event_rollback_together(store):
    from app.models import User

    uid = str(uuid4())
    with store.session() as db:
        db.add(User(id=uid, username='usage_' + uuid4().hex, password_hash='not-a-login'))
        db.flush()
        assert ledger.record(db, uid, 'example', 'period', 'atomic-event')
        db.rollback()
    with store.session() as db:
        assert db.get(User, uid) is None
        assert ledger.summary(db, uid, 'example', 'period')['used'] == 0


def test_competing_periods_cannot_double_count(store):
    company = str(uuid4())
    def worker(period):
        with store.session() as db:
            try:
                result = ledger.record(db, company, 'example', period, 'same-event')
                db.commit()
                return result
            except ledger.EventConflict:
                db.rollback()
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ('period-a', 'period-b')))
    assert results.count(True) == 1 and results.count('conflict') == 1
    with store.session() as db:
        assert sum(ledger.summary(db, company, 'example', period)['used']
                   for period in ('period-a', 'period-b')) == 1


def test_table_is_additive_and_has_no_foreign_keys():
    from app.usage.models import UsageEvent
    assert not UsageEvent.__table__.foreign_keys
    assert {column.name for column in UsageEvent.__table__.columns} == {
        'id', 'company_id', 'metric', 'period_key', 'event_hash', 'created_at'}
