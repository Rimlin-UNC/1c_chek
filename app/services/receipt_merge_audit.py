"""Агрегированная диагностика перед возможным объединением чеков.

Только SELECT. Не изменяет данные и не выдаёт фискальные реквизиты или компании.
"""
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import Receipt
from ..pool.models import PoolReceipt


def audit_receipt_overlap(db: Session) -> dict[str, int]:
    """Посчитать пересечения и расхождения, не предпринимая автоматического слияния.

    Точное совпадение тройки — не доказательство согласованности содержимого.
    Пустые реквизиты исключаются из сопоставления и считаются отдельно.
    """
    r, p = Receipt, PoolReceipt

    def complete(model):
        return and_(*(and_(col.is_not(None), func.trim(col) != "")
                      for col in (model.fn, model.fd, model.fp)))

    def count(statement):
        return int(db.scalar(statement) or 0)

    join = and_(r.fn == p.fn, r.fd == p.fd, r.fp == p.fp, complete(r), complete(p))
    overlap = select(func.count()).select_from(r).join(p, join)
    return {
        "registry_total": count(select(func.count()).select_from(r)),
        "pool_total": count(select(func.count()).select_from(p)),
        "registry_incomplete_identity": count(select(func.count()).select_from(r).where(~complete(r))),
        "pool_incomplete_identity": count(select(func.count()).select_from(p).where(~complete(p))),
        "matching_identities": count(overlap),
        # Деньги хранятся как float; разница до 0.009 руб. считается округлением.
        "different_amount": count(overlap.where(and_(r.total_sum.is_not(None), p.total_sum.is_not(None),
                                                      func.abs(r.total_sum - p.total_sum) > 0.009))),
        "different_operation": count(overlap.where(and_(r.operation.is_not(None), p.operation.is_not(None),
                                                         r.operation != p.operation))),
        "different_datetime": count(overlap.where(and_(r.receipt_date.is_not(None), p.receipt_date.is_not(None),
                                                        r.receipt_date != p.receipt_date))),
        "missing_comparison_data": count(overlap.where(or_(r.total_sum.is_(None), p.total_sum.is_(None),
                                                           r.operation.is_(None), p.operation.is_(None),
                                                           r.receipt_date.is_(None), p.receipt_date.is_(None)))),
        "different_company_assignment": count(overlap.where(and_(r.company_id.is_not(None),
            p.assigned_company_id.is_not(None), r.company_id != p.assigned_company_id))),
    }


def main() -> None:
    """Запуск: python -m app.services.receipt_merge_audit (по DATABASE_URL)."""
    import json

    from ..database import SessionLocal

    with SessionLocal() as db:
        print(json.dumps(audit_receipt_overlap(db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
