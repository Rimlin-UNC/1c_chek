"""T-90: защищённый печатный отчёт. Только синтетические данные."""
from __future__ import annotations

from uuid import uuid4

from tests import test_primary_upd as _upd
from tests.test_v1140 import _mk_user

firms = _upd.firms


def report(client, headers, period='2026-10'):
    return client.get('/api/v1/clients/report', headers=headers, params={'period': period})


def test_access_headers_and_company_isolation(client, firms):
    admin, a, b = firms
    result = report(client, a['hdr'])
    assert result.status_code == 200, result.text
    assert result.headers['content-type'].startswith('text/html')
    assert 'no-store' in result.headers['cache-control']
    assert 'default-src' in result.headers['content-security-policy']
    assert a['inn'] in result.text and b['inn'] not in result.text
    assert 'Октябрь 2026' in result.text and 'на момент формирования' in result.text
    both = report(client, admin)
    assert both.status_code == 200 and a['inn'] in both.text and b['inn'] in both.text
    acc_id = client.get('/api/v1/auth/me', headers=a['hdr']).json()['id']
    assert client.put(f'/api/v1/clients/access/{acc_id}', headers=admin, json={'company_ids': [b['id']]}).status_code == 200
    assert b['inn'] in report(client, a['hdr']).text
    assert client.put(f'/api/v1/clients/access/{acc_id}', headers=admin, json={'company_ids': []}).status_code == 200
    assert b['inn'] not in report(client, a['hdr']).text
    for role in ('user', 'manager'):
        hdr = _mk_user(client, admin, a['id'], f'report_{uuid4().hex[:10]}', role=role)
        assert report(client, hdr).status_code == 403
    client.cookies.clear()
    assert client.get('/api/v1/clients/report').status_code == 401
    for bad_period in ('2026-13', '0000-01', '9999-12'):
        assert report(client, admin, bad_period).status_code == 422


def test_report_html_escapes_and_omits_contract_details(client, firms):
    admin, a, _ = firms
    bad_name = f'<script>alert("{uuid4().hex[:8]}")</script>'
    created = client.post('/api/v1/companies', headers=admin, json={'name': bad_name})
    assert created.status_code == 201
    cid = created.json()['id']
    contract = {'number': 'secret-report-contract', 'date': '2026-01-01', 'service_since': '2026-01-01',
                'service_until': None, 'fee': '12345.67', 'frequency': 'month', 'currency': 'RUB', 'manager': ''}
    assert client.put(f'/api/v1/clients/{cid}/contract', headers=admin, json=contract).status_code == 200
    html = report(client, admin).text
    assert bad_name not in html and '&lt;script&gt;' in html
    assert 'secret-report-contract' not in html and '12345.67' not in html
    assert client.patch(f'/api/v1/companies/{cid}', headers=admin, json={'is_active': False}).status_code == 200
    assert '&lt;script&gt;' not in report(client, admin).text


def test_report_on_sqlite_and_postgresql_matches_scoped_overview(store):
    from datetime import datetime

    from app.clients import report as reports
    from app.clients.service import overview
    from app.models import Company, Receipt, User

    tag = uuid4().hex
    with store.session() as db:
        own = Company(name=f'Отчёт своя {tag}')
        other = Company(name=f'Отчёт чужая {tag}')
        db.add_all([own, other])
        db.flush()
        acc = User(username=f'report_{tag}', password_hash='synthetic', role='accountant', company_id=own.id)
        db.add(acc)
        db.flush()
        db.add(Receipt(company_id=own.id, created_by=acc.id, qr_data=f'qr-{tag}', fn=f'fn{tag[:16]}', fd='1', fp='1',
                       receipt_date=datetime(2026, 10, 3), total_sum=123.45))
        db.flush()
        data = overview(db, acc, period='2026-10')
        assert len(data['companies']) == 1
        row = data['companies'][0]
        assert row['receipts']['count'] == 1 and row['receipts']['sum'] == 123.45
        html = reports.render(data)
        assert own.name in html and other.name not in html
        assert '<td>1</td><td>123,45</td>' in html
        assert '<tfoot>' in html and 'Итого по доступным компаниям' in html
        db.rollback()
