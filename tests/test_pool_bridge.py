"""Связь пула и реестра: права, дедупликация и происхождение проверки."""
import json
from datetime import datetime
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import Receipt, ReceiptItem
from app.pool import ingest
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.services import appsettings
from tests.conftest import login
from tests.test_v1140 import _mk_user


@pytest.fixture
def setup_bridge(client):
    hdr = login(client, 'admin', 'admin123')
    c1 = client.post('/api/v1/companies', headers=hdr, json={'name': 'Пул А ' + uuid4().hex}).json()['id']
    c2 = client.post('/api/v1/companies', headers=hdr, json={'name': 'Пул Б ' + uuid4().hex}).json()['id']
    a1 = _mk_user(client, hdr, c1, 'pool_' + uuid4().hex[:12], role='accountant')
    a2 = _mk_user(client, hdr, c2, 'pool_' + uuid4().hex[:12], role='accountant')
    digits = str(int(uuid4().hex, 16) % 10**15).zfill(15)
    fn = '7' + digits
    fd, fp = '3467', '2882574203'
    qr = f't=20260922T1556&s=5764.00&fn={fn}&i={fd}&fp={fp}&n=1'
    with SessionLocal() as db:
        appsettings.set_setting(db, 'pool_enabled', '1')
        appsettings.set_setting(db, 'pool_pick_monthly_limit', '100')
        r = PoolReceipt(fn=fn, fd=fd, fp=fp, qr_data=qr, receipt_date=datetime(2026, 9, 22, 15, 56),
                        total_sum=5764, operation=1, merchant_name='Магазин', merchant_inn='4703148343',
                        merchant_address='Ленинградская область, г. Приозерск',
                        region_code='47', city='Приозерск', status='verified', full_data=True,
                        verified_at=datetime(2026, 10, 1))
        db.add(r)
        db.flush()
        pid = r.id
        db.add(PoolItem(receipt_id=pid, name='Гипсокартон', quantity=7, price=665, total=4655, vat_rate='11'))
        db.add(PoolItem(receipt_id=pid, name='Шпаклёвка', quantity=1, price=1109, total=1109, vat_rate='20'))
        db.commit()
    yield hdr, a1, a2, c1, c2, pid, qr, fn, fd, fp
    with SessionLocal() as db:
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id.in_(
            db.query(Receipt.id).filter(Receipt.fn == fn))).delete(synchronize_session=False)
        db.query(Receipt).filter(Receipt.fn == fn).delete(synchronize_session=False)
        db.query(PoolItem).filter(PoolItem.receipt_id == pid).delete(synchronize_session=False)
        db.query(PoolReceipt).filter(PoolReceipt.id == pid).delete(synchronize_session=False)
        db.commit()
    client.cookies.clear()


def test_assign_transfer_idempotency_and_provenance(client, setup_bridge, monkeypatch):
    _, a1, a2, _, _, pid, _, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    res = client.post('/api/v1/pool-company/assign', headers=a1, json={'receipt_ids': [pid]})
    assert res.status_code == 200, res.text
    assert client.post('/api/v1/pool-company/transfer', headers=a2,
                       json={'receipt_ids': [pid]}).status_code == 404
    result = client.post('/api/v1/pool-company/transfer', headers=a1, json={'receipt_ids': [pid]})
    assert result.status_code == 200, result.text
    assert result.json()['created'] == 1 and result.json()['unconfirmed'] == 1
    with SessionLocal() as db:
        r = db.query(Receipt).filter_by(fn=fn).one()
        assert r.fns_status == 'unknown' and r.status == 'pending'
        assert r.source == 'pool' and len(r.items) == 2 and r.full_data
        assert db.get(PoolReceipt, pid).assigned_company_id == r.company_id
    again = client.post('/api/v1/pool-company/transfer', headers=a1, json={'receipt_ids': [pid]})
    assert again.status_code == 200 and again.json()['existing'] == 1
    assert client.post('/api/v1/pool-company/unassign', headers=a1,
                       json={'receipt_ids': [pid]}).status_code == 409
    assert pid not in {x['id'] for x in client.get('/api/v1/pool-company/search', headers=a2).json()['items']}


def test_scan_uses_pool_without_fns(client, setup_bridge, monkeypatch):
    _, a1, a2, c1, _, pid, qr, fn, _, _ = setup_bridge
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        pool.fns_confirmed = True
        pool.fns_checked_at = datetime(2026, 9, 23)
        pool.raw = json.dumps({'buyerPhoneOrAddress': 'private@example.com', 'retailPlace': 'ТД Вимос', 'totalSum': 576400,
                               'items': [{'name': 'Гипсокартон', 'sum': 465500}]}, ensure_ascii=False)
        pool.extra_json = json.dumps({'retail_place': 'ТД Вимос', 'buyer_contact': 'private@example.com'}, ensure_ascii=False)
        db.commit()
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    monkeypatch.setattr('app.routers.receipts._enqueue_fetch', lambda *a, **k: pytest.fail('FNS fetch'))
    result = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr})
    assert result.status_code == 200, result.text
    assert result.json()['receipt']['fns_status'] == 'valid'
    assert not result.json()['auto_verify']
    with SessionLocal() as db:
        r = db.query(Receipt).filter_by(fn=fn).one()
        assert r.company_id == c1 and r.retail_place_index == 'ТД Вимос'
        assert 'private@example.com' not in r.raw_data and 'private@example.com' not in r.ext_json
        assert db.get(PoolReceipt, pid).assigned_company_id == c1
        assert len(r.items) == 2
    duplicate = client.post('/api/v1/receipts/scan', headers=a2, json={'qr_data': qr})
    assert duplicate.json()['duplicate'] is True and duplicate.json()['receipt'] is None


def test_registry_blocks_reintroduction_into_pool(client, setup_bridge, monkeypatch):
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    _, a1, _, _, _, pid, qr, fn, fd, fp = setup_bridge
    with SessionLocal() as db:
        db.query(PoolItem).filter_by(receipt_id=pid).delete()
        db.query(PoolReceipt).filter_by(id=pid).delete()
        db.commit()
    client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr})
    with SessionLocal() as db:
        user = PoolUser(vid='test_' + uuid4().hex)
        db.add(user)
        db.flush()
        _, err = ingest.precheck_ingest(db, qr, user)
        assert err['result'] == 'duplicate'
        db.rollback()


def test_pool_filters_same_item_and_before_limit(client, setup_bridge):
    _, a1, _, _, _, pid, _, _, _, _ = setup_bridge
    def search(**params):
        r = client.get('/api/v1/pool-company/search', headers=a1, params=params)
        assert r.status_code == 200, r.text
        return {x['id'] for x in r.json()['items']}
    assert pid in search(city='приозер', item_name='ГИПСОКАРТОН', vat_rate='11',
                         item_total_min=4655, item_total_max=4655, item_quantity_min=7)
    assert pid not in search(item_name='гипсокартон', item_total_max=1109)
    assert pid not in search(item_name='гипсокартон', vat_rate='20')
    assert pid not in search(q='%')
    with SessionLocal() as db:
        from app.pool.router_company import _filtered
        newer = PoolReceipt(fn='new'+uuid4().hex[:10], fd='99', fp='99', city='Москва',
                            status='verified', receipt_date=datetime(2026, 10, 1))
        db.add(newer)
        db.commit()
        nid = newer.id
        assert [r.id for r in _filtered(db, {'city': 'приозер'}, limit=1)] == [pid]
        db.delete(db.get(PoolReceipt, nid))
        db.commit()


def test_concurrent_scan_assigns_pool_to_only_one_company(client, setup_bridge):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.models import User
    from app.routers.receipts import _upsert_receipt
    from app.services.qr import parse_qr

    _, _, _, c1, c2, pid, qr, fn, _, _ = setup_bridge
    with SessionLocal() as db:
        u1 = db.query(User).filter_by(company_id=c1, role='accountant').one().id
        u2 = db.query(User).filter_by(company_id=c2, role='accountant').one().id
    barrier = Barrier(2)

    def run(uid, cid):
        from fastapi import HTTPException
        with SessionLocal() as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            try:
                receipt, created = _upsert_receipt(db, parse_qr(qr), 'web', user, cid)
                return receipt.company_id, created
            except HTTPException as exc:
                db.rollback()
                return exc.status_code, False

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(run, u1, c1)
        b = executor.submit(run, u2, c2)
        results = [a.result(timeout=20), b.result(timeout=20)]
    assert sum(1 for _, created in results if created) == 1
    with SessionLocal() as db:
        receipt = db.query(Receipt).filter_by(fn=fn).one()
        assert receipt.company_id in (c1, c2)
        assert db.get(PoolReceipt, pid).assigned_company_id == receipt.company_id


def test_ingest_rechecks_registry_before_network(client, setup_bridge, monkeypatch):
    from app.services.qr import parse_qr

    _, a1, _, _, _, pid, qr, _, _, _ = setup_bridge
    with SessionLocal() as db:
        db.query(PoolItem).filter_by(receipt_id=pid).delete()
        db.query(PoolReceipt).filter_by(id=pid).delete()
        db.commit()
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    assert client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).status_code == 200
    monkeypatch.setattr(ingest, '_fetch_details', lambda *a, **k: pytest.fail('лишний запрос ФНС'))
    with SessionLocal() as db:
        user = PoolUser(vid='bridge_' + uuid4().hex)
        db.add(user)
        db.flush()
        result = ingest.ingest_parsed(db, qr, parse_qr(qr), 'web', user)
        assert result['result'] == 'duplicate'


def test_pool_ingest_rechecks_after_network_race(client, setup_bridge, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services.external import ExternalItem, ExternalResult
    from app.services.qr import parse_qr

    _, a1, _, _, _, pid, qr, fn, fd, fp = setup_bridge
    with SessionLocal() as db:
        db.query(PoolItem).filter_by(receipt_id=pid).delete()
        db.query(PoolReceipt).filter_by(id=pid).delete()
        user = PoolUser(vid='race_' + uuid4().hex)
        db.add(user)
        db.commit()
        uid = user.id
    started, proceed = Event(), Event()
    def fetch(*args, **kwargs):
        started.set()
        assert proceed.wait(timeout=10)
        return ExternalResult(ok=True, found=True, source='fns_api', total_sum=5764,
                              items=[ExternalItem(name='товар', total=5764)])
    monkeypatch.setattr(ingest, '_fetch_details', fetch)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    def background():
        with SessionLocal() as db:
            return ingest.ingest_parsed(db, qr, parse_qr(qr), 'web', db.get(PoolUser, uid))
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(background)
        assert started.wait(timeout=10)
        assert client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).status_code == 200
        proceed.set()
        assert task.result(timeout=15)['result'] == 'duplicate'
    with SessionLocal() as db:
        assert db.query(Receipt).filter_by(fn=fn, fd=fd, fp=fp).count() == 1
        assert db.query(PoolReceipt).filter_by(fn=fn, fd=fd, fp=fp).count() == 0
        db.delete(db.get(PoolUser, uid))
        db.commit()


def test_pool_bridge_additive_schema_on_legacy_sqlite():
    from sqlalchemy import create_engine, inspect, text

    from app.pool.models import ensure_pool_schema

    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE pool_receipts (id VARCHAR(36) PRIMARY KEY, '
                          'fn VARCHAR(20), fd VARCHAR(10), fp VARCHAR(20), '
                          'raw TEXT, assigned_company_id VARCHAR(36))'))
        conn.execute(text("INSERT INTO pool_receipts (id, fn, fd, fp, raw) "
                          "VALUES ('legacy', '1', '2', '3', 'old')"))
    ensure_pool_schema(engine)
    ensure_pool_schema(engine)
    columns = {c['name'] for c in inspect(engine).get_columns('pool_receipts')}
    assert {'extra_json', 'fns_confirmed', 'fns_checked_at'} <= columns
    with engine.connect() as conn:
        row = conn.execute(text("SELECT fn, raw, fns_confirmed, fns_checked_at FROM pool_receipts WHERE id='legacy'")).one()
        assert row[0] == '1' and row[1] == 'old'
        assert not row[2] and row[3] is None
    engine.dispose()


def test_new_pool_schema_boolean_default_is_false():
    from sqlalchemy import create_engine, inspect, text

    from app.pool.models import PoolReceipt

    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE pool_users (id VARCHAR(36) PRIMARY KEY)'))
        PoolReceipt.__table__.create(conn)
    columns = {col['name']: col for col in inspect(engine).get_columns('pool_receipts')}
    assert columns['fns_confirmed']['default'] == '0'
    engine.dispose()


def test_duplicate_warning_across_entry_paths(client, setup_bridge, monkeypatch):
    from types import SimpleNamespace

    from app.services.qr import parse_qr

    hdr, a1, a2, c1, c2, pid, qr, fn, fd, fp = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    assert client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    scan = client.post('/api/v1/receipts/scan', headers=a2, json={'qr_data': qr}).json()
    assert scan['duplicate'] and scan['receipt'] is None and 'другой организац' in scan['message']
    admin_scan = client.post('/api/v1/receipts/scan', headers=hdr, json={'qr_data': qr, 'company_id': c2}).json()
    assert admin_scan['duplicate'] and admin_scan['receipt'] is None
    monkeypatch.setattr('app.routers.receipts.imaging.decode_qr_image',
                        lambda *a: SimpleNamespace(parsed=parse_qr(qr), total_codes=1))
    image = client.post('/api/v1/receipts/scan/image', headers=a2,
                        files={'file': ('receipt.png', b'image', 'image/png')}).json()
    assert image['receipt'] is None and 'другой организац' in image['message']
    monkeypatch.setattr('app.services.pdf_receipts.extract_qr_texts', lambda *a: [qr])
    pdf = client.post('/api/v1/receipts/scan/pdf', headers=a2,
                      files={'file': ('receipt.pdf', b'%PDF-test', 'application/pdf')}).json()
    assert pdf['receipt'] is None and 'другой организац' in pdf['message']
    manual = client.post('/api/v1/receipts/manual', headers=a2, json={
        'date_time': '22.09.2026 15:56', 'total_sum': 5764, 'fn': fn, 'fd': fd,
        'fp': fp, 'operation': 1}).json()
    assert manual['receipt'] is None and 'другой организац' in manual['message']
    with SessionLocal() as db:
        assert db.query(Receipt).filter_by(fn=fn).count() == 1
        assert db.get(PoolReceipt, pid).assigned_company_id == c1


def test_overlap_audit_detects_conflicts_without_mutation(client, setup_bridge, monkeypatch):
    from app.services.receipt_merge_audit import audit_receipt_overlap

    _, a1, _, _, c2, pid, qr, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    assert client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    with SessionLocal() as db:
        before = audit_receipt_overlap(db)
        pool = db.get(PoolReceipt, pid)
        pool.total_sum += 10
        pool.assigned_company_id = c2
        db.commit()
        after = audit_receipt_overlap(db)
        assert after['matching_identities'] == before['matching_identities']
        assert after['different_amount'] == before['different_amount'] + 1
        assert after['different_company_assignment'] == before['different_company_assignment'] + 1
        assert db.query(Receipt).filter_by(fn=fn).count() == 1
        assert db.get(PoolReceipt, pid).total_sum == 5774


def test_admin_upload_respects_selected_company(client, setup_bridge, monkeypatch):
    from types import SimpleNamespace

    from app.services.qr import parse_qr

    hdr, a1, _, _, c2, _, qr, _, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    assert client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    monkeypatch.setattr('app.routers.receipts.imaging.decode_qr_image',
                        lambda *a: SimpleNamespace(parsed=parse_qr(qr), total_codes=1))
    monkeypatch.setattr('app.services.pdf_receipts.extract_qr_texts', lambda *a: [qr])
    for path, filename, mime in [('image', 'receipt.png', 'image/png'), ('pdf', 'receipt.pdf', 'application/pdf')]:
        response = client.post(f'/api/v1/receipts/scan/{path}', headers=hdr,
                               files={'file': (filename, b'test', mime)}, data={'company_id': c2})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['duplicate'] and result['receipt'] is None
        assert 'другой организац' in result['message']


def test_pdf_mixed_batch_reports_foreign_duplicate_and_keeps_new(client, setup_bridge, monkeypatch):
    _, a1, a2, _, c2, _, qr, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    assert client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    new_fn = ('8' if fn[0] != '8' else '9') + fn[1:]
    new_qr = qr.replace(fn, new_fn)
    monkeypatch.setattr('app.services.pdf_receipts.extract_qr_texts', lambda *a: [qr, new_qr])
    result = client.post('/api/v1/receipts/scan/pdf', headers=a2,
                         files={'file': ('receipts.pdf', b'pdf', 'application/pdf')}).json()
    assert result['created'] and result['duplicate'] and 'другой организац' in result['message']
    assert result['receipt']['fn'] == new_fn and result['receipt']['company_id'] == c2
    with SessionLocal() as db:
        db.query(Receipt).filter_by(fn=new_fn).delete()
        db.commit()
