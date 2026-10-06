# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — админ-роуты «Чек-Пула» (v1.30.0, Этап 1).
# Настройка вкл/выкл + минимальный дашборд. Только администратор.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User

router = APIRouter(prefix="/api/v1/pool-admin", tags=["pool"])


@router.get("/overview", summary="Чек-Пул: настройка и счётчики (админ)")
def pool_overview(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import appsettings
    from . import ingest
    return {
        "enabled": ingest.pool_enabled(db),
        "offerta": ingest.OFFERTA_SHORT,
        "daily_limit": ingest.DAILY_LIMIT,
        "points_per_receipt": ingest.POINTS_PER_RECEIPT,
        **ingest.overview(db),
    }


@router.put("/settings", summary="Чек-Пул: включить/выключить приём чеков (админ)")
def pool_settings(body: dict, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    from ..services import appsettings
    from ..services.audit import log_action
    enabled = bool((body or {}).get("enabled"))
    appsettings.set_setting(db, "pool_enabled", "1" if enabled else "0")
    log_action(admin, "pool_settings_saved", details={"enabled": enabled})
    return {"ok": True, "enabled": enabled,
            "message": ("Приём чеков в пул включён — бот принимает QR"
                        if enabled else "Приём чеков в пул выключен")}


@router.get("/receipts", summary="Чек-Пул: список чеков (админ)")
def pool_receipts(status: str = Query("", max_length=16),
                  page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from .models import PoolReceipt
    q = db.query(PoolReceipt)
    if status:
        q = q.filter(PoolReceipt.status == status)
    total = q.count()
    rows = (q.order_by(PoolReceipt.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "id": r.id, "status": r.status, "source": r.source,
            "fn": r.fn, "fd": r.fd, "fp": r.fp,
            "receipt_date": r.receipt_date.isoformat() if r.receipt_date else None,
            "total_sum": r.total_sum, "operation": r.operation,
            "merchant_name": r.merchant_name, "merchant_inn": r.merchant_inn,
            "full_data": r.full_data, "points_awarded": r.points_awarded,
            "status_message": r.status_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }
