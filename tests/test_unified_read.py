"""«Единая база», шаг 4в (T-39): данные единой записи в карточке чека — с пометкой.

Решение владельца Q-28 (03.11.2026) — вариант (в): компания видит данные единой
записи, которых нет в её копии (у чека по QR — адрес, кассир и позиции из Чек-Пула),
отдельным блоком ``unified_fill``; поля копии и выгрузки в 1С не меняются. Перенести
значения в свой чек бухгалтер может только явно («Подставить» + «Сохранить» —
обычный PATCH). Спорная или удалённая единая запись не показывается. Только
синтетические данные (ADR-14).
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal
from app.models import Company, Receipt, ReceiptItem
from app.pool.models import PoolItem, PoolReceipt
from app.services import exporter
from app.unified import read
from app.unified.models import REVIEW_AMOUNT, REVIEW_VERIFICATION, UnifiedDocument
from tests import test_unified_merge as m

# фикстура компаний и пользователей — общая с tests/test_unified_receipts.py
companies = m.companies


def _qr_copy(db, company_id):
    """Чек сначала в Чек-Пуле с данными ФНС, затем компания сканирует QR: у копии нет
    адреса, кассира и позиций."""
    rid, pid = m._pool_first(db, company_id)
    db.query(ReceiptItem).filter(ReceiptItem.receipt_id == rid).delete()
    db.commit()
    return rid, pid


def test_card_shows_what_the_copy_lacks(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, _ = _qr_copy(db, c1)
    d = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
    assert d["merchant_address"] == "" and d["cashier"] == "" and d["items"] == []   # копия не меняется
    fill = d["unified_fill"]
    assert fill["fields"] == {"merchant_address": m.ADDRESS, "cashier": m.CASHIER}
    assert len(fill["items"]) == 1 and fill["items"][0]["name"]
    assert set(fill["items"][0]) == {"name", "quantity", "price", "total", "vat_rate"}


def test_list_rows_carry_fields_and_items_only_with_items(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, _ = _qr_copy(db, c1)
    rows = client.get("/api/v1/receipts?page_size=200", headers=hdr).json()["items"]
    row = next(r for r in rows if r["id"] == rid)
    assert row["unified_fill"] == {"fields": {"merchant_address": m.ADDRESS, "cashier": m.CASHIER}}
    printed = client.get(f"/api/v1/receipts?ids={rid}", headers=hdr).json()["items"]
    assert len(printed[0]["unified_fill"]["items"]) == 1          # выборка с позициями — и позиции


@pytest.mark.parametrize("flag", [REVIEW_AMOUNT, REVIEW_VERIFICATION])
def test_disputed_canon_is_not_shown(companies, client, flag):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, _ = _qr_copy(db, c1)
        doc_id = m._canon(db, rid).id
        db.execute(update(UnifiedDocument).where(UnifiedDocument.id == doc_id).values(review_flags=flag))
        db.commit()
    assert "unified_fill" not in client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()


def test_deleted_canon_is_not_shown(companies, client):
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, _ = _qr_copy(db, c1)
        doc_id = m._canon(db, rid).id
        db.execute(update(UnifiedDocument).where(UnifiedDocument.id == doc_id).values(
            deleted_at=UnifiedDocument.created_at))
        db.commit()
    assert "unified_fill" not in client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()


def test_substitute_makes_values_the_company_own(companies, client):
    """«Подставить» + «Сохранить»: значения становятся своими, подстановка исчезает."""
    hdr, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, pid = _qr_copy(db, c1)
    fill = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()["unified_fill"]
    body = {**fill["fields"],
            "items": [{k: it[k] for k in ("name", "quantity", "price", "total")} for it in fill["items"]]}
    assert client.patch(f"/api/v1/receipts/{rid}", headers=hdr, json=body).status_code == 200
    d = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
    assert (d["merchant_address"], d["cashier"], len(d["items"])) == (m.ADDRESS, m.CASHIER, 1)
    assert "unified_fill" not in d
    with SessionLocal() as db:
        pool = db.get(PoolReceipt, pid)
        assert (pool.merchant_address, pool.cashier) == (m.ADDRESS, m.CASHIER)
        assert db.query(PoolItem).filter(PoolItem.receipt_id == pid).count() == 1


def test_exports_take_only_the_company_copy(companies):
    """Q-28 (в): в выгрузку в 1С — только данные копии компании."""
    _, c1, _, _, _ = companies
    with SessionLocal() as db:
        rid, _ = _qr_copy(db, c1)
        receipt = db.get(Receipt, rid)
        push = json.dumps(exporter.build_push_payload([receipt], [], "ПоступлениеТоваровУслуг"),
                          ensure_ascii=False, default=str)
        ed_json = json.dumps(json.loads(exporter.build_enterprise_data_json(
            [receipt], [], "ПоступлениеТоваровУслуг")), ensure_ascii=False)
        ed_xml = exporter.build_enterprise_data_xml([receipt], [], "ПоступлениеТоваровУслуг")
        ed_xml = ed_xml if isinstance(ed_xml, str) else ed_xml.decode("utf-8")
    for exported in (push, ed_json, ed_xml):
        assert m.ADDRESS not in exported and m.CASHIER not in exported


def test_reader_is_batched_and_read_only():
    import inspect
    src = inspect.getsource(read.fills_for_receipts)
    assert "_registry_documents(db, list(wanted))" in src and "_chunks(" in src
    for write in ("db.add(", "db.commit(", "db.flush(", "setattr("):
        assert write not in src, write


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="не задан UNIFIED_POSTGRES_TEST_URL")
def test_postgres_fill():
    """Подстановка из единой записи — так же на PostgreSQL (одноразовая схема)."""
    pg = create_engine(os.environ["UNIFIED_POSTGRES_TEST_URL"], pool_size=2, max_overflow=0)
    if pg.dialect.name != "postgresql":
        pg.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    schema = "qa_fill_" + uuid4().hex
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
            rid, _ = m._pool_first_pg(db, company)
            db.query(ReceiptItem).filter(ReceiptItem.receipt_id == rid).delete()
            db.flush()
            db.expire_all()
            fill = read.fills_for_receipts(db, [db.get(Receipt, rid)])[rid]
            assert fill["fields"] == {"merchant_address": m.ADDRESS, "cashier": m.CASHIER}
            assert len(fill["items"]) == 1
            db.rollback()
    finally:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.dispose()
