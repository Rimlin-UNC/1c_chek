"""Права и честный результат массовых действий на синтетических компаниях."""
import pytest

from app.database import SessionLocal
from app.models import Company, User
from tests import test_docreq as _dr
from tests import test_primary_upd as _upd

client_firm = _dr.client_firm
firms = _upd.firms

URL = '/api/v1/clients/bulk'


def test_accountant_cannot_change_users_or_archive(client, client_firm):
    a, admin = client_firm['a'], client_firm['admin']
    admin_id = client.get('/api/v1/auth/me', headers=admin).json()['id']
    with SessionLocal() as db:
        before = db.get(User, admin_id).company_id
    for action in ('assign', 'archive', 'restore'):
        r = client.post(URL, headers=a['hdr'], json={
            'company_ids': [a['id']], 'action': action, 'user_id': admin_id})
        assert r.status_code == 403, (action, r.text)
    with SessionLocal() as db:
        assert db.get(User, admin_id).company_id == before
        assert db.get(Company, a['id']).is_active


def test_assign_rejects_multiple_companies_and_admin(client, client_firm):
    admin, a, b = client_firm['admin'], client_firm['a'], client_firm['b']
    emp = client_firm['emp']
    admin_id = client.get('/api/v1/auth/me', headers=admin).json()['id']
    with SessionLocal() as db:
        before = db.get(User, emp).company_id
    r = client.post(URL, headers=admin, json={'company_ids': [a['id'], b['id']], 'action': 'assign', 'user_id': emp})
    assert r.status_code == 422
    r = client.post(URL, headers=admin, json={'company_ids': [a['id']], 'action': 'assign', 'user_id': admin_id})
    assert r.status_code in (403, 409, 422)
    with SessionLocal() as db:
        assert db.get(User, emp).company_id == before
        assert db.get(User, admin_id).company_id is None


def test_cannot_archive_last_active_company(client, client_firm):
    admin, a = client_firm['admin'], client_firm['a']
    with SessionLocal() as db:
        states = {c.id: c.is_active for c in db.query(Company).all()}
        for c in db.query(Company).all():
            c.is_active = c.id == a['id']
        db.commit()
    try:
        r = client.post(URL, headers=admin, json={'company_ids': [a['id']], 'action': 'archive'})
        assert r.status_code == 200
        assert r.json()['done'] == 0 and r.json()['failed'] == 1
        with SessionLocal() as db:
            assert db.get(Company, a['id']).is_active
    finally:
        with SessionLocal() as db:
            for cid, state in states.items():
                db.get(Company, cid).is_active = state
            db.commit()


def test_bulk_safety_on_both_databases(store, monkeypatch):
    from uuid import uuid4

    from fastapi import HTTPException

    from app.clients import router

    monkeypatch.setattr(router, 'log_action', lambda *args, **kwargs: None)
    tag = uuid4().hex
    with store.session() as db:
        a, b = Company(name=f'Проверка A {tag}'), Company(name=f'Проверка B {tag}')
        db.add_all([a, b])
        db.flush()
        admin = User(username=f'admin_{tag}', password_hash='synthetic', role='admin')
        accountant = User(username=f'acc_{tag}', password_hash='synthetic', role='accountant', company_id=a.id)
        employee = User(username=f'emp_{tag}', password_hash='synthetic', role='user', company_id=a.id)
        db.add_all([admin, accountant, employee])
        db.commit()
        try:
            request = router.BulkIn(company_ids=[a.id], action='assign', user_id=admin.id)
            with pytest.raises(HTTPException) as denied:
                router.bulk(request, accountant, db)
            assert denied.value.status_code == 403
            with pytest.raises(HTTPException) as denied:
                router.bulk(request, admin, db)
            assert denied.value.status_code == 409
            with pytest.raises(HTTPException) as denied:
                router.bulk(router.BulkIn(company_ids=[a.id, b.id], action='assign', user_id=employee.id), admin, db)
            assert denied.value.status_code == 422
            result = router.bulk(router.BulkIn(company_ids=[b.id], action='assign', user_id=employee.id), admin, db)
            assert result['done'] == 1 and db.get(User, employee.id).company_id == b.id
        finally:
            db.delete(employee)
            db.delete(accountant)
            db.delete(admin)
            db.flush()
            db.delete(a)
            db.delete(b)
            db.commit()


@pytest.mark.parametrize('routes', [('single', 'single'), ('bulk', 'bulk'), ('single', 'bulk')])
def test_concurrent_archive_keeps_one_company(store, monkeypatch, routes):
    """Одиночный и массовый маршруты должны соблюдать общий инвариант при гонке."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep
    from uuid import uuid4

    from sqlalchemy import event

    from app.clients import router as bulk_router
    from app.routers import companies as company_router
    from app.schemas import CompanyPatch

    monkeypatch.setattr(bulk_router, 'log_action', lambda *args, **kwargs: None)
    monkeypatch.setattr(company_router, 'log_action', lambda *args, **kwargs: None)
    tag = uuid4().hex
    with store.session() as db:
        states = {c.id: c.is_active for c in db.query(Company).all()}
        for c in db.query(Company).all():
            c.is_active = False
        a, b = Company(name=f'Гонка A {tag}'), Company(name=f'Гонка B {tag}')
        admin = User(username=f'race_{tag}', password_hash='synthetic', role='admin')
        db.add_all([a, b, admin])
        db.commit()
        ids = (a.id, b.id, admin.id)
        engine = db.get_bind()
    start = Barrier(2, timeout=15)

    def slow_count(_conn, _cursor, statement, _parameters, _context, _many):
        if 'count(companies.id)' in statement.lower():
            sleep(0.4)  # обе транзакции могут прочитать счётчик, если нет общей блокировки

    event.listen(engine, 'after_cursor_execute', slow_count)
    try:
        def run(cid, route):
            from fastapi import HTTPException

            with store.session() as db:
                admin = db.get(User, ids[2])
                start.wait()
                try:
                    if route == 'single':
                        company_router.patch_company(cid, CompanyPatch(is_active=False), admin, db)
                        return True
                    result = bulk_router.bulk(bulk_router.BulkIn(company_ids=[cid], action='archive'), admin, db)
                    if result['failed']:
                        assert 'последнюю активную' in result['items'][0]['error']
                    return result['done'] == 1
                except HTTPException as exc:
                    db.rollback()
                    assert exc.status_code == 409 and 'последнюю активную' in exc.detail
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, cid, route) for cid, route in zip(ids[:2], routes)]
            results = [future.result(timeout=30) for future in futures]
            assert sorted(results) == [False, True]
        with store.session() as db:
            assert sum(bool(db.get(Company, cid).is_active) for cid in ids[:2]) == 1
    finally:
        event.remove(engine, 'after_cursor_execute', slow_count)
        with store.session() as db:
            for cid, state in states.items():
                db.get(Company, cid).is_active = state
            db.delete(db.get(User, ids[2]))
            db.flush()
            db.delete(db.get(Company, ids[0]))
            db.delete(db.get(Company, ids[1]))
            db.commit()
