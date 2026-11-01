"""Единый фискальный документ: дедуп, владение, перенос, журнал, backfill."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models import Company, Receipt, ReceiptItem, User
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.services import appsettings
from app.unified import backfill, service
from app.unified.models import (
    REVIEW_OWNERSHIP,
    UnifiedDocument,
    UnifiedEvent,
    UnifiedItem,
    UnifiedOrigin,
)
from tests.conftest import login
from tests.test_v1140 import _mk_user


def _identity():
    digits = str(int(uuid4().hex, 16) % 10**15).zfill(15)
    return "7" + digits, "3467", "2882574203"


def _pool(db, fn, fd, fp, *, company=None, donor=None, total=100.0):
    row = PoolReceipt(
        pool_user_id=donor.id if donor else None,
        fn=fn, fd=fd, fp=fp, qr_data=f"t=20260922T1556&s={total}&fn={fn}&i={fd}&fp={fp}&n=1",
        receipt_date=datetime(2026, 9, 22, 15, 56), total_sum=total, operation=1,
        merchant_name="Магазин", merchant_inn="4703148343", status="verified",
        full_data=True, assigned_company_id=company.id if company else None,
    )
    db.add(row)
    db.flush()
    db.add(PoolItem(receipt_id=row.id, name="Товар", quantity=1, price=total, total=total))
    return row


def _registry(db, fn, fd, fp, *, company=None, user=None, total=100.0):
    row = Receipt(
        fn=fn, fd=fd, fp=fp, qr_data=f"t=20260922T1556&s={total}&fn={fn}&i={fd}&fp={fp}&n=1",
        receipt_date=datetime(2026, 9, 22, 15, 56), total_sum=total, operation=1,
        company_id=company.id if company else None, created_by=user.id if user else None,
        merchant_name="Магазин", merchant_inn="4703148343",
    )
    db.add(row)
    db.flush()
    db.add(ReceiptItem(receipt_id=row.id, name="Товар", quantity=1, price=total, total=total, position=0))
    return row


@pytest.fixture
def companies(client):
    hdr = login(client, "admin", "admin123")
    c1 = client.post("/api/v1/companies", headers=hdr, json={"name": "Unified A " + uuid4().hex}).json()["id"]
    c2 = client.post("/api/v1/companies", headers=hdr, json={"name": "Unified B " + uuid4().hex}).json()["id"]
    a1 = _mk_user(client, hdr, c1, "unified_a_" + uuid4().hex[:10], role="accountant")
    a2 = _mk_user(client, hdr, c2, "unified_b_" + uuid4().hex[:10], role="accountant")
    with SessionLocal() as db:
        appsettings.set_setting(db, "pool_enabled", "1")
        appsettings.set_setting(db, "pool_pick_monthly_limit", "100")
    yield hdr, c1, c2, a1, a2
    client.cookies.clear()


def test_backfill_merges_one_document_and_keeps_origins(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        comp = db.get(Company, c1)
        pool = _pool(db, fn, fd, fp, company=comp)
        reg = _registry(db, fn, fd, fp, company=comp)
        db.commit()
        result = backfill.backfill(db)
        assert result["registered"] >= 2
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        assert db.query(UnifiedOrigin).filter_by(document_id=doc.id).count() == 2
        assert db.query(UnifiedItem).filter_by(document_id=doc.id).count() == 1
        assert doc.ownership.company_id == c1
        assert doc.review_flags == 0
        assert backfill.backfill(db)["registered"] == 0
        db.query(PoolItem).filter_by(receipt_id=pool.id).delete()
        db.query(ReceiptItem).filter_by(receipt_id=reg.id).delete()
        db.query(PoolItem).filter_by(receipt_id=pool.id).delete(synchronize_session=False)
        db.query(ReceiptItem).filter_by(receipt_id=reg.id).delete(synchronize_session=False)
        db.query(Receipt).filter_by(id=reg.id).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pool.id).delete(synchronize_session=False)
        db.commit()


def test_backfill_quarantines_ownership_conflict(companies):
    _, c1, c2, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c2))
        db.commit()
        backfill.backfill(db)
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        assert doc.review_flags & REVIEW_OWNERSHIP
        assert doc.ownership.company_id is None
        db.query(PoolItem).filter_by(receipt_id=pool.id).delete()
        db.query(ReceiptItem).filter_by(receipt_id=reg.id).delete()
        db.query(PoolItem).filter_by(receipt_id=pool.id).delete(synchronize_session=False)
        db.query(ReceiptItem).filter_by(receipt_id=reg.id).delete(synchronize_session=False)
        db.query(Receipt).filter_by(id=reg.id).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pool.id).delete(synchronize_session=False)
        db.commit()


def test_transfer_does_not_create_second_canonical_document(companies, client):
    hdr, c1, _, a1, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        donor = PoolUser(vid="unified_" + uuid4().hex, points=5)
        db.add(donor)
        db.flush()
        pool = _pool(db, fn, fd, fp, donor=donor)
        pid = pool.id
        db.commit()
    res = client.post("/api/v1/pool-company/pick-and-transfer", headers=a1, json={"receipt_ids": [pid]})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        assert db.query(UnifiedOrigin).filter_by(document_id=doc.id).count() == 2
        assert db.query(UnifiedItem).filter_by(document_id=doc.id).count() == 1
        assert doc.ownership.company_id == c1
        assert db.get(PoolReceipt, pid).assigned_company_id == c1
        assert db.get(PoolUser, donor.id).points == 5  # перенос не начисляет баллы повторно
        events = db.query(UnifiedEvent).filter_by(document_id=doc.id).all()
        assert any(e.event_type == "transfer" for e in events)
        reg = db.query(Receipt).filter_by(fn=fn).one()
        db.delete(reg)
        db.query(PoolItem).filter_by(receipt_id=pid).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pid).delete(synchronize_session=False)
        db.commit()


def test_unified_move_updates_projections_and_journal(companies, client):
    hdr, c1, c2, a1, a2 = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.commit()
        backfill.backfill(db)
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        doc_id, version, rid, pid = doc.id, doc.version, reg.id, pool.id
    body = {"company_id": c2, "expected_version": version, "idempotency_key": uuid4().hex}
    res = client.post(f"/api/v1/unified/documents/{doc_id}/move", headers=hdr, json=body)
    assert res.status_code == 200, res.text
    assert res.json()["company_id"] == c2
    again = client.post(f"/api/v1/unified/documents/{doc_id}/move", headers=hdr, json=body)
    assert again.status_code == 200
    with SessionLocal() as db:
        assert db.get(Receipt, rid).company_id == c2
        assert db.get(PoolReceipt, pid).assigned_company_id == c2
        assert db.query(UnifiedEvent).filter_by(document_id=doc_id, event_type="move").count() == 1
    assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=a1).status_code == 404
    assert client.get(f"/api/v1/unified/documents/{doc_id}", headers=a2).status_code == 200
    with SessionLocal() as db:
        db.delete(db.get(Receipt, rid))
        db.query(PoolItem).filter_by(receipt_id=pid).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pid).delete(synchronize_session=False)
        db.commit()


def test_unified_move_rejects_stale_version_and_non_admin(companies, client):
    hdr, c1, c2, a1, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.commit()
        backfill.backfill(db)
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        doc_id, version, rid = doc.id, doc.version, reg.id
    res = client.post(f"/api/v1/unified/documents/{doc_id}/move", headers=a1,
                      json={"company_id": c2, "expected_version": version})
    assert res.status_code == 403
    res = client.post(f"/api/v1/unified/documents/{doc_id}/move", headers=hdr,
                      json={"company_id": c2, "expected_version": version + 1})
    assert res.status_code == 409
    with SessionLocal() as db:
        db.delete(db.get(Receipt, rid))
        db.commit()


def test_delete_registry_keeps_pool_canonical_history(companies, client):
    hdr, c1, _, a1, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.commit()
        backfill.backfill(db)
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        doc_id, rid, pid = doc.id, reg.id, pool.id
    res = client.delete(f"/api/v1/receipts/{rid}", headers=hdr)
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        doc = db.get(UnifiedDocument, doc_id)
        assert doc.deleted_at is None
        assert db.query(UnifiedOrigin).filter_by(document_id=doc_id).count() == 1
        assert db.get(PoolReceipt, pid) is not None
        db.query(PoolItem).filter_by(receipt_id=pid).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pid).delete(synchronize_session=False)
        db.commit()


def test_concurrent_unified_move_has_one_winner(companies):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi import HTTPException

    _, c1, c2, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        c3 = Company(name="Unified C " + uuid4().hex)
        db.add(c3)
        db.flush()
        c3_id = c3.id
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.commit()
        backfill.backfill(db)
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        doc_id, version, rid = doc.id, doc.version, reg.id
        admin_id = db.query(User).filter_by(username="admin").one().id
    barrier = Barrier(2)

    def run(target):
        with SessionLocal() as db:
            actor = db.get(User, admin_id)
            barrier.wait(timeout=10)
            try:
                service.move_document(db, doc_id, target, actor, expected_version=version)
                db.commit()
                return "ok"
            except HTTPException as exc:
                db.rollback()
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [c2, c3_id]))
    assert sorted(map(str, results)) == ["409", "ok"]
    with SessionLocal() as db:
        doc = db.get(UnifiedDocument, doc_id)
        assert doc.version == version + 1
        assert db.query(UnifiedEvent).filter_by(document_id=doc_id, event_type="move").count() == 1
        db.query(ReceiptItem).filter_by(receipt_id=rid).delete(synchronize_session=False)
        db.query(Receipt).filter_by(id=rid).delete(synchronize_session=False)
        db.commit()


def test_patch_registry_updates_canonical_and_pool_projection(companies, client):
    hdr, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp, company=db.get(Company, c1))
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.commit()
        backfill.backfill(db)
        doc = db.query(UnifiedDocument).filter_by(identity_key=service.identity_key(fn, fd, fp)).one()
        doc_id, rid, pid = doc.id, reg.id, pool.id
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr,
                       json={"total_sum": 250.0, "items": [
                           {"name": "Новый товар", "quantity": 2, "price": 125, "total": 250}]})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        doc = db.get(UnifiedDocument, doc_id)
        assert doc.total_sum == 250.0
        assert len(doc.items) == 1 and doc.items[0].name == "Новый товар"
        pool_items = db.query(PoolItem).filter_by(receipt_id=pid).all()
        assert len(pool_items) == 1 and pool_items[0].name == "Новый товар"
        assert db.get(PoolReceipt, pid).total_sum == 250.0
        db.query(PoolItem).filter_by(receipt_id=pid).delete(synchronize_session=False)
        db.query(ReceiptItem).filter_by(receipt_id=rid).delete(synchronize_session=False)
        db.query(Receipt).filter_by(id=rid).delete(synchronize_session=False)
        db.query(PoolReceipt).filter_by(id=pid).delete(synchronize_session=False)
        db.commit()
