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
              UnifiedOwnership.__table__, UnifiedOrigin.__table__, UnifiedEvent.__table__]
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
