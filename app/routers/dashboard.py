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

from ..auth import get_current_user, require_admin
from ..database import get_db
from ..models import AuditLog, Receipt, User
from ..services.events import broadcast
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/dashboard", tags=["Аналитика"])


@router.get("/stats", summary="Сводные метрики для дашборда")
def stats(days: int = Query(14, ge=7, le=90),
          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    today = dt.date.today()
    start = today - dt.timedelta(days=days - 1)

    total = db.query(func.count(Receipt.id)).scalar() or 0
    total_sum = db.query(func.coalesce(func.sum(Receipt.total_sum), 0.0)).scalar() or 0.0
    by_status = dict(
        db.query(Receipt.status, func.count(Receipt.id)).group_by(Receipt.status).all()
    )
    by_fns = dict(
        db.query(Receipt.fns_status, func.count(Receipt.id)).group_by(Receipt.fns_status).all()
    )
    exported = db.query(func.count(Receipt.id)).filter(Receipt.exported == True).scalar() or 0  # noqa: E712
    duplicates_blocked = db.query(func.count(AuditLog.id)).filter(
        AuditLog.action == "receipt_duplicate").scalar() or 0

    # Динамика по дням
    day_from = dt.datetime.combine(start, dt.time.min)
    rows = (db.query(func.date(Receipt.receipt_date),
                     func.count(Receipt.id),
                     func.coalesce(func.sum(Receipt.total_sum), 0.0))
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
        db.query(Receipt.source, func.count(Receipt.id)).group_by(Receipt.source).all()
    )

    return {
        "total": int(total),
        "total_sum": round(float(total_sum), 2),
        "by_status": by_status,
        "by_fns": by_fns,
        "exported": int(exported),
        "pending_export": int(by_status.get("verified", 0)),
        "duplicates_blocked": int(duplicates_blocked),
        "daily": daily,
        "by_source": by_source,
        "generated_at": dt.datetime.utcnow().isoformat(),
    }


@router.get("/recent", summary="Последние события (для ленты дашборда)")
def recent(limit: int = Query(15, ge=1, le=100),
           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all())
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
