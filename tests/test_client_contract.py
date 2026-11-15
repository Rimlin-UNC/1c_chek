"""T-88: договор и тариф клиента, изоляция, обязательные поля и сроки (только синтетика)."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

from tests import test_primary_upd as _upd
from tests.test_v1140 import _mk_user

firms = _upd.firms


def valid(**changes):
    data = {"number": "Т-1", "date": "2026-01-01", "service_since": "2026-02-01",
            "fee": "12500.50", "frequency": "month", "currency": "RUB", "service_until": "2026-12-31",
            "manager": ""}
    data.update(changes)
    return data


def url(cid):
    return f"/api/v1/clients/{cid}/contract"


def test_existing_client_without_contract_is_usable(client, firms):
    _, a, _ = firms
    r = client.get(url(a['id']), headers=a['hdr'])
    assert r.status_code == 200 and r.json()['contract'] is None
    assert client.get('/api/v1/clients', headers=a['hdr']).status_code == 200


def test_save_and_read_contract_with_exact_amount(client, firms):
    _, a, _ = firms
    r = client.put(url(a['id']), headers=a['hdr'], json=valid(fee='123456789012.34'))
    assert r.status_code == 200, r.text
    contract = client.get(url(a['id']), headers=a['hdr']).json()['contract']
    assert contract['fee'] == '123456789012.34'
    assert contract['number'] == 'Т-1' and contract['currency'] == 'RUB'


def test_validation_is_atomic(client, firms):
    _, a, _ = firms
    assert client.put(url(a['id']), headers=a['hdr'], json=valid()).status_code == 200
    for changes in ({'number': ''}, {'fee': ''}, {'fee': '-1'}, {'fee': '0.001'},
                    {'frequency': ''}, {'currency': ''}, {'date': ''}, {'service_since': ''},
                    {'service_until': '2026-01-01'}):
        assert client.put(url(a['id']), headers=a['hdr'], json=valid(**changes)).status_code == 422, changes
    assert client.get(url(a['id']), headers=a['hdr']).json()['contract']['fee'] == '12500.50'


def test_access_isolation_and_optional_fields(client, firms):
    admin, a, b = firms
    acc = client.get('/api/v1/auth/me', headers=a['hdr']).json()['id']
    assert client.get(url(b['id']), headers=a['hdr']).status_code == 403
    assert client.put(url(b['id']), headers=a['hdr'], json=valid()).status_code == 403
    assert client.put(f'/api/v1/clients/access/{acc}', headers=admin, json={'company_ids': [b['id']]}).status_code == 200
    assert client.put(url(b['id']), headers=a['hdr'], json=valid(service_until=None, manager='')).status_code == 200
    emp = _mk_user(client, admin, a['id'], f'contract_{uuid4().hex[:10]}', role='user')
    assert client.get(url(b['id']), headers=emp).status_code == 403


def test_reminder_boundaries():
    from app.clients.contract import reminder

    end = date(2026, 12, 31)
    assert reminder(None, date(2026, 12, 1)) is None
    assert reminder(end, date(2026, 11, 30)) is None
    assert reminder(end, date(2026, 12, 1))['days'] == 30
    assert reminder(end, date(2026, 12, 24))['days'] == 7
    assert reminder(end, date(2027, 1, 1))['days'] == -1


def test_contract_persistence_on_both_databases(store):
    """Сумма и необязательные поля одинаковы в SQLite и PostgreSQL."""
    from app.clients.contract import ContractIn, as_dict, company_for, save
    from app.models import Company, User

    tag = uuid4().hex
    with store.session() as db:
        company = Company(name=f'Договор {tag}')
        db.add(company)
        db.flush()
        user = User(username=f'contract_{tag}', password_hash='synthetic', role='accountant', company_id=company.id)
        db.add(user)
        db.flush()
        save(company_for(db, user, company.id), ContractIn(**valid(fee='123456789012.34', service_until=None)))
        db.flush()
        db.expire(company)
        saved = as_dict(company)
        assert saved['fee'] == '123456789012.34' and saved['service_until'] is None
        db.rollback()


def test_migration_is_additive_and_idempotent(tmp_path, monkeypatch):
    """Старая таблица сохраняется, новые поля NULL, повторный запуск безопасен."""
    from sqlalchemy import create_engine, inspect, text

    from app import database

    engine = create_engine(f'sqlite:///{tmp_path}/legacy.db')
    with engine.begin() as con:
        con.execute(text('CREATE TABLE companies (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))'))
        con.execute(text("INSERT INTO companies (id, name) VALUES ('one', 'Старый клиент')"))
    monkeypatch.setattr(database, 'engine', engine)
    database._ensure_schema()
    database._ensure_schema()
    cols = {x['name'] for x in inspect(engine).get_columns('companies')}
    assert {'contract_no', 'contract_date', 'service_since', 'service_until', 'service_fee',
            'service_frequency', 'service_currency', 'service_manager'} <= cols
    with engine.connect() as con:
        row = con.execute(text("SELECT name, contract_no, service_fee FROM companies WHERE id='one'")).one()
        assert tuple(row) == ('Старый клиент', None, None)
    engine.dispose()


def test_contract_in_overview_and_no_disabled_access(client, firms):
    admin, a, b = firms
    assert client.put(url(a['id']), headers=a['hdr'], json=valid()).status_code == 200
    data = client.get('/api/v1/clients', headers=a['hdr']).json()['companies']
    row = next(row for row in data if row['id'] == a['id'])
    assert row['contract']['number'] == 'Т-1'
    assert b['id'] not in [row['id'] for row in data]
    assert client.patch(f"/api/v1/companies/{a['id']}", headers=admin, json={'is_active': False}).status_code == 200
    try:
        assert client.get(url(a['id']), headers=a['hdr']).status_code == 403
        assert client.put(url(a['id']), headers=a['hdr'], json=valid()).status_code == 403
    finally:
        client.patch(f"/api/v1/companies/{a['id']}", headers=admin, json={'is_active': True})


def test_move_company_preserves_contract_and_refuses_conflicts(client, firms):
    admin, a, b = firms
    assert client.put(url(b['id']), headers=admin, json=valid(number='Источник')).status_code == 200
    assert client.put(url(a['id']), headers=admin, json=valid(number='Назначение')).status_code == 200
    move = {'mode': 'move', 'target_company_id': a['id'], 'move_users': False}
    refused = client.post(f"/api/v1/companies/{b['id']}/delete", headers=admin, json=move)
    assert refused.status_code == 409
    assert client.get(url(b['id']), headers=admin).json()['contract']['number'] == 'Источник'
    assert client.get(url(a['id']), headers=admin).json()['contract']['number'] == 'Назначение'


def test_move_company_with_contract_to_empty_target(client, firms):
    admin, a, b = firms
    assert client.put(url(b['id']), headers=admin, json=valid(number='Переносимый')).status_code == 200
    response = client.post(f"/api/v1/companies/{b['id']}/delete", headers=admin,
                           json={'mode': 'move', 'target_company_id': a['id'], 'move_users': False})
    assert response.status_code == 200, response.text
    assert client.get(url(a['id']), headers=admin).json()['contract']['number'] == 'Переносимый'


def test_contract_deadlines_raise_attention_without_claiming_termination(client, firms):
    from datetime import datetime, timezone

    from app.clients.service import overview
    from app.database import SessionLocal
    from app.models import User

    _, a, _ = firms
    user_id = client.get('/api/v1/auth/me', headers=a['hdr']).json()['id']
    assert client.put(url(a['id']), headers=a['hdr'], json=valid(service_until='2026-12-31')).status_code == 200
    with SessionLocal() as db:
        user = db.get(User, user_id)
        def row(day):
            return next(r for r in overview(db, user, now=day)['companies'] if r['id'] == a['id'])
        assert row(datetime(2026, 11, 30, 12, tzinfo=timezone.utc))['contract']['reminder'] is None
        soon = row(datetime(2026, 12, 1, 12, tzinfo=timezone.utc))
        urgent = row(datetime(2026, 12, 24, 12, tzinfo=timezone.utc))
        assert soon['level'] in ('warn', 'bad') and soon['score'] > 0
        assert urgent['level'] == 'bad' and urgent['score'] > soon['score']


def test_legacy_postgresql_migration_is_additive_and_idempotent(monkeypatch):
    """Настоящая старая таблица PostgreSQL обновляется без изменения её строк."""
    import os

    import pytest
    from sqlalchemy import create_engine, inspect, text

    from app import database

    url = os.getenv('UNIFIED_POSTGRES_TEST_URL')
    if not url:
        pytest.skip('PostgreSQL-вариант: нужен UNIFIED_POSTGRES_TEST_URL')
    schema = 'qa_contract_' + uuid4().hex
    root = create_engine(url)
    with root.begin() as con:
        con.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        with engine.begin() as con:
            con.execute(text('CREATE TABLE companies (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))'))
            con.execute(text("INSERT INTO companies (id, name) VALUES ('one', 'Старый клиент')"))
            con.execute(text('CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)'))
            con.execute(text('CREATE TABLE receipts (id VARCHAR(36) PRIMARY KEY)'))
            con.execute(text('CREATE TABLE pool_users (id VARCHAR(36) PRIMARY KEY)'))
        monkeypatch.setattr(database, 'engine', engine)
        database._ensure_schema()
        database._ensure_schema()
        cols = {col['name'] for col in inspect(engine).get_columns('companies')}
        assert {'contract_no', 'contract_date', 'service_since', 'service_until', 'service_fee',
                'service_frequency', 'service_currency', 'service_manager'} <= cols
        with engine.connect() as con:
            assert tuple(con.execute(text("SELECT name, contract_no, service_fee FROM companies WHERE id='one'"))
                         .one()) == ('Старый клиент', None, None)
    finally:
        engine.dispose()
        with root.begin() as con:
            con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()
