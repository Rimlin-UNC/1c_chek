"""Нумерация авансовых отчётов: атомарность, изоляция и повторы."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from tests.conftest import login


def company(client, hdr):
    result = client.post('/api/v1/companies', headers=hdr,
                         json={'name': f'ООО Отчёт {uuid4()}'})
    assert result.status_code == 201, result.text
    return result.json()['id']


def reserve(client, hdr, cid, date='2026-10-31', key=None, start=0):
    return client.post('/api/v1/receipts/advance-report/number', headers=hdr,
                       json={'company_id': cid, 'report_date': date,
                             'request_id': key or str(uuid4()), 'start_after': start})


def test_sequence_seed_month_idempotency(client):
    hdr = login(client, 'admin', 'admin123')
    cid = company(client, hdr)
    key = str(uuid4())
    a = reserve(client, hdr, cid, key=key, start=45)
    assert a.status_code == 200, a.text
    assert a.json()['number'] == 'АО-2026-10-46'
    assert reserve(client, hdr, cid, key=key).json() == a.json()
    assert reserve(client, hdr, cid).json()['number'] == 'АО-2026-10-47'
    assert reserve(client, hdr, cid, date='2026-11-01').json()['number'] == 'АО-2026-11-01'
    assert reserve(client, hdr, company(client, hdr)).json()['number'] == 'АО-2026-10-01'
    assert reserve(client, hdr, cid, date='2026-11-01', key=key).status_code == 409
    assert reserve(client, hdr, cid, date='2026-02-30').status_code == 422


def test_concurrent_requests_are_unique(client):
    hdr = login(client, 'admin', 'admin123')
    cid = company(client, hdr)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: reserve(client, hdr, cid), range(24)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert sorted(r.json()['sequence'] for r in responses) == list(range(1, 25))


def test_concurrent_retries_reserve_once(client):
    hdr = login(client, 'admin', 'admin123')
    cid, key = company(client, hdr), str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: reserve(client, hdr, cid, key=key), range(8)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert {r.json()['number'] for r in responses} == {'АО-2026-10-01'}
    assert reserve(client, hdr, cid).json()['number'] == 'АО-2026-10-02'


def test_scope(client):
    from tests.test_v1140 import _mk_user
    hdr = login(client, 'admin', 'admin123')
    a, b = company(client, hdr), company(client, hdr)
    acc = _mk_user(client, hdr, a, f'number_{uuid4().hex[:12]}', role='accountant')
    assert reserve(client, acc, b).status_code == 403
    assert reserve(client, acc, a).status_code == 200
