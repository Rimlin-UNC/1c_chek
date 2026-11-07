"""Идемпотентная добавляющая миграция легаси-чеков в канонический слой.

Не удаляет и не перезаписывает исходные строки.  Конфликты получают пометки
разбора и не назначают владельца автоматически.  Запуск:
    python -m app.unified.backfill --dry-run
    python -m app.unified.backfill --batch-size 500
    python -m app.unified.backfill --claims-only [--dry-run]

v1.70.85 («Единая база», T-34): после регистрации архива — заявки компаний
задним числом (links.backfill_claims): отказы /scan из журнала аудита и
исторические пары «реестр ↔ пул другой компании». --claims-only — только
заявки (чеки архива с отказами регистрируются мягко, по одному).

v1.70.88 (фаза 4а, T-37; решение владельца Q-26): мягкая регистрация архива.
    python -m app.unified.backfill --soft --dry-run
    python -m app.unified.backfill --soft [--batch-size 500]
В единую запись попадают только чеки без расхождений: копия одна или копии
совпадают, владелец не спорный. Спорные чеки остаются как сейчас и видны как
раньше; пометок разбора мягкая регистрация не ставит. Добавляются только
строки unified_*, реестр и пул не меняются. Запускает только владелец.
Пробный прогон на SQLite выполняется на временном снимке базы на том же
сервере (снимок удаляется сразу после подсчёта), рабочая база не меняется.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Receipt
from ..pool.models import PoolReceipt
from . import links, service
from .models import UnifiedOrigin


def _unlinked(db: Session, model, kind: str):
    linked = (db.query(UnifiedOrigin.source_id)
              .filter(UnifiedOrigin.source_kind == kind).subquery())
    return (db.query(model).filter(~model.id.in_(linked.select())))


def backfill(db: Session, *, batch_size: int = 500, dry_run: bool = False) -> dict:
    """Регистрирует недостающие проекции; повторный запуск безопасен."""
    registry_total = _unlinked(db, Receipt, service.REGISTRY).count()
    pool_total = _unlinked(db, PoolReceipt, service.POOL).count()
    if dry_run:
        return {"dry_run": True, "registry_pending": registry_total,
                "pool_pending": pool_total, "registered": 0, "conflicts": 0,
                "claims": links.backfill_claims(db, dry_run=True)}
    registered = conflicts = 0
    # Реестр первым: его уточнённые данные и позиции предпочтительнее пуловых.
    for model, kind in ((Receipt, service.REGISTRY), (PoolReceipt, service.POOL)):
        while True:
            rows = (_unlinked(db, model, kind).order_by(model.id).limit(batch_size).all())
            if not rows:
                break
            for row in rows:
                before = db.query(UnifiedOrigin).filter_by(source_kind=kind, source_id=row.id).first()
                if before:
                    continue
                doc = service.register_source(db, kind, row, strict=False, event_type="backfill")
                registered += 1
                if doc.review_flags:
                    conflicts += 1
            db.commit()
    return {"dry_run": False, "registry_pending": 0, "pool_pending": 0,
            "registered": registered, "conflicts": conflicts,
            "claims": links.backfill_claims(db)}


_SKIP_DATA = "расходятся"
_SKIP_OWNER = "принадлежит"


def soft_backfill(db: Session, *, batch_size: int = 500, commit: bool = True) -> dict:
    """Мягкая регистрация: только чеки без расхождений; спорные остаются как есть.

    Каждый чек регистрируется в своей точке сохранения строгим путём живых
    операций (``strict=True``): расхождение данных или спор о владельце
    откатывает только эту точку. Повторный запуск безопасен: пропущенные чеки
    снова сверяются, зарегистрированные не трогаются.
    """
    out = {"registry_registered": 0, "pool_registered": 0, "skipped_data": 0,
           "skipped_owner": 0, "skipped_other": 0}
    for model, kind, key in ((Receipt, service.REGISTRY, "registry_registered"),
                             (PoolReceipt, service.POOL, "pool_registered")):
        last_id = ""
        while True:
            rows = (_unlinked(db, model, kind).filter(model.id > last_id)
                    .order_by(model.id).limit(batch_size).all())
            if not rows:
                break
            for row in rows:
                last_id = row.id
                try:
                    with db.begin_nested():
                        service.register_source(db, kind, row, None, strict=True,
                                                event_type="backfill_soft")
                    out[key] += 1
                except HTTPException as exc:
                    detail = str(exc.detail)
                    if _SKIP_DATA in detail:
                        out["skipped_data"] += 1
                    elif _SKIP_OWNER in detail:
                        out["skipped_owner"] += 1
                    else:
                        out["skipped_other"] += 1
            if commit:
                db.commit()
            else:
                db.flush()
    return out


def soft_dry_run(*, batch_size: int = 500) -> dict:
    """Пробный прогон мягкой регистрации: числа без изменения рабочей базы.

    SQLite — прогон на временном снимке (резервная копия средствами SQLite),
    снимок удаляется сразу. PostgreSQL — прогон в транзакции с откатом.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from ..database import SessionLocal, engine

    if engine.url.get_backend_name() != "sqlite":
        with SessionLocal() as db:
            result = soft_backfill(db, batch_size=batch_size, commit=False)
            db.rollback()
        return {"dry_run": True, **result}
    workdir = tempfile.mkdtemp(prefix="ym_soft_dry_")
    snapshot = os.path.join(workdir, "snapshot.db")
    try:
        source = sqlite3.connect(engine.url.database or "")
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        snap_engine = create_engine(f"sqlite:///{snapshot}")
        try:
            with sessionmaker(bind=snap_engine, autoflush=False, expire_on_commit=False)() as db:
                result = soft_backfill(db, batch_size=batch_size)
        finally:
            snap_engine.dispose()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {"dry_run": True, **result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Единый фискальный документ: backfill")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--claims-only", action="store_true",
                        help="только заявки компаний задним числом (v1.70.85)")
    parser.add_argument("--soft", action="store_true",
                        help="мягкая регистрация: только чеки без расхождений (v1.70.88)")
    args = parser.parse_args()
    from ..database import SessionLocal, init_db
    if args.soft and args.dry_run:
        print(json.dumps(soft_dry_run(batch_size=max(1, args.batch_size)), ensure_ascii=False, indent=2))
        return
    init_db()
    with SessionLocal() as db:
        if args.soft:
            result = {"dry_run": False, **soft_backfill(db, batch_size=max(1, args.batch_size))}
        elif args.claims_only:
            result = links.backfill_claims(db, dry_run=args.dry_run)
        else:
            result = backfill(db, batch_size=max(1, args.batch_size), dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
