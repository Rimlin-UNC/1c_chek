"""Optional live PostgreSQL test of the canonical store in a disposable schema.

Requires UNIFIED_POSTGRES_TEST_URL and psycopg.  Never point it at production.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Company, Receipt, ReceiptIdentityLock, ReceiptItem, User
from app.pool.models import PoolItem, PoolReceipt, PoolUser
from app.unified import service
from app.unified.models import (
    UnifiedCompanyLink,
    UnifiedDocument,
    UnifiedEvent,
    UnifiedItem,
    UnifiedOrigin,
    UnifiedOwnership,
)


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="dedicated PostgreSQL test URL not configured")
def test_postgres_canonical_move_and_unique_identity():
    url = os.environ["UNIFIED_POSTGRES_TEST_URL"]
    engine = create_engine(url, pool_size=4, max_overflow=0)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL must point to PostgreSQL")
    schema = "qa_unified_" + uuid4().hex
    tables = [Company.__table__, User.__table__, Receipt.__table__, ReceiptItem.__table__,
              PoolUser.__table__, PoolReceipt.__table__, PoolItem.__table__,
              ReceiptIdentityLock.__table__, UnifiedDocument.__table__, UnifiedItem.__table__,
              UnifiedOwnership.__table__, UnifiedOrigin.__table__, UnifiedEvent.__table__,
              UnifiedCompanyLink.__table__]   # v1.70.85: перемещение пишет связи компаний
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            Base.metadata.create_all(bind=conn, tables=tables)
        with Session(engine) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            c1 = Company(name="PG A")
            c2 = Company(name="PG B")
            c3 = Company(name="PG C")
            admin = User(username="pg_admin", password_hash="x", role="admin")
            db.add_all([c1, c2, c3, admin])
            db.flush()
            fn, fd, fp = "7" + uuid4().hex[:15], "1", "2"
            receipt = Receipt(fn=fn, fd=fd, fp=fp, qr_data="qr",
                              receipt_date=datetime(2026, 1, 1), total_sum=10,
                              company_id=c1.id)
            db.add(receipt)
            db.flush()
            doc = service.register_source(db, service.REGISTRY, receipt, admin, strict=True)
            doc_id, version = doc.id, doc.version
            db.commit()
        barrier = Barrier(2)

        def move(target):
            with Session(engine) as db:
                db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                actor = db.query(User).filter_by(username="pg_admin").one()
                barrier.wait(timeout=10)
                try:
                    service.move_document(db, doc_id, target, actor, expected_version=version)
                    db.commit()
                    return "ok"
                except Exception as exc:
                    db.rollback()
                    return getattr(exc, "status_code", type(exc).__name__)

        with Session(engine) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            ids = [c.id for c in db.query(Company).filter(Company.name != "PG A").order_by(Company.name).all()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(move, ids))
        assert sorted(map(str, results)) == ["409", "ok"]
        with Session(engine) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            doc = db.get(UnifiedDocument, doc_id)
            assert doc.version == version + 1
            assert db.query(UnifiedEvent).filter_by(document_id=doc_id, event_type="move").count() == 1
            assert db.query(Receipt).filter_by(fn=fn).one().company_id == doc.ownership.company_id
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="dedicated PostgreSQL test URL not configured")
def test_postgres_reconcile_and_visibility_sql():
    """v1.70.82: сверка после отката и фильтры видимости работают на PostgreSQL."""
    from app.services.scoping import scope_receipts
    from app.unified import reconcile
    from app.unified.models import REVIEW_OWNERSHIP

    url = os.environ["UNIFIED_POSTGRES_TEST_URL"]
    schema = "qa_unified_" + uuid4().hex
    engine = create_engine(url, pool_size=2, max_overflow=0,
                           connect_args={"options": f"-csearch_path={schema}"})
    tables = [Company.__table__, User.__table__, Receipt.__table__, ReceiptItem.__table__,
              PoolUser.__table__, PoolReceipt.__table__, PoolItem.__table__,
              ReceiptIdentityLock.__table__, UnifiedDocument.__table__, UnifiedItem.__table__,
              UnifiedOwnership.__table__, UnifiedOrigin.__table__, UnifiedEvent.__table__,
              UnifiedCompanyLink.__table__]   # v1.70.85: перемещение пишет связи компаний

    def receipt(db, company, n, total=10.0):
        row = Receipt(fn="8" + uuid4().hex[:15], fd=str(n), fp=str(1000 + n), qr_data="qr",
                      receipt_date=datetime(2026, 1, n), total_sum=total, company_id=company.id)
        db.add(row)
        db.flush()
        return row

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            Base.metadata.create_all(bind=conn, tables=tables)
        with Session(engine) as db:
            a, b = Company(name="PG A"), Company(name="PG B")
            db.add_all([a, b])
            db.flush()
            admin = User(username="pg_admin2", password_hash="x", role="admin")
            acc_a = User(username="pg_acc_a", password_hash="x", role="accountant", company_id=a.id)
            acc_b = User(username="pg_acc_b", password_hash="x", role="accountant", company_id=b.id)
            db.add_all([admin, acc_a, acc_b])
            db.flush()
            moved, deleted = receipt(db, a, 1), receipt(db, a, 2)
            for row in (moved, deleted):
                service.register_source(db, service.REGISTRY, row, admin, strict=True)
            pool = PoolReceipt(fn="9" + uuid4().hex[:15], fd="3", fp="3003", qr_data="qr",
                               receipt_date=datetime(2026, 1, 3), total_sum=5, status="verified",
                               assigned_company_id=a.id)
            db.add(pool)
            db.flush()
            service.register_source(db, service.POOL, pool, admin, strict=True)
            # спорный документ: реестр у A, пул у B — карантин переноса архива
            disputed = receipt(db, a, 4, total=7)
            twin = PoolReceipt(fn=disputed.fn, fd=disputed.fd, fp=disputed.fp, qr_data="qr",
                               receipt_date=disputed.receipt_date, total_sum=7, status="verified",
                               assigned_company_id=b.id)
            db.add(twin)
            db.flush()
            service.register_source(db, service.REGISTRY, disputed, None, strict=False)
            doc = service.register_source(db, service.POOL, twin, None, strict=False)
            assert doc.review_flags & REVIEW_OWNERSHIP
            ids = {"moved": moved.id, "deleted": deleted.id, "pool": pool.id, "disputed": disputed.id}
            users = {"admin": admin.id, "acc_a": acc_a.id, "acc_b": acc_b.id}
            b_id = b.id
            db.commit()
        with Session(engine) as db:   # «старая версия»: только реестр и пул
            db.query(Receipt).filter_by(id=ids["moved"]).update({Receipt.company_id: b_id})
            db.query(Receipt).filter_by(id=ids["deleted"]).delete()
            db.query(PoolReceipt).filter_by(id=ids["pool"]).update({PoolReceipt.assigned_company_id: None})
            db.commit()
        with Session(engine) as db:
            acc_a, acc_b = db.get(User, users["acc_a"]), db.get(User, users["acc_b"])
            # до сверки: чек виден новой компании, спорный — никому из компаний
            seen_b = {r.id for r in scope_receipts(db.query(Receipt), acc_b).all()}
            seen_a = {r.id for r in scope_receipts(db.query(Receipt), acc_a).all()}
            assert ids["moved"] in seen_b and ids["moved"] not in seen_a
            assert ids["disputed"] not in seen_a
            listed = service.scope_documents(db.query(UnifiedDocument).outerjoin(UnifiedOwnership), db, acc_a).all()
            assert all(ids["moved"] not in [o.source_id for o in d.origins] for d in listed)
            result = reconcile.reconcile(db)
            db.commit()
            assert result["owners_fixed"] >= 2 and result["documents_closed"] >= 1
            assert service.document_for_source(db, service.REGISTRY, ids["moved"]).ownership.company_id == b_id
            assert service.document_for_source(db, service.POOL, ids["pool"]).ownership.company_id is None
            again = reconcile.reconcile(db)
            assert again["owners_fixed"] == again["documents_closed"] == again["dangling_removed"] == 0
            listed_b = service.scope_documents(db.query(UnifiedDocument).outerjoin(UnifiedOwnership), db, acc_b).all()
            assert ids["moved"] in [o.source_id for d in listed_b for o in d.origins]
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()
