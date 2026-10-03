# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Аналитика: агрегированные метрики для дашборда.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_admin, require_accountant
from ..database import get_db
from ..models import AuditLog, Company, Receipt, ReceiptItem, User
from ..services.scoping import scope_receipts, user_company_id
from ..services.events import broadcast
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/dashboard", tags=["Аналитика"])


@router.get("/stats", summary="Сводные метрики для дашборда")
def stats(days: int = Query(14, ge=7, le=90),
          company_id: str | None = Query(None, max_length=36,
                                         description="v1.11.0: компания (только админ)"),
          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    today = dt.date.today()
    start = today - dt.timedelta(days=days - 1)

    # v1.11.0: изоляция пространства
    def scoped(q):
        return scope_receipts(q, user, company_id)

    total = scoped(db.query(func.count(Receipt.id))).scalar() or 0
    total_sum = scoped(db.query(func.coalesce(func.sum(Receipt.total_sum), 0.0))).scalar() or 0.0
    by_status = dict(
        scoped(db.query(Receipt.status, func.count(Receipt.id))).group_by(Receipt.status).all()
    )
    by_fns = dict(
        scoped(db.query(Receipt.fns_status, func.count(Receipt.id))).group_by(Receipt.fns_status).all()
    )
    exported = scoped(db.query(func.count(Receipt.id))).filter(Receipt.exported == True).scalar() or 0  # noqa: E712
    duplicates_blocked = db.query(func.count(AuditLog.id)).filter(
        AuditLog.action == "receipt_duplicate").scalar() or 0

    # Динамика по дням
    day_from = dt.datetime.combine(start, dt.time.min)
    rows = (scoped(db.query(func.date(Receipt.receipt_date),
                     func.count(Receipt.id),
                     func.coalesce(func.sum(Receipt.total_sum), 0.0)))
            .filter(Receipt.receipt_date >= day_from)
            .group_by(func.date(Receipt.receipt_date)).all())
    day_map = {str(r[0]): (int(r[1]), float(r[2])) for r in rows}
    daily = []
    for i in range(days):
        d = (start + dt.timedelta(days=i)).isoformat()
        cnt, s = day_map.get(d, (0, 0.0))
        daily.append({"date": d, "count": cnt, "sum": round(s, 2)})

    # Топ источников
    by_source = dict(
        scoped(db.query(Receipt.source, func.count(Receipt.id))).group_by(Receipt.source).all()
    )

    # --- v1.4.0: виджеты бухгалтера ---
    attention = 0
    notified = 0
    vat_month = 0.0
    by_assignee: list[dict] = []
    if user.role in ("admin", "accountant"):
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=3)
        attention = (scoped(db.query(func.count(Receipt.id)))
                     .filter(Receipt.status.in_(["new", "verifying"]),
                             Receipt.created_at < cutoff).scalar()) or 0
        notified = (scoped(db.query(func.count(Receipt.id)))
                    .filter(Receipt.notified == True,  # noqa: E712
                            Receipt.exported == False).scalar()) or 0   # noqa: E712
        month_start = dt.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
        vat_month = (scoped(db.query(func.coalesce(func.sum(ReceiptItem.vat_sum), 0.0))
                            .join(Receipt, ReceiptItem.receipt_id == Receipt.id))
                     .filter(Receipt.receipt_date >= month_start).scalar()) or 0.0
        arows = (scoped(db.query(Receipt.assignee, func.count(Receipt.id),
                          func.coalesce(func.sum(Receipt.total_sum), 0.0)))
                 .filter(Receipt.assignee != "")
                 .group_by(Receipt.assignee)
                 .order_by(func.sum(Receipt.total_sum).desc()).limit(8).all())
        by_assignee = [{"name": a, "count": int(c), "sum": round(float(s), 2)}
                       for a, c, s in arows]

    # v1.11.0: админу без фильтра — сводка по всем компаниям
    by_company = []
    if user.role == "admin" and not company_id:
        comp_rows = (db.query(Company.id, Company.name,
                              func.count(Receipt.id),
                              func.coalesce(func.sum(Receipt.total_sum), 0.0))
                     .outerjoin(Receipt, Receipt.company_id == Company.id)
                     .group_by(Company.id, Company.name)
                     .order_by(func.sum(Receipt.total_sum).desc()).all())
        by_company = [{"id": cid, "name": name, "count": int(c), "sum": round(float(s), 2)}
                      for cid, name, c, s in comp_rows]

    return {
        "by_company": by_company,               # v1.11.0 (только админ)
        "total": int(total),
        "total_sum": round(float(total_sum), 2),
        "by_status": by_status,
        "by_fns": by_fns,
        "exported": int(exported),
        "pending_export": int(by_status.get("verified", 0)),
        "duplicates_blocked": int(duplicates_blocked),
        "daily": daily,
        "by_source": by_source,
        # v1.4.0
        "attention_count": int(attention),      # не обработаны > 3 дней
        "notified_count": int(notified),        # уведомления сотрудников
        "vat_month": round(float(vat_month), 2),  # НДС за текущий месяц
        "by_assignee": by_assignee,             # сводка по подотчётникам
        "generated_at": dt.datetime.utcnow().isoformat(),
    }


@router.get("/recent", summary="Последние события (для ленты дашборда)")
def recent(limit: int = Query(15, ge=1, le=100),
           user: User = Depends(require_accountant), db: Session = Depends(get_db)):
    # v1.11.0: бухгалтер видит события своей компании (+платформенные),
    # администратор — всё
    q = db.query(AuditLog).order_by(AuditLog.id.desc())
    if user.role != "admin":
        q = q.filter((AuditLog.company_id == user.company_id)
                     | (AuditLog.company_id.is_(None)))
    rows = q.limit(limit).all()
    return [a.to_dict() for a in rows]


@router.post("/demo-data", summary="Загрузить демонстрационные чеки (админ)")
def demo_data(count: int = Query(24, ge=1, le=200),
              admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(Receipt).count() > 0:
        return {"ok": False, "message": "В базе уже есть чеки — демо-данные не требуются"}
    from ..seed import make_demo_receipts
    make_demo_receipts(count)
    log_action(admin, "demo_data_loaded", details={"count": count})
    broadcast("receipt_created", {"demo": True, "count": count})
    return {"ok": True, "message": f"Загружено демо-чеков: {count}"}
