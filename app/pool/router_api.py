# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — платное API для внешних клиентов (v1.39.0, Этап 9.1).
#
# Принцип (plan.md, разд. 10): НЕ продаём чеки — продаём доступ к
# анонимным агрегатам и фильтрам. Наружу не уходят: сырые чеки, позиции,
# участники, e-mail, идентификаторы; только счётчики, суммы-агрегаты и
# обезличенные топы продавцов (ИНН/имя магазина — публичные данные).
#
# Доступ: заголовок X-API-Key (ключ «apk_…»; в БД только sha256,
# показывается один раз при выдаче). Тариф: лимит запросов/час и квота
# /мес (на ключ). Каждое обращение — в журнал pool_api_calls (path без
# query, чистка старше 90 дней — 152-ФЗ). Включается администратором,
# по умолчанию выключено.
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from . import ingest
from .models import PoolApiCall, PoolApiKey, PoolReceipt

router = APIRouter(prefix="/api/v1/pool-api", tags=["pool-api"])

API_ENABLED_KEY = "pool_api_enabled"
DEFAULT_RATE = 60            # запросов/час — тариф по умолчанию
DEFAULT_QUOTA = 5000         # запросов/мес
CALLS_RETENTION_DAYS = 90    # 152-ФЗ: журнал вызовов ≤ 90 дней
KEY_PREFIX = "apk_"


def _utcnow() -> datetime:
    return datetime.utcnow()


def _month_start(now: datetime | None = None) -> datetime:
    n = now or _utcnow()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def api_enabled(db: Session) -> bool:
    from ..services import appsettings
    return (appsettings.get_setting(db, API_ENABLED_KEY, "0") == "1")


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key(db: Session, name: str, created_by: str = "",
                 rate_per_hour: int = DEFAULT_RATE,
                 monthly_quota: int = DEFAULT_QUOTA) -> tuple[PoolApiKey, str]:
    """Новый ключ: возвращает (запись, ПОЛНЫЙ ключ — показывается один раз)."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    rec = PoolApiKey(name=(name or "").strip()[:200] or "партнёр",
                     key_hash=_hash_key(raw),
                     prefix=raw[:12] + "…",
                     active=True,
                     rate_per_hour=max(1, int(rate_per_hour or DEFAULT_RATE)),
                     monthly_quota=max(1, int(monthly_quota or DEFAULT_QUOTA)),
                     created_by=created_by or "")
    db.add(rec)
    db.flush()
    return rec, raw


def _authenticate(db: Session, x_api_key: str) -> PoolApiKey:
    """Проверка ключа + включённости API + лимитов (час/месяц)."""
    if not api_enabled(db):
        raise HTTPException(403, "API для внешних клиентов выключен — "
                                 "обратитесь к администратору")
    raw = (x_api_key or "").strip()
    if not raw:
        raise HTTPException(401, "Укажите ключ в заголовке X-API-Key")
    key = (db.query(PoolApiKey)
           .filter(PoolApiKey.key_hash == _hash_key(raw),
                   PoolApiKey.active.is_(True)).first())
    if key is None:
        raise HTTPException(401, "Ключ недействителен или отозван")
    now = _utcnow()
    hour_ago = now - timedelta(hours=1)
    used_hour = (db.query(PoolApiCall)
                 .filter(PoolApiCall.key_id == key.id,
                         PoolApiCall.created_at >= hour_ago).count())
    if used_hour >= key.rate_per_hour:
        raise HTTPException(429, f"Лимит тарифа: {key.rate_per_hour} "
                                 "запросов/час")
    used_month = (db.query(PoolApiCall)
                  .filter(PoolApiCall.key_id == key.id,
                          PoolApiCall.created_at >= _month_start(now)).count())
    if used_month >= key.monthly_quota:
        raise HTTPException(429, f"Квота тарифа исчерпана: "
                                 f"{key.monthly_quota} запросов/мес")
    key.last_used_at = now
    return key


def _log_call(db: Session, key: PoolApiKey, path: str) -> None:
    db.add(PoolApiCall(key_id=key.id, path=(path or "")[:120]))


def _require_enabled(db: Session) -> None:
    if not ingest.pool_enabled(db):
        raise HTTPException(403, "Приём чеков выключен — агрегаты недоступны")


# --- общий каркас эндпоинтов -------------------------------------------------
def _stats_filter(db: Session, f: dict):
    """База для агрегатов: верифицированные чеки по фильтрам."""
    q = db.query(PoolReceipt).filter(PoolReceipt.status == "verified")
    if f.get("date_from"):
        try:
            q = q.filter(PoolReceipt.receipt_date >= datetime.strptime(
                f["date_from"], "%Y-%m-%d"))
        except ValueError:
            pass
    if f.get("date_to"):
        try:
            dt = datetime.strptime(f["date_to"], "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(PoolReceipt.receipt_date < dt)
        except ValueError:
            pass
    if f.get("region"):
        q = q.filter(PoolReceipt.region_code == f["region"].strip())
    if f.get("city"):
        # SQL lower() не понимает кириллицу — фильтруем после выборки
        pass
    if f.get("industry"):
        q = q.filter(PoolReceipt.industry == f["industry"].strip())
    if f.get("sum_min") is not None:
        try:
            q = q.filter(PoolReceipt.total_sum >= float(f["sum_min"]))
        except (TypeError, ValueError):
            pass
    if f.get("sum_max") is not None:
        try:
            q = q.filter(PoolReceipt.total_sum <= float(f["sum_max"]))
        except (TypeError, ValueError):
            pass
    if f.get("inn"):
        q = q.filter(PoolReceipt.merchant_inn == f["inn"].strip())
    return q


def _apply_city(rows: list, city: str) -> list:
    c = (city or "").strip().lower()
    if not c:
        return rows
    return [r for r in rows if c in (r.city or "").lower()]


class _F(BaseModel):
    date_from: str = ""
    date_to: str = ""
    region: str = ""
    city: str = ""
    industry: str = ""
    inn: str = ""
    sum_min: float | None = None
    sum_max: float | None = None


@router.get("/v1/stats/overview",
            summary="API: сводка пула (анонимные агрегаты)")
def stats_overview(days: int = 30,
                   x_api_key: str = Header("", alias="X-API-Key"),
                   db: Session = Depends(get_db)):
    key = _authenticate(db, x_api_key)
    _log_call(db, key, "v1/stats/overview")
    db.commit()
    _require_enabled(db)
    since = _utcnow() - timedelta(days=max(1, min(days, 365)))
    q = db.query(PoolReceipt)
    total = q.count()
    verified = q.filter(PoolReceipt.status == "verified").count()
    users = (db.query(func.count(func.distinct(PoolReceipt.pool_user_id)))
             .filter(PoolReceipt.pool_user_id.isnot(None)).scalar() or 0)
    geo = (db.query(PoolReceipt)
           .filter(PoolReceipt.status == "verified",
                   PoolReceipt.region_code != "").count())
    month = (db.query(PoolReceipt)
             .filter(PoolReceipt.status == "verified",
                     func.coalesce(PoolReceipt.verified_at,
                                   PoolReceipt.created_at) >= since).count())
    avg = (db.query(func.avg(PoolReceipt.total_sum))
           .filter(PoolReceipt.status == "verified").scalar()) or 0.0
    return {"aggregated": True,
            "receipts_total": total, "receipts_verified": verified,
            "participants": users,
            "geo_coverage_pct": round(100 * geo / verified, 1) if verified else 0,
            "verified_in_period": month, "period_days": days,
            "avg_receipt_sum": round(float(avg), 2)}


@router.get("/v1/stats/regions",
            summary="API: чеки по регионам (анонимные агрегаты)")
def stats_regions(days: int = 30, limit: int = 12,
                  x_api_key: str = Header("", alias="X-API-Key"),
                  db: Session = Depends(get_db)):
    key = _authenticate(db, x_api_key)
    _log_call(db, key, "v1/stats/regions")
    db.commit()
    _require_enabled(db)
    from .geo import region_name
    since = _utcnow() - timedelta(days=max(1, min(days, 365)))
    rows = (db.query(PoolReceipt.region_code, func.count(PoolReceipt.id))
            .filter(PoolReceipt.status == "verified",
                    PoolReceipt.region_code != "",
                    func.coalesce(PoolReceipt.verified_at,
                                  PoolReceipt.created_at) >= since)
            .group_by(PoolReceipt.region_code)
            .order_by(func.count(PoolReceipt.id).desc())
            .limit(max(1, min(limit, 95))).all())
    return {"aggregated": True, "period_days": days,
            "items": [{"region_code": code, "region": region_name(code),
                       "receipts": n} for code, n in rows]}


@router.get("/v1/stats/industries",
            summary="API: чеки по отраслям (анонимные агрегаты)")
def stats_industries(days: int = 30, limit: int = 18,
                     x_api_key: str = Header("", alias="X-API-Key"),
                     db: Session = Depends(get_db)):
    key = _authenticate(db, x_api_key)
    _log_call(db, key, "v1/stats/industries")
    db.commit()
    _require_enabled(db)
    from .geo import industry_name
    since = _utcnow() - timedelta(days=max(1, min(days, 365)))
    rows = (db.query(PoolReceipt.industry, func.count(PoolReceipt.id),
                     func.avg(PoolReceipt.total_sum))
            .filter(PoolReceipt.status == "verified",
                    PoolReceipt.industry != "",
                    func.coalesce(PoolReceipt.verified_at,
                                  PoolReceipt.created_at) >= since)
            .group_by(PoolReceipt.industry)
            .order_by(func.count(PoolReceipt.id).desc())
            .limit(max(1, min(limit, 50))).all())
    return {"aggregated": True, "period_days": days,
            "items": [{"industry": code, "name": industry_name(code),
                       "receipts": n, "avg_sum": round(float(a or 0), 2)}
                      for code, n, a in rows]}


@router.get("/v1/stats/merchants",
            summary="API: топ продавцов (публичные реквизиты, без чеков)")
def stats_merchants(days: int = 30, limit: int = 10,
                    x_api_key: str = Header("", alias="X-API-Key"),
                    db: Session = Depends(get_db)):
    key = _authenticate(db, x_api_key)
    _log_call(db, key, "v1/stats/merchants")
    db.commit()
    _require_enabled(db)
    since = _utcnow() - timedelta(days=max(1, min(days, 365)))
    rows = (db.query(PoolReceipt.merchant_name, PoolReceipt.merchant_inn,
                     func.count(PoolReceipt.id),
                     func.avg(PoolReceipt.total_sum))
            .filter(PoolReceipt.status == "verified",
                    func.coalesce(PoolReceipt.verified_at,
                                  PoolReceipt.created_at) >= since,
                    PoolReceipt.merchant_name != "")
            .group_by(PoolReceipt.merchant_name, PoolReceipt.merchant_inn)
            .order_by(func.count(PoolReceipt.id).desc())
            .limit(max(1, min(limit, 50))).all())
    return {"aggregated": True, "period_days": days,
            "items": [{"merchant": (name or "")[:120], "inn": inn or "",
                       "receipts": n, "avg_sum": round(float(a or 0), 2)}
                      for name, inn, n, a in rows]}


@router.post("/v1/filters/count",
             summary="API: сколько чеков под фильтрами (без самих чеков)")
def filters_count(body: _F, x_api_key: str = Header("", alias="X-API-Key"),
                  db: Session = Depends(get_db)):
    key = _authenticate(db, x_api_key)
    _log_call(db, key, "v1/filters/count")
    db.commit()
    _require_enabled(db)
    f = body.model_dump()
    rows = _apply_city(_stats_filter(db, f).all(), f.get("city", ""))
    return {"aggregated": True, "count": len(rows)}


@router.post("/v1/filters/aggregate",
             summary="API: агрегаты под фильтрами (сумма/среднее/мин/макс)")
def filters_aggregate(body: _F, x_api_key: str = Header("", alias="X-API-Key"),
                      db: Session = Depends(get_db)):
    key = _authenticate(db, x_api_key)
    _log_call(db, key, "v1/filters/aggregate")
    db.commit()
    _require_enabled(db)
    f = body.model_dump()
    rows = _apply_city(_stats_filter(db, f).all(), f.get("city", ""))
    sums = [float(r.total_sum or 0) for r in rows]
    if not sums:
        return {"aggregated": True, "count": 0, "sum": 0.0, "avg": 0.0,
                "min": 0.0, "max": 0.0,
                "by_industry": []}
    ind: dict[str, int] = {}
    for r in rows:
        if (r.industry or "").strip():
            ind[r.industry] = ind.get(r.industry, 0) + 1
    by_ind = sorted(ind.items(), key=lambda kv: -kv[1])[:20]
    from .geo import industry_name
    return {"aggregated": True, "count": len(rows),
            "sum": round(sum(sums), 2), "avg": round(sum(sums) / len(sums), 2),
            "min": round(min(sums), 2), "max": round(max(sums), 2),
            "by_industry": [{"industry": code, "name": industry_name(code),
                             "receipts": n} for code, n in by_ind]}


# --- служебное ----------------------------------------------------------------
def purge_calls(db: Session, days: int = CALLS_RETENTION_DAYS) -> int:
    """Журнал вызовов старше N дней (152-ФЗ: техданные)."""
    edge = _utcnow() - timedelta(days=days)
    return (db.query(PoolApiCall)
            .filter(PoolApiCall.created_at < edge).delete(
                synchronize_session=False))
