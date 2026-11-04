"""Первичка: приём разобранных УПД, дубли, выдача (T-53, v1.70.94; T-61, v1.70.95).

Правила доступа — как у чеков (services/scoping.py): администратор видит все компании,
бухгалтер — только свою. Компания нового документа: у бухгалтера — его компания;
администратор выбирает явно, а без выбора документ сам находит клиента по ИНН
покупателя — если такая компания ровно одна (аутсорсинг: пачка УПД разных клиентов).

С 1.70.95: XML формата 5.03 дополнительно сверяется со схемой ФНС (schema.py). PDF, сканы и
фото распознаются в черновик (print_form.py, ocr.py). В базу черновик не пишется, пока бухгалтер
его не сверит и не подтвердит; при подтверждении сервер проверяет всё заново. Подпись черновика
(HMAC на секрете сервера) не даёт выдать ручной ввод за распознанный файл.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Query, Session

from ..auth import ROLE_ADMIN
from ..config import settings
from ..models import Company, User
from ..services.companies_util import inn_is_valid
from ..services.scoping import target_company_id
from . import ocr, print_form, schema, upd
from .models import PrimaryDocument, PrimaryDocumentLine

# Не первичный документ продавца — не сохраняем, только объясняем причину
REJECT_CODES = frozenset({"buyer_title", "correction_doc", "function"})
MAX_FILES = 50
SOURCES = {"xml": "файл ЭДО", "pdf": "PDF", "scan": "скан или фото", "manual": "введён вручную"}
TOKEN_TTL = 24 * 3600
IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"II*\x00": ".tif", b"MM\x00*": ".tif"}


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
    found = None
    if sha:                     # у ручного ввода файла нет — сравнивать по пустому хешу нельзя
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


def _store(db: Session, user: User, parsed: dict, issues: list[dict], company_id: str | None, name: str,
           sha: str, source: str) -> PrimaryDocument:
    seller, buyer, totals = parsed["seller"], parsed["buyer"], parsed["totals"]

    def total(key: str, line_key: str) -> float:
        value = totals.get(key)
        return value if value is not None else round(sum(ln[line_key] or 0 for ln in parsed["lines"]), 2)

    doc = PrimaryDocument(
        company_id=company_id, kind=parsed["kind"], function=parsed["function"],
        format_version=parsed["format_version"][:10], file_id=parsed["file_id"][:255], file_name=name,
        file_sha256=sha, doc_number=parsed["number"][:100], doc_date=parsed["date"],
        correction_number=parsed["correction_number"][:20], correction_date=parsed["correction_date"],
        seller_name=seller["name"][:500], seller_inn=seller["inn"][:12], seller_kpp=seller["kpp"][:9],
        buyer_name=buyer["name"][:500], buyer_inn=buyer["inn"][:12], buyer_kpp=buyer["kpp"][:9],
        currency=parsed["currency"][:3], total_without_vat=total("without_vat", "sum_without_vat"),
        vat_total=total("vat", "vat_sum"), total_with_vat=total("with_vat", "sum_with_vat"),
        status=upd.status_of(issues), issues_json=json.dumps(issues, ensure_ascii=False),
        created_by=user.id, source=source)
    doc.lines = [PrimaryDocumentLine(
        position=ln["position"] or i, name=ln["name"], unit_code=ln["unit_code"][:10], unit_name=ln["unit_name"][:40],
        quantity=ln["quantity"], price=ln["price"], sum_without_vat=ln["sum_without_vat"],
        vat_rate=ln["vat_rate"][:10], vat_sum=ln["vat_sum"], sum_with_vat=ln["sum_with_vat"],
        item_code=ln["item_code"][:100]) for i, ln in enumerate(parsed["lines"], 1)]
    db.add(doc)
    db.commit()
    return doc


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
    # v1.70.95: сверка со схемой ФНС 5.03 — замечание, а не отказ (см. schema.py)
    issues += schema.issues(schema.validate(data, parsed["format_version"]))
    sha = hashlib.sha256(data).hexdigest()
    duplicate = find_duplicate(db, company_id, parsed, sha)
    if duplicate is not None:
        return {"file": name, "result": "duplicate", "issues": issues, "document": brief(duplicate),
                "message": "Этот документ уже загружен"}
    doc = _store(db, user, parsed, issues, company_id, name, sha, "xml")
    return {"file": name, "result": "created", "issues": issues, "document": brief(doc)}


# ------------------------------------------------------------------ печатные формы: черновик → подтверждение
class RecognizeError(ValueError):
    """Файл не распознан — сообщение для пользователя (status — код ответа API)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def sign(user_id: str, sha: str, source: str, issued: int | None = None) -> str:
    issued = int(time.time()) if issued is None else issued
    body = f"{user_id}|{sha}|{source}|{issued}".encode()
    mac = hmac.new(settings.SECRET_KEY.encode(), body, hashlib.sha256).hexdigest()
    return f"{issued}.{mac}"


def token_ok(token: str, user_id: str, sha: str, source: str) -> bool:
    try:
        issued = int((token or "").split(".", 1)[0])
    except ValueError:
        return False
    if not 0 <= time.time() - issued <= TOKEN_TTL:
        return False
    return hmac.compare_digest(token, sign(user_id, sha, source, issued))


def file_kind(data: bytes) -> tuple[str, str]:
    """Тип файла по сигнатуре: (pdf | image, расширение для обработчика)."""
    if data.startswith(b"%PDF"):
        return "pdf", ".pdf"
    for magic, suffix in IMAGE_TYPES.items():
        if data.startswith(magic):
            return "image", suffix
    raise RecognizeError("Распознаются PDF, JPG, PNG и TIFF; XML и ZIP загружайте обычной загрузкой", 400)


def _draft_from_ocr(answer: dict) -> dict:
    pages = answer.get("pages") or []
    plain = "\n".join(p.get("text", "") for p in pages)
    layout = "\n".join(p.get("layout", "") for p in pages)
    return print_form.parse_variants([(plain, layout), (layout, layout), (plain, "")])


def hints(db: Session, user: User, draft: dict) -> dict:
    """Подсказки названий: продавец — из прошлых документов, покупатель — из карточки компании."""
    out: dict = {}
    seller_inn = draft["seller"]["inn"]
    if seller_inn and inn_is_valid(seller_inn):
        prev = (scope(db.query(PrimaryDocument), user)
                .filter(PrimaryDocument.seller_inn == seller_inn, PrimaryDocument.seller_name != "")
                .order_by(PrimaryDocument.created_at.desc()).first())
        if prev and (prev.seller_name != draft["seller"]["name"] or prev.seller_kpp != draft["seller"]["kpp"]):
            out["seller"] = {"name": prev.seller_name, "kpp": prev.seller_kpp, "from": "прошлые документы"}
    buyer_inn = draft["buyer"]["inn"]
    if buyer_inn and inn_is_valid(buyer_inn):
        query = db.query(Company).filter(Company.inn == buyer_inn)
        if user.role != ROLE_ADMIN:
            query = query.filter(Company.id == user.company_id)
        company = query.first()
        if company and company.name and company.name != draft["buyer"]["name"]:
            out["buyer"] = {"name": company.name, "kpp": "", "from": "карточка компании"}
    return out


def _jsonable(draft: dict) -> dict:
    out = {k: v for k, v in draft.items() if k not in ("found", "uncertain")}
    for key in ("date", "correction_date"):
        out[key] = draft[key].isoformat() if draft.get(key) else None
    return out


def recognize_file(db: Session, user: User, data: bytes, file_name: str, requested: str | None) -> dict:
    """PDF, скан или фото → черновик для сверки. В базу ничего не пишется."""
    kind, suffix = file_kind(data)
    source, ocr_info, preview = "pdf", None, None
    try:
        if kind == "pdf":
            plain, layout, _pages = print_form.pdf_text(data)
            if print_form.has_text(plain):
                draft = print_form.parse_variants([(plain, layout), (plain, "")])
            else:
                kind = "scan_pdf"
        if kind != "pdf":
            source = "scan"
            answer = ocr.recognize(data, suffix, preview=kind == "scan_pdf")
            preview = answer.get("preview")
            ocr_info = {"engine": answer.get("engine", ""), **answer.get("stats", {}),
                        "confidence": [p.get("confidence") for p in answer.get("pages") or []],
                        "angle": [p.get("angle") for p in answer.get("pages") or []]}
            draft = _draft_from_ocr(answer)
    except print_form.PrintFormError as exc:
        raise RecognizeError(str(exc)) from exc
    except ocr.OcrUnavailable as exc:
        raise RecognizeError(str(exc), 409) from exc
    except ocr.OcrBusy as exc:            # 429 «повторите позже»: код 503 в приложении запрещён (#15)
        raise RecognizeError(str(exc), 429) from exc
    except ocr.OcrError as exc:
        raise RecognizeError(str(exc)) from exc
    company_id = resolve_company(db, user, draft, requested)
    issues = upd.check(draft, company_inn=_company_inn(db, company_id), electronic=False)
    sha = hashlib.sha256(data).hexdigest()
    duplicate = find_duplicate(db, company_id, draft, sha)
    return {"source": source, "file_name": (file_name or "")[:255], "file_sha256": sha,
            "token": sign(user.id, sha, source), "company_id": company_id, "draft": _jsonable(draft),
            "found": draft["found"], "uncertain": draft["uncertain"], "issues": issues,
            "duplicate": brief(duplicate) if duplicate else None, "hints": hints(db, user, draft),
            "ocr": ocr_info, "preview": f"data:image/jpeg;base64,{preview}" if preview else None}


def _digits(value: str, limit: int) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())[:limit]


def draft_to_parsed(draft: dict) -> dict:
    """Черновик из формы (JSON) → словарь в формате upd.parse; всё чистится и обрезается."""
    def party(p: dict) -> dict:
        inn = _digits(p.get("inn", ""), 12)
        kind = "ИП" if len(inn) == 12 else "ЮЛ" if inn else ("ЮЛ" if (p.get("name") or "").strip() else "")
        return {"kind": kind, "name": " ".join((p.get("name") or "").split())[:500], "inn": inn,
                "kpp": "".join(ch for ch in (p.get("kpp") or "").upper() if ch.isalnum())[:9]}

    function = draft.get("function") if draft.get("function") in upd.KINDS else "СЧФДОП"
    lines = []
    for i, ln in enumerate(draft.get("lines") or [], 1):
        rate = upd.vat_rate(str(ln.get("vat_rate") or "none"))
        lines.append({
            "position": int(ln.get("position") or i), "name": " ".join(str(ln.get("name") or "").split())[:1000],
            "unit_code": str(ln.get("unit_code") or "")[:10], "unit_name": str(ln.get("unit_name") or "")[:40],
            "quantity": ln.get("quantity"), "price": ln.get("price"), "sum_without_vat": ln.get("sum_without_vat"),
            "vat_rate": rate[:10], "vat_raw": str(ln.get("vat_rate") or ""), "vat_sum": ln.get("vat_sum"),
            "sum_with_vat": ln.get("sum_with_vat"), "item_code": str(ln.get("item_code") or "")[:100]})
    totals = draft.get("totals") or {}

    def day(value) -> date | None:
        if isinstance(value, date) or value is None:
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    return {
        "file_id": "", "format_version": "", "function": function, "kind": upd.KINDS[function],
        "number": " ".join(str(draft.get("number") or "").split())[:100], "date": day(draft.get("date")),
        "currency": _digits(draft.get("currency") or "643", 3) or "643",
        "correction_number": str(draft.get("correction_number") or "").strip()[:20],
        "correction_date": day(draft.get("correction_date")),
        "seller": party(draft.get("seller") or {}), "buyer": party(draft.get("buyer") or {}), "lines": lines,
        "totals": {"without_vat": totals.get("without_vat"), "vat": totals.get("vat"),
                   "with_vat": totals.get("with_vat")},
    }


def confirm(db: Session, user: User, payload: dict) -> dict:
    """Сверенный человеком черновик → документ реестра (created | duplicate)."""
    source = payload["source"]
    sha = (payload.get("file_sha256") or "").lower()
    if source in ("pdf", "scan") and not token_ok(payload.get("token", ""), user.id, sha, source):
        raise RecognizeError("Черновик устарел или изменён — распознайте файл заново", 400)
    if source == "manual":
        sha = ""
    parsed = draft_to_parsed(payload.get("draft") or {})
    company_id = resolve_company(db, user, parsed, payload.get("company_id"))
    issues = upd.check(parsed, company_inn=_company_inn(db, company_id), electronic=False)
    note = {"pdf": "Реквизиты распознаны из PDF и сверены пользователем",
            "scan": "Реквизиты распознаны со скана или фото и сверены пользователем",
            "manual": "Документ введён вручную"}[source]
    issues.append({"level": "info", "code": f"source_{source}",
                   "message": f"{note}. Это не электронный документ ЭДО: оригинал — на бумаге или в PDF"})
    duplicate = find_duplicate(db, company_id, parsed, sha) if (sha or parsed["number"]) else None
    if duplicate is not None:
        return {"result": "duplicate", "document": brief(duplicate), "message": "Этот документ уже есть в реестре"}
    name = (payload.get("file_name") or "")[:255] if source != "manual" else ""
    doc = _store(db, user, parsed, issues, company_id, name, sha, source)
    return {"result": "created", "document": brief(doc), "issues": issues}


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
        "source": doc.source or "xml",
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
