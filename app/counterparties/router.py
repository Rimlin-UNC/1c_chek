"""API «Контрагенты» (T-80, #65, v1.71.0, ADR-48)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_accountant
from ..database import get_db
from ..models import User
from ..services.audit import log_action
from . import service

router = APIRouter(prefix="/api/v1/counterparties", tags=["counterparties"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]


class CardIn(BaseModel):
    inn: str = Field("", max_length=20)
    name: str = Field("", max_length=500)
    kpp: str = Field("", max_length=20)
    company_id: str | None = None
    source: str = Field("manual", max_length=20)


class PatchIn(BaseModel):
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    name_full: str | None = None
    name_short: str | None = None
    opf: str | None = None
    address: str | None = None
    status: str | None = None
    signatory_post: str | None = None
    signatory_name: str | None = None
    signatory_basis: str | None = None
    genitive: str | None = None
    note: str | None = None
    company_id: str | None = None


class AccountIn(BaseModel):
    account: str = Field("", max_length=20)
    bik: str = Field("", max_length=9)
    company_id: str | None = None


class MergeIn(BaseModel):
    duplicate_id: str
    company_id: str | None = None


class VerifyIn(BaseModel):
    company_id: str | None = None


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    raise exc


@router.get("")
def list_cards(user: Accountant, db: Db, search: str = Query("", max_length=100),
               company_id: str | None = None):
    return service.list_cards(db, user, company_id=company_id, search=search)


@router.get("/candidates")
def candidates(user: Accountant, db: Db, company_id: str | None = None):
    return service.candidates(db, user, company_id=company_id)


@router.post("", status_code=201)
def create_card(body: CardIn, user: Accountant, db: Db):
    try:
        card = service.create_card(db, user, body.model_dump())
    except (LookupError, FileExistsError, ValueError) as exc:
        raise _error(exc) from exc
    log_action(user, "counterparty_created", "counterparty", card["id"],
               {"inn": card["inn"], "company_id": card["company_id"]})
    return card


@router.get("/{card_id}")
def get_card(card_id: str, user: Accountant, db: Db, company_id: str | None = None):
    row = service.get_card(db, user, card_id, company_id=company_id)
    if row is None:
        raise HTTPException(404, "Карточка не найдена")
    return service._card_dict(db, row)


@router.patch("/{card_id}")
def patch_card(card_id: str, body: PatchIn, user: Accountant, db: Db):
    try:
        card = service.patch_card(db, user, card_id,
                                  {k: v for k, v in body.model_dump().items()
                                   if k != "company_id" and v is not None},
                                  company_id=body.company_id)
    except (LookupError, FileExistsError, ValueError) as exc:
        raise _error(exc) from exc
    log_action(user, "counterparty_updated", "counterparty", card_id,
               {"fields": [k for k, v in body.model_dump().items() if v is not None]})
    return card


@router.post("/{card_id}/verify")
def verify_card(card_id: str, body: VerifyIn, user: Accountant, db: Db):
    try:
        card = service.verify_card(db, user, card_id, company_id=body.company_id)
    except LookupError as exc:
        raise _error(exc) from exc
    log_action(user, "counterparty_verified", "counterparty", card_id,
               {"inn": card["inn"], "risk": card["risk"]["level"]})
    return card


@router.post("/{card_id}/accounts", status_code=201)
def add_account(card_id: str, body: AccountIn, user: Accountant, db: Db):
    try:
        item = service.add_account(db, user, card_id, body.model_dump(),
                                   company_id=body.company_id)
    except (LookupError, FileExistsError, ValueError) as exc:
        raise _error(exc) from exc
    log_action(user, "counterparty_account_added", "counterparty", card_id,
               {"bik": item["bik"]})
    return item


@router.post("/{card_id}/merge")
def merge_cards(card_id: str, body: MergeIn, user: Accountant, db: Db):
    try:
        card = service.merge_cards(db, user, card_id, body.duplicate_id,
                                   company_id=body.company_id)
    except (LookupError, FileExistsError, ValueError) as exc:
        raise _error(exc) from exc
    log_action(user, "counterparty_merged", "counterparty", card_id,
               {"duplicate_id": body.duplicate_id})
    return card
