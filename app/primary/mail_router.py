"""Первичка из почтового ящика: API для бухгалтера и администратора (T-63, v1.70.104, ADR-34).

/mailbox — подключение ящика компании (состояние, сохранение, «Проверить и подключить», «Загрузить сейчас»,
отключение); /inbox — что пришло из почты: черновики PDF и сканов на сверку и журнал вложений, файл вложения
для сверки, подтверждение и отказ. Бухгалтер работает со своей компанией, администратор — с выбранной
(company_id обязателен для настроек ящика); чужие ящики и черновики — 404, как у первички и банка.

Роутер подключается в main.py РАНЬШЕ основного роутера первички: иначе GET /api/v1/primary/inbox перехватил бы
маршрут карточки документа /api/v1/primary/{doc_id}.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, require_accountant
from ..database import get_db
from ..models import Company, User
from ..services.audit import log_action
from . import mailbox
from .models import PrimaryMailFile, PrimaryMailItem
from .router import DraftIn

router = APIRouter(prefix="/api/v1/primary", tags=["primary"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]
CompanyId = Annotated[str | None, Query(max_length=36)]


class MailboxIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    host: str = Field("", max_length=200)
    port: int | None = Field(None, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    username: str = Field("", max_length=200)
    password: str = Field("", max_length=500)       # пусто — оставить сохранённый
    folder: str = Field("INBOX", max_length=200)
    senders: str = Field("", max_length=5000)
    start_date: date | None = None


class CompanyIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)


class ConfirmMailIn(BaseModel):
    company_id: str | None = Field(None, max_length=36)
    draft: DraftIn


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


def _fail(exc: mailbox.MailboxError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _state(db: Session, company: Company) -> dict:
    return mailbox.state(db, company, mailbox.mailbox_of(db, company.id))


@router.get("/mailbox")
def mailbox_state(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    return _state(db, _company(db, user, company_id))


@router.put("/mailbox")
def mailbox_save(user: Accountant, db: Db, body: MailboxIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        box = mailbox.save(db, company, body.model_dump(), user)
    except mailbox.MailboxError as exc:
        raise _fail(exc) from exc
    log_action(user, "primary_mailbox_saved", details={"company": company.name, "host": box.host,
                                                       "folder": box.folder})
    return _state(db, company)


@router.post("/mailbox/check")
def mailbox_check(user: Accountant, db: Db, body: CompanyIn) -> dict:
    """Вход в ящик и папка только на чтение — без загрузки писем; успех включает проверку раз в 15 минут."""
    company = _company(db, user, body.company_id)
    box = mailbox.mailbox_of(db, company.id)
    if box is None:
        raise HTTPException(404, "Ящик не подключён: сначала сохраните адрес, логин и пароль")
    try:
        result = mailbox.test_connection(db, box)
    except mailbox.MailboxError as exc:
        raise _fail(exc) from exc
    log_action(user, "primary_mailbox_connected", details={"company": company.name, "host": box.host,
                                                           "messages": result["messages_in_folder"]})
    return {"result": result, "mailbox": _state(db, company)}


@router.post("/mailbox/sync")
def mailbox_sync(user: Accountant, db: Db, body: CompanyIn) -> dict:
    company = _company(db, user, body.company_id)
    try:
        result = mailbox.check_now(db, company)
    except mailbox.MailboxError as exc:
        raise _fail(exc) from exc
    log_action(user, "primary_mail_checked", details={"company": company.name, **{
        k: result.get(k, 0) for k in ("messages", "created", "duplicates", "drafts", "rejected", "skipped")}})
    return {"result": result, "mailbox": _state(db, company)}


@router.delete("/mailbox")
def mailbox_remove(user: Accountant, db: Db, company_id: CompanyId = None) -> dict:
    company = _company(db, user, company_id)
    removed = mailbox.remove(db, company)
    if removed:
        log_action(user, "primary_mailbox_removed", details={"company": company.name})
    return {"removed": removed}


@router.get("/inbox")
def inbox(user: Accountant, db: Db, company_id: CompanyId = None,
          limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    if user.role != ROLE_ADMIN and company_id and company_id != user.company_id:
        raise HTTPException(404, "Компания не найдена")
    return mailbox.inbox(db, user, company_id if user.role == ROLE_ADMIN else None, limit)


def _item(db: Session, user: User, item_id: str) -> PrimaryMailItem:
    item = db.get(PrimaryMailItem, item_id)
    if item is None or not mailbox.visible(user, item):
        raise HTTPException(404, "Вложение не найдено")
    return item


@router.get("/inbox/{item_id}")
def inbox_item(item_id: str, user: Accountant, db: Db) -> dict:
    try:
        return mailbox.item_for_review(db, _item(db, user, item_id))
    except mailbox.MailboxError as exc:
        raise _fail(exc) from exc


@router.get("/inbox/{item_id}/file")
def inbox_file(item_id: str, user: Accountant, db: Db) -> Response:
    """Вложение для сверки: только PDF, JPG, PNG, TIFF (тип — по сигнатуре файла, не по имени)."""
    item = _item(db, user, item_id)
    blob = db.get(PrimaryMailFile, item.id)
    if blob is None:
        raise HTTPException(404, "Файл уже удалён: черновик разобран или вышел срок хранения")
    media = mailbox.media_type(blob.data)
    disposition = "inline" if media in ("application/pdf", "image/jpeg", "image/png") else "attachment"
    return Response(content=blob.data, media_type=media, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.file_name or 'vlozhenie')}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.post("/inbox/{item_id}/confirm")
def inbox_confirm(item_id: str, user: Accountant, db: Db, body: ConfirmMailIn) -> dict:
    item = _item(db, user, item_id)
    try:
        return mailbox.confirm_item(db, user, item, body.draft.model_dump(), body.company_id)
    except mailbox.MailboxError as exc:
        raise _fail(exc) from exc


@router.post("/inbox/{item_id}/dismiss")
def inbox_dismiss(item_id: str, user: Accountant, db: Db) -> dict:
    item = _item(db, user, item_id)
    try:
        mailbox.dismiss_item(db, user, item)
    except mailbox.MailboxError as exc:
        raise _fail(exc) from exc
    return {"ok": True}
