"""Готовность единой записи (фаза 4а, T-37): только числа, без строк базы.

Отчёт отвечает на вопросы перед следующими шагами (раздел 12 проекта единой
базы, docs/single_base_design.md):

* сколько чеков реестра и Чек-Пула уже в единой записи и сколько ждёт мягкой
  регистрации архива (её запускает только владелец — решение Q-26);
* сколько документов ждёт разбора;
* есть ли копии, которые расходятся с единой записью (после сверки при старте
  их быть не должно).

Запуск на сервере — только чтение, сервис останавливать не нужно:
    python -m app.unified.readiness
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Receipt
from ..pool.models import PoolReceipt
from . import copies, service
from .models import (
    REVIEW_BLOCKING,
    REVIEW_INCOMPLETE_IDENTITY,
    REVIEW_OWNERSHIP,
    REVIEW_VERIFICATION,
    UnifiedDocument,
    UnifiedOrigin,
)


def _linked(db: Session, model, kind: str) -> int:
    has_origin = (select(UnifiedOrigin.id)
                  .where(UnifiedOrigin.source_kind == kind, UnifiedOrigin.source_id == model.id)
                  .exists())
    return db.query(func.count(model.id)).filter(has_origin).scalar() or 0


def _flagged(db: Session, flag: int) -> int:
    return (db.query(func.count(UnifiedDocument.id))
            .filter(UnifiedDocument.deleted_at.is_(None),
                    UnifiedDocument.review_flags.op("&")(flag) != 0).scalar() or 0)


def readiness(db: Session) -> dict:
    """Числа готовности. Ничего не меняет."""
    out: dict = {}
    for key, model, kind in (("registry", Receipt, service.REGISTRY),
                             ("pool", PoolReceipt, service.POOL)):
        total = db.query(func.count(model.id)).scalar() or 0
        linked = _linked(db, model, kind)
        out[key] = {"total": total, "linked": linked, "pending": total - linked}
    out["documents"] = (db.query(func.count(UnifiedDocument.id))
                        .filter(UnifiedDocument.deleted_at.is_(None)).scalar() or 0)
    out["review"] = {
        "blocking": _flagged(db, REVIEW_BLOCKING),
        "ownership": _flagged(db, REVIEW_OWNERSHIP),
        "verification": _flagged(db, REVIEW_VERIFICATION),
        "incomplete_identity": _flagged(db, REVIEW_INCOMPLETE_IDENTITY),
    }
    found = copies.candidates(db)
    out["drift"] = {"registry": len(found[service.REGISTRY]), "pool": len(found[service.POOL])}
    total = out["registry"]["total"] + out["pool"]["total"]
    linked = out["registry"]["linked"] + out["pool"]["linked"]
    out["ready_percent"] = round(100.0 * linked / total, 1) if total else 100.0
    return out


def main() -> None:
    from ..database import SessionLocal

    with SessionLocal() as db:
        result = readiness(db)
        db.rollback()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
