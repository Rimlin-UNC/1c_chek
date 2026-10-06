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
    from .router_api import DEFAULT_QUOTA, DEFAULT_RATE, api_enabled
    from .router_company import PICK_LIMIT_DEFAULT, PICK_LIMIT_KEY, pick_limit
    from ..services import appsettings
    from .models import PoolApiCall, PoolApiKey
    m_start = _utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "enabled": ingest.pool_enabled(db),
        "offerta": ingest.OFFERTA_SHORT,
        "daily_limit": ingest.DAILY_LIMIT,
        "points_per_receipt": ingest.POINTS_PER_RECEIPT,
        "withdrawals": engage.admin_counts(db),   # v1.36.0: заявки на вывод
        # v1.37.0: квота «Подбора из пула» на компанию
        "pick_monthly_limit": int(appsettings.get_setting(
            db, PICK_LIMIT_KEY, str(PICK_LIMIT_DEFAULT))),
        # v1.39.0: платное API для внешних клиентов
        "api_enabled": api_enabled(db),
        "api_keys_total": db.query(PoolApiKey).count(),
        "api_keys_active": db.query(PoolApiKey)
            .filter(PoolApiKey.active.is_(True)).count(),
        "api_calls_month": db.query(PoolApiCall)
            .filter(PoolApiCall.created_at >= m_start).count(),
        "api_defaults": {"rate_per_hour": DEFAULT_RATE,
                         "monthly_quota": DEFAULT_QUOTA},
        **ingest.overview(db),
    }


def _utcnow():
    from datetime import datetime as _dt
    return _dt.utcnow()


@router.put("/settings", summary="Чек-Пул: включить/выключить приём чеков (админ)")
def pool_settings(body: dict, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    from ..services import appsettings
    from ..services.audit import log_action
    enabled = bool((body or {}).get("enabled"))
    appsettings.set_setting(db, "pool_enabled", "1" if enabled else "0")
    # v1.37.0: месячная квота «Подбора из пула» на компанию (0 — выключен)
    pick_limit_msg = ""
    pl = (body or {}).get("pick_limit")
    if pl is not None:
        try:
            v = max(0, int(pl))
        except (TypeError, ValueError):
            raise HTTPException(422, "Лимит подбора — целое число, 0 = выключен")
        appsettings.set_setting(db, "pool_pick_monthly_limit", str(v))
        pick_limit_msg = f" · квота подбора: {v} чеков/мес на компанию"
    # v1.39.0: платное API для внешних клиентов (вкл/выкл)
    api_msg = ""
    api_on = (body or {}).get("api_enabled")
    if api_on is not None:
        from .router_api import API_ENABLED_KEY
        appsettings.set_setting(db, API_ENABLED_KEY, "1" if api_on else "0")
        api_msg = (" · API включён" if api_on else " · API выключен")
    log_action(admin, "pool_settings_saved",
               details={"enabled": enabled, "pick_limit": pl,
                        "api_enabled": api_on})
    return {"ok": True, "enabled": enabled,
            "message": ("Приём чеков включён — форма на сайте принимает чеки"
                        " (бот — резервный канал)"
                        if enabled else "Приём чеков в пул выключен")
            + pick_limit_msg + api_msg}


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
        db.commit()                    # аудит отдельной сессией: коммитим
        log_action(admin, "pool_receipt_approved",   # ДО записи в журнал
                   details={"receipt_id": rid, "points": pts})
    elif action == "reject":
        if r.status not in ("pending", "verified"):
            raise HTTPException(409, "Отклонить можно чек с ручной проверкой")
        r.status = "rejected"
        r.status_message = ("Отклонено модератором"
                            + (f": {comment}" if comment else ""))
        db.commit()
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
    db.commit()
    log_action(admin, "pool_receipt_marked",
               details={"receipt_id": rid, "region": r.region_code,
                        "industry": r.industry})
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


# --------------------------------------------------------------------------
# v1.39.0: платное API — ключи партнёров (хэш в БД, полный ключ виден
# один раз), лимиты по тарифу, счётчики вызовов. Выдача/отзыв — в аудите.
# --------------------------------------------------------------------------
@router.get("/api-keys", summary="Чек-Пул: ключи платного API (админ)")
def api_keys_list(db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    from .models import PoolApiCall, PoolApiKey
    m_start = _utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    items = []
    for k in (db.query(PoolApiKey)
              .order_by(PoolApiKey.created_at.desc()).limit(200).all()):
        used_month = (db.query(PoolApiCall)
                      .filter(PoolApiCall.key_id == k.id,
                              PoolApiCall.created_at >= m_start).count())
        items.append({
            "id": k.id, "name": k.name, "prefix": k.prefix, "active": k.active,
            "rate_per_hour": k.rate_per_hour, "monthly_quota": k.monthly_quota,
            "used_month": used_month,
            "last_used_at": (k.last_used_at.strftime("%d.%m.%Y %H:%M")
                             if k.last_used_at else "—"),
            "created_at": (k.created_at.strftime("%d.%m.%Y")
                           if k.created_at else ""),
        })
    return {"items": items}


@router.post("/api-keys", summary="Чек-Пул: выдать ключ API (админ)")
def api_keys_create(body: dict, db: Session = Depends(get_db),
                    admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .router_api import DEFAULT_QUOTA, DEFAULT_RATE, generate_key
    name = ((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Укажите название партнёра")
    rate = (body or {}).get("rate_per_hour") or DEFAULT_RATE
    quota = (body or {}).get("monthly_quota") or DEFAULT_QUOTA
    try:
        rate, quota = max(1, int(rate)), max(1, int(quota))
    except (TypeError, ValueError):
        raise HTTPException(422, "Лимиты — целые числа")
    rec, raw = generate_key(db, name, created_by=admin.id,
                            rate_per_hour=rate, monthly_quota=quota)
    db.commit()                    # до аудита: вторая сессия ждёт блокировку
    log_action(admin, "pool_api_key_created",
               details={"name": name, "prefix": rec.prefix,
                        "rate_per_hour": rate, "monthly_quota": quota})
    return {"ok": True, "id": rec.id, "key": raw, "prefix": rec.prefix,
            "message": "Ключ выдан. Покажите его партнёру сейчас — "
                       "полностью он больше не виден (в БД только хэш)"}


@router.post("/api-keys/{kid}/revoke",
             summary="Чек-Пул: отозвать ключ API (админ)")
def api_keys_revoke(kid: str, db: Session = Depends(get_db),
                    admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .models import PoolApiKey
    k = db.get(PoolApiKey, kid)
    if k is None:
        raise HTTPException(404, "Ключ не найден")
    k.active = False
    db.commit()
    log_action(admin, "pool_api_key_revoked",
               details={"name": k.name, "prefix": k.prefix})
    return {"ok": True, "message": f"Ключ {k.prefix} отозван"}


# --------------------------------------------------------------------------
#  v1.41.0: ПРОСМОТР КАБИНЕТА УЧАСТНИКА (только администратор).
#  Полная картина ролей системы: администратор / бухгалтер / сотрудник
#  (ядро) и участник Чек-Пула (отдельный контур, самостоятельная
#  регистрация). Администратору выдаётся pool-JWT участника: ядро-логин
#  админа сохраняется, кабинет пула подхватывает токен участника — видно
#  ровно то, что видит он. Цепочки исключены: pool-токен не проходит
#  ядро (typ≠pool), админ-эндпоинты пула проверяют require_admin.
#  Включение и выход — в журнале действий ядра.
# --------------------------------------------------------------------------
@router.post("/impersonate-pool/stop",
             summary="Чек-Пул: выйти из просмотра кабинета участника (админ)")
def impersonate_pool_stop(body: dict, admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    pid = str((body or {}).get("participant_id") or "")
    log_action(admin, "impersonate_pool_stop",
               details={"participant_id": pid})
    return {"ok": True,
            "message": "Просмотр кабинета участника завершён"}


@router.get("/participants",
            summary="Чек-Пул: список участников (админ, для режима просмотра)")
def participants(q: str = "", limit: int = Query(50, ge=1, le=200),
                 db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    from sqlalchemy import func
    from .models import PoolReceipt, PoolUser
    rows = (db.query(PoolUser)
              .order_by(PoolUser.created_at.desc())
              .limit(500).all())
    ql = (q or "").strip().lower()          # кириллица: сравнение в Python
    items = []
    for u in rows:
        email = u.email or ""
        nick = u.tg_username or ""
        if ql and ql not in email.lower() and ql not in nick.lower():
            continue
        rcpt = db.query(func.count(PoolReceipt.id)).filter(
            PoolReceipt.pool_user_id == u.id).scalar() or 0
        items.append({
            "id": u.id, "email": email,
            "email_verified": bool(u.email_verified),
            "nickname": nick,
            "points": int(u.points or 0), "receipts": int(rcpt),
            "is_blocked": bool(u.is_blocked),
            "risk_score": int(u.risk_score or 0),
            "created_at": (u.created_at.isoformat() if u.created_at else None),
        })
        if len(items) >= limit:
            break
    return {"items": items, "total": len(items)}


@router.post("/impersonate-pool/{pid}",
             summary="Чек-Пул: посмотреть кабинет глазами участника (админ)")
def impersonate_pool_start(pid: str, db: Session = Depends(get_db),
                           admin: User = Depends(require_admin)):
    from ..services.audit import log_action
    from .accounts import issue_pool_token
    from .models import PoolUser
    u = db.get(PoolUser, pid)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Участник не найден")
    if u.is_blocked:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Участник заблокирован — кабинет недоступен")
    db.commit()                    # аудит отдельной сессией — коммитим заранее
    log_action(admin, "impersonate_pool_start",
               details={"participant_id": u.id, "email": u.email})
    return {"ok": True,
            "pool_token": issue_pool_token(u.id),
            "user": {"id": u.id, "email": u.email, "points": int(u.points or 0)},
            "act": {"sub": admin.id, "username": admin.username},
            "message": ("Режим просмотра: кабинет участника "
                        + (u.email or u.id))}
