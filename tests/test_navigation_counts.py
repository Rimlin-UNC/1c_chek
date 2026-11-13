"""Счётчики меню считают только доступные данные и не изменяют их."""
from datetime import date, datetime
from uuid import uuid4

from tests.conftest import login
from tests.test_v1140 import _mk_user


def test_counts_access_and_switch(client):
    admin = login(client, 'admin', 'admin123')
    a = client.post('/api/v1/companies', headers=admin, json={'name': 'Счётчики А ' + uuid4().hex}).json()['id']
    b = client.post('/api/v1/companies', headers=admin, json={'name': 'Счётчики Б ' + uuid4().hex}).json()['id']
    acc = _mk_user(client, admin, a, 'navacc_' + uuid4().hex[:10], role='accountant')
    employee = _mk_user(client, admin, a, 'navuser_' + uuid4().hex[:10], role='user')
    uid = client.get('/api/v1/auth/me', headers=acc).json()['id']
    from app.database import SessionLocal
    from app.primary.models import PrimaryDocument
    with SessionLocal() as db:
        db.add_all([PrimaryDocument(company_id=a), PrimaryDocument(company_id=b), PrimaryDocument(company_id=b)])
        db.commit()
    assert client.put(f'/api/v1/clients/access/{uid}', headers=admin, json={'company_ids': [b]}).status_code == 200
    own = client.get('/api/v1/navigation/counts', headers=acc)
    assert own.status_code == 200
    assert own.headers['cache-control'] == 'no-store, private'
    assert own.json()['clients'] == 2 and own.json()['primary'] == 1
    assert 'companies' not in own.json() and 'users' not in own.json()
    switched = client.get('/api/v1/navigation/counts', headers={**acc, 'X-Ym-Company': b})
    assert switched.status_code == 200 and switched.json()['primary'] == 2
    assert client.get('/api/v1/navigation/counts', headers=acc, params={'company_id': b}).json()['primary'] == 1
    assert client.put(f'/api/v1/clients/access/{uid}', headers=admin, json={'company_ids': []}).status_code == 200
    assert client.get('/api/v1/navigation/counts', headers={**acc, 'X-Ym-Company': b}).status_code == 403
    limited = client.get('/api/v1/navigation/counts', headers=employee).json()
    assert 'receipts' in limited and 'primary' not in limited and 'companies' not in limited
    client.cookies.clear()
    assert client.get('/api/v1/navigation/counts').status_code == 401


def test_counts_company_isolation_and_read_only_on_both_databases(store):
    from app.bank.models import BankStatement
    from app.counterparties.models import Counterparty
    from app.docreq.models import DocRequest
    from app.models import Company, Receipt, User
    from app.primary.models import PrimaryDocument
    from app.services.navigation_counts import counts

    tag = uuid4().hex
    with store.session() as db:
        a, b = Company(name='Счётчики А ' + tag), Company(name='Счётчики Б ' + tag)
        db.add_all([a, b]); db.flush()
        acc = User(username='nav_' + tag, password_hash='synthetic', role='accountant', company_id=a.id)
        outsider = User(username='nav_null_' + tag, password_hash='synthetic', role='accountant', company_id=None)
        employee = User(username='nav_employee_' + tag, password_hash='synthetic', role='user', company_id=a.id)
        manager = User(username='nav_manager_' + tag, password_hash='synthetic', role='manager', company_id=a.id)
        admin = User(username='nav_admin_' + tag, password_hash='synthetic', role='admin')
        db.add_all([acc, outsider, employee, manager, admin]); db.flush()
        # В общей тестовой базе могут уже быть платформенные записи без компании.
        baseline = counts(db, outsider, b.id)
        for company, num in ((a, 1), (b, 2)):
            for n in range(num):
                db.add(PrimaryDocument(company_id=company.id, doc_number=f'{tag}-{n}'))
                db.add(Counterparty(company_id=company.id, inn=f'{num}{n}{tag[:8]}', name_full='Синтетика'))
                db.add(BankStatement(company_id=company.id))
                db.add(DocRequest(company_id=company.id, period='2026-10', status='open'))
                db.add(Receipt(company_id=company.id, created_by=acc.id, qr_data=f'{tag}-{num}-{n}',
                               fn=f'{num}{n}{tag[:14]}', fd='1', fp='1', receipt_date=datetime(2026, 10, 1),
                               total_sum=10))
        db.flush()
        result = counts(db, acc, None)
        assert result['receipts'] == 1 and result['primary'] == 1
        assert counts(db, employee, b.id)['receipts'] == 0  # сотрудник не видит чужой чек своей компании
        assert counts(db, manager, b.id)['receipts'] == 1  # руководитель видит компанию, не произвольную
        assert counts(db, outsider, b.id)['receipts'] == baseline['receipts']
        assert result['counterparties'] == 1 and result['bank'] == 1 and result['docreq'] == 1
        assert counts(db, admin, b.id)['primary'] == 2
        assert counts(db, admin, b.id)['counterparties'] == 2
        assert counts(db, outsider, b.id)['counterparties'] == baseline['counterparties']
        assert counts(db, outsider, b.id)['primary'] == baseline['primary']
        db.add(PrimaryDocument(company_id=a.id, doc_date=date(2026, 10, 1))); db.flush()
        assert counts(db, acc, None)['primary'] == 2
        assert db.query(DocRequest).filter(DocRequest.company_id == a.id).count() == 1
        db.rollback()
