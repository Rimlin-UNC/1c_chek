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
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, false, func, or_
from sqlalchemy.orm import Session

from ..auth import require_accountant
from ..config import settings  # v1.70.38: PAGE_MAX
from ..database import get_db
from ..models import Company, Receipt, User
from . import ingest
from .models import PoolItem, PoolReceipt, PoolUser
from .participants import UNKNOWN_PARTICIPANT, participant_key, participant_label

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


def _filtered_query(db: Session, f: dict):
    """Незакреплённые верифицированные чеки по фильтрам подбора."""
    q = db.query(PoolReceipt).filter(PoolReceipt.status == "verified",
                                     PoolReceipt.assigned_company_id.is_(None))
    # Старые совпадения с реестром не предлагаем повторно, даже если запись
    # пула по историческим причинам не была закреплена.
    q = q.filter(~db.query(Receipt.id).filter(
        Receipt.fn == PoolReceipt.fn, Receipt.fd == PoolReceipt.fd,
        Receipt.fp == PoolReceipt.fp).exists())
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
    def folded(column):
        return (func.ym_casefold(column) if db.get_bind().dialect.name == "sqlite"
                else func.lower(column))

    def contains(column, value):
        term = value.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return folded(column).like(f"%{term}%", escape="\\")

    if (f.get("city") or "").strip():
        q = q.filter(contains(PoolReceipt.city, f["city"]))
    if (f.get("q") or "").strip():
        q = q.filter(or_(contains(PoolReceipt.merchant_name, f["q"]),
            db.query(PoolItem.id).filter(PoolItem.receipt_id == PoolReceipt.id,
                                        contains(PoolItem.name, f["q"])).exists()))
    item_conditions = []
    if (f.get("item_name") or "").strip():
        item_conditions.append(contains(PoolItem.name, f["item_name"]))
    if f.get("vat_rate"):
        item_conditions.append(PoolItem.vat_rate == f["vat_rate"])
    for key, column, is_min in (("item_total_min", PoolItem.total, True),
                                ("item_total_max", PoolItem.total, False),
                                ("item_price_min", PoolItem.price, True),
                                ("item_price_max", PoolItem.price, False),
                                ("item_quantity_min", PoolItem.quantity, True),
                                ("item_quantity_max", PoolItem.quantity, False)):
        if f.get(key) is not None:
            item_conditions.append(column >= f[key] if is_min else column <= f[key])
    if item_conditions:
        q = q.filter(db.query(PoolItem.id).filter(
            PoolItem.receipt_id == PoolReceipt.id, and_(*item_conditions)).exists())
    if f.get("operation") is not None:
        q = q.filter(PoolReceipt.operation == f["operation"])
    return q


def _filtered(db: Session, f: dict, limit: int = SUGGEST_POOL):
    return _filtered_query(db, f).order_by(PoolReceipt.receipt_date.desc(), PoolReceipt.id).limit(limit).all()


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
           item_name: str = Query("", max_length=200), vat_rate: str = Query("", max_length=8),
           item_total_min: float | None = Query(None, ge=0), item_total_max: float | None = Query(None, ge=0),
           item_price_min: float | None = Query(None, ge=0), item_price_max: float | None = Query(None, ge=0),
           item_quantity_min: float | None = Query(None, ge=0), item_quantity_max: float | None = Query(None, ge=0),
           operation: int | None = Query(None, ge=1, le=4),
           page: int = Query(1, ge=1, le=settings.PAGE_MAX), per_page: int = Query(20, ge=1, le=50),
           company_id: str = "", db: Session = Depends(get_db),
           user: User = Depends(require_accountant)):
    _company(db, user, company_id)          # права; чеки — общие незакреплённые
    _enabled(db)
    f = {"date_from": date_from, "date_to": date_to, "region": region,
         "city": city, "industry": industry, "inn": inn,
         "sum_min": sum_min, "sum_max": sum_max, "q": q_str,
         "item_name": item_name, "vat_rate": vat_rate, "item_total_min": item_total_min,
         "item_total_max": item_total_max, "item_price_min": item_price_min,
         "item_price_max": item_price_max, "item_quantity_min": item_quantity_min,
         "item_quantity_max": item_quantity_max, "operation": operation}
    query = _filtered_query(db, f)
    total = query.count()
    per = per_page
    start = (page - 1) * per
    chunk = query.order_by(PoolReceipt.receipt_date.desc(), PoolReceipt.id).offset(start).limit(per).all()
    names = _names_for(db, [r.id for r in chunk])
    return {"total": total, "page": page, "per_page": per,
            "items": [_row(r, names.get(r.id, "")) for r in chunk]}


def _mine_query(db: Session, company_id: str, date_from: str, date_to: str):
    """Область «Моих чеков компании»: назначение и период, без данных донора."""
    from ..services.scoping import canonical_pool_company_filter
    q = db.query(PoolReceipt).filter(PoolReceipt.assigned_company_id == company_id,
                                     canonical_pool_company_filter(company_id))
    if date_from:
        try:
            q = q.filter(PoolReceipt.receipt_date >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(PoolReceipt.receipt_date < dt)
        except ValueError:
            pass
    return q


def _participant_context(query, company_id: str):
    """Счётчики и внутреннее соответствие кодов исходным связям компании.

    Исходные ID не покидают сервер. Отсутствующая, пробельная или битая
    связь попадает в одну группу unknown, а не выдумывает участника.
    """
    rows = (query.with_entities(PoolReceipt.pool_user_id, PoolUser.id,
                                func.count(PoolReceipt.id))
            .outerjoin(PoolUser, PoolUser.id == PoolReceipt.pool_user_id)
            .group_by(PoolReceipt.pool_user_id, PoolUser.id).all())
    groups: dict[str, list[str | None]] = {}
    counts: dict[str, int] = {}
    known = {}
    for raw_id, existing_id, count in rows:
        key = (participant_key(company_id, raw_id)
               if existing_id is not None else UNKNOWN_PARTICIPANT)
        groups.setdefault(key, []).append(raw_id)
        counts[key] = counts.get(key, 0) + int(count)
        if key != UNKNOWN_PARTICIPANT:
            known[raw_id] = key
    facets = [{"key": key, "label": participant_label(key), "count": counts[key]}
              for key in sorted(groups, key=lambda k: (k == UNKNOWN_PARTICIPANT,
                                                       participant_label(k), k))]
    return facets, groups, known


def _apply_participant(query, participant: str, groups: dict[str, list[str | None]]):
    if not participant:
        return query
    raw_ids = groups.get(participant)
    if raw_ids is None:
        return query.filter(false())
    conditions = []
    named = [value for value in raw_ids if value is not None]
    if named:
        conditions.append(PoolReceipt.pool_user_id.in_(named))
    if None in raw_ids:
        conditions.append(PoolReceipt.pool_user_id.is_(None))
    return query.filter(or_(*conditions))


_PARTICIPANT_QUERY = Query("", max_length=32, pattern=r"^(?:[0-9a-f]{32}|unknown)?$")


@router.get("/mine", summary="Чек-Пул: чеки компании с обезличенной группировкой участников")
def mine(date_from: str = "", date_to: str = "",
         participant: str = _PARTICIPANT_QUERY,
         page: int = Query(1, ge=1, le=settings.PAGE_MAX), per_page: int = Query(20, ge=1, le=50),
         company_id: str = "", db: Session = Depends(get_db),
         user: User = Depends(require_accountant)):
    comp = _company(db, user, company_id)
    base = _mine_query(db, comp.id, date_from, date_to)
    # Список участников не зависит от выбранного участника: иначе из него
    # нельзя было бы переключиться обратно. Период и компания учитываются.
    facets, groups, known = _participant_context(base, comp.id)
    q = _apply_participant(base, participant, groups)
    total = q.count()
    rows = (q.order_by(PoolReceipt.assigned_at.desc(), PoolReceipt.id)
            .offset((page - 1) * per_page).limit(per_page).all())
    names = _names_for(db, [r.id for r in rows])
    items = []
    for r in rows:
        d = _row(r, names.get(r.id, ""))
        d["assigned_at"] = (r.assigned_at.strftime("%d.%m.%Y %H:%M")
                            if r.assigned_at else "")
        key = known.get(r.pool_user_id, UNKNOWN_PARTICIPANT)
        d["participant"], d["participant_label"] = key, participant_label(key)
        items.append(d)
    return {"total": total, "page": page, "per_page": per_page,
            "participants": facets, "items": items}


def _lock_pool_identities(db: Session, rows: list[PoolReceipt]) -> dict[str, tuple[str, str, str]]:
    """Заблокировать сохранённые тройки в том же порядке, что правка реестра."""
    from ..services.receipt_identity import lock_identity

    identities = {row.id: (row.fn, row.fd, row.fp) for row in rows}
    if any(not all(isinstance(part, str) and part.strip() for part in identity)
           for identity in identities.values()):
        raise HTTPException(409, "Некорректные фискальные реквизиты — нужна сверка")
    # Python-порядок совпадает с правкой старой и новой тройки реестра;
    # порядок SQL может зависеть от collation базы.
    for identity in sorted(set(identities.values())):
        lock_identity(db, *identity)
    return identities


def _check_locked_pool_identities(rows: list[PoolReceipt], identities: dict[str, tuple[str, str, str]]) -> None:
    if any(identities.get(row.id) != (row.fn, row.fd, row.fp) for row in rows):
        raise HTTPException(409, "Фискальные реквизиты чека изменились — обновите список")


def _refresh_locked_pool_rows(db: Session, identities: dict[str, tuple[str, str, str]]) -> list[PoolReceipt]:
    rows = db.query(PoolReceipt).populate_existing().filter(PoolReceipt.id.in_(identities)).all()
    if len(rows) != len(identities):
        raise HTTPException(409, "Чек изменён или удалён — обновите список")
    _check_locked_pool_identities(rows, identities)
    return rows


def _recheck_pool_company_access(db: Session, company_id: str, user_id: str) -> tuple[Company, User]:
    """После замка чека сверить и удержать действующие права до commit."""
    # Единый порядок: фискальные тройки, компания, пользователь. На SQLite
    # тройка удерживает write-lock; PostgreSQL дополнительно блокирует строки.
    company = (db.query(Company).populate_existing().filter(Company.id == company_id)
               .with_for_update().first())
    actor = (db.query(User).populate_existing().filter(User.id == user_id)
             .with_for_update().first())
    if (not company or not company.is_active or not actor or not actor.is_active
            or actor.role not in ("admin", "accountant")
            or (actor.role != "admin" and actor.company_id != company_id)):
        raise HTTPException(403, "Права или состояние компании изменились — обновите страницу")
    return company, actor


def _prepare_pool_assignment(db: Session, body: IdsBody, company_id: str, user: User, *,
                             allow_assigned: bool = False):
    """Проверить и назначить выбранные чеки; commit остаётся вызывающему."""
    comp = _company(db, user, company_id)
    _enabled(db)
    if pick_limit(db) <= 0:
        raise HTTPException(403, "Подбор из пула выключен настройкой")
    ids = list(dict.fromkeys(body.receipt_ids or []))
    if not ids or len(ids) > ASSIGN_BATCH:
        raise HTTPException(422, f"Выберите от 1 до {ASSIGN_BATCH} чеков")
    actor_id, selected_company_id = user.id, comp.id
    candidates = db.query(PoolReceipt).filter(PoolReceipt.id.in_(ids)).all()
    identities = _lock_pool_identities(db, candidates)
    db.expire_all()
    comp, user = _recheck_pool_company_access(db, selected_company_id, actor_id)
    _enabled(db)
    limit = pick_limit(db)
    if limit <= 0:
        raise HTTPException(403, "Подбор из пула выключен настройкой")
    rows = (db.query(PoolReceipt).filter(PoolReceipt.id.in_(ids), PoolReceipt.status == "verified").all())
    if len(rows) != len(ids):
        raise HTTPException(409, "Часть выбранных чеков уже занята — обновите список")
    _check_locked_pool_identities(rows, identities)
    new_rows = []
    for row in rows:
        if row.assigned_company_id not in (None, comp.id) or (row.assigned_company_id and not allow_assigned):
            raise HTTPException(409, "Один из чеков уже закреплён — обновите список")
        existing = db.query(Receipt).filter_by(fn=row.fn, fd=row.fd, fp=row.fp).first()
        if existing:
            # Повтор единого запроса допустим только для явно подтверждённого
            # прежнего переноса этого источника в ту же компанию.
            try:
                raw = json.loads(existing.raw_data or "{}")
                provenance = raw.get("pool_transfer", {}) if isinstance(raw, dict) else {}
            except (ValueError, TypeError):
                provenance = {}
            if (not allow_assigned or row.assigned_company_id != comp.id or existing.company_id != comp.id
                    or existing.source != "pool" or not isinstance(provenance, dict)
                    or provenance.get("pool_receipt_id") != row.id):
                raise HTTPException(409, "Один из чеков уже есть в реестре — обновите список")
        if not row.assigned_company_id:
            new_rows.append(row)
    used = month_used(db, comp.id)
    if new_rows and used + len(new_rows) > limit:
        raise HTTPException(409, f"Лимит тарифа: {limit} чеков/мес, "
                                 f"в этом месяце уже подобрано {used}. " + TARIFF_NOTE)
    from ..unified import service as unified
    for row in rows:
        unified.claim_pool(db, row, comp, user, event_type="claim")
    return comp, user, rows, len(new_rows), used, limit


@router.post("/assign", summary="Чек-Пул: привязать чеки к компании")
def assign(body: IdsBody, company_id: str = "", db: Session = Depends(get_db),
           user: User = Depends(require_accountant)):
    from ..services.audit import log_action
    comp, user, rows, assigned, used, limit = _prepare_pool_assignment(db, body, company_id, user)
    db.commit()
    log_action(user, "pool_pick_assign", details={"company": comp.name, "count": assigned})
    return {"ok": True, "assigned": assigned, "used": used + assigned,
            "left": max(0, limit - used - assigned),
            "message": f"Привязано чеков: {assigned}. Выгрузка — во вкладке "
                       "«Мои чеки компании»"}


@router.post("/pick-and-transfer", summary="Чек-Пул: привязать и перенести чеки одной операцией")
def pick_and_transfer(body: IdsBody, db: Annotated[Session, Depends(get_db)],
                      user: Annotated[User, Depends(require_accountant)], company_id: str = ""):
    from ..services.audit import log_action
    from .transfer import copy_to_registry

    comp, user, rows, assigned, used, limit = _prepare_pool_assignment(
        db, body, company_id, user, allow_assigned=True)
    db.flush()  # назначение и перенос остаются в одной внешней транзакции
    created, existing, unconfirmed = 0, 0, 0
    for row in rows:
        receipt, was_created = copy_to_registry(db, row, comp.id, user)
        created += int(was_created)
        existing += int(not was_created)
        unconfirmed += int(was_created and receipt.fns_status != "valid")
    db.commit()
    log_action(user, "pool_pick_transfer", details={"company_id": comp.id, "assigned": assigned,
                                                     "created": created, "existing": existing,
                                                     "unconfirmed": unconfirmed})
    return {"ok": True, "assigned": assigned, "created": created, "existing": existing,
            "unconfirmed": unconfirmed, "used": used + assigned, "left": max(0, limit - used - assigned),
            "message": (f"Привязано: {assigned}; перенесено в реестр: {created}; уже было: {existing}. "
                        f"Без подтверждённого происхождения проверки ФНС: {unconfirmed}. "
                        "Запросов в ФНС не выполнялось.")}


@router.post("/unassign", summary="Чек-Пул: вернуть чеки в общий пул")
def unassign(body: IdsBody, company_id: str = "",
             db: Session = Depends(get_db),
             user: User = Depends(require_accountant)):
    from ..services.audit import log_action
    comp = _company(db, user, company_id)
    ids = list(dict.fromkeys(body.receipt_ids or []))
    if not ids or len(ids) > ASSIGN_BATCH:
        raise HTTPException(422, f"Выберите от 1 до {ASSIGN_BATCH} чеков")
    rows = (db.query(PoolReceipt)
            .filter(PoolReceipt.id.in_(ids),
                    PoolReceipt.assigned_company_id == comp.id).all())
    actor_id, selected_company_id = user.id, comp.id
    identities = _lock_pool_identities(db, rows)
    comp, user = _recheck_pool_company_access(db, selected_company_id, actor_id)
    rows = _refresh_locked_pool_rows(db, identities)
    for r in rows:
        if r.assigned_company_id != comp.id:
            raise HTTPException(409, "Чек больше не закреплён за компанией")
        if db.query(Receipt.id).filter_by(fn=r.fn, fd=r.fd, fp=r.fp).first():
            raise HTTPException(409, "Чек уже находится в реестре: сначала решите вопрос с его учётом")
    from ..unified import service as unified
    for r in rows:
        unified.release_pool(db, r, user)
    db.commit()
    log_action(user, "pool_pick_unassign",
               details={"company": comp.name, "count": len(rows)})
    return {"ok": True, "unassigned": len(rows),
            "message": f"Возвращено в общий пул: {len(rows)} "
                       "(квота месяца восстанавливается)"}


@router.post("/transfer", summary="Чек-Пул: перенести закреплённые чеки в реестр")
def transfer(body: IdsBody, db: Annotated[Session, Depends(get_db)],
             user: Annotated[User, Depends(require_accountant)], company_id: str = ""):
    from ..services.audit import log_action
    from .transfer import copy_to_registry

    comp = _company(db, user, company_id)
    ids = list(dict.fromkeys(body.receipt_ids or []))
    if not ids or len(ids) > ASSIGN_BATCH:
        raise HTTPException(422, f"Выберите от 1 до {ASSIGN_BATCH} чеков")
    rows = (db.query(PoolReceipt).filter(PoolReceipt.id.in_(ids),
            PoolReceipt.assigned_company_id == comp.id).all())
    if len(rows) != len(ids):
        raise HTTPException(404, "Некоторые чеки не найдены или недоступны")
    actor_id, selected_company_id = user.id, comp.id
    identities = _lock_pool_identities(db, rows)
    comp, user = _recheck_pool_company_access(db, selected_company_id, actor_id)
    rows = _refresh_locked_pool_rows(db, identities)
    created, existing, unconfirmed = 0, 0, 0
    for row in rows:
        receipt, was_created = copy_to_registry(db, row, comp.id, user)
        created += int(was_created)
        existing += int(not was_created)
        unconfirmed += int(was_created and receipt.fns_status != "valid")
    db.commit()
    log_action(user, "pool_transfer", details={"company_id": comp.id, "created": created,
                                                 "existing": existing, "unconfirmed": unconfirmed})
    return {"ok": True, "created": created, "existing": existing, "unconfirmed": unconfirmed,
            "message": (f"В реестр перенесено: {created}; уже было: {existing}. "
                        f"Без подтверждённого происхождения проверки ФНС: {unconfirmed}. "
                        "Запросов в ФНС не выполнялось.")}


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
        if len(picked) < ASSIGN_BATCH and acc + (r.total_sum or 0) <= hi:
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
               participant: str = _PARTICIPANT_QUERY,
               company_id: str = "", db: Session = Depends(get_db),
               user: User = Depends(require_accountant)):
    import csv as _csv
    import io

    from fastapi import Response

    from ..services.audit import log_action
    comp = _company(db, user, company_id)
    base = _mine_query(db, comp.id, date_from, date_to)
    _, groups, known = _participant_context(base, comp.id)
    q = _apply_participant(base, participant, groups)
    rows = q.order_by(PoolReceipt.receipt_date.desc(), PoolReceipt.id).limit(10000).all()
    names = _names_for(db, [r.id for r in rows])
    from .geo import industry_name
    buf = io.StringIO()
    w = _csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Дата чека", "Магазин", "ИНН", "Сумма, ₽", "Город",
                "Отрасль", "Позиции", "Участник (обезличенный код)"])
    for r in rows:
        w.writerow([
            r.receipt_date.strftime("%d.%m.%Y %H:%M") if r.receipt_date else "",
            r.merchant_name or "", r.merchant_inn or "",
            f"{r.total_sum:.2f}".replace(".", ","),
            r.city or "", industry_name(r.industry) if r.industry else "",
            names.get(r.id, ""), known.get(r.pool_user_id, UNKNOWN_PARTICIPANT)])
    db.commit()                    # чтение не пишет; фиксируем на всякий случай
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
