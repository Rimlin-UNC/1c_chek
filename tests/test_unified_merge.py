"""Фаза 4б (T-38): правило слияния единой записи — пустое значение копии ничего не стирает.

До 1.70.89 копия реестра, которая знала о чеке меньше единой записи (например,
отсканирована по QR, а продавца и кассира Чек-Пул получил из ФНС), стирала эти
данные в единой записи и в Чек-Пуле: при любой правке бухгалтера (с 1.70.82) и при
сверке копий на старте (1.70.88). Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal
from app.models import Company, Receipt, ReceiptItem
from app.pool.models import PoolItem, PoolReceipt
from app.unified import copies, service
from app.unified.models import UnifiedDocument, UnifiedItem
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity, _pool, _registry

# фикстура компаний и пользователей — общая с tests/test_unified_receipts.py
companies = _base.companies

ADDRESS, CASHIER = "г. Пул, ул. Полная, 1", "Кассир Пулов"


def _rich_pool(db, fn, fd, fp, **extra):
    pool = _pool(db, fn, fd, fp)
    pool.merchant_address, pool.cashier = ADDRESS, CASHIER
    for field, value in extra.items():
        setattr(pool, field, value)
    db.flush()
    return pool


def _pool_first(db, company_id):
    """Чек сначала в Чек-Пуле (данные ФНС), затем компания сканирует QR — копия беднее."""
    fn, fd, fp = _identity()
    pool = _rich_pool(db, fn, fd, fp)
    service.register_source(db, service.POOL, pool, None, strict=True)
    reg = _registry(db, fn, fd, fp, company=db.get(Company, company_id))
    db.flush()
    service.register_source(db, service.REGISTRY, reg, None, strict=True)
    db.commit()
    return reg.id, pool.id


def _drifting(db) -> set[str]:
    found = copies.candidates(db)
    return set(found[service.REGISTRY]) | set(found[service.POOL])


def _canon(db, receipt_id):
    db.expire_all()
    return service.document_for_source(db, service.REGISTRY, receipt_id)


def test_reconcile_never_erases_what_the_registry_copy_does_not_know(companies):
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _pool_first(db, c1)
        doc_id = _canon(db, rid).id
        assert doc_id not in _drifting(db)
        copies.reconcile_copies(db)
        db.commit()
        doc = _canon(db, rid)
        assert (doc.merchant_address, doc.cashier) == (ADDRESS, CASHIER)
        assert (db.get(PoolReceipt, pid).merchant_address, db.get(PoolReceipt, pid).cashier) == (ADDRESS, CASHIER)
        assert db.get(Receipt, rid).merchant_address in ("", None)      # копию реестра не трогаем


def test_staff_edit_of_a_poorer_registry_copy_keeps_canon_and_pool(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _pool_first(db, c1)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"comment": "только комментарий"})
    assert res.status_code == 200, res.text
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"merchant_name": "Магазин после правки"})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        doc, pool = _canon(db, rid), db.get(PoolReceipt, pid)
        assert (doc.merchant_address, doc.cashier) == (ADDRESS, CASHIER)
        assert (pool.merchant_address, pool.cashier) == (ADDRESS, CASHIER)
        # правка бухгалтера — по-прежнему главная: дошла до единой записи и Чек-Пула
        assert doc.merchant_name == pool.merchant_name == "Магазин после правки"


def test_explicit_clear_in_the_card_reaches_canon_and_pool(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _pool_first(db, c1)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"cashier": "  "})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        doc, pool = _canon(db, rid), db.get(PoolReceipt, pid)
        assert doc.cashier == "" and pool.cashier == ""
        assert doc.merchant_address == pool.merchant_address == ADDRESS     # не очищали — осталось


def test_registry_first_then_richer_pool_fills_canon(companies):
    _, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        db.flush()
        service.register_source(db, service.REGISTRY, reg, None, strict=True)
        pool = _rich_pool(db, fn, fd, fp)
        service.register_source(db, service.POOL, pool, None, strict=True)
        db.commit()
        doc = _canon(db, reg.id)
        assert (doc.merchant_address, doc.cashier) == (ADDRESS, CASHIER)      # дополнена из пула
        copies.reconcile_copies(db)
        db.commit()
        assert db.get(PoolReceipt, pool.id).merchant_address == ADDRESS        # до 1.70.89 стиралось
        assert _canon(db, reg.id).cashier == CASHIER
        assert _canon(db, reg.id).id not in _drifting(db)


def test_canon_impoverished_by_an_older_version_is_refilled_from_the_pool(companies):
    """База после 1.70.82–1.70.88: единая запись уже обеднела, Чек-Пул ещё помнит."""
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _pool_first(db, c1)
        doc_id = _canon(db, rid).id
        db.execute(update(UnifiedDocument).where(UnifiedDocument.id == doc_id)
                   .values(merchant_address="", cashier=""))
        db.commit()
        assert doc_id in copies.candidates(db)[service.POOL]
        out = copies.reconcile_copies(db)
        db.commit()
        assert out["restored"] >= 1 and out["errors"] == 0
        doc = _canon(db, rid)
        assert (doc.merchant_address, doc.cashier) == (ADDRESS, CASHIER)
        assert db.get(PoolReceipt, pid).merchant_address == ADDRESS
        assert doc_id not in _drifting(db)


def test_pool_labels_are_not_overwritten_by_a_company_edit(companies, client):
    hdr, c1, _, _, _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        marked = _rich_pool(db, fn, fd, fp, city="Разметка пула", industry="Отрасль пула", region_code="77")
        service.register_source(db, service.POOL, marked, None, strict=True)
        reg = _registry(db, fn, fd, fp, company=db.get(Company, c1))
        reg.industry, reg.region_code, reg.region_city = "Прочие покупки", "50", "Город компании"
        db.flush()
        service.register_source(db, service.REGISTRY, reg, None, strict=True)
        db.commit()
        rid, pid = reg.id, marked.id
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"total_sum": 222.0})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        assert float(pool.total_sum) == 222.0                                   # факт — общий
        assert (pool.city, pool.industry, pool.region_code) == ("Разметка пула", "Отрасль пула", "77")
        # пустые метки пула заполняются из единой записи
        pool.city, pool.industry = "", ""
        db.commit()
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"total_sum": 223.0})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        assert pool.industry and pool.city and pool.region_code == "77"


def test_pool_only_copy_that_lost_a_field_is_refilled_not_accepted(companies):
    _ = companies
    fn, fd, fp = _identity()
    with SessionLocal() as db:
        pool = _rich_pool(db, fn, fd, fp)
        service.register_source(db, service.POOL, pool, None, strict=True)
        db.commit()
        db.execute(update(PoolReceipt).where(PoolReceipt.id == pool.id).values(cashier=""))
        db.commit()
        copies.reconcile_copies(db)
        db.commit()
        db.expire_all()
        doc = service.document_for_source(db, service.POOL, pool.id)
        assert doc.cashier == CASHIER and db.get(PoolReceipt, pool.id).cashier == CASHIER
        assert doc.id not in _drifting(db)


def test_pool_items_survive_when_the_canon_has_none(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _pool_first(db, c1)
        doc_id = _canon(db, rid).id
        # у копии реестра и у единой записи позиций нет, у Чек-Пула — есть
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == rid).delete()
        db.query(UnifiedItem).filter(UnifiedItem.document_id == doc_id).delete()
        db.commit()
    res = client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json={"comment": "без позиций"})
    assert res.status_code == 200, res.text
    with SessionLocal() as db:
        assert db.query(PoolItem).filter(PoolItem.receipt_id == pid).count() == 1


def test_canon_without_items_takes_them_from_the_pool_once(companies):
    """Позиции есть только у Чек-Пула — сверка отдаёт их единой записи, а не ищет кандидата вечно."""
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _pool_first(db, c1)
        doc_id = _canon(db, rid).id
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == rid).delete()
        db.query(UnifiedItem).filter(UnifiedItem.document_id == doc_id).delete()
        db.commit()
        assert doc_id in copies.candidates(db)[service.POOL]
        assert copies.reconcile_copies(db)["restored"] >= 1
        db.commit()
        assert db.query(UnifiedItem).filter(UnifiedItem.document_id == doc_id).count() == 1
        assert db.query(PoolItem).filter(PoolItem.receipt_id == pid).count() == 1
        assert doc_id not in _drifting(db)


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="не задан UNIFIED_POSTGRES_TEST_URL")
def test_postgres_merge_rule():
    """Правило слияния и запрос кандидатов — так же на PostgreSQL (одноразовая схема)."""
    pg = create_engine(os.environ["UNIFIED_POSTGRES_TEST_URL"], pool_size=2, max_overflow=0)
    if pg.dialect.name != "postgresql":
        pg.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    schema = "qa_merge_" + uuid4().hex
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
            rid, pid = _pool_first_pg(db, company)
            doc_id = service.document_for_source(db, service.REGISTRY, rid).id
            found = copies.candidates(db)
            assert doc_id not in found[service.REGISTRY] and doc_id not in found[service.POOL]
            db.execute(update(UnifiedDocument).where(UnifiedDocument.id == doc_id).values(cashier=""))
            db.expire_all()
            assert doc_id in copies.candidates(db)[service.POOL]
            out = copies.reconcile_copies(db)
            assert out["restored"] == 1 and out["errors"] == 0
            db.expire_all()
            doc = service.document_for_source(db, service.REGISTRY, rid)
            assert (doc.merchant_address, doc.cashier) == (ADDRESS, CASHIER)
            assert db.get(PoolReceipt, pid).cashier == CASHIER
            assert copies.reconcile_copies(db)["candidates"] == 0
            db.rollback()
    finally:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.dispose()


def _pool_first_pg(db, company):
    fn, fd, fp = _identity()
    pool = _rich_pool(db, fn, fd, fp)
    service.register_source(db, service.POOL, pool, None, strict=True)
    reg = _registry(db, fn, fd, fp, company=company)
    db.flush()
    service.register_source(db, service.REGISTRY, reg, None, strict=True)
    db.flush()
    return reg.id, pool.id
