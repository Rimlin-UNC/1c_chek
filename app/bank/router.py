"""Банковская выписка: API для бухгалтера и администратора (T-45, v1.70.98).

Бухгалтер работает со своей компанией, администратор — с выбранной (company_id обязателен). Чужие выписки,
операции и чеки — 404, как у первички и «Что нужно от клиента». Файл выписки не хранится: только итоги
разбора и операции, которым нужны документы (ADR-28).

v1.70.99 (ADR-29): подключение к банку по API — /connection (состояние, сохранение, вход в Точку, проверка
JWT-ключа, загрузка сейчас, отключение) и адрес возврата из банка /tochka/callback. Возврат открывает браузер
клиента без заголовка авторизации — компанию определяет одноразовая отметка state (15 минут).

v1.70.102 (ADR-32): банков у компании несколько — параметр bank (tochka | tbank | sber | alfa | modul) у
состояния, сохранения, проверки ключей (/connection/check, прежний адрес /connection/jwt — тот же), загрузки
и отключения. Без bank: состояние — первого подключённого банка компании, остальное — Точка (как в 1.70.99).
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, require_accountant
from ..database import get_db
from ..models import Company, User
from ..services.audit import log_action
from . import closing, connect, parser, service
from .models import BankOperation, BankStatement

router = APIRouter(prefix="/api/v1/bank", tags=["bank"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]
CompanyId = Annotated[str | None, Query(max_length=36)]


class CardIn(BaseModel):
    tail: str = Field(..., max_length=8)
    user_id: str = Field("", max_length=36)


class SettingsIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    retention_days: int = Field(service.DEFAULT_RETENTION, ge=1, le=100000)
    cards: list[CardIn] = Field(default_factory=list, max_length=service.MAX_CARDS + 1)


class OperationIn(BaseModel):
    action: Literal["ignore", "open", "link"]
    note: str = Field("", max_length=300)
    ref_type: Literal["receipt", "primary", ""] = ""
    ref_id: str = Field("", max_length=36)


class DecisionIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    status: Literal["paid", "declined", "pending"]
    note: str = Field("", max_length=300)


BankId = Literal["tochka", "tbank", "sber", "alfa", "modul"]


class ConnectionIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    bank: BankId = "tochka"
    method: Literal["oauth", "jwt", "token", "refresh"] = "oauth"
    client_id: str = Field("", max_length=200)
    client_secret: str = Field("", max_length=500)
    jwt: str = Field("", max_length=8000)
    token: str = Field("", max_length=4000)              # v1.70.102: ключ API (Т-Банк, Модульбанк, Альфа-Банк)
    refresh_token: str = Field("", max_length=4000)      # v1.70.102: Сбер — из личного кабинета Sber API
    cert: str = Field("", max_length=70000)              # v1.70.102: сертификат клиента и ключ (PEM)
    cert_p12: str = Field("", max_length=90000)          # или .p12/.pfx в base64
    cert_password: str = Field("", max_length=200)
    cert_remove: bool = False
    start_date: date | None = None


class CompanyIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    bank: BankId | None = None


Side = Literal["expense", "income"]


class LinkIn(BaseModel):
    """v1.70.113 (T-79): ручная привязка платежа к документам (заменяет прежние ручные привязки платежа)."""
    company_id: str | None = Field(None, max_length=36)
    side: Side = "expense"
    operation_id: str = Field(..., min_length=1, max_length=36)
    document_ids: list[Annotated[str, Field(max_length=36)]] = Field(default_factory=list, max_length=50)


class UnlinkIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    side: Side = "expense"
    operation_id: str = Field(..., min_length=1, max_length=36)


class SettleIn(BaseModel):
    """После загрузки папки с УПД: пересчитать сверку компаний, куда легли документы."""
    company_ids: list[Annotated[str, Field(max_length=36)]] = Field(default_factory=list, max_length=200)


def _company(db: Session, user: User, company_id: str | None) -> Company:
    """Компания запроса: у бухгалтера — своя, у администратора — выбранная."""
    if user.role == ROLE_ADMIN:
        if not company_id:
            raise HTTPException(400, "Выберите компанию")
        company = db.get(Company, company_id)
    else:
        if company_id and company_id != user.company_id:
            raise HTTPException(404, "Компания не найдена")
        company = db.get(Company, user.company_id) if user.company_id else None
    if company is None:
        raise HTTPException(404, "Компания не найдена")
    return company


def _fail(exc: service.BankError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("")
def overview(user: Accountant, db: Db, company_id: CompanyId = None,
             status: Annotated[Literal["open", "matched", "ignored", "all"], Query()] = "open") -> dict:
    company = _company(db, user, company_id)
    return {"company": {"id": company.id, "name": company.short_name or company.name},
            **service.overview(db, company.id, status)}


@router.post("/statements")
async def upload(user: Accountant, db: Db, file: Annotated[UploadFile, File()],
                 company_id: Annotated[str | None, Form()] = None) -> dict:
    company = _company(db, user, company_id)
    raw = await file.read(parser.MAX_BYTES + 1)
    try:
        result = service.import_statement(db, company, raw, user)
    except service.BankError as exc:
        raise _fail(exc) from exc
    log_action(user, "bank_statement_uploaded", details={
        "company": company.name, "period": f"{result['statement']['date_from']} — {result['statement']['date_to']}",
        "operations": result["operations"], "saved": result["saved"], "matched": result["matched"]})
    return result


@router.delete("/statements/{statement_id}")
def remove(statement_id: str, user: Accountant, db: Db) -> dict:
    st = db.get(BankStatement, statement_id[:36])
    if st is None or (user.role != ROLE_ADMIN and st.company_id != user.company_id):
        raise HTTPException(404, "Выписка не найдена")
    company = db.get(Company, st.company_id)
    removed = service.delete_statement(db, st, user)
    log_action(user, "bank_statement_deleted", details={"company": company.name if company else "",
                                                        "operations": removed})
    return {"deleted": removed}


@router.post("/match")
def rematch(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    """Сопоставить открытые операции заново: пришли новые чеки и УПД."""
    company = _company(db, user, company_id)
    return {"matched": service.rematch(db, company.id)}


@router.post("/operations/{op_id}")
def operation(op_id: str, user: Accountant, db: Db, body: OperationIn) -> dict:
    op = db.get(BankOperation, op_id[:36])
    if op is None or (user.role != ROLE_ADMIN and op.company_id != user.company_id):
        raise HTTPException(404, "Операция не найдена")
    try:
        service.set_operation(db, op, body.action, user, body.note, body.ref_type, body.ref_id)
    except service.BankError as exc:
        raise _fail(exc) from exc
    if body.action == "ignore":
        log_action(user, "bank_operation_ignored", details={"amount": op.amount, "date": op.op_date.isoformat(),
                                                            "note": op.match_note})
    return service.op_dict(op)


@router.get("/reimburse")
def reimburse(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    return service.reimbursements(db, company.id)


@router.get("/reimburse.csv")
def reimburse_csv(user: Accountant, db: Db, company_id: CompanyId = None) -> Response:
    """К возмещению — таблица для авансового отчёта в 1С (точка с запятой, UTF-8 с меткой для Excel)."""
    company = _company(db, user, company_id)
    data = service.reimbursements(db, company.id)
    buf = io.StringIO()
    out = csv.writer(buf, delimiter=";")
    out.writerow(["Сотрудник", "Дата чека", "Продавец", "Оплата", "Сумма чека", "К возмещению", "Решение",
                  "Примечание"])
    titles = {"pending": "к возмещению", "paid": "возмещено", "declined": "не возмещать"}
    for group in data["employees"]:
        for item in group["items"]:
            out.writerow([group["name"], item["date"][:10], item["merchant"], item["payment"],
                          f"{item['total_sum']:.2f}".replace(".", ","), f"{item['amount']:.2f}".replace(".", ","),
                          titles.get(item["status"], item["status"]), item["note"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="k_vozmeshcheniyu.csv"'})


@router.post("/reimburse/{receipt_id}")
def decide(receipt_id: str, user: Accountant, db: Db, body: DecisionIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        result = service.decide(db, company.id, receipt_id[:36], body.status, user, body.note)
    except service.BankError as exc:
        raise _fail(exc) from exc
    if body.status != "pending":
        log_action(user, "bank_reimbursement_decided", details={"company": company.name, "status": body.status,
                                                                "note": result.get("note", "")})
    return result


@router.get("/settings")
def get_settings(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    return service.settings_dict(db, service.settings_of(db, company.id))


@router.put("/settings")
def put_settings(user: Accountant, db: Db, body: SettingsIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        saved = service.save_settings(db, company.id, body.model_dump(), user)
    except service.BankError as exc:
        raise _fail(exc) from exc
    log_action(user, "bank_settings_updated", details={"company": company.name, "retention_days": saved.retention_days,
                                                       "cards": len(service.cards_of(saved))})
    return service.settings_dict(db, saved)


# ------------------------------------------------------------------ v1.70.99: банк по API (ADR-29)
def _cfail(exc: connect.ConnectError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _state(db: Session, company: Company, bank: str = "tochka") -> dict:
    return connect.connection_dict(db, company, connect.connection_of(db, company.id, bank), bank)


BankQuery = Annotated[BankId | None, Query()]


@router.get("/connection")
def connection(user: Accountant, db: Db, company_id: CompanyId = None, bank: BankQuery = None) -> dict:
    company = _company(db, user, company_id)
    return _state(db, company, connect.bank_of(db, company.id, bank))


@router.put("/connection")
def save_connection(user: Accountant, db: Db, body: ConnectionIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        conn = connect.save(db, company, body.model_dump(), user)
    except connect.ConnectError as exc:
        raise _cfail(exc) from exc
    log_action(user, "bank_connection_saved", details={"company": company.name, "bank": connect.BANKS[conn.bank],
                                                       "method": connect.METHODS[conn.method]})
    return _state(db, company, conn.bank)


@router.post("/connection/authorize")
def authorize(user: Accountant, db: Db, body: CompanyIn) -> dict:
    """Адрес входа в Точку: браузер переходит туда, клиент подтверждает доступ, банк возвращает его к нам."""
    company = _company(db, user, body.company_id)
    if body.bank not in (None, "tochka"):
        raise HTTPException(409, "Вход через интернет-банк — для Точки; у этого банка — «Проверить и подключить»")
    try:
        return {"url": connect.start_oauth(db, company, user)}
    except connect.ConnectError as exc:
        raise _cfail(exc) from exc


@router.post("/connection/jwt")
def check_jwt(user: Accountant, db: Db, body: CompanyIn) -> dict:
    """Проверить ключ запросом в банк и запомнить счета: JWT-ключ Точки, ключ API или токены других банков."""
    company = _company(db, user, body.company_id)
    bank = body.bank or "tochka"
    try:
        connect.connect_key(db, company, user, bank)
    except connect.ConnectError as exc:
        raise _cfail(exc) from exc
    return _state(db, company, bank)


@router.post("/connection/check")
def check_key(user: Accountant, db: Db, body: CompanyIn) -> dict:
    """v1.70.102: то же, что /connection/jwt, — понятное имя для всех банков."""
    return check_jwt(user, db, body)


@router.post("/connection/sync")
def sync_connection(user: Accountant, db: Db, body: CompanyIn) -> dict:
    company = _company(db, user, body.company_id)
    bank = body.bank or "tochka"
    try:
        result = connect.sync_now(db, company, user, bank)
    except connect.ConnectError as exc:
        raise _cfail(exc) from exc
    return {"result": result, "connection": _state(db, company, bank)}


@router.delete("/connection")
def remove_connection(user: Accountant, db: Db, company_id: CompanyId = None, bank: BankQuery = None) -> dict:
    company = _company(db, user, company_id)
    removed = connect.remove(db, company, bank or "tochka")
    if removed:
        log_action(user, "bank_connection_removed", details={"company": company.name,
                                                             "bank": connect.BANKS[bank or "tochka"]})
    return {"removed": removed}


@router.get("/tochka/callback", include_in_schema=False)
def tochka_callback(db: Db, state: Annotated[str, Query(max_length=100)] = "",
                    code: Annotated[str, Query(max_length=600)] = "",
                    error: Annotated[str, Query(max_length=200)] = "") -> RedirectResponse:
    """Возврат из Точки после входа клиента: код → токены → счета; затем — обратно в программу."""
    conn, problem = connect.finish_oauth(db, state, code, error)
    company = f"&company={conn.company_id}" if conn is not None else ""
    if problem:
        return RedirectResponse(f"/?bank=error&reason={quote(problem[:240])}{company}#/bank", status_code=303)
    return RedirectResponse(f"/?bank=connected{company}#/bank", status_code=303)


# ------------------------------------------------------------------ v1.70.113 (T-79): закрытие выписки документами
def _period(date_from: date | None, date_to: date | None) -> tuple[date | None, date | None]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Начало периода позже конца")
    return date_from, date_to


def _closing_fail(exc: closing.ClosingError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("/closing")
def closing_report(user: Accountant, db: Db, company_id: CompanyId = None, date_from: date | None = None,
                   date_to: date | None = None) -> dict:
    """Какие расходы и поступления за период закрыты документами (перед отчётом — пересчёт сверки)."""
    company = _company(db, user, company_id)
    start, end = _period(date_from, date_to)
    return closing.report(db, company, start, end)


@router.get("/closing.csv")
def closing_csv(user: Accountant, db: Db, company_id: CompanyId = None, date_from: date | None = None,
                date_to: date | None = None, side: Side = "expense") -> Response:
    company = _company(db, user, company_id)
    start, end = _period(date_from, date_to)
    data = closing.report(db, company, start, end)
    tail = f"{start or 'начало'}_{end or 'сегодня'}"
    name = f"zakrytie-{'rashody' if side == 'expense' else 'postupleniya'}-{company.inn or company.id[:8]}-{tail}.csv"
    return Response(closing.to_csv(data, side), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/closing/candidates")
def closing_candidates(user: Accountant, db: Db, operation_id: Annotated[str, Query(max_length=36)],
                       company_id: CompanyId = None, side: Side = "expense") -> dict:
    company = _company(db, user, company_id)
    try:
        return closing.candidates(db, company, side, operation_id)
    except closing.ClosingError as exc:
        raise _closing_fail(exc) from exc


@router.post("/closing/link")
def closing_link(user: Accountant, db: Db, body: LinkIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        count = closing.link(db, company, body.side, body.operation_id, body.document_ids, user)
    except closing.ClosingError as exc:
        raise _closing_fail(exc) from exc
    log_action(user, "bank_closing_linked", "bank_operation", body.operation_id,
               details={"company": company.short_name or company.name, "side": body.side, "documents": count})
    return {"linked": count, "settle": closing.settle(db, company)}


@router.post("/closing/unlink")
def closing_unlink(user: Accountant, db: Db, body: UnlinkIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        count = closing.unlink(db, company, body.side, body.operation_id)
    except closing.ClosingError as exc:
        raise _closing_fail(exc) from exc
    if count:
        log_action(user, "bank_closing_unlinked", "bank_operation", body.operation_id,
                   details={"company": company.short_name or company.name, "side": body.side})
    return {"unlinked": count, "settle": closing.settle(db, company)}


@router.post("/closing/settle")
def closing_settle(user: Accountant, db: Db, body: SettleIn) -> dict:
    """Пересчёт сверки компаний после загрузки папки: только доступные пользователю, итог — по каждой."""
    from ..clients import service as clients
    allowed = clients.allowed_company_ids(db, user)            # None — администратор, все компании
    out = []
    for cid in dict.fromkeys(body.company_ids):
        if allowed is not None and cid not in allowed:
            continue
        company = db.get(Company, cid)
        if company is None:
            continue
        result = closing.settle(db, company)
        out.append({"company_id": company.id, "company_name": company.short_name or company.name,
                    "matched": result["matched"], "expense": result["expense"], "income": result["income"]})
    return {"companies": out}
