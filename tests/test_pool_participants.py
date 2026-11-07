"""Обезличенная группировка участников только внутри закреплённых чеков компании."""
import csv
import io
import sqlite3
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.pool.participants import UNKNOWN_PARTICIPANT, participant_key
from tests.conftest import login
from tests.test_v1140 import _mk_user


@pytest.fixture
def participant_scene(client):
    admin = login(client, 'admin', 'admin123')
    companies = [client.post('/api/v1/companies', headers=admin,
                             json={'name': f'Участники {n} ' + uuid.uuid4().hex}).json()['id']
                 for n in (1, 2)]
    accountants = [_mk_user(client, admin, companies[n], f'participant_{n}_' + uuid.uuid4().hex[:10],
                            role='accountant') for n in (0, 1)]
    receipt_ids, donor_ids = [], []
    with SessionLocal() as db:
        donors = [PoolUser(vid=f'participant-{n}-' + uuid.uuid4().hex,
                           email=f'private{n}@example.test', points=9) for n in (1, 2)]
        db.add_all(donors)
        db.flush()
        donor_ids = [d.id for d in donors]
        # Две записи первого участника в компании A, одна второго; в компании B
        # тот же первый участник. Ещё три записи: NULL, пробельная и битая связь.
        specs = [(0, 0, '2026-09-01', 100), (0, 0, '2026-09-02', 200),
                 (0, 1, '2026-09-03', 300), (1, 0, '2026-09-04', 400),
                 (0, None, '2026-09-05', 500), (0, None, '2026-09-06', 600),
                 (0, None, '2026-09-07', 700)]
        for n, (company, donor, day, amount) in enumerate(specs, 1):
            raw_id = donor_ids[donor] if isinstance(donor, int) else donor
            receipt = PoolReceipt(pool_user_id=raw_id, assigned_company_id=companies[company],
                                  assigned_at=datetime.fromisoformat(day),
                                  fn=f'8800{n:012d}', fd=str(n), fp=str(9000 + n),
                                  receipt_date=datetime.fromisoformat(day), total_sum=amount,
                                  operation=1, merchant_name=f'Магазин {n}', industry='food',
                                  status='verified', raw='{"private":"hidden@example.test"}')
            db.add(receipt)
            db.flush()
            receipt_ids.append(receipt.id)
            db.add(PoolItem(receipt_id=receipt.id, name=f'Товар {n}', quantity=1,
                            price=amount, total=amount))
        db.commit()
    # Исторические пробельную и битую связи нельзя вставить при включённых FK;
    # воспроизводим их отдельным соединением, не меняя настройки приложения.
    with sqlite3.connect(engine.url.database) as legacy:
        legacy.execute('UPDATE pool_receipts SET pool_user_id=? WHERE id=?',
                       ('\u2003\t', receipt_ids[-2]))
        legacy.execute('UPDATE pool_receipts SET pool_user_id=? WHERE id=?',
                       ('missing-donor', receipt_ids[-1]))
    yield {'admin': admin, 'companies': companies, 'accountants': accountants,
           'receipts': receipt_ids, 'donors': donor_ids}
    with SessionLocal() as db:
        db.query(PoolItem).filter(PoolItem.receipt_id.in_(receipt_ids)).delete(synchronize_session=False)
        db.query(PoolReceipt).filter(PoolReceipt.id.in_(receipt_ids)).delete(synchronize_session=False)
        db.query(PoolUser).filter(PoolUser.id.in_(donor_ids)).delete(synchronize_session=False)
        db.commit()
    client.cookies.clear()


def test_participant_key_is_company_scoped_and_not_reversible(monkeypatch):
    from app.config import settings
    first = participant_key('company-a', 'donor')
    assert first == participant_key('company-a', 'donor')
    assert first != participant_key('company-b', 'donor')
    assert 'donor' not in first and 'company-a' not in first and len(first) == 32
    for company, donor in ((None, 'donor'), ('company-a', None), (' ', 'donor'), ('company-a', '\u2003\t')):
        assert participant_key(company, donor) == UNKNOWN_PARTICIPANT
    monkeypatch.setattr(settings, 'SECRET_KEY', 'another-test-secret')
    assert participant_key('company-a', 'donor') != first


def test_mine_groups_and_filters_before_pagination_without_pii(client, participant_scene):
    p = participant_scene
    headers, company = p['accountants'][0], p['companies'][0]
    key = participant_key(company, p['donors'][0])
    response = client.get('/api/v1/pool-company/mine', headers=headers,
                          params={'participant': key, 'per_page': 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['total'] == 2 and len(data['items']) == 1
    assert {f['key']: f['count'] for f in data['participants']} == {
        key: 2, participant_key(company, p['donors'][1]): 1, UNKNOWN_PARTICIPANT: 3}
    page2 = client.get('/api/v1/pool-company/mine', headers=headers,
                       params={'participant': key, 'per_page': 1, 'page': 2}).json()
    assert {r['id'] for r in data['items'] + page2['items']} == set(p['receipts'][:2])
    assert all(r['participant'] == key for r in data['items'] + page2['items'])
    encoded = response.text
    for private in p['donors'] + ['private1@example.test', 'hidden@example.test', 'private2@example.test', 'missing-donor']:
        assert private not in encoded


def test_unknown_group_includes_null_blank_and_orphan_without_inventing_donor(client, participant_scene):
    p = participant_scene
    data = client.get('/api/v1/pool-company/mine', headers=p['accountants'][0],
                      params={'participant': UNKNOWN_PARTICIPANT}).json()
    assert data['total'] == 3
    assert {r['id'] for r in data['items']} == set(p['receipts'][4:])
    assert all(r['participant_label'] == 'Без участника' for r in data['items'])


def test_period_facets_and_filter_use_same_scope(client, participant_scene):
    p = participant_scene
    data = client.get('/api/v1/pool-company/mine', headers=p['accountants'][0],
                      params={'date_from': '2026-09-02', 'date_to': '2026-09-03'}).json()
    assert data['total'] == 2
    assert sum(f['count'] for f in data['participants']) == 2
    key = participant_key(p['companies'][0], p['donors'][0])
    filtered = client.get('/api/v1/pool-company/mine', headers=p['accountants'][0],
                          params={'date_from': '2026-09-02', 'date_to': '2026-09-03',
                                  'participant': key}).json()
    assert filtered['total'] == 1 and filtered['items'][0]['id'] == p['receipts'][1]
    assert filtered['participants'] == data['participants']


def test_company_isolation_and_admin_explicit_company(client, participant_scene):
    p = participant_scene
    key_a = participant_key(p['companies'][0], p['donors'][0])
    key_b = participant_key(p['companies'][1], p['donors'][0])
    assert key_a != key_b
    foreign = client.get('/api/v1/pool-company/mine', headers=p['accountants'][1],
                         params={'participant': key_a}).json()
    assert foreign['total'] == 0
    assert key_a not in {f['key'] for f in foreign['participants']}
    assert client.get('/api/v1/pool-company/mine', headers=p['accountants'][1],
                      params={'company_id': p['companies'][0]}).status_code == 403
    admin = client.get('/api/v1/pool-company/mine', headers=p['admin'],
                       params={'company_id': p['companies'][1], 'participant': key_b}).json()
    assert admin['total'] == 1 and admin['items'][0]['id'] == p['receipts'][3]
    assert client.get('/api/v1/pool-company/mine', headers=p['admin']).status_code == 403


def test_csv_respects_participant_and_keeps_columns_aligned(client, participant_scene):
    p = participant_scene
    key = participant_key(p['companies'][0], p['donors'][0])
    response = client.get('/api/v1/pool-company/export.csv', headers=p['accountants'][0],
                          params={'participant': key})
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig')), delimiter=';'))
    assert rows[0][-1] == 'Участник (обезличенный код)'
    assert len(rows) == 3 and all(len(row) == len(rows[0]) for row in rows)
    assert {row[-1] for row in rows[1:]} == {key}
    assert {row[1] for row in rows[1:]} == {'Магазин 1', 'Магазин 2'}
    assert all(row[5] == 'Продукты и напитки' for row in rows[1:])
    for private in p['donors'] + ['private1@example.test', 'hidden@example.test']:
        assert private not in response.text


def test_invalid_filter_is_rejected_and_reading_does_not_mutate(client, participant_scene, monkeypatch):
    p = participant_scene
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    with SessionLocal() as db:
        before = [(r.id, r.pool_user_id, r.assigned_company_id, r.total_sum)
                  for r in db.query(PoolReceipt).order_by(PoolReceipt.id).all()]
        points = [(u.id, u.points) for u in db.query(PoolUser).order_by(PoolUser.id).all()]
    for value in ('../donor', 'A' * 32, 'unknown-extra', 'x' * 33):
        assert client.get('/api/v1/pool-company/mine', headers=p['accountants'][0],
                          params={'participant': value}).status_code == 422
        assert client.get('/api/v1/pool-company/export.csv', headers=p['accountants'][0],
                          params={'participant': value}).status_code == 422
    client.get('/api/v1/pool-company/mine', headers=p['accountants'][0])
    client.get('/api/v1/pool-company/export.csv', headers=p['accountants'][0])
    with SessionLocal() as db:
        assert [(r.id, r.pool_user_id, r.assigned_company_id, r.total_sum)
                for r in db.query(PoolReceipt).order_by(PoolReceipt.id).all()] == before
        assert [(u.id, u.points) for u in db.query(PoolUser).order_by(PoolUser.id).all()] == points
