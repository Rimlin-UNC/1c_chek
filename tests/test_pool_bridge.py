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


def test_edit_cannot_break_pool_history_or_create_pool_duplicate(client, setup_bridge, monkeypatch):
    _, a1, _, _, _, pid, qr, fn, fd, fp = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    other_fn = ('8' if fn[0] != '8' else '9') + fn[1:]
    other_qr = qr.replace(fn, other_fn)
    independent = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': other_qr}).json()['receipt']
    assert independent
    collision = client.patch(f"/api/v1/receipts/{independent['id']}", headers=a1,
                             json={'fn': fn, 'fd': fd, 'fp': fp})
    assert collision.status_code == 409
    transferred = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    assert transferred
    rekey = client.patch(f"/api/v1/receipts/{transferred['id']}", headers=a1,
                         json={'fn': '6' + fn[1:]})
    assert rekey.status_code == 409 and 'истори' in rekey.json()['detail']
    with SessionLocal() as db:
        assert db.get(Receipt, transferred['id']).fn == fn
        assert db.get(PoolReceipt, pid).fn == fn
        assert db.get(Receipt, independent['id']).fn == other_fn
        db.query(Receipt).filter_by(id=independent['id']).delete()
        db.commit()


def test_offline_copy_migration_preserves_access_points_and_conflicts(client, setup_bridge, monkeypatch, tmp_path):
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolPoint
    from app.services.scoping import can_view_receipt
    from qa.migrate_receipt_copy import migrate_copy

    _, a1, _, c1, c2, pid, qr, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    created = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    with SessionLocal() as db:
        donor = PoolUser(vid='migration_' + uuid4().hex, points=9)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id = donor_id
        db.get(Receipt, created['id']).comment = 'личная заметка компании'
        pool.total_sum = 999.0  # прошлое расхождение нельзя молча объединять
        pool.assigned_company_id = c2
        db.add(PoolPoint(user_id=donor_id, delta=9, reason='receipt', ref_id=pid))
        db.commit()
    copy_path = tmp_path / 'migrated.db'
    summary = migrate_copy(engine.url.database, copy_path)
    assert summary['needs_review'] >= 1
    with sqlite3.connect(copy_path) as db:
        doc = db.execute('SELECT id, total_sum, review_mask FROM unified_fiscal_documents WHERE fn=?', (fn,)).fetchone()
        assert doc and doc[1] is None and doc[2] & 2 and doc[2] & 8
        assert set(db.execute('SELECT origin_kind, origin_id FROM unified_receipt_origins WHERE document_id=?',
                              (doc[0],)).fetchall()) == {('pool', pid), ('registry', created['id'])}
        assert db.execute('SELECT points FROM pool_users WHERE id=?', (donor_id,)).fetchone() == (9,)
        assert db.execute('SELECT delta FROM pool_points WHERE ref_id=?', (pid,)).fetchone() == (9,)
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    copied_engine = create_engine('sqlite:///' + str(copy_path))
    with Session(copied_engine) as db:
        receipt = db.get(Receipt, created['id'])
        assert receipt.comment == 'личная заметка компании' and len(receipt.items) == 2
        assert db.query(PoolItem).filter_by(receipt_id=pid).count() == 2
        owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
        other = db.query(User).filter_by(company_id=c2, role='accountant').first()
        assert can_view_receipt(owner, receipt) and not can_view_receipt(other, receipt)
    copied_engine.dispose()
    with SessionLocal() as db:
        assert db.get(PoolReceipt, pid).total_sum == 999.0
        assert not db.connection().exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name='unified_fiscal_documents'").first()
        db.query(PoolPoint).filter_by(ref_id=pid).delete()
        db.get(PoolReceipt, pid).pool_user_id = None
        db.delete(db.get(PoolUser, donor_id))
        db.commit()
    with pytest.raises(FileExistsError):
        migrate_copy(engine.url.database, copy_path)


def test_migrated_copy_api_scopes_companies_and_donors(client, setup_bridge, monkeypatch, tmp_path):
    """Чтение через реальные API с подменой БД только на мигрированную копию."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import engine, get_db
    from app.main import app
    from app.pool.accounts import issue_pool_token
    from app.pool.models import PoolPoint
    from qa.migrate_receipt_copy import migrate_copy

    _, a1, a2, c1, _, pid, qr, _, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    created = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    with SessionLocal() as db:
        donor = PoolUser(vid='donor_' + uuid4().hex, points=9)
        stranger = PoolUser(vid='stranger_' + uuid4().hex, points=0)
        db.add_all([donor, stranger])
        db.flush()
        donor_id, stranger_id = donor.id, stranger.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded = donor_id, 9
        db.add(PoolPoint(user_id=donor_id, delta=9, reason='receipt', ref_id=pid))
        db.get(Receipt, created['id']).comment = 'private company remark'
        db.commit()
    copy_path = tmp_path / 'api-rehearsal.db'
    migrate_copy(engine.url.database, copy_path)
    copied_engine = create_engine('sqlite:///' + str(copy_path), connect_args={'check_same_thread': False})
    copied_session = sessionmaker(bind=copied_engine)
    def copied_db():
        with copied_session() as db:
            yield db
    app.dependency_overrides[get_db] = copied_db
    donor_header = {'Authorization': 'Bearer ' + issue_pool_token(donor_id)}
    stranger_header = {'Authorization': 'Bearer ' + issue_pool_token(stranger_id)}
    try:
        own = client.get('/api/v1/receipts/' + created['id'], headers=a1)
        assert own.status_code == 200 and own.json()['comment'] == 'private company remark'
        assert client.get('/api/v1/receipts/' + created['id'], headers=a2).status_code == 404
        own_pool = client.get('/api/v1/pool-company/mine', headers=a1).json()['items']
        foreign_pool = client.get('/api/v1/pool-company/mine', headers=a2).json()['items']
        assert pid in {r['id'] for r in own_pool} and pid not in {r['id'] for r in foreign_pool}
        assert client.get('/api/v1/pool-my/summary', headers=donor_header).json()['points'] == 9
        donor_receipt = client.get('/api/v1/pool-my/receipt/' + pid, headers=donor_header)
        assert donor_receipt.status_code == 200 and donor_receipt.json()['points'] == 9
        assert len(donor_receipt.json()['items']) == 2
        assert 'private company remark' not in donor_receipt.text
        assert client.get('/api/v1/pool-my/receipt/' + pid, headers=stranger_header).status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
        copied_engine.dispose()
        with SessionLocal() as db:
            db.query(PoolPoint).filter_by(ref_id=pid).delete()
            db.get(PoolReceipt, pid).pool_user_id = None
            db.delete(db.get(PoolUser, donor_id))
            db.delete(db.get(PoolUser, stranger_id))
            db.commit()


def test_migrated_copy_starts_app_with_same_access_and_donor_history(client, setup_bridge, monkeypatch, tmp_path):
    """Отдельный процесс запускает настоящее приложение на SQLite-копии."""
    import os
    import subprocess
    import sys

    from app.config import settings
    from app.database import engine
    from app.pool.accounts import issue_pool_token
    from app.pool.models import PoolPoint
    from qa.migrate_receipt_copy import migrate_copy

    _, a1, a2, _, _, pid, qr, _, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    created = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    with SessionLocal() as db:
        donor = PoolUser(vid='copy_start_' + uuid4().hex, points=11)
        stranger = PoolUser(vid='copy_start_' + uuid4().hex, points=0)
        db.add_all([donor, stranger])
        db.flush()
        donor_id, stranger_id = donor.id, stranger.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded = donor_id, 11
        db.add(PoolPoint(user_id=donor_id, delta=11, reason='receipt', ref_id=pid))
        db.get(Receipt, created['id']).comment = 'private company remark'
        db.commit()
    copy_path = tmp_path / 'startup-rehearsal.db'
    migrate_copy(engine.url.database, copy_path)
    code = r'''
import json, sys
from fastapi.testclient import TestClient
from app.main import app
from app.services import backups, telegram_bot, max_bot, webhooks
from app.openstack_monitor import worker
from app.routers import receipts
from app.services import releases
backups.daily_backup.start = lambda: None
telegram_bot.start_worker = max_bot.start_worker = webhooks.start_worker = worker.start_worker = lambda: False
receipts.start_fetch_workers = lambda: 0
receipts.start_limit_retry_worker = lambda: False
releases.mark_healthy = lambda *a, **k: None
p = json.load(sys.stdin)
with TestClient(app) as c:
    own = c.get('/api/v1/receipts/' + p['rid'], headers=p['a1'])
    assert own.status_code == 200 and own.json()['comment'] == 'private company remark', own.text
    assert c.get('/api/v1/receipts/' + p['rid'], headers=p['a2']).status_code == 404
    assert c.get('/api/v1/pool-my/summary', headers=p['donor']).json()['points'] == 11
    donor = c.get('/api/v1/pool-my/receipt/' + p['pid'], headers=p['donor'])
    assert donor.status_code == 200 and donor.json()['points'] == 11 and len(donor.json()['items']) == 2
    assert 'private company remark' not in donor.text
    assert c.get('/api/v1/pool-my/receipt/' + p['pid'], headers=p['stranger']).status_code == 404
    mine = c.get('/api/v1/pool-company/mine', headers=p['a1']).json()['items']
    other = c.get('/api/v1/pool-company/mine', headers=p['a2']).json()['items']
    assert p['pid'] in {x['id'] for x in mine} and p['pid'] not in {x['id'] for x in other}
'''
    payload = json.dumps({'rid': created['id'], 'pid': pid, 'a1': a1, 'a2': a2,
                          'donor': {'Authorization': 'Bearer ' + issue_pool_token(donor_id)},
                          'stranger': {'Authorization': 'Bearer ' + issue_pool_token(stranger_id)}})
    env = {**os.environ, 'DATABASE_URL': 'sqlite:///' + str(copy_path),
           'SECRET_KEY': settings.SECRET_KEY, 'YM_NO_REEXEC': '1', 'YM_FETCHQ_OFF': '1'}
    try:
        run = subprocess.run([sys.executable, '-c', code], input=payload, text=True,
                             env=env, capture_output=True, timeout=60, check=False)
        assert run.returncode == 0, run.stdout + run.stderr
    finally:
        with SessionLocal() as db:
            db.query(PoolPoint).filter_by(ref_id=pid).delete()
            db.get(PoolReceipt, pid).pool_user_id = None
            db.delete(db.get(PoolUser, donor_id))
            db.delete(db.get(PoolUser, stranger_id))
            db.commit()


def test_unified_preview_respects_company_and_donor_privacy(client, setup_bridge, monkeypatch, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_receipt_preview import company_view, donor_view

    _, a1, _, c1, c2, pid, qr, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    receipt_id = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']['id']
    with SessionLocal() as db:
        donor = PoolUser(vid='unified_' + uuid4().hex, points=7)
        stranger = PoolUser(vid='unified_' + uuid4().hex, points=0)
        db.add_all([donor, stranger])
        db.flush()
        donor_id, stranger_id = donor.id, stranger.id
        db.get(PoolReceipt, pid).pool_user_id = donor_id
        db.get(Receipt, receipt_id).comment = 'private company remark'
        db.commit()
    path = tmp_path / 'unified-preview.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc_id = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            a1_user = db.query(User).filter_by(company_id=c1, role='accountant').first()
            a2_user = db.query(User).filter_by(company_id=c2, role='accountant').first()
            company = company_view(db, doc_id, a1_user, c1)
            assert company['receipt']['comment'] == 'private company remark'
            with pytest.raises(HTTPException) as err:
                company_view(db, doc_id, a2_user, c2)
            assert err.value.status_code == 404
            # Историческое расхождение назначений не даёт второй компании
            # доступ к чеку, принадлежащему первой компании в реестре.
            db.get(PoolReceipt, pid).assigned_company_id = c2
            db.flush()
            with pytest.raises(HTTPException) as err:
                company_view(db, doc_id, a2_user, c2)
            assert err.value.status_code == 404
            donor = donor_view(db, doc_id, db.get(PoolUser, donor_id))
            assert len(donor['receipt']['items']) == 2
            assert 'private company remark' not in str(donor)
            with pytest.raises(HTTPException) as err:
                donor_view(db, doc_id, db.get(PoolUser, stranger_id))
            assert err.value.status_code == 404
            # При изменении исходного ФН старый снимок не используется как факт.
            db.get(PoolReceipt, pid).fn = 'mismatch'
            db.flush()
            with pytest.raises(HTTPException) as err:
                donor_view(db, doc_id, db.get(PoolUser, donor_id))
            assert err.value.status_code == 404
            db.rollback()
    finally:
        copied.dispose()
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).pool_user_id = None
            db.delete(db.get(PoolUser, donor_id))
            db.delete(db.get(PoolUser, stranger_id))
            db.commit()


def test_unified_preview_assigned_pool_permissions(client, setup_bridge, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_receipt_preview import company_view

    hdr, a1, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _mk_user(client, hdr, c1, 'unified_' + uuid4().hex[:12], role='user')
    assert client.post('/api/v1/pool-company/assign', headers=a1, json={'receipt_ids': [pid]}).status_code == 200
    path = tmp_path / 'assigned-preview.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            accountant = db.query(User).filter_by(company_id=c1, role='accountant').first()
            other = db.query(User).filter_by(company_id=c2, role='accountant').first()
            employee = db.query(User).filter_by(company_id=c1, role='user').first()
            view = company_view(db, doc, accountant, c1)
            assert view['origin'] == 'pool' and view['receipt']['id'] == pid
            assert 'pool_user_id' not in view['receipt'] and 'raw' not in view['receipt']
            for user, company in ((other, c2), (other, c1), (employee, c1)):
                with pytest.raises(HTTPException) as err:
                    company_view(db, doc, user, company)
                assert err.value.status_code == 404
    finally:
        copied.dispose()


def test_unified_claim_is_atomic_exclusive_and_preserves_donor(setup_bridge, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session, sessionmaker

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolPoint
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_claims import claim_pool_document
    from qa.unified_receipt_preview import company_view, donor_view

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS fetch'))
    with SessionLocal() as db:
        donor = PoolUser(vid='claim_' + uuid4().hex, points=13)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded = donor_id, 13
        db.add(PoolPoint(user_id=donor_id, delta=13, reason='receipt', ref_id=pid))
        user_ids = {cid: db.query(User).filter_by(company_id=cid, role='accountant').first().id
                    for cid in (c1, c2)}
        db.commit()
    path = tmp_path / 'claim-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    SessionCopy = sessionmaker(bind=copied)
    barrier = Barrier(2)
    with copied.connect() as conn:
        doc_id = conn.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
    def attempt(cid):
        with SessionCopy() as db:
            user = db.get(User, user_ids[cid])
            barrier.wait(timeout=10)
            try:
                created = claim_pool_document(db, doc_id, cid, user)
                db.commit()
                return cid, created, None
            except HTTPException as err:
                db.rollback()
                return cid, False, err.status_code
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, (c1, c2)))
        winners = [cid for cid, created, _ in results if created]
        losers = [cid for cid, _, code in results if code == 409]
        assert len(winners) == 1 and len(losers) == 1, results
        winner, loser = winners[0], losers[0]
        with Session(copied) as db:
            claim = db.execute(text('SELECT company_id FROM unified_company_claims WHERE document_id=:doc'),
                               {'doc': doc_id}).scalar_one()
            assert claim == winner and db.get(PoolReceipt, pid).assigned_company_id == winner
            assert db.query(Receipt).filter_by(fn=fn).count() == 0  # не создаём копию реестра
            assert claim_pool_document(db, doc_id, winner, db.get(User, user_ids[winner])) is False
            db.commit()
            assert company_view(db, doc_id, db.get(User, user_ids[winner]), winner)['origin'] == 'pool'
            with pytest.raises(HTTPException) as err:
                company_view(db, doc_id, db.get(User, user_ids[loser]), loser)
            assert err.value.status_code == 404
            assert month_used(db, winner) == 1
            assert db.get(PoolUser, donor_id).points == 13
            assert db.query(PoolPoint).filter_by(user_id=donor_id, ref_id=pid).one().delta == 13
            assert len(donor_view(db, doc_id, db.get(PoolUser, donor_id))['receipt']['items']) == 2
        import sqlite3
        with sqlite3.connect(path) as conn:
            _validate(conn)
        with SessionLocal() as db:
            assert db.get(PoolReceipt, pid).assigned_company_id is None  # исходная БД не затронута
    finally:
        copied.dispose()
        with SessionLocal() as db:
            db.query(PoolPoint).filter_by(ref_id=pid).delete()
            db.get(PoolReceipt, pid).pool_user_id = None
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


def test_unified_claim_enforces_rights_quota_and_snapshot_freshness(client, setup_bridge, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_claims import claim_pool_document

    hdr, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _mk_user(client, hdr, c1, 'claim_user_' + uuid4().hex[:8], role='user')
    path = tmp_path / 'claim-rules.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            accountant = db.query(User).filter_by(company_id=c1, role='accountant').first()
            employee = db.query(User).filter_by(company_id=c1, role='user').first()
            with pytest.raises(HTTPException) as err:
                claim_pool_document(db, doc, c1, employee)
            assert err.value.status_code == 403
            with pytest.raises(HTTPException) as err:
                claim_pool_document(db, doc, c2, accountant)
            assert err.value.status_code == 403
            db.rollback()
            appsettings.set_setting(db, 'pool_pick_monthly_limit', '0')
            with pytest.raises(HTTPException) as err:
                claim_pool_document(db, doc, c1, accountant)
            assert err.value.status_code == 409 and 'Квота' in err.value.detail
            db.rollback()
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            appsettings.set_setting(db, 'pool_pick_monthly_limit', '10')
            db.get(PoolReceipt, pid).total_sum += 1
            db.commit()
            with pytest.raises(HTTPException) as err:
                claim_pool_document(db, doc, c1, accountant)
            assert err.value.status_code == 409 and 'расхождения' in err.value.detail
            db.rollback()
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            # Незавершённая транзакция не оставляет ни привязку, ни владельца.
            db.get(PoolReceipt, pid).total_sum -= 1
            db.commit()
            assert claim_pool_document(db, doc, c1, accountant) is True
            db.rollback()
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_unified_claim_race_respects_single_company_quota(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_claims import claim_pool_document

    _, _, _, c1, _, pid, _, fn, fd, fp = setup_bridge
    other_fn = '6' + fn[1:]
    with SessionLocal() as db:
        source = db.get(PoolReceipt, pid)
        other = PoolReceipt(fn=other_fn, fd=fd, fp=fp, receipt_date=source.receipt_date,
                            total_sum=source.total_sum, operation=source.operation, status='verified')
        db.add(other)
        db.commit()
        other_id = other.id
        uid = db.query(User).filter_by(company_id=c1, role='accountant').first().id
        appsettings.set_setting(db, 'pool_pick_monthly_limit', '1')
    path = tmp_path / 'quota-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    with copied.connect() as conn:
        docs = list(conn.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn IN (:a,:b)'),
                                 {'a': fn, 'b': other_fn}).scalars())
    barrier = Barrier(2)
    def attempt(doc):
        with sessions() as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            try:
                ok = claim_pool_document(db, doc, c1, user)
                db.commit()
                return ok, None
            except HTTPException as err:
                db.rollback()
                return False, err.status_code
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, docs))
        assert sorted(results) == [(False, 409), (True, None)]
        with sessions() as db:
            assert month_used(db, c1) == 1
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE company_id=:c'),
                              {'c': c1}).scalar_one() == 1
    finally:
        copied.dispose()
        with SessionLocal() as db:
            db.delete(db.get(PoolReceipt, other_id))
            db.commit()


@pytest.mark.parametrize('confirmed,checked,expected', [
    (False, None, 'unknown'),
    (True, None, 'unknown'),
    (True, datetime(2026, 9, 23, 8, 0), 'valid'),
])
def test_unified_account_uses_one_fiscal_record_and_private_company_data(
        client, setup_bridge, tmp_path, monkeypatch, confirmed, checked, expected):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolPoint
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_accounts import account_view, promote_pool_claim, update_account_private
    from qa.unified_claims import claim_pool_document
    from qa.unified_receipt_preview import donor_view

    hdr, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _mk_user(client, hdr, c1, 'account_user_' + uuid4().hex[:8], role='user')
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS fetch'))
    with SessionLocal() as db:
        donor = PoolUser(vid='account_' + uuid4().hex, points=8)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded = donor_id, 8
        pool.fns_confirmed, pool.fns_checked_at = confirmed, checked
        pool.raw = '{"buyerPhoneOrAddress":"private@example.com"}'
        db.add(PoolPoint(user_id=donor_id, delta=8, reason='receipt', ref_id=pid))
        db.commit()
    path = tmp_path / 'unified-account.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            other = db.query(User).filter_by(company_id=c2, role='accountant').first()
            employee = db.query(User).filter_by(company_id=c1, role='user').first()
            assert claim_pool_document(db, doc, c1, owner) is True
            db.commit()
            assert month_used(db, c1) == 1
            assert promote_pool_claim(db, doc, c1, owner) is True
            db.commit()
            assert promote_pool_claim(db, doc, c1, owner) is False
            db.commit()
            assert month_used(db, c1) == 1
            assert db.query(Receipt).filter_by(fn=fn).count() == 0
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_documents WHERE fn=:fn'),
                              {'fn': fn}).scalar_one() == 1
            assert update_account_private(db, doc, c1, owner, comment='private company note', personal_sum=100)
            db.commit()
            assert not update_account_private(db, doc, c1, owner, comment='private company note')
            db.commit()
            with pytest.raises(HTTPException) as denied:
                update_account_private(db, doc, c1, other, comment='not allowed')
            assert denied.value.status_code == 403
            with pytest.raises(HTTPException) as bad_sum:
                update_account_private(db, doc, c1, owner, personal_sum=999999)
            assert bad_sum.value.status_code == 422
            db.rollback()
            events = db.execute(text('SELECT event_type, fields FROM unified_company_account_events '
                                     'WHERE document_id=:id ORDER BY created_at'), {'id': doc}).all()
            assert [event.event_type for event in events] == ['created', 'updated']
            assert 'private company note' not in str(events)
            assert update_account_private(db, doc, c1, owner, comment='temporary change')
            db.rollback()
            assert db.execute(text('SELECT count(*) FROM unified_company_account_events '
                                   'WHERE document_id=:id'), {'id': doc}).scalar_one() == 2
            view = account_view(db, doc, c1, owner)['receipt']
            assert view['fns_status'] == expected and len(view['items']) == 2
            assert view['status'] == ('verified' if expected == 'valid' else 'pending')
            assert view['comment'] == 'private company note' and view['personal_sum'] == 100
            assert 'private@example.com' not in str(view)
            assert (view['fns_checked_at'] is not None) == (expected == 'valid')
            for actor, company in ((other, c2), (other, c1), (employee, c1)):
                with pytest.raises(HTTPException) as err:
                    account_view(db, doc, company, actor)
                assert err.value.status_code == 404
            donor = donor_view(db, doc, db.get(PoolUser, donor_id))
            assert 'private company note' not in str(donor)
            assert db.get(PoolUser, donor_id).points == 8
            assert db.query(PoolPoint).filter_by(user_id=donor_id, ref_id=pid).one().delta == 8
        with sqlite3.connect(path) as conn:
            _validate(conn)
            conn.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('UPDATE unified_company_accounts SET company_id=? WHERE document_id=?', (c2, doc))
            conn.rollback()
    finally:
        copied.dispose()
        with SessionLocal() as db:
            db.query(PoolPoint).filter_by(ref_id=pid).delete()
            db.get(PoolReceipt, pid).pool_user_id = None
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


def test_unified_migration_preserves_legacy_registry_projection(client, setup_bridge, tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view, promote_pool_claim

    _, a1, _, c1, _, _, qr, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts._maybe_auto_verify', lambda *a: False)
    monkeypatch.setattr('app.routers.receipts._maybe_auto_fetch', lambda *a: False)
    created = client.post('/api/v1/receipts/scan', headers=a1, json={'qr_data': qr}).json()['receipt']
    with SessionLocal() as db:
        receipt = db.get(Receipt, created['id'])
        receipt.comment = 'old accounting note'
        receipt.category = 'old category'
        receipt.exported = True
        db.commit()
    path = tmp_path / 'legacy-account.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            row = db.execute(text('SELECT legacy_receipt_id FROM unified_company_accounts WHERE document_id=:doc'),
                             {'doc': doc}).scalar_one()
            assert row == created['id']
            view = account_view(db, doc, c1, owner)
            assert view['origin'] == 'registry'
            assert view['receipt']['comment'] == 'old accounting note'
            assert view['receipt']['category'] == 'old category' and view['receipt']['exported']
            assert promote_pool_claim(db, doc, c1, owner) is False
            db.commit()
            assert db.query(Receipt).filter_by(fn=fn).count() == 1
    finally:
        copied.dispose()


def test_unified_account_promotion_is_atomic_and_idempotent(setup_bridge, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import promote_pool_claim
    from qa.unified_claims import claim_pool_document

    _, _, _, c1, _, _, _, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS fetch'))
    path = tmp_path / 'promote-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    with sessions() as db:
        doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
        uid = db.query(User).filter_by(company_id=c1, role='accountant').first().id
        assert claim_pool_document(db, doc, c1, db.get(User, uid))
        db.commit()
    barrier = Barrier(2)
    def promote():
        with sessions() as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            result = promote_pool_claim(db, doc, c1, user)
            db.commit()
            return result
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: promote(), range(2))) == [False, True]
        with sessions() as db:
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
            assert db.execute(text("SELECT count(*) FROM unified_company_account_events "
                                   "WHERE document_id=:id AND event_type='created'"), {'id': doc}).scalar_one() == 1
            assert db.query(Receipt).filter_by(fn=fn).count() == 0
    finally:
        copied.dispose()


def test_unified_account_refuses_changed_positions(setup_bridge, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view, promote_pool_claim
    from qa.unified_claims import claim_pool_document

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **k: pytest.fail('FNS request'))
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        pool.fns_confirmed, pool.fns_checked_at = True, datetime(2026, 9, 23)
        db.commit()
    path = tmp_path / 'changed-account.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            assert claim_pool_document(db, doc, c1, owner)
            db.commit()
            pool_item = db.query(PoolItem).filter_by(receipt_id=pid).first()
            db.delete(pool_item)
            db.commit()
            with pytest.raises(HTTPException) as err:
                promote_pool_claim(db, doc, c1, owner)
            assert err.value.status_code == 409
            db.rollback()
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_unified_account_refuses_modified_position_and_revoked_fns(setup_bridge, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view, promote_pool_claim
    from qa.unified_claims import claim_pool_document

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        pool.fns_confirmed, pool.fns_checked_at = True, datetime(2026, 9, 23)
        db.commit()
    path = tmp_path / 'changed-content.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            assert claim_pool_document(db, doc, c1, owner)
            db.commit()
            item = db.query(PoolItem).filter_by(receipt_id=pid).first()
            item.name = 'changed after snapshot'
            db.commit()
            with pytest.raises(HTTPException) as err:
                promote_pool_claim(db, doc, c1, owner)
            assert err.value.status_code == 409
            db.rollback()
        # Independent snapshot: no position change, but confirmation is revoked after promotion.
        second = tmp_path / 'revoked-proof.db'
        migrate_copy(engine.url.database, second)
        other = create_engine('sqlite:///' + str(second))
        try:
            with Session(other) as db:
                doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
                owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
                assert claim_pool_document(db, doc, c1, owner)
                assert promote_pool_claim(db, doc, c1, owner)
                db.commit()
                assert account_view(db, doc, c1, owner)['receipt']['fns_status'] == 'valid'
                db.get(PoolReceipt, pid).fns_confirmed = False
                db.commit()
                with pytest.raises(HTTPException) as err:
                    account_view(db, doc, c1, owner)
                assert err.value.status_code == 409
                db.rollback()
                with pytest.raises(HTTPException) as err:
                    promote_pool_claim(db, doc, c1, owner)
                assert err.value.status_code == 409
                db.rollback()
        finally:
            other.dispose()
    finally:
        copied.dispose()


@pytest.mark.parametrize('change', ['delete', 'modify', 'add'])
def test_unified_claim_rejects_stale_positions_without_consuming_quota(setup_bridge, tmp_path, change):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_claims import claim_pool_document

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'before-claim.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            item = db.query(PoolItem).filter_by(receipt_id=pid).first()
            if change == 'delete':
                db.delete(item)
            elif change == 'modify':
                item.name = 'modified item'
            else:
                db.add(PoolItem(receipt_id=pid, name='extra item', quantity=1, price=1, total=1))
            db.commit()
            used = month_used(db, c1)
            with pytest.raises(HTTPException) as err:
                claim_pool_document(db, doc, c1, owner)
            assert err.value.status_code == 409
            db.rollback()
            assert month_used(db, c1) == used
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_unified_atomic_pick_rolls_back_partial_failure_and_outer_transaction(setup_bridge, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    import qa.unified_pool_accounting as accounting
    from app.database import engine
    from app.models import Company, User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'atomic-accounting.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            used = month_used(db, c1)
            company = db.get(Company, c1)
            company.name = 'unrelated outer change'
            original = accounting.promote_pool_claim
            def fail_promotion(*args, **kwargs):
                assert original(*args, **kwargs)
                raise HTTPException(409, 'injected failure after account creation')
            monkeypatch.setattr(accounting, 'promote_pool_claim', fail_promotion)
            with pytest.raises(HTTPException):
                accounting.claim_and_account_pool_document(db, doc, c1, owner)
            db.commit()  # A caller catching the error cannot accidentally commit the claim.
            assert db.get(Company, c1).name == 'unrelated outer change'
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert month_used(db, c1) == used
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.execute(text('SELECT count(*) FROM unified_company_account_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            monkeypatch.setattr(accounting, 'promote_pool_claim', original)
            assert accounting.claim_and_account_pool_document(db, doc, c1, owner) == (True, True)
            db.rollback()
        # A new session verifies the released savepoint did not commit on its own.
        with Session(copied) as db:
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert month_used(db, c1) == used
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            assert accounting.claim_and_account_pool_document(db, doc, c1, owner) == (True, True)
            db.commit()
            assert accounting.claim_and_account_pool_document(db, doc, c1, owner) == (False, False)
            db.commit()
            assert month_used(db, c1) == used + 1
            assert db.execute(text("SELECT count(*) FROM unified_company_account_events WHERE document_id=:id"),
                              {'id': doc}).scalar_one() == 1
    finally:
        copied.dispose()


def test_unified_atomic_pick_concurrent_same_company(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_pool_accounting import claim_and_account_pool_document

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'atomic-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    barrier = Barrier(2)
    try:
        with sessions() as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            uid = db.query(User).filter_by(company_id=c1, role='accountant').first().id
            used = month_used(db, c1)
        def pick():
            with sessions() as db:
                user = db.get(User, uid)
                barrier.wait(timeout=10)
                result = claim_and_account_pool_document(db, doc, c1, user)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: pick(), range(2))) == [(False, False), (True, True)]
        with sessions() as db:
            assert month_used(db, c1) == used + 1
            assert db.get(PoolReceipt, pid).assigned_company_id == c1
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
    finally:
        copied.dispose()


def test_unified_atomic_pick_concurrent_companies(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_pool_accounting import claim_and_account_pool_document

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'atomic-companies.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    barrier = Barrier(2)
    try:
        with sessions() as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            uids = {c: db.query(User).filter_by(company_id=c, role='accountant').first().id for c in (c1, c2)}
            before = {c: month_used(db, c) for c in (c1, c2)}
        def pick(company):
            with sessions() as db:
                user = db.get(User, uids[company])
                barrier.wait(timeout=10)
                try:
                    result = claim_and_account_pool_document(db, doc, company, user)
                    db.commit()
                    return company, result
                except HTTPException as err:
                    db.rollback()
                    return company, err.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = dict(executor.map(pick, (c1, c2)))
        winner = next(c for c, result in results.items() if result == (True, True))
        loser = c2 if winner == c1 else c1
        assert results[loser] == 409
        with sessions() as db:
            assert db.get(PoolReceipt, pid).assigned_company_id == winner
            assert month_used(db, winner) == before[winner] + 1
            assert month_used(db, loser) == before[loser]
            assert db.execute(text('SELECT company_id FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == winner
            assert db.execute(text('SELECT count(*) FROM unified_company_account_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
    finally:
        copied.dispose()


@pytest.mark.parametrize('field', ['merchant_name', 'merchant_inn', 'merchant_address'])
@pytest.mark.parametrize('change_after_claim', [False, True])
def test_unified_pool_rejects_stale_seller_without_disclosure(setup_bridge, tmp_path, field, change_after_claim):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view, promote_pool_claim, update_account_private
    from qa.unified_claims import claim_pool_document
    from qa.unified_pool_accounting import claim_and_account_pool_document
    from qa.unified_receipt_preview import company_view

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'seller-changed.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            before = month_used(db, c1)
            if change_after_claim:
                assert claim_and_account_pool_document(db, doc, c1, owner) == (True, True)
                db.commit()
                assert account_view(db, doc, c1, owner)['origin'] == 'unified_pool'
                assert company_view(db, doc, owner, c1)['origin'] == 'pool'
            setattr(db.get(PoolReceipt, pid), field, 'changed sensitive seller value')
            db.commit()
            actions = [lambda: claim_pool_document(db, doc, c1, owner)]
            if change_after_claim:
                actions += [lambda: promote_pool_claim(db, doc, c1, owner),
                            lambda: account_view(db, doc, c1, owner),
                            lambda: company_view(db, doc, owner, c1),
                            lambda: update_account_private(db, doc, c1, owner, comment='not saved')]
            for action in actions:
                with pytest.raises(HTTPException) as err:
                    action()
                assert err.value.status_code == 409
                assert 'changed sensitive seller value' not in str(err.value.detail)
                db.rollback()
            assert month_used(db, c1) == before + int(change_after_claim)
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == int(change_after_claim)
            if change_after_claim:
                assert db.execute(text('SELECT comment FROM unified_company_accounts WHERE document_id=:id'),
                                  {'id': doc}).scalar_one() == ''
    finally:
        copied.dispose()


def test_unified_company_projection_refuses_new_conflicting_registry_receipt(setup_bridge, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view
    from qa.unified_pool_accounting import claim_and_account_pool_document
    from qa.unified_receipt_preview import company_view

    _, _, _, c1, c2, pid, _, fn, fd, fp = setup_bridge
    path = tmp_path / 'registry-conflict.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            assert claim_and_account_pool_document(db, doc, c1, owner) == (True, True)
            db.commit()
            pool = db.get(PoolReceipt, pid)
            db.add(Receipt(fn=fn, fd=fd, fp=fp, qr_data=pool.qr_data, company_id=c2,
                           receipt_date=pool.receipt_date, total_sum=pool.total_sum,
                           operation=pool.operation, comment='other company private note'))
            db.commit()
            for action in (lambda: company_view(db, doc, owner, c1),
                           lambda: account_view(db, doc, c1, owner)):
                with pytest.raises(HTTPException) as err:
                    action()
                assert err.value.status_code == 409
                assert 'other company' not in str(err.value.detail)
            # Conflict detection does not erase the new registry record or the earlier claim.
            assert db.query(Receipt).filter_by(fn=fn, company_id=c2).count() == 1
            assert db.execute(text('SELECT company_id FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == c1
    finally:
        copied.dispose()


@pytest.mark.parametrize('change_after_claim', [False, True])
def test_unified_pool_rejects_damaged_canonical_item_refs(setup_bridge, tmp_path, change_after_claim):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.router_company import month_used
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view, promote_pool_claim
    from qa.unified_claims import claim_pool_document
    from qa.unified_pool_accounting import claim_and_account_pool_document
    from qa.unified_receipt_preview import company_view

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'damaged-item-set.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            used = month_used(db, c1)
            if change_after_claim:
                assert claim_and_account_pool_document(db, doc, c1, owner) == (True, True)
                db.commit()
            db.execute(text('DELETE FROM unified_fiscal_item_refs WHERE document_id=:doc'), {'doc': doc})
            db.commit()
            actions = [lambda: claim_pool_document(db, doc, c1, owner)]
            if change_after_claim:
                actions += [lambda: promote_pool_claim(db, doc, c1, owner),
                            lambda: account_view(db, doc, c1, owner),
                            lambda: company_view(db, doc, owner, c1)]
            for action in actions:
                with pytest.raises(HTTPException) as err:
                    action()
                assert err.value.status_code == 409
                db.rollback()
            assert month_used(db, c1) == used + int(change_after_claim)
            assert db.get(PoolReceipt, pid).assigned_company_id == (c1 if change_after_claim else None)
    finally:
        copied.dispose()


def test_unified_account_view_uses_one_sqlite_read_snapshot(setup_bridge, tmp_path):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view
    from qa.unified_pool_accounting import claim_and_account_pool_document

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'consistent-read.db'
    migrate_copy(engine.url.database, path)
    # WAL permits a writer to commit while the account reader holds a snapshot.
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            assert claim_and_account_pool_document(db, doc, c1, owner) == (True, True)
            db.commit()
            original_name = db.get(PoolReceipt, pid).merchant_name
            db.commit()
            changed = []
            def write_between_queries(_conn, _cursor, statement, _parameters, _context, _executemany):
                if 'FROM unified_company_accounts WHERE document_id' in statement and not changed:
                    changed.append(True)
                    with sqlite3.connect(path) as writer:
                        writer.execute("UPDATE pool_receipts SET merchant_name='new name' WHERE id=?", (pid,))
            event.listen(copied, 'after_cursor_execute', write_between_queries)
            try:
                view = account_view(db, doc, c1, owner)
                assert changed == [True]
                assert view['receipt']['merchant_name'] == original_name
            finally:
                event.remove(copied, 'after_cursor_execute', write_between_queries)
            db.commit()
            with pytest.raises(HTTPException) as err:
                account_view(db, doc, c1, owner)
            assert err.value.status_code == 409
            db.rollback()
    finally:
        copied.dispose()


def test_pool_transfer_requires_fns_confirmation_date(client, setup_bridge, monkeypatch):
    _, a1, _, _, _, pid, _, fn, _, _ = setup_bridge
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS request'))
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        pool.fns_confirmed = True
        pool.fns_checked_at = None
        db.commit()
    assert client.post('/api/v1/pool-company/assign', headers=a1,
                       json={'receipt_ids': [pid]}).status_code == 200
    response = client.post('/api/v1/pool-company/transfer', headers=a1, json={'receipt_ids': [pid]})
    assert response.status_code == 200 and response.json()['unconfirmed'] == 1
    with SessionLocal() as db:
        receipt = db.query(Receipt).filter_by(fn=fn).one()
        assert receipt.fns_status == 'unknown' and receipt.status == 'pending'
        assert receipt.fns_checked_at is None


def test_idempotent_transfer_rechecks_source_access(setup_bridge, client):
    from fastapi import HTTPException

    from app.models import User
    from app.pool.transfer import copy_to_registry

    _, a1, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    assert client.post('/api/v1/pool-company/assign', headers=a1,
                       json={'receipt_ids': [pid]}).status_code == 200
    assert client.post('/api/v1/pool-company/transfer', headers=a1,
                       json={'receipt_ids': [pid]}).status_code == 200
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        actor = db.query(User).filter_by(company_id=c1, role='accountant').first()
        assert db.query(Receipt).filter_by(fn=fn, company_id=c1).count() == 1
        # Simulate a state change after the caller's initial pool selection.
        pool.assigned_company_id = c2
        db.flush()
        with pytest.raises(HTTPException) as denied:
            copy_to_registry(db, pool, c1, actor)
        assert denied.value.status_code == 409
        db.rollback()
        pool.status = 'rejected'
        db.flush()
        with pytest.raises(HTTPException) as denied:
            copy_to_registry(db, pool, c1, actor)
        assert denied.value.status_code == 409
        db.rollback()


def test_transfer_race_rechecks_assignment_before_returning_existing(client, setup_bridge, monkeypatch):
    from app.services import receipt_identity

    _, a1, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    assert client.post('/api/v1/pool-company/assign', headers=a1,
                       json={'receipt_ids': [pid]}).status_code == 200
    assert client.post('/api/v1/pool-company/transfer', headers=a1,
                       json={'receipt_ids': [pid]}).status_code == 200
    original_lock = receipt_identity.lock_identity
    switched = []
    def change_before_lock(db, key_fn, fd, fp):
        if key_fn == fn and not switched:
            switched.append(True)
            with SessionLocal() as other:
                other.get(PoolReceipt, pid).assigned_company_id = c2
                other.commit()
        return original_lock(db, key_fn, fd, fp)
    monkeypatch.setattr(receipt_identity, 'lock_identity', change_before_lock)
    response = client.post('/api/v1/pool-company/transfer', headers=a1, json={'receipt_ids': [pid]})
    assert switched == [True]
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.query(Receipt).filter_by(fn=fn, company_id=c1).count() == 1
        assert db.get(PoolReceipt, pid).assigned_company_id == c2


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '5764.001'])
def test_scan_rejects_nonfinite_or_different_pool_amount_without_using_quota(client, setup_bridge, amount):
    _, a1, _, c1, _, pid, qr, fn, _, _ = setup_bridge
    from app.pool.router_company import month_used
    with SessionLocal() as db:
        before = month_used(db, c1)
    response = client.post('/api/v1/receipts/scan', headers=a1,
                           json={'qr_data': qr.replace('s=5764.00', 's=' + amount)})
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.get(PoolReceipt, pid).assigned_company_id is None
        assert db.query(Receipt).filter_by(fn=fn).count() == 0
        assert month_used(db, c1) == before


def _create_legacy_registry_for_item_resolution(c1, pid, *, mismatch=''):
    """Подготовить исторический дубликат только для экспериментальной копии."""
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        receipt = Receipt(qr_data=pool.qr_data, fn=pool.fn, fd=pool.fd, fp=pool.fp,
                          company_id=c1, receipt_date=pool.receipt_date, total_sum=pool.total_sum,
                          operation=pool.operation, merchant_name=pool.merchant_name,
                          merchant_inn=pool.merchant_inn, merchant_address=pool.merchant_address,
                          comment='private accounting note', raw_data='{"private": "company secret"}')
        if mismatch == 'seller':
            receipt.merchant_name = 'different seller'
        if mismatch == 'amount':
            receipt.total_sum += 0.01
        db.add(receipt)
        db.flush()
        for item in db.query(PoolItem).filter_by(receipt_id=pid).all():
            db.add(ReceiptItem(receipt_id=receipt.id, name=item.name, quantity=item.quantity,
                               price=item.price, total=item.total, vat_rate=item.vat_rate,
                               vat_sum=item.vat_sum, extra_json='{"marking": ["private-extra"]}'))
        if mismatch == 'item':
            pool_item = db.query(PoolItem).filter_by(receipt_id=pid).first()
            pool_item.price += 0.01
        if mismatch == 'missing_item':
            db.query(PoolItem).filter_by(receipt_id=pid).delete()
        if mismatch == 'owner':
            pool.assigned_company_id = 'different-company'
        db.commit()
        return receipt.id


def test_matching_legacy_sources_can_be_resolved_without_copying_or_losing_data(setup_bridge, tmp_path, monkeypatch):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_item_resolution import resolve_matching_items
    from qa.unified_receipt_preview import company_view

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid)
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS request'))
    path = tmp_path / 'resolved-items.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            accountant = db.query(User).filter_by(company_id=c1, role='accountant').first()
            with pytest.raises(HTTPException) as denied:
                resolve_matching_items(db, doc, accountant)
            assert denied.value.status_code == 403
            assert resolve_matching_items(db, doc, admin) is True
            db.rollback()
            assert db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 'needs_review'
            assert resolve_matching_items(db, doc, admin) is True
            db.commit()
            assert resolve_matching_items(db, doc, admin) is False
            db.commit()
            assert db.execute(text('SELECT state,origin_kind,origin_id FROM unified_fiscal_item_sets '
                                   'WHERE document_id=:id'), {'id': doc}).one() == ('resolved_registry', 'registry', rid)
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_item_refs WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 2
            event = db.execute(text('SELECT method,actor_id FROM unified_item_resolution_events '
                                    'WHERE document_id=:id'), {'id': doc}).one()
            assert event == ('equal_common_fields', admin.id)
            assert 'private' not in str(event)
            assert db.query(Receipt).filter_by(fn=fn).count() == 1
            assert db.query(PoolReceipt).filter_by(fn=fn).count() == 1
            assert db.query(PoolItem).filter_by(receipt_id=pid).count() == 2
            assert db.query(ReceiptItem).filter_by(receipt_id=rid).count() == 2
            assert db.query(ReceiptItem).filter_by(receipt_id=rid).first().extra_json == '{"marking": ["private-extra"]}'
            assert company_view(db, doc, accountant, c1)['receipt']['comment'] == 'private accounting note'
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        copied.dispose()


@pytest.mark.parametrize('mismatch', ['seller', 'item', 'missing_item', 'amount', 'owner'])
def test_item_resolution_refuses_ambiguous_or_conflicting_sources(setup_bridge, tmp_path, mismatch):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_resolution import resolve_matching_items

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch=mismatch)
    path = tmp_path / 'unresolved-items.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            with pytest.raises(HTTPException) as err:
                resolve_matching_items(db, doc, admin)
            assert err.value.status_code == 409
            db.rollback()
            assert db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 'needs_review'
            assert db.execute(text('SELECT count(*) FROM unified_item_resolution_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.query(Receipt).filter_by(id=rid).one().comment == 'private accounting note'
            assert db.get(PoolReceipt, pid) is not None
    finally:
        copied.dispose()


def test_item_resolution_preserves_donor_consent_history_and_private_data(setup_bridge, tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolConsent, PoolPoint
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_resolution import resolve_matching_items
    from qa.unified_receipt_preview import company_view, donor_view

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        donor = PoolUser(vid='resolution_' + uuid4().hex, points=7)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded, pool.raw = donor_id, 7, '{"email":"private-donor@example.com"}'
        db.add(PoolPoint(user_id=donor_id, delta=7, reason='receipt', ref_id=pid))
        db.add(PoolConsent(vid=donor.vid, user_id=donor_id, offerta_version='1'))
        db.commit()
    path = tmp_path / 'resolution-privacy.db'
    copied = None
    try:
        migrate_copy(engine.url.database, path)
        copied = create_engine('sqlite:///' + str(path))
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            owner = db.query(User).filter_by(company_id=c1, role='accountant').first()
            assert resolve_matching_items(db, doc, admin)
            db.commit()
            assert db.get(PoolUser, donor_id).points == 7
            assert db.query(PoolPoint).filter_by(user_id=donor_id, ref_id=pid).one().delta == 7
            assert db.query(PoolConsent).filter_by(user_id=donor_id).one().offerta_version == '1'
            assert 'private-donor@example.com' not in str(company_view(db, doc, owner, c1))
            assert 'private accounting note' not in str(donor_view(db, doc, db.get(PoolUser, donor_id)))
            assert db.get(PoolReceipt, pid).raw == '{"email":"private-donor@example.com"}'
    finally:
        if copied is not None:
            copied.dispose()
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).pool_user_id = None
            db.query(PoolPoint).filter_by(user_id=donor_id).delete()
            db.query(PoolConsent).filter_by(user_id=donor_id).delete()
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


def test_item_resolution_concurrent_admins_is_idempotent(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_resolution import resolve_matching_items

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid)
    path = tmp_path / 'resolution-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    barrier = Barrier(2)
    try:
        with sessions() as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            uid = db.query(User).filter_by(role='admin').first().id
        def resolve():
            with sessions() as db:
                actor = db.get(User, uid)
                barrier.wait(timeout=10)
                result = resolve_matching_items(db, doc, actor)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: resolve(), range(2))) == [False, True]
        with sessions() as db:
            assert db.execute(text('SELECT count(*) FROM unified_item_resolution_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
    finally:
        copied.dispose()


@pytest.mark.parametrize('changed', ['registry_item', 'pool_item', 'seller', 'event', 'ref'])
def test_item_resolution_rejects_later_corruption(setup_bridge, tmp_path, changed):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_item_resolution import resolve_matching_items

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid)
    path = tmp_path / 'resolved-corrupt.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            assert resolve_matching_items(db, doc, admin)
            db.commit()
            if changed == 'registry_item':
                db.query(ReceiptItem).filter_by(receipt_id=rid).first().price += 1
            elif changed == 'pool_item':
                db.query(PoolItem).filter_by(receipt_id=pid).first().price += 1
            elif changed == 'seller':
                db.get(Receipt, rid).merchant_name = 'different now'
            elif changed == 'event':
                db.execute(text('DELETE FROM unified_item_resolution_events WHERE document_id=:id'), {'id': doc})
            else:
                db.execute(text('DELETE FROM unified_fiscal_item_refs WHERE document_id=:id'), {'id': doc})
            db.commit()
            with pytest.raises(HTTPException) as err:
                resolve_matching_items(db, doc, admin)
            assert err.value.status_code == 409
            db.rollback()
        with sqlite3.connect(path) as db:
            with pytest.raises(RuntimeError):
                _validate(db)
    finally:
        copied.dispose()


def test_item_resolution_failure_does_not_leave_partial_decision(setup_bridge, tmp_path):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_resolution import resolve_matching_items

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid)
    path = tmp_path / 'resolution-failure.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            def fail_event(_conn, _cursor, statement, _params, _context, _many):
                if statement.startswith('INSERT INTO unified_item_resolution_events'):
                    raise RuntimeError('simulated audit failure')
            event.listen(copied, 'before_cursor_execute', fail_event)
            try:
                with pytest.raises(RuntimeError, match='simulated audit failure'):
                    resolve_matching_items(db, doc, admin)
            finally:
                event.remove(copied, 'before_cursor_execute', fail_event)
            db.commit()  # Даже если вызывающий поймал ошибку, половина решения не фиксируется.
            assert db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 'needs_review'
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_item_refs WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.execute(text('SELECT count(*) FROM unified_item_resolution_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_reviewed_item_disagreement_preserves_both_sources(setup_bridge, tmp_path, monkeypatch):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).raw = '{"buyerEmail":"private-donor@example.com"}'
        db.commit()
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS request'))
    path = tmp_path / 'manual-review.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            accountant = db.query(User).filter_by(company_id=c1, role='accountant').first()
            with pytest.raises(HTTPException) as denied:
                preview_item_disagreement(db, doc, c1, accountant)
            assert denied.value.status_code == 403
            with pytest.raises(HTTPException) as denied:
                preview_item_disagreement(db, doc, c2, admin)
            assert denied.value.status_code == 404
            view = preview_item_disagreement(db, doc, c1, admin)
            assert view['seller_equal'] and not view['positions_equal']
            assert len(view['registry']['items']) == len(view['pool']['items']) == 2
            assert 'private accounting note' not in str(view)
            assert 'private-donor@example.com' not in str(view)
            assert 'private-extra' not in str(view)
            revision = view['revision']
            db.commit()
            with pytest.raises(HTTPException) as denied:
                resolve_reviewed_registry(db, doc, c1, admin, revision=revision, confirm=False, acknowledge_unreviewed=True)
            assert denied.value.status_code == 422
            with pytest.raises(HTTPException) as denied:
                resolve_reviewed_registry(db, doc, c1, admin, revision=revision, confirm=True)
            assert denied.value.status_code == 422
            assert resolve_reviewed_registry(db, doc, c1, admin, revision=revision, confirm=True, acknowledge_unreviewed=True)
            db.rollback()
            assert db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 'needs_review'
            assert resolve_reviewed_registry(db, doc, c1, admin, revision=revision, confirm=True, acknowledge_unreviewed=True)
            db.commit()
            assert not resolve_reviewed_registry(db, doc, c1, admin, revision=revision, confirm=True, acknowledge_unreviewed=True)
            db.commit()
            event = db.execute(text('SELECT method, review_digest, actor_id, acknowledged_incomplete '
                                    'FROM unified_item_resolution_events WHERE document_id=:id'), {'id': doc}).one()
            assert event == ('reviewed_registry', revision, admin.id, 1)
            assert 'private' not in str(event)
            assert db.query(PoolItem).filter_by(receipt_id=pid).count() == 2
            assert db.query(ReceiptItem).filter_by(receipt_id=rid).count() == 2
            assert db.query(Receipt).filter_by(id=rid).one().comment == 'private accounting note'
            assert db.get(PoolReceipt, pid).raw == '{"buyerEmail":"private-donor@example.com"}'
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_item_refs WHERE document_id=:id '
                                   "AND origin_kind='registry'"), {'id': doc}).scalar_one() == 2
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        copied.dispose()


@pytest.mark.parametrize('mismatch', ['seller', 'missing_item'])
def test_explicit_review_handles_seller_or_missing_pool_positions(setup_bridge, tmp_path, mismatch):
    import sqlite3

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch=mismatch)
    path = tmp_path / 'different-source.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            view = preview_item_disagreement(db, doc, c1, admin)
            assert (not view['seller_equal']) if mismatch == 'seller' else (not view['pool']['items'])
            db.commit()
            assert resolve_reviewed_registry(db, doc, c1, admin, revision=view['revision'], confirm=True, acknowledge_unreviewed=True)
            db.commit()
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        copied.dispose()


@pytest.mark.parametrize('change', ['registry_item', 'pool_item', 'registry_seller', 'pool_seller',
                                    'registry_extra', 'pool_extra', 'ownership', 'token'])
def test_review_rejects_stale_versions_and_does_not_create_decision(setup_bridge, tmp_path, change):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    path = tmp_path / 'stale-review.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            revision = preview_item_disagreement(db, doc, c1, admin)['revision']
            db.commit()
            if change == 'registry_item':
                db.query(ReceiptItem).filter_by(receipt_id=rid).first().price += 1
            elif change == 'pool_item':
                db.query(PoolItem).filter_by(receipt_id=pid).first().price += 1
            elif change == 'registry_seller':
                db.get(Receipt, rid).merchant_name = 'changed'
            elif change == 'pool_seller':
                db.get(PoolReceipt, pid).merchant_name = 'changed'
            elif change == 'registry_extra':
                db.get(Receipt, rid).ext_json = '{"new":"information"}'
            elif change == 'pool_extra':
                db.get(PoolReceipt, pid).extra_json = '{"new":"information"}'
            elif change == 'ownership':
                db.get(PoolReceipt, pid).assigned_company_id = c2
            else:
                revision = '0' * 64
            db.commit()
            with pytest.raises(HTTPException) as denied:
                resolve_reviewed_registry(db, doc, c1, admin, revision=revision, confirm=True, acknowledge_unreviewed=True)
            assert denied.value.status_code == 409
            db.rollback()
            assert db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 'needs_review'
            assert db.execute(text('SELECT count(*) FROM unified_item_resolution_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_review_decision_concurrent_same_administrator(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    path = tmp_path / 'review-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    barrier = Barrier(2)
    try:
        with sessions() as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            uid = admin.id
            revision = preview_item_disagreement(db, doc, c1, admin)['revision']
        def decide():
            with sessions() as db:
                actor = db.get(User, uid)
                barrier.wait(timeout=10)
                result = resolve_reviewed_registry(db, doc, c1, actor, revision=revision, confirm=True, acknowledge_unreviewed=True)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: decide(), range(2))) == [False, True]
        with sessions() as db:
            assert db.execute(text('SELECT count(*) FROM unified_item_resolution_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
    finally:
        copied.dispose()


def test_review_decision_audit_failure_rolls_back_savepoint(setup_bridge, tmp_path):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    path = tmp_path / 'review-failure.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            revision = preview_item_disagreement(db, doc, c1, actor)['revision']
            db.commit()
            def fail_event(_conn, _cursor, statement, _params, _context, _many):
                if statement.startswith('INSERT INTO unified_item_resolution_events'):
                    raise RuntimeError('simulated audit failure')
            event.listen(copied, 'before_cursor_execute', fail_event)
            try:
                with pytest.raises(RuntimeError, match='simulated audit failure'):
                    resolve_reviewed_registry(db, doc, c1, actor, revision=revision, confirm=True, acknowledge_unreviewed=True)
            finally:
                event.remove(copied, 'before_cursor_execute', fail_event)
            db.commit()
            assert db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 'needs_review'
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_item_refs WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.execute(text('SELECT count(*) FROM unified_item_resolution_events WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_manual_review_keeps_donor_consent_points_and_history(setup_bridge, tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolConsent, PoolPoint
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry
    from qa.unified_receipt_preview import donor_view

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    with SessionLocal() as db:
        donor = PoolUser(vid='manual_review_' + uuid4().hex, points=11)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded = donor_id, 11
        db.add(PoolPoint(user_id=donor_id, delta=11, reason='receipt', ref_id=pid))
        db.add(PoolConsent(vid=donor.vid, user_id=donor_id, offerta_version='1'))
        db.commit()
    path = tmp_path / 'review-donor.db'
    copied = None
    try:
        migrate_copy(engine.url.database, path)
        copied = create_engine('sqlite:///' + str(path))
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            view = preview_item_disagreement(db, doc, c1, admin)
            db.commit()
            assert resolve_reviewed_registry(db, doc, c1, admin, revision=view['revision'], confirm=True, acknowledge_unreviewed=True)
            db.commit()
            assert db.get(PoolUser, donor_id).points == 11
            assert db.query(PoolPoint).filter_by(user_id=donor_id, ref_id=pid).one().delta == 11
            assert db.query(PoolConsent).filter_by(user_id=donor_id).one().offerta_version == '1'
            assert 'private accounting note' not in str(donor_view(db, doc, db.get(PoolUser, donor_id)))
    finally:
        if copied is not None:
            copied.dispose()
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).pool_user_id = None
            db.query(PoolPoint).filter_by(user_id=donor_id).delete()
            db.query(PoolConsent).filter_by(user_id=donor_id).delete()
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


def test_review_snapshot_validator_detects_forged_digest(setup_bridge, tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    path = tmp_path / 'review-tampered.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            revision = preview_item_disagreement(db, doc, c1, actor)['revision']
            db.commit()
            assert resolve_reviewed_registry(db, doc, c1, actor, revision=revision, confirm=True, acknowledge_unreviewed=True)
            db.commit()
            db.execute(text("UPDATE unified_item_resolution_events SET review_digest=:digest WHERE document_id=:doc"),
                       {'digest': '0' * 64, 'doc': doc})
            db.commit()
        with sqlite3.connect(path) as db:
            with pytest.raises(RuntimeError, match='не соответствует просмотренному снимку'):
                _validate(db)
    finally:
        copied.dispose()


def test_review_shows_selected_registry_fiscal_metadata_without_raw_personal_values(setup_bridge, tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_item_review import preview_item_disagreement, resolve_reviewed_registry

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='item')
    with SessionLocal() as db:
        registry = db.get(Receipt, rid)
        registry.ext_json = '{"buyer":"registry-private@example.com"}'
        pool = db.get(PoolReceipt, pid)
        pool.extra_json = '{"buyer":"donor-private@example.com"}'
        item = db.query(ReceiptItem).filter_by(receipt_id=rid).first()
        item.product_type, item.payment_type, item.unit = 4, 1, 'кг'
        item.marking_present = True
        item.extra_json = '{"marking":[{"serial":"secret-serial"}]}'
        db.commit()
    path = tmp_path / 'metadata-review.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            view = preview_item_disagreement(db, doc, c1, admin)
            metadata = [i['fiscal_metadata'] for i in view['registry']['items'] if i['fiscal_metadata']['unit'] == 'кг']
            assert metadata == [{'product_type': 4, 'payment_type': 1, 'unit': 'кг',
                                 'marking_present': True, 'has_unreviewed_extra': True}]
            assert view['registry']['has_unreviewed_header_extra']
            assert view['pool']['has_unreviewed_header_extra']
            assert 'registry-private@example.com' not in str(view)
            assert 'donor-private@example.com' not in str(view)
            assert 'secret-serial' not in str(view)
            assert view['review_scope'] == 'common_fields_and_registry_metadata'
            db.commit()
            assert resolve_reviewed_registry(db, doc, c1, admin, revision=view['revision'],
                                              confirm=True, acknowledge_unreviewed=True)
            db.commit()
            saved = db.query(ReceiptItem).filter_by(receipt_id=rid, unit='кг').one()
            assert 'secret-serial' in saved.extra_json
            assert 'registry-private@example.com' in db.get(Receipt, rid).ext_json
            assert 'donor-private@example.com' in db.get(PoolReceipt, pid).extra_json
    finally:
        copied.dispose()


def test_fiscal_proposal_preserves_conflict_ownership_and_source_data(setup_bridge, tmp_path, monkeypatch):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).raw = '{"email":"private-donor@example.com"}'
        db.commit()
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS request'))
    path = tmp_path / 'fiscal-proposal.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            accountant = db.query(User).filter_by(company_id=c1, role='accountant').first()
            with pytest.raises(HTTPException) as err:
                preview_fiscal_conflict(db, doc, c1, accountant)
            assert err.value.status_code == 403
            with pytest.raises(HTTPException) as err:
                preview_fiscal_conflict(db, doc, c2, admin)
            assert err.value.status_code == 404
            view = preview_fiscal_conflict(db, doc, c1, admin)
            assert view['conflicts'] == {'receipt_date': False, 'total_sum': True, 'operation': False}
            assert view['sources']['pool']['total_sum'] == 5764
            assert view['sources']['registry']['total_sum'] == 5764.01
            assert 'private accounting note' not in str(view)
            assert 'private-donor@example.com' not in str(view)
            revision = view['revision']
            db.commit()
            with pytest.raises(HTTPException) as err:
                propose_fiscal_source(db, doc, c1, admin, origin_kind='pool', revision=revision,
                                      confirm=True, acknowledge_unverified=False)
            assert err.value.status_code == 422
            assert propose_fiscal_source(db, doc, c1, admin, origin_kind='pool', revision=revision,
                                         confirm=True, acknowledge_unverified=True)
            db.rollback()
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_resolution_proposals WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert propose_fiscal_source(db, doc, c1, admin, origin_kind='pool', revision=revision,
                                         confirm=True, acknowledge_unverified=True)
            db.commit()
            assert not propose_fiscal_source(db, doc, c1, admin, origin_kind='pool', revision=revision,
                                             confirm=True, acknowledge_unverified=True)
            db.commit()
            with pytest.raises(HTTPException) as err:
                propose_fiscal_source(db, doc, c1, admin, origin_kind='registry', revision=revision,
                                      confirm=True, acknowledge_unverified=True)
            assert err.value.status_code == 409
            db.rollback()
            proposal = db.execute(text('SELECT company_id, origin_kind, origin_id, acknowledged_unverified '
                                       'FROM unified_fiscal_resolution_proposals WHERE document_id=:id'),
                                  {'id': doc}).one()
            assert proposal == (c1, 'pool', pid, 1)
            assert db.execute(text('SELECT total_sum,review_mask FROM unified_fiscal_documents WHERE id=:id'),
                              {'id': doc}).one() == (None, 2)
            assert db.get(Receipt, rid).comment == 'private accounting note'
            assert db.get(Receipt, rid).fns_status == 'unknown'
            assert db.get(PoolReceipt, pid).raw == '{"email":"private-donor@example.com"}'
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            assert db.execute(text('SELECT company_id FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == c1
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        copied.dispose()


@pytest.mark.parametrize('changed', ['before_preview', 'after_preview', 'pool_item', 'pool_extra', 'ownership', 'actor'])
def test_fiscal_proposal_rejects_stale_sources_or_lost_rights(setup_bridge, tmp_path, changed):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    path = tmp_path / 'stale-fiscal.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            if changed == 'before_preview':
                db.get(Receipt, rid).total_sum += 1
                db.commit()
                with pytest.raises(HTTPException) as err:
                    preview_fiscal_conflict(db, doc, c1, admin)
                assert err.value.status_code == 409
                db.rollback()
            else:
                revision = preview_fiscal_conflict(db, doc, c1, admin)['revision']
                db.commit()
                if changed == 'after_preview':
                    db.get(Receipt, rid).total_sum += 1
                elif changed == 'pool_item':
                    db.query(PoolItem).filter_by(receipt_id=pid).first().total += 1
                elif changed == 'pool_extra':
                    db.get(PoolReceipt, pid).extra_json = '{"new":"information"}'
                elif changed == 'ownership':
                    db.get(PoolReceipt, pid).assigned_company_id = c2
                else:
                    admin.is_active = False
                db.commit()
                with pytest.raises(HTTPException) as err:
                    propose_fiscal_source(db, doc, c1, admin, origin_kind='pool', revision=revision,
                                          confirm=True, acknowledge_unverified=True)
                assert err.value.status_code == (403 if changed == 'actor' else 409)
                db.rollback()
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_resolution_proposals WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert db.execute(text('SELECT review_mask FROM unified_fiscal_documents WHERE id=:id'),
                              {'id': doc}).scalar_one() == 2
    finally:
        copied.dispose()


def test_fiscal_proposal_refuses_incomplete_source_and_conflicting_owner(setup_bridge, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).receipt_date = None
        db.commit()
    path = tmp_path / 'incomplete-fiscal.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            view = preview_fiscal_conflict(db, doc, c1, admin)
            assert view['conflicts']['receipt_date']
            assert not view['sources']['pool']['selectable'] and view['sources']['registry']['selectable']
            db.commit()
            with pytest.raises(HTTPException) as err:
                propose_fiscal_source(db, doc, c1, admin, origin_kind='pool', revision=view['revision'],
                                      confirm=True, acknowledge_unverified=True)
            assert err.value.status_code == 409
            db.rollback()
        # An ownership conflict on an independent source copy never creates a claim.
        other = tmp_path / 'ownership-conflict.db'
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).assigned_company_id = c2
            db.commit()
        migrate_copy(engine.url.database, other)
        conflict = create_engine('sqlite:///' + str(other))
        try:
            with Session(conflict) as db:
                doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
                admin = db.query(User).filter_by(role='admin').first()
                with pytest.raises(HTTPException) as err:
                    preview_fiscal_conflict(db, doc, c1, admin)
                assert err.value.status_code == 404
                assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                                  {'id': doc}).scalar_one() == 0
                assert db.execute(text('SELECT count(*) FROM unified_fiscal_resolution_proposals WHERE document_id=:id'),
                                  {'id': doc}).scalar_one() == 0
        finally:
            conflict.dispose()
    finally:
        copied.dispose()


def test_fiscal_proposal_competing_choices_record_only_one(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    path = tmp_path / 'fiscal-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    barrier = Barrier(2)
    try:
        with sessions() as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            uid, revision = actor.id, preview_fiscal_conflict(db, doc, c1, actor)['revision']
        def propose(kind):
            with sessions() as db:
                actor = db.get(User, uid)
                barrier.wait(timeout=10)
                try:
                    result = propose_fiscal_source(db, doc, c1, actor, origin_kind=kind, revision=revision,
                                                   confirm=True, acknowledge_unverified=True)
                    db.commit()
                    return result
                except HTTPException as err:
                    db.rollback()
                    return err.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(propose, ('pool', 'registry')))
        assert sorted(results) == [True, 409]
        with sessions() as db:
            assert db.execute(text('SELECT count(*) FROM unified_fiscal_resolution_proposals WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
            assert db.execute(text('SELECT total_sum,review_mask FROM unified_fiscal_documents WHERE id=:id'),
                              {'id': doc}).one() == (None, 2)
    finally:
        copied.dispose()


@pytest.mark.parametrize('change', ['proposal_digest', 'registry_core', 'pool_core'])
def test_fiscal_proposal_validator_rejects_corruption(setup_bridge, tmp_path, change):
    import sqlite3

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    path = tmp_path / 'fiscal-corruption.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            revision = preview_fiscal_conflict(db, doc, c1, actor)['revision']
            db.commit()
            assert propose_fiscal_source(db, doc, c1, actor, origin_kind='registry', revision=revision,
                                         confirm=True, acknowledge_unverified=True)
            db.commit()
            if change == 'registry_core':
                db.get(Receipt, rid).total_sum += 1
            elif change == 'pool_core':
                db.get(PoolReceipt, pid).operation = 2
            else:
                db.execute(text('UPDATE unified_fiscal_resolution_proposals SET review_digest=:digest '
                                'WHERE document_id=:doc'), {'digest': '0' * 64, 'doc': doc})
            db.commit()
        with sqlite3.connect(path) as db:
            with pytest.raises(RuntimeError):
                _validate(db)
    finally:
        copied.dispose()


def test_fiscal_proposal_handles_multiple_core_conflicts_without_fns_request(setup_bridge, tmp_path, monkeypatch):
    import sqlite3

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    with SessionLocal() as db:
        receipt = db.get(Receipt, rid)
        receipt.receipt_date = datetime(2026, 9, 21, 15, 56)
        receipt.operation = 2
        db.commit()
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS request'))
    path = tmp_path / 'three-conflicts.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            view = preview_fiscal_conflict(db, doc, c1, actor)
            assert view['conflicts'] == {'receipt_date': True, 'total_sum': True, 'operation': True}
            db.commit()
            assert propose_fiscal_source(db, doc, c1, actor, origin_kind='registry', revision=view['revision'],
                                         confirm=True, acknowledge_unverified=True)
            db.commit()
            assert db.execute(text('SELECT receipt_date,total_sum,operation,review_mask '
                                   'FROM unified_fiscal_documents WHERE id=:id'), {'id': doc}).one() == (
                                       None, None, None, 7)
            assert db.get(Receipt, rid).fns_status == 'unknown'
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        copied.dispose()


def test_fiscal_proposal_preserves_donor_consent_and_points(setup_bridge, tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolConsent, PoolPoint
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_fiscal_proposals import preview_fiscal_conflict, propose_fiscal_source
    from qa.unified_receipt_preview import donor_view

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    with SessionLocal() as db:
        donor = PoolUser(vid='fiscal_proposal_' + uuid4().hex, points=9)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded = donor_id, 9
        db.add(PoolPoint(user_id=donor_id, delta=9, reason='receipt', ref_id=pid))
        db.add(PoolConsent(vid=donor.vid, user_id=donor_id, offerta_version='1'))
        db.commit()
    path = tmp_path / 'fiscal-donor.db'
    copied = None
    try:
        migrate_copy(engine.url.database, path)
        copied = create_engine('sqlite:///' + str(path))
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            view = preview_fiscal_conflict(db, doc, c1, actor)
            db.commit()
            assert propose_fiscal_source(db, doc, c1, actor, origin_kind='registry', revision=view['revision'],
                                         confirm=True, acknowledge_unverified=True)
            db.commit()
            assert db.get(PoolUser, donor_id).points == 9
            assert db.query(PoolPoint).filter_by(user_id=donor_id, ref_id=pid).one().delta == 9
            assert db.query(PoolConsent).filter_by(user_id=donor_id).one().offerta_version == '1'
            assert 'private accounting note' not in str(donor_view(db, doc, db.get(PoolUser, donor_id)))
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
    finally:
        if copied is not None:
            copied.dispose()
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).pool_user_id = None
            db.query(PoolPoint).filter_by(user_id=donor_id).delete()
            db.query(PoolConsent).filter_by(user_id=donor_id).delete()
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


def test_ownership_conflict_review_never_creates_a_second_claim(setup_bridge, tmp_path, monkeypatch):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_ownership_review import (
        preview_ownership_conflict,
        propose_registry_owner_review,
    )

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        pool.assigned_company_id = c2
        pool.raw = '{"email":"private-donor@example.com"}'
        db.commit()
    monkeypatch.setattr('app.pool.ingest._fetch_details', lambda *a, **k: pytest.fail('FNS request'))
    path = tmp_path / 'ownership-conflict.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            accountant = db.query(User).filter_by(company_id=c1, role='accountant').first()
            with pytest.raises(HTTPException) as err:
                preview_ownership_conflict(db, doc, c1, accountant)
            assert err.value.status_code == 403
            with pytest.raises(HTTPException) as err:
                preview_ownership_conflict(db, doc, c2, admin)
            assert err.value.status_code == 404
            view = preview_ownership_conflict(db, doc, c1, admin)
            assert view['ownership_conflict'] and view['other_assignment_exists']
            assert not view['additional_fiscal_conflicts']
            assert c2 not in str(view)
            assert 'private-donor@example.com' not in str(view)
            assert 'private accounting note' not in str(view)
            revision = view['revision']
            db.commit()
            with pytest.raises(HTTPException) as err:
                propose_registry_owner_review(db, doc, c1, admin, revision=revision,
                                              confirm=True, acknowledge_no_transfer=False)
            assert err.value.status_code == 422
            assert propose_registry_owner_review(db, doc, c1, admin, revision=revision,
                                                 confirm=True, acknowledge_no_transfer=True)
            db.rollback()
            assert db.execute(text('SELECT count(*) FROM unified_ownership_review_proposals WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
            assert propose_registry_owner_review(db, doc, c1, admin, revision=revision,
                                                 confirm=True, acknowledge_no_transfer=True)
            db.commit()
            assert not propose_registry_owner_review(db, doc, c1, admin, revision=revision,
                                                     confirm=True, acknowledge_no_transfer=True)
            db.commit()
            event = db.execute(text('SELECT registry_company_id, registry_origin_id, pool_origin_id, '
                                    'acknowledged_no_transfer FROM unified_ownership_review_proposals '
                                    'WHERE document_id=:id'), {'id': doc}).one()
            assert event == (c1, rid, pid, 1)
            assert db.execute(text('SELECT review_mask FROM unified_fiscal_documents WHERE id=:id'),
                              {'id': doc}).scalar_one() == 8
            for table in ('unified_company_claims', 'unified_company_accounts'):
                assert db.execute(text(f'SELECT count(*) FROM {table} WHERE document_id=:id'),
                                  {'id': doc}).scalar_one() == 0
            assert db.get(Receipt, rid).company_id == c1
            assert db.get(Receipt, rid).comment == 'private accounting note'
            assert db.get(PoolReceipt, pid).assigned_company_id == c2
            assert db.get(PoolReceipt, pid).raw == '{"email":"private-donor@example.com"}'
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        copied.dispose()


@pytest.mark.parametrize('change', ['pool_owner', 'registry_owner', 'pool_core', 'registry_extra',
                                    'pool_item', 'actor', 'token'])
def test_ownership_review_refuses_stale_snapshot_or_lost_rights(setup_bridge, tmp_path, change):
    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_ownership_review import (
        preview_ownership_conflict,
        propose_registry_owner_review,
    )

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).assigned_company_id = c2
        db.commit()
    path = tmp_path / 'stale-ownership.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            admin = db.query(User).filter_by(role='admin').first()
            revision = preview_ownership_conflict(db, doc, c1, admin)['revision']
            db.commit()
            if change == 'pool_owner':
                db.get(PoolReceipt, pid).assigned_company_id = c1
            elif change == 'registry_owner':
                db.get(Receipt, rid).company_id = c2
            elif change == 'pool_core':
                db.get(PoolReceipt, pid).total_sum += 1
            elif change == 'registry_extra':
                db.get(Receipt, rid).ext_json = '{"new":"information"}'
            elif change == 'pool_item':
                db.query(PoolItem).filter_by(receipt_id=pid).first().price += 1
            elif change == 'actor':
                admin.is_active = False
            else:
                revision = '0' * 64
            db.commit()
            with pytest.raises(HTTPException) as err:
                propose_registry_owner_review(db, doc, c1, admin, revision=revision,
                                              confirm=True, acknowledge_no_transfer=True)
            assert err.value.status_code == (403 if change == 'actor' else 404 if change == 'registry_owner' else 409)
            db.rollback()
            for table in ('unified_ownership_review_proposals', 'unified_company_claims', 'unified_company_accounts'):
                assert db.execute(text(f'SELECT count(*) FROM {table} WHERE document_id=:id'),
                                  {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_ownership_review_concurrent_repeat_is_idempotent(setup_bridge, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_ownership_review import (
        preview_ownership_conflict,
        propose_registry_owner_review,
    )

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).assigned_company_id = c2
        db.commit()
    path = tmp_path / 'ownership-race.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False, 'timeout': 20})
    sessions = sessionmaker(bind=copied)
    barrier = Barrier(2)
    try:
        with sessions() as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            uid, revision = actor.id, preview_ownership_conflict(db, doc, c1, actor)['revision']
        def propose():
            with sessions() as db:
                actor = db.get(User, uid)
                barrier.wait(timeout=10)
                result = propose_registry_owner_review(db, doc, c1, actor, revision=revision,
                                                       confirm=True, acknowledge_no_transfer=True)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: propose(), range(2))) == [False, True]
        with sessions() as db:
            assert db.execute(text('SELECT count(*) FROM unified_ownership_review_proposals WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 1
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


@pytest.mark.parametrize('change', ['digest', 'pool_owner', 'registry_owner', 'core'])
def test_ownership_review_validator_rejects_corruption(setup_bridge, tmp_path, change):
    import sqlite3

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_ownership_review import (
        preview_ownership_conflict,
        propose_registry_owner_review,
    )

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    rid = _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).assigned_company_id = c2
        db.commit()
    path = tmp_path / 'ownership-corrupt.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            revision = preview_ownership_conflict(db, doc, c1, actor)['revision']
            db.commit()
            assert propose_registry_owner_review(db, doc, c1, actor, revision=revision,
                                                 confirm=True, acknowledge_no_transfer=True)
            db.commit()
            if change == 'pool_owner':
                db.get(PoolReceipt, pid).assigned_company_id = c1
            elif change == 'registry_owner':
                db.get(Receipt, rid).company_id = c2
            elif change == 'core':
                db.get(PoolReceipt, pid).total_sum += 1
            else:
                db.execute(text('UPDATE unified_ownership_review_proposals SET review_digest=:digest '
                                'WHERE document_id=:doc'), {'digest': '0' * 64, 'doc': doc})
            db.commit()
        with sqlite3.connect(path) as db:
            with pytest.raises(RuntimeError):
                _validate(db)
    finally:
        copied.dispose()


def test_ownership_review_preserves_donor_and_additional_fiscal_conflict(setup_bridge, tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from app.pool.models import PoolConsent, PoolPoint
    from qa.migrate_receipt_copy import _validate, migrate_copy
    from qa.unified_ownership_review import (
        preview_ownership_conflict,
        propose_registry_owner_review,
    )
    from qa.unified_receipt_preview import donor_view

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid, mismatch='amount')
    with SessionLocal() as db:
        donor = PoolUser(vid='ownership_review_' + uuid4().hex, points=6)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        pool = db.get(PoolReceipt, pid)
        pool.pool_user_id, pool.points_awarded, pool.assigned_company_id = donor_id, 6, c2
        db.add(PoolPoint(user_id=donor_id, delta=6, reason='receipt', ref_id=pid))
        db.add(PoolConsent(vid=donor.vid, user_id=donor_id, offerta_version='1'))
        db.commit()
    path = tmp_path / 'ownership-donor.db'
    copied = None
    try:
        migrate_copy(engine.url.database, path)
        copied = create_engine('sqlite:///' + str(path))
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(role='admin').first()
            view = preview_ownership_conflict(db, doc, c1, actor)
            assert view['additional_fiscal_conflicts']
            db.commit()
            assert propose_registry_owner_review(db, doc, c1, actor, revision=view['revision'],
                                                 confirm=True, acknowledge_no_transfer=True)
            db.commit()
            assert db.get(PoolUser, donor_id).points == 6
            assert db.query(PoolPoint).filter_by(user_id=donor_id, ref_id=pid).one().delta == 6
            assert db.query(PoolConsent).filter_by(user_id=donor_id).one().offerta_version == '1'
            assert 'private accounting note' not in str(donor_view(db, doc, db.get(PoolUser, donor_id)))
            assert db.execute(text('SELECT review_mask FROM unified_fiscal_documents WHERE id=:id'),
                              {'id': doc}).scalar_one() == 10
            assert db.execute(text('SELECT count(*) FROM unified_company_claims WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == 0
        with sqlite3.connect(path) as db:
            _validate(db)
    finally:
        if copied is not None:
            copied.dispose()
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).pool_user_id = None
            db.query(PoolPoint).filter_by(user_id=donor_id).delete()
            db.query(PoolConsent).filter_by(user_id=donor_id).delete()
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


def test_experimental_claim_trigger_refuses_conflicting_sources_even_if_mask_cleared(setup_bridge, tmp_path):
    import sqlite3

    from app.database import engine
    from qa.migrate_receipt_copy import migrate_copy

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    _create_legacy_registry_for_item_resolution(c1, pid)
    with SessionLocal() as db:
        db.get(PoolReceipt, pid).assigned_company_id = c2
        db.commit()
    path = tmp_path / 'claim-trigger.db'
    migrate_copy(engine.url.database, path)
    with sqlite3.connect(path) as db:
        doc = db.execute('SELECT id FROM unified_fiscal_documents WHERE fn=?', (fn,)).fetchone()[0]
        for owner in (c1, c2):
            with pytest.raises(sqlite3.IntegrityError, match='ownership conflict'):
                db.execute('INSERT INTO unified_company_claims(document_id, company_id) VALUES (?, ?)', (doc, owner))
        db.execute('UPDATE unified_fiscal_documents SET review_mask=review_mask & ~8 WHERE id=?', (doc,))
        for owner in (c1, c2):
            with pytest.raises(sqlite3.IntegrityError, match='ownership conflict'):
                db.execute('INSERT INTO unified_company_claims(document_id, company_id) VALUES (?, ?)', (doc, owner))
        assert db.execute('SELECT count(*) FROM unified_company_claims WHERE document_id=?', (doc,)).fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['claim', 'promote', 'update'])
@pytest.mark.parametrize('revocation', ['inactive', 'role', 'company', 'deleted'])
def test_unified_writes_recheck_actor_after_identity_lock(setup_bridge, tmp_path, monkeypatch, operation, revocation):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa import unified_accounts, unified_claims
    from qa.migrate_receipt_copy import migrate_copy

    _, _, _, c1, c2, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'revoked-actor.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(company_id=c1, role='accountant').first()
            uid = actor.id
            if operation in ('promote', 'update'):
                assert unified_claims.claim_pool_document(db, doc, c1, actor)
                if operation == 'update':
                    assert unified_accounts.promote_pool_claim(db, doc, c1, actor)
                db.commit()
            # Cache the actor before a separate transaction revokes access.
            assert actor.role == 'accountant' and actor.is_active and actor.company_id == c1
            module = unified_claims if operation == 'claim' else unified_accounts
            original_lock = module.lock_identity
            def revoke_before_lock(session, *identity):
                with sqlite3.connect(path) as writer:
                    if revocation == 'deleted':
                        writer.execute('DELETE FROM users WHERE id=?', (uid,))
                    else:
                        column, value = {'inactive': ('is_active', 0), 'role': ('role', 'user'),
                                         'company': ('company_id', c2)}[revocation]
                        writer.execute(f'UPDATE users SET {column}=? WHERE id=?', (value, uid))
                return original_lock(session, *identity)
            monkeypatch.setattr(module, 'lock_identity', revoke_before_lock)
            with pytest.raises(HTTPException) as denied:
                if operation == 'claim':
                    unified_claims.claim_pool_document(db, doc, c1, actor)
                elif operation == 'promote':
                    unified_accounts.promote_pool_claim(db, doc, c1, actor)
                else:
                    unified_accounts.update_account_private(db, doc, c1, actor, comment='not authorised')
            assert denied.value.status_code == 403
            db.rollback()
            assert db.get(PoolReceipt, pid).assigned_company_id == (None if operation == 'claim' else c1)
            assert db.execute(text('SELECT count(*) FROM unified_company_accounts WHERE document_id=:id'),
                              {'id': doc}).scalar_one() == (1 if operation == 'update' else 0)
            assert db.execute(text("SELECT count(*) FROM unified_company_account_events "
                                   "WHERE document_id=:id AND event_type='updated'"), {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


def test_unified_atomic_pick_rechecks_cached_revoked_actor(setup_bridge, tmp_path):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_pool_accounting import claim_and_account_pool_document

    _, _, _, c1, _, pid, _, fn, _, _ = setup_bridge
    path = tmp_path / 'atomic-revocation.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(company_id=c1, role='accountant').first()
            uid = actor.id
            assert actor.is_active
            with sqlite3.connect(path) as writer:
                writer.execute('UPDATE users SET is_active=0 WHERE id=?', (uid,))
            with pytest.raises(HTTPException) as denied:
                claim_and_account_pool_document(db, doc, c1, actor)
            assert denied.value.status_code == 403
            db.commit()
            assert db.get(PoolReceipt, pid).assigned_company_id is None
            for table in ('unified_company_claims', 'unified_company_accounts', 'unified_company_account_events'):
                assert db.execute(text(f'SELECT count(*) FROM {table} WHERE document_id=:id'),
                                  {'id': doc}).scalar_one() == 0
    finally:
        copied.dispose()


@pytest.mark.parametrize('projection', ['pool', 'account'])
@pytest.mark.parametrize('revocation', ['inactive', 'company', 'deleted'])
def test_unified_read_reloads_cached_actor(setup_bridge, tmp_path, projection, revocation):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from app.models import User
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_accounts import account_view
    from qa.unified_claims import claim_pool_document
    from qa.unified_pool_accounting import claim_and_account_pool_document
    from qa.unified_receipt_preview import company_view

    _, _, _, c1, c2, _, _, fn, _, _ = setup_bridge
    path = tmp_path / 'read-revoked.db'
    migrate_copy(engine.url.database, path)
    copied = create_engine('sqlite:///' + str(path))
    try:
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            actor = db.query(User).filter_by(company_id=c1, role='accountant').first()
            if projection == 'pool':
                assert claim_pool_document(db, doc, c1, actor)
            else:
                assert claim_and_account_pool_document(db, doc, c1, actor) == (True, True)
            db.commit()
            uid = actor.id
            assert actor.is_active and actor.company_id == c1
            with sqlite3.connect(path) as writer:
                if revocation == 'deleted':
                    writer.execute('DELETE FROM users WHERE id=?', (uid,))
                elif revocation == 'inactive':
                    writer.execute('UPDATE users SET is_active=0 WHERE id=?', (uid,))
                else:
                    writer.execute('UPDATE users SET company_id=? WHERE id=?', (c2, uid))
            with pytest.raises(HTTPException) as denied:
                (company_view(db, doc, actor, c1) if projection == 'pool'
                 else account_view(db, doc, c1, actor))
            assert denied.value.status_code == 404
            db.rollback()
    finally:
        copied.dispose()


def test_unified_donor_read_reloads_cached_block_status(setup_bridge, tmp_path):
    import sqlite3

    from fastapi import HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import engine
    from qa.migrate_receipt_copy import migrate_copy
    from qa.unified_receipt_preview import donor_view

    _, _, _, _, _, pid, _, fn, _, _ = setup_bridge
    with SessionLocal() as db:
        donor = PoolUser(vid='donor_revoked_' + uuid4().hex, points=0)
        db.add(donor)
        db.flush()
        donor_id = donor.id
        db.get(PoolReceipt, pid).pool_user_id = donor_id
        db.commit()
    path = tmp_path / 'donor-blocked.db'
    copied = None
    try:
        migrate_copy(engine.url.database, path)
        copied = create_engine('sqlite:///' + str(path))
        with Session(copied) as db:
            doc = db.execute(text('SELECT id FROM unified_fiscal_documents WHERE fn=:fn'), {'fn': fn}).scalar_one()
            donor = db.get(PoolUser, donor_id)
            assert not donor.is_blocked
            with sqlite3.connect(path) as writer:
                writer.execute('UPDATE pool_users SET is_blocked=1 WHERE id=?', (donor_id,))
            with pytest.raises(HTTPException) as denied:
                donor_view(db, doc, donor)
            assert denied.value.status_code == 404
            db.rollback()
    finally:
        if copied is not None:
            copied.dispose()
        with SessionLocal() as db:
            db.get(PoolReceipt, pid).pool_user_id = None
            db.delete(db.get(PoolUser, donor_id))
            db.commit()


@pytest.mark.parametrize('operation', ['assign', 'unassign', 'transfer'])
def test_batch_pool_actions_refuse_identity_changed_before_lock(client, setup_bridge, monkeypatch, operation):
    from app.pool.router_company import month_used
    from app.services import receipt_identity

    _, headers, _, c1, _, pid, _, fn, fd, fp = setup_bridge
    other_fn = '6' + str(int(uuid4().hex, 16) % 10**15).zfill(15)
    with SessionLocal() as db:
        other = PoolReceipt(fn=other_fn, fd=fd, fp=fp, receipt_date=datetime(2026, 9, 22, 15, 56),
                            total_sum=5764, operation=1, status='verified')
        db.add(other)
        db.flush()
        other_id = other.id
        db.commit()
    ids = [pid, other_id]
    try:
        if operation != 'assign':
            response = client.post('/api/v1/pool-company/assign', headers=headers, json={'receipt_ids': ids})
            assert response.status_code == 200
        with SessionLocal() as db:
            used_before = month_used(db, c1)
        original_lock = receipt_identity.lock_identity
        changed = []
        def change_after_selection(db, key_fn, key_fd, key_fp):
            if not changed:
                changed.append(True)
                with SessionLocal() as writer:
                    writer.get(PoolReceipt, pid).fp = '987654321'
                    writer.commit()
            return original_lock(db, key_fn, key_fd, key_fp)
        monkeypatch.setattr(receipt_identity, 'lock_identity', change_after_selection)
        monkeypatch.setattr('app.routers.receipts.check_receipt', lambda *a, **kw: pytest.fail('FNS called'))
        response = client.post('/api/v1/pool-company/' + operation, headers=headers, json={'receipt_ids': ids})
        assert changed == [True]
        assert response.status_code == 409
        with SessionLocal() as db:
            assert db.get(PoolReceipt, pid).fp == '987654321'
            assert db.get(PoolReceipt, pid).assigned_company_id == (None if operation == 'assign' else c1)
            assert db.get(PoolReceipt, other_id).assigned_company_id == (None if operation == 'assign' else c1)
            assert db.query(Receipt).filter(Receipt.fn.in_([fn, other_fn])).count() == 0
            assert month_used(db, c1) == used_before
            db.get(PoolReceipt, pid).fp = fp
            db.commit()
    finally:
        with SessionLocal() as db:
            db.query(ReceiptItem).filter(ReceiptItem.receipt_id.in_(
                db.query(Receipt.id).filter(Receipt.fn == other_fn))).delete(synchronize_session=False)
            db.query(Receipt).filter(Receipt.fn == other_fn).delete(synchronize_session=False)
            db.query(PoolReceipt).filter(PoolReceipt.id == other_id).delete(synchronize_session=False)
            db.commit()


def test_pool_identity_batch_lock_order_is_independent_of_input():
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.pool.router_company import _lock_pool_identities
    from app.services import receipt_identity

    rows = [SimpleNamespace(id='a', fn='z', fd='2', fp='1'),
            SimpleNamespace(id='b', fn='a', fd='2', fp='1'),
            SimpleNamespace(id='c', fn='z', fd='2', fp='1')]
    calls = []
    with patch.object(receipt_identity, 'lock_identity', lambda db, *identity: calls.append(identity)):
        snapshot = _lock_pool_identities(None, rows)
    assert calls == [('a', '2', '1'), ('z', '2', '1')]
    assert snapshot['a'] == snapshot['c'] == ('z', '2', '1')


@pytest.mark.parametrize('operation', ['assign', 'unassign', 'transfer'])
def test_pool_batch_action_fails_cleanly_when_selected_record_deleted(client, setup_bridge, monkeypatch, operation):
    from app.services import receipt_identity

    _, headers, _, c1, _, pid, _, fn, _, _ = setup_bridge
    if operation != 'assign':
        assert client.post('/api/v1/pool-company/assign', headers=headers,
                           json={'receipt_ids': [pid]}).status_code == 200
    lock = receipt_identity.lock_identity
    def delete_before_lock(db, *identity):
        with SessionLocal() as writer:
            writer.query(PoolItem).filter_by(receipt_id=pid).delete()
            writer.query(PoolReceipt).filter_by(id=pid).delete()
            writer.commit()
        return lock(db, *identity)
    monkeypatch.setattr(receipt_identity, 'lock_identity', delete_before_lock)
    response = client.post('/api/v1/pool-company/' + operation, headers=headers, json={'receipt_ids': [pid]})
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.get(PoolReceipt, pid) is None
        assert db.query(Receipt).filter_by(fn=fn, company_id=c1).count() == 0
