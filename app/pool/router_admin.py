# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — админ-роуты «Чек-Пула» (v1.30.0, Этап 1).
# Настройка вкл/выкл + минимальный дашборд. Только администратор.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User

router = APIRouter(prefix="/api/v1/pool-admin", tags=["pool"])


def _or_like(column, s: str):
    """Поиск «как в ядре», но кириллица тоже без регистра: SQLite LIKE
    регистронезависим только для ASCII — добавляем варианты регистра."""
    like = f"%{s}%"
    return (column.ilike(like)
            | column.ilike(f"%{s.lower()}%")
            | column.ilike(f"%{s.capitalize()}%")
            | column.ilike(f"%{s.upper()}%"))


@router.get("/overview", summary="Чек-Пул: настройка и счётчики (админ)")
def pool_overview(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import appsettings
    from . import ingest
    from . import engage
    return {
        "enabled": ingest.pool_enabled(db),
        "offerta": ingest.OFFERTA_SHORT,
        "daily_limit": ingest.DAILY_LIMIT,
        "points_per_receipt": ingest.POINTS_PER_RECEIPT,
        "withdrawals": engage.admin_counts(db),   # v1.36.0: заявки на вывод
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
            "message": ("Приём чеков включён — форма на сайте принимает чеки"
                        " (бот — резервный канал)"
                        if enabled else "Приём чеков в пул выключен")}


@router.get("/receipts", summary="Чек-Пул: список чеков с поиском (админ)")
def pool_receipts(status: str = Query("", max_length=16),
                  q_str: str = Query("", alias="q", max_length=100),
                  region: str = Query("", max_length=8),
                  industry: str = Query("", max_length=32),
                  missing_geo: bool = Query(False),
                  missing_industry: bool = Query(False),
                  page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from .models import PoolReceipt
    q = db.query(PoolReceipt)
    if status:
        q = q.filter(PoolReceipt.status == status)
    if q_str.strip():                      # поиск: ФН / ИНН / магазин
        s = q_str.strip()
        q = q.filter(_or_like(PoolReceipt.fn, s)
                     | _or_like(PoolReceipt.merchant_inn, s)
                     | _or_like(PoolReceipt.merchant_name, s))
    if region:
        q = q.filter(PoolReceipt.region_code == region)
    if industry:
        q = q.filter(PoolReceipt.industry == industry)
    if missing_geo:
        q = q.filter(PoolReceipt.region_code == "")
    if missing_industry:
        q = q.filter(PoolReceipt.industry == "")
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
            "region_code": r.region_code, "city": r.city,
            "industry": r.industry, "geo_accuracy": r.geo_accuracy,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


# --------------------------------------------------------------------------
# v1.33.0: модерация (одобрить/отклонить), справочники, ручная разметка,
# дообогащение старых чеков, выгрузка CSV
# --------------------------------------------------------------------------
@router.get("/dicts", summary="Чек-Пул: справочники регионов и отраслей (админ)")
def pool_dicts(admin: User = Depends(require_admin)):
    from .geo import industries, regions
    return {
        "regions": [{"code": r["code"], "name": r["name"]} for r in regions()],
        "industries": [{"code": i["code"], "name": i["name"]} for i in industries()],
    }


def _award_points_for(db: Session, receipt) -> int:
    """Начисляет баллы за чек, если ещё не начислены. Возвращает дельту."""
    from . import ingest
    from .models import PoolUser
    if receipt.points_awarded:
        return 0
    user = db.get(PoolUser, receipt.pool_user_id) if receipt.pool_user_id else None
    if user is None:
        return 0
    pts = ingest.POINTS_PER_RECEIPT
    ingest.add_points(db, user, pts, "receipt", receipt.id)
    receipt.points_awarded = pts
    return pts


@router.post("/receipt/{rid}/moderate",
             summary="Чек-Пул: модерация чека — approve/reject (админ)")
def pool_moderate(rid: str, body: dict, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    from ..models import utcnow
    from ..services.audit import log_action
    from .models import PoolReceipt
    r = db.get(PoolReceipt, rid)
    if r is None:
        raise HTTPException(404, "Чек не найден")
    action = (body or {}).get("action", "")
    comment = ((body or {}).get("comment") or "").strip()[:400]
    if action == "approve":
        if r.status != "pending":
            raise HTTPException(409, "Одобрить можно только чек с ручной проверкой")
        r.status = "verified"
        r.verified_at = utcnow()
        pts = _award_points_for(db, r)
        r.status_message = ("Одобрено модератором"
                            + (f": {comment}" if comment else ""))
        log_action(admin, "pool_receipt_approved",
                   details={"receipt_id": rid, "points": pts})
    elif action == "reject":
        if r.status not in ("pending", "verified"):
            raise HTTPException(409, "Отклонить можно чек с ручной проверкой")
        r.status = "rejected"
        r.status_message = ("Отклонено модератором"
                            + (f": {comment}" if comment else ""))
        log_action(admin, "pool_receipt_rejected", details={"receipt_id": rid})
    else:
        raise HTTPException(422, "action: approve или reject")
    db.commit()
    return {"ok": True, "id": r.id, "status": r.status,
            "points_awarded": r.points_awarded}


@router.patch("/receipt/{rid}",
              summary="Чек-Пул: ручная разметка региона/отрасли (админ)")
def pool_patch_receipt(rid: str, body: dict, db: Session = Depends(get_db),
                       admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .geo import industry_name, region_name
    from .models import PoolReceipt
    r = db.get(PoolReceipt, rid)
    if r is None:
        raise HTTPException(404, "Чек не найден")
    b = body or {}
    if "region_code" in b:
        code = str(b.get("region_code") or "").strip()
        if code and region_name(code) == "":
            raise HTTPException(422, "Неизвестный код региона")
        r.region_code = code
        r.geo_accuracy = "manual" if code else r.geo_accuracy
    if "city" in b:
        r.city = str(b.get("city") or "").strip()[:128]
    if "industry" in b:
        ind = str(b.get("industry") or "").strip()
        if ind and industry_name(ind) == "":
            raise HTTPException(422, "Неизвестная отрасль")
        r.industry = ind
    log_action(admin, "pool_receipt_marked",
               details={"receipt_id": rid, "region": r.region_code,
                        "industry": r.industry})
    db.commit()
    return {"ok": True, "id": r.id, "region_code": r.region_code,
            "city": r.city, "industry": r.industry,
            "geo_accuracy": r.geo_accuracy}


@router.post("/enrich-missing",
             summary="Чек-Пул: дообогатить чеки без гео/отрасли (админ)")
def pool_enrich_missing(db: Session = Depends(get_db),
                        admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .geo import enrich_receipt
    from .models import PoolReceipt
    rows = (db.query(PoolReceipt)
            .filter((PoolReceipt.region_code == "")
                    | (PoolReceipt.industry == ""))
            .order_by(PoolReceipt.created_at.desc()).limit(500).all())
    had_region = had_industry = 0
    for r in rows:
        was_region, was_ind = r.region_code, r.industry
        enrich_receipt(db, r)
        if r.region_code and not was_region:
            had_region += 1
        if r.industry and not was_ind:
            had_industry += 1
    db.commit()
    log_action(admin, "pool_enrich_missing",
               details={"processed": len(rows), "region": had_region,
                        "industry": had_industry})
    return {"ok": True, "processed": len(rows), "with_region": had_region,
            "with_industry": had_industry,
            "message": f"Размечено: регион +{had_region}, отрасль +{had_industry} из {len(rows)}"}


@router.get("/export.csv", summary="Чек-Пул: выгрузка чеков CSV (админ)")
def pool_export(status: str = Query("", max_length=16),
                q_str: str = Query("", alias="q", max_length=100),
                missing_geo: bool = Query(False),
                missing_industry: bool = Query(False),
                db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    import csv as _csv
    import io
    from datetime import datetime as _dt

    from ..services.audit import log_action
    from .geo import industry_name, region_name
    from .models import PoolReceipt
    q = db.query(PoolReceipt)
    if status:
        q = q.filter(PoolReceipt.status == status)
    if q_str.strip():
        s = q_str.strip()
        q = q.filter(_or_like(PoolReceipt.fn, s)
                     | _or_like(PoolReceipt.merchant_inn, s)
                     | _or_like(PoolReceipt.merchant_name, s))
    if missing_geo:
        q = q.filter(PoolReceipt.region_code == "")
    if missing_industry:
        q = q.filter(PoolReceipt.industry == "")
    rows = q.order_by(PoolReceipt.created_at.desc()).limit(10000).all()
    buf = io.StringIO()
    w = _csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Когда сдан", "Дата чека", "Магазин", "ИНН", "Сумма, ₽",
                "Статус", "Регион", "Город", "Отрасль", "Точность гео",
                "Баллы", "Источник", "ФН", "ФД", "ФП"])
    st = {"verified": "Принят", "pending": "На проверке", "rejected": "Не принят"}
    acc = {"address": "адрес", "inn": "ИНН", "manual": "вручную"}
    for r in rows:
        w.writerow([
            r.created_at.strftime("%d.%m.%Y %H:%M") if r.created_at else "",
            r.receipt_date.strftime("%d.%m.%Y %H:%M") if r.receipt_date else "",
            r.merchant_name or "", r.merchant_inn or "",
            f"{r.total_sum:.2f}".replace(".", ","),
            st.get(r.status, r.status),
            region_name(r.region_code), r.city or "",
            industry_name(r.industry), acc.get(r.geo_accuracy, r.geo_accuracy),
            r.points_awarded or 0, r.source, r.fn, r.fd, r.fp])
    log_action(admin, "pool_exported_csv", details={"count": len(rows)})
    content = "\ufeff" + buf.getvalue()
    filename = f"chek-pool-admin-{_dt.utcnow().strftime('%Y%m%d-%H%M')}.csv"
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------
# v1.32.0: SMTP для писем кабинета (подтверждение e-mail, magic link)
# и адрес сайта для ссылок. Пароль — шифруемый секрет (appsettings).
# --------------------------------------------------------------------------
@router.get("/smtp", summary="Чек-Пул: настройки почты (админ)")
def get_smtp(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..services import appsettings
    from .mailer import SET_BASE_URL, SET_SMTP_FROM, SET_SMTP_HOST, SET_SMTP_PORT, SET_SMTP_TLS, SET_SMTP_USER
    g = appsettings.get_setting
    return {
        "host": g(db, SET_SMTP_HOST), "port": g(db, SET_SMTP_PORT, "587"),
        "user": g(db, SET_SMTP_USER), "has_password": bool(g(db, "smtp_pass")),
        "sender": g(db, SET_SMTP_FROM), "tls": g(db, SET_SMTP_TLS, "1") == "1",
        "base_url": g(db, SET_BASE_URL),
        "configured": bool(g(db, SET_SMTP_HOST).strip()),
    }


@router.put("/smtp", summary="Чек-Пул: сохранить настройки почты (админ)")
def put_smtp(body: dict, db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    from ..services import appsettings
    from ..services.audit import log_action
    from .mailer import SET_BASE_URL, SET_SMTP_FROM, SET_SMTP_HOST, SET_SMTP_PORT, SET_SMTP_TLS, SET_SMTP_USER
    b = body or {}
    appsettings.set_setting(db, SET_SMTP_HOST, (b.get("host") or "").strip())
    appsettings.set_setting(db, SET_SMTP_PORT, str(int(b.get("port") or 587)))
    appsettings.set_setting(db, SET_SMTP_USER, (b.get("user") or "").strip())
    if b.get("password") not in (None, ""):       # "" — не менять
        appsettings.set_setting(db, "smtp_pass", b["password"])
    appsettings.set_setting(db, SET_SMTP_FROM, (b.get("sender") or "").strip())
    appsettings.set_setting(db, SET_SMTP_TLS, "1" if b.get("tls") else "0")
    appsettings.set_setting(db, SET_BASE_URL, (b.get("base_url") or "").strip())
    log_action(admin, "pool_smtp_saved", details={"configured": bool((b.get("host") or "").strip())})
    return {"ok": True, "message": "Настройки почты сохранены"}


@router.post("/smtp-test", summary="Чек-Пул: тестовое письмо (админ)")
def smtp_test(body: dict, db: Session = Depends(get_db),
              admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .mailer import send_mail
    to = ((body or {}).get("email") or "").strip()
    ok, err = send_mail(db, to, "Ямастер Чек-Пул: тестовое письмо",
                        "SMTP настроен верно — письма кабинета будут доходить.\n\nООО «Ямастер»")
    log_action(admin, "pool_smtp_test", details={"ok": ok})
    return {"ok": ok, "message": ("Письмо отправлено" if ok else f"Не отправлено: {err}")}
