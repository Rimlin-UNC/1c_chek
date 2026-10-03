# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — компании-клиенты (мультикомпанийность, v1.11.0).
#
# Платформа аутсорсинга бухгалтерии: администратор ведёт неограниченное
# число компаний (ООО, ИП); каждая — изолированное пространство со своими
# сотрудниками и чеками. Модель изоляции: общая БД, разграничение по
# company_id на уровне КАЖДОГО запроса (см. services/scoping.py) —
# стандартный подход SaaS для этой связки технологий.
#
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_USER, require_admin
from ..database import get_db
from ..models import Company, Receipt, User
from ..schemas import CompanyCreate, CompanyPatch
from ..services.audit import log_action

router = APIRouter(prefix="/api/v1/companies", tags=["Компании"])


def _stats(db: Session, company_ids: list[str]) -> dict[str, dict]:
    """Чеки/сумма/последняя активность по списку компаний (одним запросом)."""
    if not company_ids:
        return {}
    rows = (db.query(Receipt.company_id,
                     func.count(Receipt.id),
                     func.coalesce(func.sum(Receipt.total_sum), 0.0),
                     func.max(Receipt.created_at))
            .filter(Receipt.company_id.in_(company_ids))
            .group_by(Receipt.company_id).all())
    return {r[0]: {"receipts": int(r[1]), "sum": round(float(r[2]), 2),
                   "last_activity": r[3].isoformat() if r[3] else None}
            for r in rows}


def _team(db: Session, company_ids: list[str]) -> dict[str, dict]:
    """Сотрудники по компаниям: бухгалтеры / пользователи / активные."""
    if not company_ids:
        return {}
    rows = (db.query(User.company_id, User.role, func.count(User.id))
            .filter(User.company_id.in_(company_ids), User.is_active == True)  # noqa: E712
            .group_by(User.company_id, User.role).all())
    out: dict[str, dict] = {cid: {"accountants": 0, "users": 0} for cid in company_ids}
    for cid, role, cnt in rows:
        if cid not in out:
            continue
        if role == ROLE_ACCOUNTANT:
            out[cid]["accountants"] = int(cnt)
        elif role == ROLE_USER:
            out[cid]["users"] = int(cnt)
    return out


@router.get("", summary="Список компаний со статистикой (администратор)")
def list_companies(admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    comps = db.query(Company).order_by(Company.created_at).all()
    ids = [c.id for c in comps]
    receipts = _stats(db, ids)
    teams = _team(db, ids)
    return [{
        **c.to_dict(),
        "receipts": receipts.get(c.id, {}).get("receipts", 0),
        "receipts_sum": receipts.get(c.id, {}).get("sum", 0.0),
        "last_activity": receipts.get(c.id, {}).get("last_activity"),
        "accountants": teams.get(c.id, {}).get("accountants", 0),
        "users": teams.get(c.id, {}).get("users", 0),
    } for c in comps]


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Создать компанию-клиента (администратор)")
def create_company(body: CompanyCreate, admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Название компании слишком короткое")
    # SQLite lower() не понимает кириллицу — сравниваем в Python (casefold)
    name_cf = name.casefold()
    if any(c.name.casefold() == name_cf for c in db.query(Company).all()):
        raise HTTPException(status.HTTP_409_CONFLICT, "Компания с таким названием уже есть")
    comp = Company(name=name, inn=body.inn.strip(), note=body.note.strip())
    db.add(comp)
    db.commit()
    db.refresh(comp)
    log_action(admin, "company_created", "company", comp.id, {"name": comp.name})
    return {**comp.to_dict(), "receipts": 0, "receipts_sum": 0.0,
            "last_activity": None, "accountants": 0, "users": 0}


@router.patch("/{company_id}", summary="Изменить компанию (администратор)")
def patch_company(company_id: str, body: CompanyPatch,
                  admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    if body.name is not None:
        name = body.name.strip()
        if len(name) < 2:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Название компании слишком короткое")
        name_cf = name.casefold()
        dup = any(c.name.casefold() == name_cf and c.id != comp.id
                  for c in db.query(Company).all())
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Компания с таким названием уже есть")
        comp.name = name
    if body.inn is not None:
        comp.inn = body.inn.strip()
    if body.note is not None:
        comp.note = body.note.strip()
    if body.is_active is not None:
        if body.is_active is False:
            # Архивировать можно; последняя активная — нет (система остаётся
            # хотя бы с одной рабочей компанией)
            active = db.query(func.count(Company.id)).filter(
                Company.is_active == True, Company.id != comp.id).scalar()  # noqa: E712
            if not active:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "Нельзя архивировать последнюю активную компанию")
            comp.is_active = False
        else:
            comp.is_active = True
    db.commit()
    log_action(admin, "company_updated", "company", comp.id,
               {"name": comp.name, "is_active": comp.is_active})
    return comp.to_dict()


@router.get("/{company_id}/users", summary="Сотрудники компании (администратор)")
def company_users(company_id: str, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    rows = (db.query(User).filter(User.company_id == company_id)
            .order_by(User.role, User.username).all())
    return [u.to_dict() for u in rows]
