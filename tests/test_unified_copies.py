"""Фаза 4а (T-37): копии реестра и Чек-Пула сводятся с единой записью.

Только синтетические данные (ADR-14). Правку «старой версии» имитирует прямая
запись в таблицы реестра и пула мимо синхронизации — так меняет данные версия
без единого слоя (1.70.81 и раньше) после отката.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal
from app.models import Company, Receipt, ReceiptItem
from app.pool.models import PoolItem, PoolReceipt
from app.unified import backfill, copies, readiness, service
from app.unified.models import (
    REVIEW_AMOUNT,
    REVIEW_OWNERSHIP,
    UnifiedDocument,
    UnifiedEvent,
    UnifiedOrigin,
)
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity, _pool, _registry

# фикстура компаний и пользователей — общая с tests/test_unified_receipts.py
companies = _base.companies


def _register(db, *rows):
    for row in rows:
        kind = service.REGISTRY if isinstance(row, Receipt) else service.POOL
        service.register_source(db, kind, row, None, strict=True)
    db.commit()
    first = rows[0]
    kind = service.REGISTRY if isinstance(first, Receipt) else service.POOL
    return service.document_for_source(db, kind, first.id).id


def _reasons(db, doc_id):
    events = db.query(UnifiedEvent).filter_by(document_id=doc_id, event_type="reconcile").all()
    return [json.loads(e.details or "{}") for e in events]


def _drifting(db) -> set[str]:
    found = copies.candidates(db)
    return set(found[service.REGISTRY]) | set(found[service.POOL])


def _cleanup(db, *rows):
    for row in rows:
        if isinstance(row, Receipt):
            db.query(ReceiptItem).filter_by(receipt_id=row.id).delete(synchronize_session=False)
            db.query(Receipt).filter_by(id=row.id).delete(synchronize_session=False)
        else:
            db.query(PoolItem).filter_by(receipt_id=row.id).delete(synchronize_session=False)
            db.query(PoolReceipt).filter_by(id=row.id).delete(synchronize_session=False)
    db.commit()


def test_old_version_registry_edit_reaches_canon_and_pool(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        comp = db.get(Company, c1)
        reg = _registry(db, fn, fd, fp, company=comp)
        pool = _pool(db, fn, fd, fp, company=comp)
        db.flush()
        doc_id = _register(db, reg, pool)
        rid, pid = reg.id, pool.id
    with SessionLocal() as old:           # «старая версия» правит реестр напрямую
        row = old.get(Receipt, rid)
        row.total_sum, row.merchant_name = 250.0, "Магазин после отката"
        old.query(ReceiptItem).filter_by(receipt_id=rid).delete(synchronize_session=False)
        old.add(ReceiptItem(receipt_id=rid, name="Товар 2", quantity=2, price=125, total=250, position=0))
        old.commit()
    with SessionLocal() as db:
        assert doc_id in _drifting(db)
        result = copies.reconcile_copies(db)
        db.commit()
        assert result["accepted"] >= 1
    with SessionLocal() as db:
        doc = db.get(UnifiedDocument, doc_id)
        assert (doc.total_sum, doc.merchant_name) == (250.0, "Магазин после отката")
        assert [i.name for i in doc.items] == ["Товар 2"]
        copy = db.get(PoolReceipt, pid)
        assert (copy.total_sum, copy.merchant_name) == (250.0, "Магазин после отката")
        assert [i.name for i in db.query(PoolItem).filter_by(receipt_id=pid)] == ["Товар 2"]
        assert {"source_kind": "registry", "reason": "legacy_edit"} in _reasons(db, doc_id)
        assert doc.review_flags == 0
        assert doc_id not in _drifting(db)            # повторная сверка — без работы
        version = doc.version
        copies.reconcile_copies(db)
        db.commit()
        assert db.get(UnifiedDocument, doc_id).version == version
        _cleanup(db, db.get(Receipt, rid), copy)


def test_registry_without_items_keeps_canonical_items(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.flush()
        doc_id = _register(db, reg)
        rid = reg.id
        db.query(ReceiptItem).filter_by(receipt_id=rid).delete(synchronize_session=False)
        db.commit()
        assert doc_id not in _drifting(db)
        copies.reconcile_copies(db)
        db.commit()
        assert [i.name for i in db.get(UnifiedDocument, doc_id).items] == ["Товар"]
        _cleanup(db, db.get(Receipt, rid))


def test_pool_copy_changed_beside_registry_is_restored(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        comp = db.get(Company, c1)
        reg = _registry(db, fn, fd, fp, company=comp)
        pool = _pool(db, fn, fd, fp, company=comp)
        db.flush()
        doc_id = _register(db, reg, pool)
        rid, pid = reg.id, pool.id
        db.get(PoolReceipt, pid).total_sum = 999.0
        db.commit()
        copies.reconcile_copies(db)
        db.commit()
    with SessionLocal() as db:
        assert db.get(PoolReceipt, pid).total_sum == 100.0
        assert db.get(UnifiedDocument, doc_id).total_sum == 100.0
        assert any(r.get("reason") == "copy_restored" for r in _reasons(db, doc_id))
        _cleanup(db, db.get(Receipt, rid), db.get(PoolReceipt, pid))


@pytest.mark.usefixtures("companies")
def test_pool_only_document_accepts_pool_change():
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, fn, fd, fp)
        db.flush()
        doc_id = _register(db, pool)
        pid = pool.id
        db.get(PoolReceipt, pid).merchant_name = "Новое название"
        db.commit()
        copies.reconcile_copies(db)
        db.commit()
    with SessionLocal() as db:
        assert db.get(UnifiedDocument, doc_id).merchant_name == "Новое название"
        assert {"source_kind": "pool", "reason": "legacy_edit"} in _reasons(db, doc_id)
        _cleanup(db, db.get(PoolReceipt, pid))


def test_copies_changed_differently_go_to_review_without_overwrite(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        comp = db.get(Company, c1)
        reg = _registry(db, fn, fd, fp, company=comp)
        pool = _pool(db, fn, fd, fp, company=comp)
        db.flush()
        doc_id = _register(db, reg, pool)
        rid, pid = reg.id, pool.id
        db.get(Receipt, rid).total_sum = 300.0
        db.get(PoolReceipt, pid).total_sum = 400.0
        db.commit()
        result = copies.reconcile_copies(db)
        db.commit()
        assert result["review"] >= 1
    with SessionLocal() as db:
        doc = db.get(UnifiedDocument, doc_id)
        assert doc.review_flags & REVIEW_AMOUNT
        assert doc.total_sum == 100.0
        assert db.get(Receipt, rid).total_sum == 300.0
        assert db.get(PoolReceipt, pid).total_sum == 400.0
        assert any(r.get("reason") == "copies_diverged" for r in _reasons(db, doc_id))
        assert doc_id not in _drifting(db)           # на разборе — сверка не трогает
        _cleanup(db, db.get(Receipt, rid), db.get(PoolReceipt, pid))


def test_identity_change_is_accepted_and_clash_is_left_alone(companies):
    """Новая тройка копии реестра принимается; тройка чужого документа — 409 без изменений.

    Две копии реестра с одной тройкой невозможны (уникальный индекс), поэтому
    чужой документ — из Чек-Пула.
    """
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    fn2, fd2, fp2 = _identity()
    fn3, _, _ = _identity()
    with SessionLocal() as db:
        moved = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        other = _pool(db, fn2, fd2, fp2)
        db.flush()
        moved_doc, other_doc = _register(db, moved), _register(db, other)
        mid, oid = moved.id, other.id
        db.get(Receipt, mid).fn = fn3
        db.commit()
        copies.reconcile_copies(db)
        db.commit()
        assert db.get(UnifiedDocument, moved_doc).identity_key == service.identity_key(fn3, fd, fp)
        row = db.get(Receipt, mid)
        row.fn, row.fd, row.fp = fn2, fd2, fp2
        db.commit()
        result = copies.reconcile_copies(db)
        db.commit()
        assert result["conflicts"] >= 1
        assert db.get(UnifiedDocument, moved_doc).identity_key == service.identity_key(fn3, fd, fp)
        assert db.get(UnifiedDocument, other_doc).identity_key == service.identity_key(fn2, fd2, fp2)
        _cleanup(db, db.get(Receipt, mid), db.get(PoolReceipt, oid))


def test_fns_evidence_from_old_version_is_merged(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.flush()
        doc_id = _register(db, reg)
        rid = reg.id
        checked = datetime(2026, 11, 1, 12, 0)
        row = db.get(Receipt, rid)
        row.fns_status, row.fns_checked_at = "valid", checked
        db.commit()
        result = copies.reconcile_copies(db)
        db.commit()
        doc = db.get(UnifiedDocument, doc_id)
        assert (doc.fns_status, doc.fns_checked_at) == ("valid", checked)
        assert result["fns_merged"] >= 1
        row.fns_checked_at = checked + timedelta(days=1)
        db.commit()
        copies.reconcile_copies(db)
        db.commit()
        assert db.get(UnifiedDocument, doc_id).fns_checked_at == checked + timedelta(days=1)
        _cleanup(db, db.get(Receipt, rid))


def test_document_on_ownership_review_is_not_touched(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.flush()
        doc_id = _register(db, reg)
        rid = reg.id
        db.get(UnifiedDocument, doc_id).review_flags |= REVIEW_OWNERSHIP
        db.get(Receipt, rid).total_sum = 555.0
        db.commit()
        assert doc_id not in _drifting(db)
        copies.reconcile_copies(db)
        db.commit()
        assert db.get(UnifiedDocument, doc_id).total_sum == 100.0
        _cleanup(db, db.get(Receipt, rid))


def test_readiness_numbers_and_admin_only_api(companies, client):
    hdr, c1, _, a1, _ = companies
    fn, fd, fp = _identity()
    fn2, fd2, fp2 = _identity()
    with SessionLocal() as db:
        before = readiness.readiness(db)
        comp = db.get(Company, c1)
        linked = _registry(db, fn, fd, fp, company=comp)
        pending = _registry(db, fn2, fd2, fp2, company=comp)
        db.flush()
        _register(db, linked)
        after = readiness.readiness(db)
        assert after["registry"]["total"] == before["registry"]["total"] + 2
        assert after["registry"]["linked"] == before["registry"]["linked"] + 1
        assert after["registry"]["pending"] == before["registry"]["pending"] + 1
        assert set(after) == {"registry", "pool", "documents", "review", "drift", "ready_percent"}
        assert 0.0 <= after["ready_percent"] <= 100.0
        rows = (linked, pending)
    res = client.get("/api/v1/unified/readiness", headers=hdr)
    assert res.status_code == 200, res.text
    assert res.json()["registry"]["total"] == after["registry"]["total"]
    assert client.get("/api/v1/unified/readiness", headers=a1).status_code == 403
    with SessionLocal() as db:
        _cleanup(db, *(db.get(Receipt, r.id) for r in rows))


def test_soft_backfill_registers_only_clean_receipts(companies):
    _, c1, c2, _, _ = companies
    clean = _identity()
    pair = _identity()
    owners = _identity()
    with SessionLocal() as db:
        comp1, comp2 = db.get(Company, c1), db.get(Company, c2)
        clean_reg = _registry(db, *clean, company=comp1)
        pair_reg = _registry(db, *pair, company=comp1)
        pair_pool = _pool(db, *pair, total=777.0)           # данные расходятся
        own_reg = _registry(db, *owners, company=comp1)
        own_pool = _pool(db, *owners, company=comp2)        # спор о владельце
        db.commit()
        ids = {"clean": clean_reg.id, "pair_reg": pair_reg.id, "pair_pool": pair_pool.id,
               "own_reg": own_reg.id, "own_pool": own_pool.id}
    dry = backfill.soft_dry_run()
    assert dry["dry_run"] is True and dry["skipped_data"] >= 1 and dry["skipped_owner"] >= 1
    with SessionLocal() as db:                              # пробный прогон ничего не записал
        assert service.document_for_source(db, service.REGISTRY, ids["clean"]) is None
        result = backfill.soft_backfill(db)
        assert result["skipped_data"] >= 1 and result["skipped_owner"] >= 1
        linked = {k: service.document_for_source(
            db, service.POOL if k.endswith("pool") else service.REGISTRY, v) for k, v in ids.items()}
        assert linked["clean"] and linked["pair_reg"] and linked["own_reg"]
        assert linked["pair_pool"] is None and linked["own_pool"] is None
        for key in ("clean", "pair_reg", "own_reg"):
            assert linked[key].review_flags == 0             # пометок мягкая регистрация не ставит
        again = backfill.soft_backfill(db)                  # повторно — те же отказы, без новых документов
        assert again["registry_registered"] == 0 or again["registry_registered"] < result["registry_registered"] + 1
        _cleanup(db, *(db.get(Receipt, ids[k]) for k in ("clean", "pair_reg", "own_reg")),
                 *(db.get(PoolReceipt, ids[k]) for k in ("pair_pool", "own_pool")))


def test_every_write_path_keeps_copies_equal(companies, client):
    """Тест-инвариант: после скана QR, правки карточки, переноса из пула и перемещения копии совпадают."""
    hdr, c1, c2, a1, _ = companies
    fn, fd, fp = _identity()
    qr = f"t=20260922T1556&s=321.00&fn={fn}&i={fd}&fp={fp}&n=1"
    res = client.post("/api/v1/receipts/scan", json={"qr_data": qr, "source": "manual"}, headers=a1)
    assert res.status_code == 200, res.text
    rid = res.json()["receipt"]["id"]
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={
        "merchant_name": "После правки", "items": [{"name": "Позиция", "quantity": 1, "price": 321, "total": 321}]})
    assert res.status_code == 200, res.text
    pfn, pfd, pfp = _identity()
    with SessionLocal() as db:
        pool = _pool(db, pfn, pfd, pfp)
        db.flush()
        _register(db, pool)
        pid = pool.id
    res = client.post("/api/v1/pool-company/pick-and-transfer", headers=a1, json={"receipt_ids": [pid]})
    assert res.status_code == 200, res.text
    res = client.post("/api/v1/receipts/move", headers=hdr, json={"receipt_ids": [rid], "company_id": c2})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        docs = {service.document_for_source(db, service.REGISTRY, rid).id,
                service.document_for_source(db, service.POOL, pid).id}
        assert not docs & _drifting(db)
        transferred = (db.query(UnifiedOrigin)
                       .filter(UnifiedOrigin.document_id.in_(docs),
                               UnifiedOrigin.source_kind == service.REGISTRY).count())
        assert transferred == 2


def test_manual_entry_enrich_and_delete_keep_copies_equal(companies, client):
    """Тест-инвариант: ручной ввод, «Разобрать данные чеков» и удаление.

    Разбор сохранённого ответа ФНС дописывает адрес и кассира в реестр; до 1.70.88
    единая запись об этом не узнавала (расхождение до следующей правки).
    """
    hdr, c1, _, a1, _ = companies
    fn, fd, fp = _identity()
    res = client.post("/api/v1/receipts/manual", headers=a1, json={
        "date_time": "22.09.2026 15:56", "total_sum": 321.0, "fn": fn, "fd": fd, "fp": fp})
    assert res.status_code == 200, res.text
    rid = res.json()["receipt"]["id"]
    with SessionLocal() as db:
        doc_id = service.document_for_source(db, service.REGISTRY, rid).id
        assert doc_id not in _drifting(db)
        receipt = db.get(Receipt, rid)
        assert not receipt.cashier and not receipt.merchant_address
        receipt.raw_data = json.dumps({"fns": {"ticket": {"receipt": {
            "totalSum": 32100, "operator": "Кассир Сверкин",
            "retailPlaceAddress": "г. Тестовый, ул. Сверки, 1"}}}}, ensure_ascii=False)
        db.commit()
    res = client.post("/api/v1/receipts/enrich", headers=hdr, json={"only_empty": False, "company_id": c1})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        assert db.get(Receipt, rid).cashier == "Кассир Сверкин"
        doc = db.get(UnifiedDocument, doc_id)
        assert (doc.cashier, doc.merchant_address) == ("Кассир Сверкин", "г. Тестовый, ул. Сверки, 1")
        assert doc_id not in _drifting(db)
    res = client.delete(f"/api/v1/receipts/{rid}", headers=hdr)
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        assert db.get(UnifiedDocument, doc_id).deleted_at is not None
        assert doc_id not in _drifting(db)
        assert copies.reconcile_copies(db)["errors"] == 0


def test_reconcile_without_drift_is_cheap(companies):
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        comp = db.get(Company, c1)
        rows = [_registry(db, *_identity(), company=comp) for _ in range(600)]
        db.flush()
        service._register_registry_many(db, rows, None)
        db.commit()
        ids = [r.id for r in rows]
        started = time.perf_counter()
        result = copies.reconcile_copies(db)
        elapsed = time.perf_counter() - started
        db.commit()
        assert elapsed < 2.0, f"сверка без расхождений: {elapsed:.2f} с"
        assert result["errors"] == 0
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id.in_(ids)).delete(synchronize_session=False)
        db.query(Receipt).filter(Receipt.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def test_pool_labels_marked_by_admin_survive_reconcile(companies):
    """Метки классификации (регион, город, отрасль) сверка не возвращает."""
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        comp = db.get(Company, c1)
        reg = _registry(db, fn, fd, fp, company=comp)
        pool = _pool(db, fn, fd, fp, company=comp)
        db.flush()
        doc_id = _register(db, reg, pool)
        rid, pid = reg.id, pool.id
        marked = db.get(PoolReceipt, pid)
        marked.region_code, marked.city, marked.industry = "78", "Санкт-Петербург", "Продукты"
        db.commit()
        assert doc_id not in _drifting(db)
        copies.reconcile_copies(db)
        db.commit()
        assert (db.get(PoolReceipt, pid).industry, db.get(PoolReceipt, pid).city) == ("Продукты", "Санкт-Петербург")
        _cleanup(db, db.get(Receipt, rid), db.get(PoolReceipt, pid))


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="не задан UNIFIED_POSTGRES_TEST_URL")
def test_postgres_reconcile_readiness_and_soft_registration():
    """Сверка копий, отчёт готовности и мягкая регистрация — так же на PostgreSQL.

    Одноразовая схема и синтетические данные (ADR-14); транзакция откатывается.
    A — чек в реестре и пуле, старая версия правит сумму реестра; B — чек только
    в пуле, у него меняется лишь отрасль (метка, не сравнивается); C — чек только
    в пуле, правка адреса; D — архивный чек реестра без единой записи.
    """
    pg = create_engine(os.environ["UNIFIED_POSTGRES_TEST_URL"], pool_size=2, max_overflow=0)
    if pg.dialect.name != "postgresql":
        pg.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    schema = "qa_copies_" + uuid4().hex
    try:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            Base.metadata.create_all(bind=conn)
        with Session(pg) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            company = Company(name="PG Альфа")
            db.add(company)
            db.flush()
            fn, fd, fp = _identity()
            reg_a, pool_a = _registry(db, fn, fd, fp, company=company), _pool(db, fn, fd, fp, company=company)
            pool_b = _pool(db, *_identity()[:1], "4001", "4001")
            pool_c = _pool(db, *_identity()[:1], "4002", "4002")
            db.flush()
            for kind, row in ((service.REGISTRY, reg_a), (service.POOL, pool_a),
                              (service.POOL, pool_b), (service.POOL, pool_c)):
                service.register_source(db, kind, row, None, strict=True)
            archive = _registry(db, *_identity()[:1], "5001", "5001", company=company)
            db.flush()
            doc_a = service.document_for_source(db, service.REGISTRY, reg_a.id).id
            doc_c = service.document_for_source(db, service.POOL, pool_c.id).id
            # правки мимо единой записи — так пишет версия без единого слоя
            db.execute(update(Receipt).where(Receipt.id == reg_a.id).values(total_sum=333.0))
            db.execute(update(PoolReceipt).where(PoolReceipt.id == pool_b.id)
                       .values(industry="Разметка администратора"))
            db.execute(update(PoolReceipt).where(PoolReceipt.id == pool_c.id)
                       .values(merchant_address="Новый адрес"))
            db.expire_all()

            found = copies.candidates(db)
            assert (set(found[service.REGISTRY]), set(found[service.POOL])) == ({doc_a}, {doc_c})
            stats = copies.reconcile_copies(db)
            assert (stats["candidates"], stats["accepted"], stats["conflicts"], stats["errors"]) == (2, 2, 0, 0)
            db.expire_all()
            assert float(service.document_for_source(db, service.REGISTRY, reg_a.id).total_sum) == 333.0
            assert float(db.get(PoolReceipt, pool_a.id).total_sum) == 333.0
            assert service.document_for_source(db, service.POOL, pool_c.id).merchant_address == "Новый адрес"
            assert db.get(PoolReceipt, pool_b.id).industry == "Разметка администратора"
            assert {r.get("reason") for r in _reasons(db, doc_a)} == {"legacy_edit"}
            again = copies.reconcile_copies(db)
            assert all(v == 0 for v in again.values()), again

            report = readiness.readiness(db)
            assert report["registry"] == {"total": 2, "linked": 1, "pending": 1}
            assert report["pool"] == {"total": 3, "linked": 3, "pending": 0}
            assert report["documents"] == 3 and report["drift"] == {"registry": 0, "pool": 0}
            assert report["ready_percent"] == 80.0

            soft = backfill.soft_backfill(db, commit=False)
            assert soft["registry_registered"] == 1 and soft["pool_registered"] == 0
            assert service.document_for_source(db, service.REGISTRY, archive.id) is not None
            assert readiness.readiness(db)["ready_percent"] == 100.0
            db.rollback()
    finally:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.dispose()
