# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — «Подбор из пула» для компаний (v1.37.0, Этап 8).
# Бухгалтер подбирает чеки из открытой базы под авансовые отчёты:
# фильтры (период/регион/город/отрасль/сумма/ИНН/поиск), привязка к
# компании (assigned_company_id), авто-подбор под сумму ±5%, выгрузка
# CSV для АО-1, сценарий C («свои» сотрудники по подтверждённому e-mail).
#
# Тариф решается при запуске (подписка 5 000 ₽/мес или 50 ₽/чек) —
# квота задаётся настройкой pool_pick_monthly_limit (админ). Привязанный
# чек чужим компаниям не виден; участник сохраняет свои баллы.
# Права: бухгалтер и администратор (ядро, require_accountant).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import require_accountant
from ..database import get_db
from ..models import Company, User
from . import ingest
from .models import PoolItem, PoolReceipt, PoolUser

router = APIRouter(prefix="/api/v1/pool-company", tags=["pool-company"])

PICK_LIMIT_KEY = "pool_pick_monthly_limit"
PICK_LIMIT_DEFAULT = 100        # план: подписка 5 000 ₽/мес ≈ 100 чеков
ASSIGN_BATCH = 50               # максимум чеков в одной заявке
SUGGEST_POOL = 500              # сколько кандидатов берём в авто-подбор
TARIFF_NOTE = ("Тариф решается при запуске: подписка 5 000 ₽/мес "
               "или 50 ₽/чек")


def _utcnow() -> datetime:
    return datetime.utcnow()


def pick_limit(db: Session) -> int:
    """Месячный лимит подбора на компанию (настройка, 0 = подбор выключен)."""
    from ..services import appsettings
    try:
        return max(0, int(appsettings.get_setting(
            db, PICK_LIMIT_KEY, str(PICK_LIMIT_DEFAULT))))
    except (TypeError, ValueError):
        return PICK_LIMIT_DEFAULT


def _month_start(now: datetime | None = None) -> datetime:
    n = now or _utcnow()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_used(db: Session, company_id: str) -> int:
    """Сколько чеков компании привязано сейчас из подобранных в этом
    месяце (сценарий C считается: чек тоже ушёл компании)."""
    return (db.query(PoolReceipt)
            .filter(PoolReceipt.assigned_company_id == company_id,
                    PoolReceipt.assigned_at >= _month_start()).count())


def _company(db: Session, user: User, company_id: str = "") -> Company:
    """Компания запроса: бухгалтер — своя; админ — указанная в параметре."""
    cid = (company_id or "").strip() or user.company_id
    if user.role == "admin" and not (company_id or "").strip():
        cid = None                       # админ выбирает компанию явно
    if not cid:
        raise HTTPException(403, "Укажите компанию (для бухгалтера она "
                                 "назначена администратором)")
    comp = db.get(Company, cid)
    if comp is None or not comp.is_active:
        raise HTTPException(403, "Компания не найдена или отключена")
    if user.role != "admin" and user.company_id != comp.id:
        raise HTTPException(403, "Доступ только к своей компании")
    return comp


def _enabled(db: Session) -> None:
    if not ingest.pool_enabled(db):
        raise HTTPException(403, "Приём чеков в пул выключен — подбор "
                                 "недоступен")


def _like(col, needle: str):
    from .router_admin import _or_like
    return _or_like(col, needle)


def _filtered(db: Session, f: dict, limit: int = SUGGEST_POOL):
    """Незакреплённые верифицированные чеки по фильтрам подбора."""
    q = db.query(PoolReceipt).filter(PoolReceipt.status == "verified",
                                     PoolReceipt.assigned_company_id.is_(None))
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
    if f.get("industry"):
        q = q.filter(PoolReceipt.industry == f["industry"].strip())
    if f.get("inn"):
        q = q.filter(PoolReceipt.merchant_inn == f["inn"].strip())
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
    rows = q.order_by(PoolReceipt.receipt_date.desc()).limit(limit).all()
    # город и поиск — в Python: SQL lower() не понимает кириллицу
    city = (f.get("city") or "").strip().lower()
    needle = (f.get("q") or "").strip().lower()
    if city:
        rows = [r for r in rows if city in (r.city or "").lower()]
    if needle:
        ids = {r.id for r in rows}
        hits = {it.receipt_id for it in db.query(PoolItem)
                .filter(PoolItem.receipt_id.in_(ids)).all()
                if needle in (it.name or "").lower()}
        rows = [r for r in rows
                if needle in (r.merchant_name or "").lower() or r.id in hits]
    return rows


def _row(r: PoolReceipt, names: str = "") -> dict:
    from .geo import industry_name, region_name
    return {
        "id": r.id,
        "date": (r.receipt_date.strftime("%d.%m.%Y %H:%M")
                 if r.receipt_date else ""),
        "merchant": r.merchant_name or "",
        "inn": r.merchant_inn or "",
        "sum": r.total_sum or 0.0,
        "region": region_name(r.region_code) if r.region_code else "",
        "city": r.city or "",
        "industry": industry_name(r.industry) if r.industry else "",
        "items": names,
    }


def _names_for(db: Session, ids: list[str]) -> dict[str, str]:
    """Позиции чеков: «Название × кол-во; …» (до трёх на чек)."""
    if not ids:
        return {}
    rows = (db.query(PoolItem)
            .filter(PoolItem.receipt_id.in_(ids))
            .order_by(PoolItem.total.desc()).all())
    out: dict[str, list[str]] = {}
    for it in rows:
        if len(out.get(it.receipt_id, [])) < 3:
            qty = f"{it.quantity:g}" if it.quantity else "1"
            out.setdefault(it.receipt_id, []).append(
                f"{(it.name or '')[:60]} × {qty}")
    return {k: "; ".join(v) for k, v in out.items()}


class IdsBody(BaseModel):
    receipt_ids: list[str] = []


class AutoBody(BaseModel):
    target_sum: float = 0
    date_from: str = ""
    date_to: str = ""
    region: str = ""
    city: str = ""
    industry: str = ""
    inn: str = ""


@router.get("/dicts", summary="Чек-Пул: справочники регионов и отраслей")
def dicts(user: User = Depends(require_accountant)):
    from .geo import industries, regions
    return {
        "regions": [{"code": r["code"], "name": r["name"]} for r in regions()],
        "industries": [{"code": i["code"], "name": i["name"]}
                       for i in industries()],
    }


@router.get("/quota", summary="Чек-Пул: квота подбора компании")
def quota(company_id: str = "", db: Session = Depends(get_db),
          user: User = Depends(require_accountant)):
    comp = _company(db, user, company_id)
    limit = pick_limit(db)
    used = month_used(db, comp.id)
    mine_total = (db.query(PoolReceipt)
                  .filter(PoolReceipt.assigned_company_id == comp.id).count())
    return {"company": {"id": comp.id, "name": comp.name},
            "limit": limit, "used": used, "left": max(0, limit - used),
            "mine_total": mine_total, "tariff": TARIFF_NOTE}


@router.get("/search", summary="Чек-Пул: подбор чеков (незакреплённые)")
def search(date_from: str = "", date_to: str = "", region: str = "",
           city: str = "", industry: str = "", inn: str = "",
           sum_min: float | None = Query(None), sum_max: float | None = Query(None),
           q_str: str = Query("", alias="q", max_length=100),
           page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50),
           company_id: str = "", db: Session = Depends(get_db),
           user: User = Depends(require_accountant)):
    _company(db, user, company_id)          # права; чеки — общие незакреплённые
    _enabled(db)
    f = {"date_from": date_from, "date_to": date_to, "region": region,
         "city": city, "industry": industry, "inn": inn,
         "sum_min": sum_min, "sum_max": sum_max, "q": q_str}
    rows = _filtered(db, f, limit=100000)
    total = len(rows)
    per = per_page
    start = (page - 1) * per
    chunk = rows[start:start + per]
    names = _names_for(db, [r.id for r in chunk])
    return {"total": total, "page": page, "per_page": per,
            "items": [_row(r, names.get(r.id, "")) for r in chunk]}


@router.get("/mine", summary="Чек-Пул: чеки, привязанные к компании")
def mine(date_from: str = "", date_to: str = "",
         page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50),
         company_id: str = "", db: Session = Depends(get_db),
         user: User = Depends(require_accountant)):
    comp = _company(db, user, company_id)
    q = db.query(PoolReceipt).filter(PoolReceipt.assigned_company_id == comp.id)
    if date_from:
        try:
            q = q.filter(PoolReceipt.receipt_date >= datetime.strptime(
                date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(PoolReceipt.receipt_date < dt)
        except ValueError:
            pass
    total = q.count()
    rows = (q.order_by(PoolReceipt.assigned_at.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    names = _names_for(db, [r.id for r in rows])
    items = []
    for r in rows:
        d = _row(r, names.get(r.id, ""))
        d["assigned_at"] = (r.assigned_at.strftime("%d.%m.%Y %H:%M")
                            if r.assigned_at else "")
        items.append(d)
    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.post("/assign", summary="Чек-Пул: привязать чеки к компании")
def assign(body: IdsBody, company_id: str = "", db: Session = Depends(get_db),
           user: User = Depends(require_accountant)):
    from ..services.audit import log_action
    comp = _company(db, user, company_id)
    _enabled(db)
    limit = pick_limit(db)
    if limit <= 0:
        raise HTTPException(403, "Подбор из пула выключен настройкой")
    ids = list(dict.fromkeys((body.receipt_ids or [])))[:ASSIGN_BATCH]
    if not ids:
        raise HTTPException(422, "Не выбрано ни одного чека")
    used = month_used(db, comp.id)
    if used + len(ids) > limit:
        raise HTTPException(409, f"Лимит тарифа: {limit} чеков/мес, "
                                 f"в этом месяце уже подобрано {used}. "
                                 + TARIFF_NOTE)
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.id.in_(ids),
                    PoolReceipt.status == "verified",
                    PoolReceipt.assigned_company_id.is_(None)).all())
    if not rows:
        raise HTTPException(409, "Выбранные чеки уже заняты — обновите список")
    now = _utcnow()
    for r in rows:
        r.assigned_company_id = comp.id
        r.assigned_at = now
        r.assigned_by = user.id
    log_action(user, "pool_pick_assign",
               details={"company": comp.name, "count": len(rows)})
    db.commit()
    return {"ok": True, "assigned": len(rows), "used": used + len(rows),
            "left": max(0, limit - used - len(rows)),
            "message": f"Привязано чеков: {len(rows)}. Выгрузка — во вкладке "
                       "«Мои чеки компании»"}


@router.post("/unassign", summary="Чек-Пул: вернуть чеки в общий пул")
def unassign(body: IdsBody, company_id: str = "",
             db: Session = Depends(get_db),
             user: User = Depends(require_accountant)):
    from ..services.audit import log_action
    comp = _company(db, user, company_id)
    ids = list(dict.fromkeys((body.receipt_ids or [])))[:ASSIGN_BATCH]
    if not ids:
        raise HTTPException(422, "Не выбрано ни одного чека")
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.id.in_(ids),
                    PoolReceipt.assigned_company_id == comp.id).all())
    for r in rows:
        r.assigned_company_id = None
        r.assigned_at = None
        r.assigned_by = ""
    log_action(user, "pool_pick_unassign",
               details={"company": comp.name, "count": len(rows)})
    db.commit()
    return {"ok": True, "unassigned": len(rows),
            "message": f"Возвращено в общий пул: {len(rows)} "
                       "(квота месяца восстанавливается)"}


@router.post("/autosuggest", summary="Чек-Пул: авто-подбор под сумму отчёта (±5%)")
def autosuggest(body: AutoBody, company_id: str = "",
                db: Session = Depends(get_db),
                user: User = Depends(require_accountant)):
    comp = _company(db, user, company_id)
    _enabled(db)
    target = float(body.target_sum or 0)
    if target < 100:
        raise HTTPException(422, "Сумма отчёта — от 100 ₽")
    f = {"date_from": body.date_from, "date_to": body.date_to,
         "region": body.region, "city": body.city, "industry": body.industry,
         "inn": body.inn, "q": ""}
    cands = _filtered(db, f)
    hi, lo = target * 1.05, target * 0.95
    picked: list = []
    acc = 0.0
    for r in sorted(cands, key=lambda x: -(x.total_sum or 0)):
        if acc + (r.total_sum or 0) <= hi:
            picked.append(r)
            acc += r.total_sum or 0
            if acc >= lo:
                break
    ok = bool(picked) and lo <= acc <= hi
    return {"ok": ok, "count": len(picked), "sum": round(acc, 2),
            "diff_pct": round((acc - target) / target * 100, 1) if picked else 0,
            "ids": [r.id for r in picked],
            "items": [_row(r) for r in picked[:50]],
            "message": ("Набор подобран" if ok else
                        "Подходящего набора нет — ослабьте фильтры "
                        "или измените сумму")}


@router.get("/export.csv", summary="Чек-Пул: выгрузка чеков компании (АО-1)")
def export_csv(date_from: str = "", date_to: str = "",
               company_id: str = "", db: Session = Depends(get_db),
               user: User = Depends(require_accountant)):
    import csv as _csv
    import io

    from fastapi import Response

    from ..services.audit import log_action
    comp = _company(db, user, company_id)
    q = db.query(PoolReceipt).filter(PoolReceipt.assigned_company_id == comp.id)
    if date_from:
        try:
            q = q.filter(PoolReceipt.receipt_date >= datetime.strptime(
                date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(PoolReceipt.receipt_date < dt)
        except ValueError:
            pass
    rows = q.order_by(PoolReceipt.receipt_date.desc()).limit(10000).all()
    names = _names_for(db, [r.id for r in rows])
    buf = io.StringIO()
    w = _csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Дата чека", "Магазин", "ИНН", "Сумма, ₽", "Город",
                "Отрасль", "Позиции"])
    for r in rows:
        w.writerow([
            r.receipt_date.strftime("%d.%m.%Y %H:%M") if r.receipt_date else "",
            r.merchant_name or "", r.merchant_inn or "",
            f"{r.total_sum:.2f}".replace(".", ","),
            r.city or "", names.get(r.id, "")])
    log_action(user, "pool_pick_export_csv",
               details={"company": comp.name, "count": len(rows)})
    content = "\ufeff" + buf.getvalue()
    filename = f"chek-pool-ao1-{_utcnow().strftime('%Y%m%d-%H%M')}.csv"
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename={filename}"})


@router.get("/employees", summary="Чек-Пул: «свои» сотрудники (сценарий C)")
def employees(company_id: str = "", db: Session = Depends(get_db),
              user: User = Depends(require_accountant)):
    """Сотрудники компании и привязка к пулу: если подтверждённый e-mail
    аккаунта пула совпадает с логином сотрудника, новые чеки сотрудника
    уходят компании, минуя общий пул."""
    comp = _company(db, user, company_id)
    us = (db.query(User).filter(User.company_id == comp.id,
                                User.is_active.is_(True))
          .order_by(User.full_name, User.username).all())
    verified = {pu.email.strip().lower()
                for pu in db.query(PoolUser)
                .filter(PoolUser.email_verified.is_(True)).all() if pu.email}
    return {"company": {"id": comp.id, "name": comp.name},
            "items": [{"username": u.username, "full_name": u.full_name,
                       "role": u.role,
                       "pool_linked": u.username.strip().lower() in verified}
                      for u in us]}


# --- сценарий C: хук для ingest ---------------------------------------------
def company_for_email(db: Session, email: str) -> Company | None:
    """E-mail → компания (логин сотрудника = e-mail, сотрудник активен)."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return None
    u = (db.query(User)
         .filter(func.lower(User.username) == e,
                 User.is_active.is_(True),
                 User.company_id.isnot(None)).first())
    if u is None:
        return None
    comp = db.get(Company, u.company_id)
    if comp is None or not comp.is_active:
        return None
    return comp


def assign_employee_receipt(db: Session, pool_user, receipt) -> bool:
    """Чек участника с подтверждённым e-mail сотрудника → компании.
    Баллы участника не меняются: чек остаётся его, но в общий подбор
    другим компаниям не попадает."""
    if receipt.assigned_company_id is not None:
        return False
    if not getattr(pool_user, "email_verified", False):
        return False
    comp = company_for_email(db, getattr(pool_user, "email", ""))
    if comp is None:
        return False
    receipt.assigned_company_id = comp.id
    receipt.assigned_at = _utcnow()
    receipt.assigned_by = "employee-link"
    receipt.status_message = (receipt.status_message +
                              f" · чек сотрудника компании «{comp.name}»")[:500]
    return True
