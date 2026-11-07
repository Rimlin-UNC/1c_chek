"""API понимания чеков (T-69, v1.70.101, ADR-31).

GET    /api/v1/goods/catalog                 — категории по группам, источники, профили, сведения о модели;
PUT    /api/v1/goods/items/{item_id}         — бухгалтер выбирает категорию позиции (запомнить для компании);
GET    /api/v1/goods/memory                  — что программа запомнила по исправлениям (бухгалтер+);
DELETE /api/v1/goods/memory/{memory_id}      — забыть запись.
Профиль компании — в настройке политики (/api/v1/settings/policy, поле goods_profile).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_accountant
from ..database import get_db
from ..models import Company, Receipt, ReceiptItem, User
from ..services.audit import log_action
from ..services.scoping import can_view_receipt
from . import service

router = APIRouter(prefix="/api/v1/goods", tags=["Понимание чеков"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]


class CategoryIn(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    remember: bool = True


def _fail(exc: service.GoodsError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _company_of(db: Session, user: User, company_id: str | None) -> str:
    """Компания запроса: администратор — любая (по параметру), бухгалтер — активная своя."""
    if user.role == "admin" and company_id:
        if db.get(Company, company_id[:36]) is None:
            raise HTTPException(404, "Компания не найдена")
        return company_id[:36]
    if not user.company_id:
        raise HTTPException(400, "Не выбрана компания")
    return user.company_id


@router.get("/catalog", summary="Категории товаров, источники и профили компании",
            dependencies=[Depends(get_current_user)])
def catalog() -> dict:
    return service.catalog()


@router.put("/items/{item_id}", summary="Категория позиции чека — выбор бухгалтера (запоминается для компании)")
def set_item_category(item_id: str, body: CategoryIn, user: Accountant, db: Db) -> dict:
    item = db.get(ReceiptItem, item_id[:36])
    receipt = db.get(Receipt, item.receipt_id) if item is not None else None
    if item is None or receipt is None or not can_view_receipt(user, receipt):
        raise HTTPException(404, "Позиция не найдена")
    before = item.goods_category or ""
    try:
        result = service.correct_item(db, user, receipt, item, body.category, remember=body.remember)
    except service.GoodsError as exc:
        raise _fail(exc) from exc
    db.commit()
    log_action(user, "goods_category_set", "receipt", receipt.id,
               {"item_id": item.id, "before": before, "after": body.category,
                "remembered": result["remembered"], "applied": result["applied"]})
    return result


@router.get("/memory", summary="Память исправлений компании (бухгалтер+)")
def memory(user: Accountant, db: Db,
           company_id: Annotated[str | None, Query(max_length=36)] = None) -> dict:
    cid = _company_of(db, user, company_id)
    return {"company_id": cid, "items": service.list_memory(db, cid)}


@router.delete("/memory/{memory_id}", summary="Забыть исправление (категории позиций не меняются)")
def forget(memory_id: str, user: Accountant, db: Db,
           company_id: Annotated[str | None, Query(max_length=36)] = None) -> dict:
    cid = _company_of(db, user, company_id)
    try:
        data = service.forget(db, cid, memory_id)
    except service.GoodsError as exc:
        raise _fail(exc) from exc
    db.commit()
    log_action(user, "goods_memory_forget", "company", cid,
               {"name_key": data["name_key"], "category": data["category"]})
    return {"ok": True, "message": f"Забыто: «{data['sample_name'] or data['name_key']}»", "item": data}
