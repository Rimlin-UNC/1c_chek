"""Отложенный скан не должен сменить пользователя или компанию на сервере."""
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import Company, Receipt, User
from tests.conftest import login
from tests.test_v1140 import _mk_user


@pytest.fixture
def ctx(client):
    admin = login(client, 'admin', 'admin123')
    company = client.post('/api/v1/companies', headers=admin,
                          json={'name': 'Офлайн А ' + uuid4().hex}).json()['id']
    other = client.post('/api/v1/companies', headers=admin,
                        json={'name': 'Офлайн Б ' + uuid4().hex}).json()['id']
    employee = _mk_user(client, admin, company, 'offline_' + uuid4().hex[:12], role='user')
    user_id = client.get('/api/v1/auth/me', headers=employee).json()['id']
    return admin, company, other, employee, user_id


def scan_body(user_id, company_id):
    fn = '8' + str(int(uuid4().hex, 16) % 10**15).zfill(15)
    return {'qr_data': f't=20260922T1556&s=100.00&fn={fn}&i=3467&fp=2882574203&n=1',
            'source': 'web', 'company_id': company_id,
            'expected_user_id': user_id, 'expected_company_id': company_id}


def test_context_mandatory_and_no_creation_for_wrong_user_or_company(client, ctx):
    _, company, other, employee, user_id = ctx
    body = scan_body(user_id, company)
    missing = dict(body)
    missing.pop('expected_company_id')
    assert client.post('/api/v1/receipts/scan/offline', headers=employee, json=missing).status_code == 422
    wrong_user = {**body, 'expected_user_id': str(uuid4())}
    assert client.post('/api/v1/receipts/scan/offline', headers=employee, json=wrong_user).status_code == 409
    wrong_company = {**body, 'expected_company_id': other}
    assert client.post('/api/v1/receipts/scan/offline', headers=employee, json=wrong_company).status_code == 409
    with SessionLocal() as db:
        assert not db.query(Receipt).filter_by(qr_data=body['qr_data']).first()


def test_transfer_blocks_old_context_and_retry_deduplicates(client, ctx):
    _, company, other, employee, user_id = ctx
    body = scan_body(user_id, company)
    first = client.post('/api/v1/receipts/scan/offline', headers=employee, json=body)
    assert first.status_code == 200, first.text
    again = client.post('/api/v1/receipts/scan/offline', headers=employee, json=body)
    assert again.status_code == 200 and again.json()['duplicate'] is True
    assert again.json()['receipt']['id'] == first.json()['receipt']['id']
    with SessionLocal() as db:
        db.get(User, user_id).company_id = other
        db.commit()
    later = scan_body(user_id, company)
    response = client.post('/api/v1/receipts/scan/offline', headers=employee, json=later)
    assert response.status_code == 409
    with SessionLocal() as db:
        assert not db.query(Receipt).filter_by(qr_data=later['qr_data']).first()


def test_admin_missing_target_cannot_fall_back_to_platform(client, ctx):
    admin, company, _, _, _ = ctx
    admin_id = client.get('/api/v1/auth/me', headers=admin).json()['id']
    body = scan_body(admin_id, str(uuid4()))
    response = client.post('/api/v1/receipts/scan/offline', headers=admin, json=body)
    assert response.status_code == 409
    with SessionLocal() as db:
        assert not db.query(Receipt).filter_by(qr_data=body['qr_data']).first()
        assert db.get(Company, company)


def test_revoked_client_access_does_not_fall_back_to_home_company(client, ctx):
    admin, company, other, _, _ = ctx
    accountant = _mk_user(client, admin, company, 'offacc_' + uuid4().hex[:12], role='accountant')
    uid = client.get('/api/v1/auth/me', headers=accountant).json()['id']
    access_url = f'/api/v1/clients/access/{uid}'
    assert client.put(access_url, headers=admin, json={'company_ids': [other]}).status_code == 200
    body = scan_body(uid, other)
    switched = {**accountant, 'X-Ym-Company': other}
    assert client.put(access_url, headers=admin, json={'company_ids': []}).status_code == 200
    assert client.post('/api/v1/receipts/scan/offline', headers=switched, json=body).status_code == 403
    # Даже клиент, ошибочно повторивший запрос без заголовка, не переназначит чек.
    assert client.post('/api/v1/receipts/scan/offline', headers=accountant, json=body).status_code == 409
    with SessionLocal() as db:
        assert not db.query(Receipt).filter_by(qr_data=body['qr_data']).first()


def test_context_guard_in_both_databases(store, monkeypatch):
    from fastapi import BackgroundTasks, HTTPException

    from app.routers import receipts
    from app.schemas import OfflineScanRequest

    captured = []
    monkeypatch.setattr(receipts, '_scan_in_company', lambda body, background, user, db, cid: captured.append(cid))
    tag = uuid4().hex
    with store.session() as db:
        a, b = Company(name='Контекст А ' + tag), Company(name='Контекст Б ' + tag)
        db.add_all([a, b])
        db.flush()
        user = User(username='offline_' + tag, role='user', company_id=a.id, password_hash='synthetic')
        db.add(user)
        db.flush()
        body = OfflineScanRequest(**scan_body(user.id, a.id))
        receipts.scan_offline(body, BackgroundTasks(), user, db)
        assert captured == [a.id]
        user.company_id = b.id
        with pytest.raises(HTTPException) as err:
            receipts.scan_offline(body, BackgroundTasks(), user, db)
        assert err.value.status_code == 409
        assert captured == [a.id]
        db.rollback()
