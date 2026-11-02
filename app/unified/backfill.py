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
"""
from __future__ import annotations

import argparse
import json

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Единый фискальный документ: backfill")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--claims-only", action="store_true",
                        help="только заявки компаний задним числом (v1.70.85)")
    args = parser.parse_args()
    from ..database import SessionLocal, init_db
    init_db()
    with SessionLocal() as db:
        if args.claims_only:
            result = links.backfill_claims(db, dry_run=args.dry_run)
        else:
            result = backfill(db, batch_size=max(1, args.batch_size), dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
