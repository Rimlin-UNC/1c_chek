"""Сервис карточек контрагентов (T-80, v1.71.0, ADR-48).

Изоляция компаний — ADR-20: бухгалтер видит карточки своей компании, администратор
может явно выбрать компанию, один ИНН у двух компаний даёт две карточки. Данные
только синтетические в тестах (ADR-14); рабочая база извне не трогается.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN
from ..bank.models import BankIncome, BankOperation
from ..models import Company, User
from ..primary.models import PrimaryDocument
from ..services import appsettings, checko
from ..services.companies_util import inn_is_valid, normalize_inn, risk_assessment
from . import blocks, sources, verify
from .models import Counterparty, CounterpartyAlias, CounterpartyBankAccount


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def scope_company(user: User, requested: str | None = None) -> str | None:
    if user.role == ROLE_ADMIN:
        return requested or None
    return user.company_id


def _scoped_query(user: User, company_id: str | None = None):
    query = select(Counterparty)
    scoped = scope_company(user, company_id)
    if scoped is not None:
        query = query.where(Counterparty.company_id == scoped)
    return query


def own_company(db: Session, company_id: str | None) -> dict:
    if not company_id:
        return {}
    comp = db.get(Company, company_id)
    if comp is None:
        return {}
    return {
        "name_full": comp.short_name or comp.name or "",
        "inn": comp.inn or "",
        "kpp": "",
    }


def _card_dict(db: Session, row: Counterparty) -> dict:
    value = row.to_dict()
    value["accounts"] = [a.to_dict() for a in row.accounts]
    value["aliases"] = [a.to_dict() for a in row.aliases]
    value["risk"] = row.risk() or risk_assessment(None, row.inn)
    value["blocks"] = blocks.build_blocks(value, own_company(db, row.company_id))
    return value


def _require_card(db: Session, user: User, card_id: str,
                  company_id: str | None = None) -> Counterparty:
    row = db.get(Counterparty, card_id)
    if row is None:
        raise LookupError("Карточка не найдена")
    scoped = scope_company(user, company_id)
    if scoped is not None and row.company_id != scoped:
        raise LookupError("Карточка не найдена")
    return row


def get_card(db: Session, user: User, card_id: str,
             company_id: str | None = None) -> Counterparty | None:
    try:
        return _require_card(db, user, card_id, company_id)
    except LookupError:
        return None


def create_card(db: Session, user: User, payload: dict) -> dict:
    inn = normalize_inn(payload.get("inn"))
    if not inn_is_valid(inn):
        raise ValueError("Некорректный ИНН — проверьте контрольные цифры")
    company_id = scope_company(user, payload.get("company_id")) or payload.get("company_id") \
        or user.company_id or ""
    name = str(payload.get("name") or payload.get("name_full") or "").strip()
    if not name:
        raise ValueError("Укажите наименование контрагента")
    exists = db.scalar(select(Counterparty).where(
        Counterparty.company_id == company_id, Counterparty.inn == inn))
    if exists:
        raise FileExistsError("Карточка с таким ИНН уже есть в этой компании")
    row = Counterparty(
        company_id=company_id, inn=inn,
        kpp=str(payload.get("kpp") or ""),
        name_full=name, name_short=str(payload.get("name_short") or name)[:200],
        opf=str(payload.get("opf") or ""),
        address=str(payload.get("address") or ""),
        source=str(payload.get("source") or "manual"),
        risk_json=_json(risk_assessment(None, inn)),
        created_by=user.id or "",
    )
    row.accounts = []
    row.aliases = [CounterpartyAlias(name=name, source=row.source)]
    db.add(row)
    db.commit()
    db.refresh(row)
    return _card_dict(db, row)


def list_cards(db: Session, user: User, company_id: str | None = None,
               search: str = "") -> list[dict]:
    query = _scoped_query(user, company_id)
    needle = (search or "").strip().casefold()
    if needle:
        query = query.where(or_(
            Counterparty.name_full.ilike(f"%{needle}%"),
            Counterparty.name_short.ilike(f"%{needle}%"),
            Counterparty.inn.ilike(f"%{needle}%"),
        ))
    rows = list(db.scalars(query.order_by(Counterparty.name_full)).all())
    return [_card_dict(db, row) for row in rows]


def patch_card(db: Session, user: User, card_id: str, payload: dict,
               company_id: str | None = None) -> dict:
    row = _require_card(db, user, card_id, company_id)
    allowed = ("kpp", "ogrn", "name_full", "name_short", "opf", "address", "status",
               "signatory_post", "signatory_name", "signatory_basis", "genitive", "note")
    for key in allowed:
        if key in payload:
            setattr(row, key, str(payload[key] or ""))
    if "inn" in payload:
        inn = normalize_inn(payload["inn"])
        if not inn_is_valid(inn):
            raise ValueError("Некорректный ИНН — проверьте контрольные цифры")
        other = db.scalar(select(Counterparty).where(
            Counterparty.company_id == row.company_id,
            Counterparty.inn == inn, Counterparty.id != row.id))
        if other:
            raise FileExistsError("Карточка с таким ИНН уже есть в этой компании")
        row.inn = inn
    row.updated_by = user.id or ""
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return _card_dict(db, row)


def add_account(db: Session, user: User, card_id: str, payload: dict,
                company_id: str | None = None) -> dict:
    row = _require_card(db, user, card_id, company_id)
    account = str(payload.get("account") or "").strip()
    if not account.isdigit() or len(account) != 20:
        raise ValueError("Расчётный счёт должен содержать 20 цифр")
    exists = db.scalar(select(CounterpartyBankAccount).where(
        CounterpartyBankAccount.account == account))
    if exists:
        raise FileExistsError("Такой счёт уже есть у другой карточки")
    bank = verify.lookup_bik(payload.get("bik"))
    item = CounterpartyBankAccount(
        counterparty_id=row.id, account=account,
        bik=str(payload.get("bik") or ""),
        bank_name=str(payload.get("bank_name") or bank.get("bank_name") or ""),
        corr_account=str(payload.get("corr_account") or bank.get("corr_account") or ""),
        is_primary=bool(payload.get("is_primary")) or not row.accounts,
    )
    db.add(item)
    db.commit()
    db.refresh(row)
    return item.to_dict()


def add_alias(db: Session, card_id: str, name: str, source: str = "manual") -> dict | None:
    value = str(name or "").strip()
    if not value:
        return None
    exists = db.scalar(select(CounterpartyAlias).where(
        CounterpartyAlias.counterparty_id == card_id,
        CounterpartyAlias.name == value))
    if exists:
        return exists.to_dict()
    item = CounterpartyAlias(counterparty_id=card_id, name=value, source=source)
    db.add(item)
    db.flush()
    return item.to_dict()


def merge_cards(db: Session, user: User, primary_id: str, duplicate_id: str,
                company_id: str | None = None) -> dict:
    primary = _require_card(db, user, primary_id, company_id)
    duplicate = _require_card(db, user, duplicate_id, company_id)
    if primary.id == duplicate.id:
        raise ValueError("Нельзя объединить карточку с собой")
    moved = list(db.scalars(select(CounterpartyBankAccount).where(
        CounterpartyBankAccount.counterparty_id == duplicate.id)).all())
    for item in moved:
        item.counterparty_id = primary.id
    old_aliases = list(db.scalars(select(CounterpartyAlias).where(
        CounterpartyAlias.counterparty_id == duplicate.id)).all())
    for item in old_aliases:
        add_alias(db, primary.id, item.name, item.source)
        db.delete(item)
    add_alias(db, primary.id, duplicate.name_full, "merge")
    db.flush()
    # Связи загружены через selectin: после переноса FK их надо забыть, иначе
    # каскад delete-orphan удалит уже переехавшие счета вместе с дубликатом.
    db.expire(duplicate, ["accounts", "aliases"])
    db.delete(duplicate)
    db.commit()
    db.refresh(primary)
    db.expire(primary, ["accounts", "aliases"])
    return _card_dict(db, primary)


def candidates(db: Session, user: User, company_id: str | None = None) -> list[dict]:
    scoped = scope_company(user, company_id)
    docs = list(db.scalars(select(PrimaryDocument)).all())
    ops = list(db.scalars(select(BankOperation)).all())
    incomes = list(db.scalars(select(BankIncome)).all())
    return sources.collect_candidates(docs, ops, incomes, company_id=scoped)


def verify_card(db: Session, user: User, card_id: str,
                company_id: str | None = None) -> dict:
    row = _require_card(db, user, card_id, company_id)
    raw: dict = {}
    source = "manual"
    try:
        key = appsettings.get_setting(db, checko.SETTING_KEY, "")
        if key and row.inn:
            raw = checko.fetch_card(key, row.inn)
            source = "checko"
    except Exception:                                   # noqa: BLE001 — нет ключа/сети
        raw = {}
    result = verify.verify_by_inn(row.inn, raw=raw or None)
    card = result.get("card") or {}
    for field, key in (("name_full", "name_full"), ("name_short", "name_short"),
                       ("kpp", "kpp"), ("ogrn", "ogrn"), ("address", "address"),
                       ("status", "status"), ("opf", "opf")):
        if card.get(key) and not getattr(row, field, ""):
            setattr(row, field, str(card[key])[:500])
    row.risk_json = _json(result["risk"])
    row.verify_source = source if card else "manual"
    row.verified_at = _now()
    db.commit()
    db.refresh(row)
    return _card_dict(db, row)
