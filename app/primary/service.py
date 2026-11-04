"""Первичка: приём разобранных УПД, дубли, выдача (T-53, v1.70.94).

Правила доступа — как у чеков (services/scoping.py): администратор видит все компании,
бухгалтер — только свою. Компания нового документа: у бухгалтера — его компания;
администратор выбирает явно, а без выбора документ сам находит клиента по ИНН
покупателя — если такая компания ровно одна (аутсорсинг: пачка УПД разных клиентов).
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Query, Session

from ..auth import ROLE_ADMIN
from ..models import Company, User
from ..services.scoping import target_company_id
from . import upd
from .models import PrimaryDocument, PrimaryDocumentLine

# Не первичный документ продавца — не сохраняем, только объясняем причину
REJECT_CODES = frozenset({"buyer_title", "correction_doc", "function"})
MAX_FILES = 50


def resolve_company(db: Session, user: User, parsed: dict, requested: str | None) -> str | None:
    company_id = target_company_id(db, user, requested)
    if company_id or user.role != ROLE_ADMIN or requested:
        return company_id
    inn = parsed["buyer"]["inn"]
    if not inn:
        return None
    found = db.execute(select(Company.id).where(Company.inn == inn).limit(2)).scalars().all()
    return found[0] if len(found) == 1 else None


def _company_inn(db: Session, company_id: str | None) -> str:
    company = db.get(Company, company_id) if company_id else None
    return (company.inn or "").strip() if company else ""


def _same_space(query, company_id: str | None):
    if company_id is None:
        return query.where(PrimaryDocument.company_id.is_(None))
    return query.where(PrimaryDocument.company_id == company_id)


def find_duplicate(db: Session, company_id: str | None, parsed: dict, sha: str) -> PrimaryDocument | None:
    """Тот же файл или тот же документ: ИНН продавца + номер + дата + номер исправления."""
    same_file = _same_space(select(PrimaryDocument).where(PrimaryDocument.file_sha256 == sha), company_id)
    found = db.execute(same_file.limit(1)).scalars().first()
    if found or not parsed["number"] or not parsed["date"]:
        return found
    same_doc = _same_space(select(PrimaryDocument).where(
        PrimaryDocument.seller_inn == parsed["seller"]["inn"],
        PrimaryDocument.doc_number == parsed["number"],
        PrimaryDocument.doc_date == parsed["date"],
        PrimaryDocument.correction_number == parsed["correction_number"]), company_id)
    return db.execute(same_doc.limit(1)).scalars().first()


def accept(db: Session, user: User, data: bytes, file_name: str, requested: str | None) -> dict:
    """Один файл → результат для интерфейса: created | duplicate | rejected."""
    name = (file_name or "")[:255]
    try:
        parsed = upd.parse(data)
    except upd.UpdError as exc:
        return {"file": name, "result": "rejected", "message": str(exc), "issues": []}
    company_id = resolve_company(db, user, parsed, requested)
    issues = upd.check(parsed, company_inn=_company_inn(db, company_id))
    rejected = [i for i in issues if i["code"] in REJECT_CODES]
    if rejected:
        return {"file": name, "result": "rejected", "message": rejected[0]["message"], "issues": issues}
    sha = hashlib.sha256(data).hexdigest()
    duplicate = find_duplicate(db, company_id, parsed, sha)
    if duplicate is not None:
        return {"file": name, "result": "duplicate", "issues": issues, "document": brief(duplicate),
                "message": "Этот документ уже загружен"}
    seller, buyer, totals = parsed["seller"], parsed["buyer"], parsed["totals"]
    doc = PrimaryDocument(
        company_id=company_id, kind=parsed["kind"], function=parsed["function"],
        format_version=parsed["format_version"][:10], file_id=parsed["file_id"][:255], file_name=name,
        file_sha256=sha, doc_number=parsed["number"][:100], doc_date=parsed["date"],
        correction_number=parsed["correction_number"][:20], correction_date=parsed["correction_date"],
        seller_name=seller["name"][:500], seller_inn=seller["inn"][:12], seller_kpp=seller["kpp"][:9],
        buyer_name=buyer["name"][:500], buyer_inn=buyer["inn"][:12], buyer_kpp=buyer["kpp"][:9],
        currency=parsed["currency"][:3],
        total_without_vat=totals["without_vat"] if totals["without_vat"] is not None else
        round(sum(ln["sum_without_vat"] or 0 for ln in parsed["lines"]), 2),
        vat_total=totals["vat"] if totals["vat"] is not None else
        round(sum(ln["vat_sum"] or 0 for ln in parsed["lines"]), 2),
        total_with_vat=totals["with_vat"] if totals["with_vat"] is not None else
        round(sum(ln["sum_with_vat"] or 0 for ln in parsed["lines"]), 2),
        status=upd.status_of(issues), issues_json=json.dumps(issues, ensure_ascii=False),
        created_by=user.id)
    doc.lines = [PrimaryDocumentLine(
        position=ln["position"], name=ln["name"], unit_code=ln["unit_code"][:10], unit_name=ln["unit_name"][:40],
        quantity=ln["quantity"], price=ln["price"], sum_without_vat=ln["sum_without_vat"],
        vat_rate=ln["vat_rate"][:10], vat_sum=ln["vat_sum"], sum_with_vat=ln["sum_with_vat"],
        item_code=ln["item_code"][:100]) for ln in parsed["lines"]]
    db.add(doc)
    db.commit()
    return {"file": name, "result": "created", "issues": issues, "document": brief(doc)}


# ------------------------------------------------------------------ выдача
def scope(query: Query, user: User, company_id: str | None = None) -> Query:
    if user.role == ROLE_ADMIN:
        return query.filter(PrimaryDocument.company_id == company_id) if company_id else query
    if user.company_id:
        return query.filter(PrimaryDocument.company_id == user.company_id)
    return query.filter(PrimaryDocument.company_id.is_(None))


def visible(user: User, doc: PrimaryDocument) -> bool:
    if user.role == ROLE_ADMIN:
        return True
    return doc.company_id == user.company_id


def _issues(doc: PrimaryDocument) -> list[dict]:
    try:
        value = json.loads(doc.issues_json or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def brief(doc: PrimaryDocument) -> dict:
    issues = _issues(doc)
    return {
        "id": doc.id, "company_id": doc.company_id, "kind": doc.kind, "function": doc.function,
        "format_version": doc.format_version, "file_name": doc.file_name,
        "doc_number": doc.doc_number, "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
        "correction_number": doc.correction_number,
        "correction_date": doc.correction_date.isoformat() if doc.correction_date else None,
        "seller": {"name": doc.seller_name, "inn": doc.seller_inn, "kpp": doc.seller_kpp},
        "buyer": {"name": doc.buyer_name, "inn": doc.buyer_inn, "kpp": doc.buyer_kpp},
        "currency": doc.currency, "total_without_vat": doc.total_without_vat, "vat_total": doc.vat_total,
        "total_with_vat": doc.total_with_vat, "status": doc.status,
        "errors": sum(1 for i in issues if i.get("level") == "error"),
        "warnings": sum(1 for i in issues if i.get("level") == "warning"),
        "lines_count": len(doc.lines),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "exported_at": doc.exported_at.isoformat() if doc.exported_at else None,
    }


def detail(doc: PrimaryDocument) -> dict:
    out = brief(doc)
    out["file_id"] = doc.file_id
    out["issues"] = _issues(doc)
    out["lines"] = [{
        "position": ln.position, "name": ln.name, "unit_code": ln.unit_code, "unit_name": ln.unit_name,
        "quantity": ln.quantity, "price": ln.price, "sum_without_vat": ln.sum_without_vat,
        "vat_rate": ln.vat_rate, "vat_sum": ln.vat_sum, "sum_with_vat": ln.sum_with_vat,
        "item_code": ln.item_code} for ln in doc.lines]
    return out
