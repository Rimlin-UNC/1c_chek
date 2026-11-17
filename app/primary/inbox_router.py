"""Приёмная папка первички: API администратора (T-109, v1.73.0, ADR-60).

Папка лежит на сервере, а не у бухгалтера, — поэтому настройка одна на программу и правит её только
администратор (путь, включение фонового обхода, интервал). Бухгалтер видит состояние и журнал: сколько
принято, что уже было и что попало в карантин с причиной.

Роутер подключается в main.py РАНЬШЕ основного роутера первички: иначе GET /api/v1/primary/inbox/log
перехватил бы маршрут карточки документа /api/v1/primary/{doc_id} (та же причина, что у /primary/inbox
из почты — T-63).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_accountant, require_admin
from ..database import get_db
from ..models import User
from ..services.audit import log_action
from . import inbox

router = APIRouter(prefix="/api/v1/primary", tags=["primary"])

Accountant = Annotated[User, Depends(require_accountant)]
Admin = Annotated[User, Depends(require_admin)]
Db = Annotated[Session, Depends(get_db)]


class InboxSettingsIn(BaseModel):
    path: str = Field("", max_length=inbox.MAX_PATH)
    enabled: bool = False
    interval_minutes: int = Field(inbox.DEFAULT_INTERVAL, ge=inbox.MIN_INTERVAL, le=inbox.MAX_INTERVAL)


def _fail(exc: inbox.InboxError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("/inbox/state")
def inbox_state(db: Db, _: Accountant) -> dict:
    """Состояние приёмной папки: путь, включён ли фоновый обход, итог и ошибка последнего."""
    return inbox.state(db)


@router.put("/inbox/settings")
def inbox_save(user: Admin, db: Db, body: InboxSettingsIn) -> dict:
    """Путь, включение и интервал — только администратор: это каталог на сервере, а не у бухгалтера."""
    try:
        result = inbox.save(db, path=body.path, enabled=body.enabled, interval=body.interval_minutes, user=user)
    except inbox.InboxError as exc:
        raise _fail(exc) from exc
    log_action(user, "primary_inbox_saved", details={"path": result["path"], "enabled": result["enabled"],
                                                     "interval_minutes": result["interval_minutes"]})
    return result


@router.post("/inbox/run")
def inbox_run(user: Annotated[User, Depends(require_accountant)], db: Db) -> dict:
    """«Забрать сейчас»: один обход приёмной папки вручную. Права разбора — как у загрузки на экране."""
    try:
        result = inbox.run_once(db, user=user)
    except inbox.InboxError as exc:
        raise _fail(exc) from exc
    log_action(user, "primary_inbox_run", details={
        "files": result["files"], "ok": result["ok"], "duplicate": result["duplicate"],
        "rejected": result["rejected"], "left": result["left"]})
    return {"result": result, "inbox": inbox.state(db)}


@router.get("/inbox/log")
def inbox_log(user: Accountant, db: Db, status: Annotated[str | None, Query(max_length=12)] = None,
              limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    """Журнал: что программа сделала с файлами папки (в пределах доступных пользователю компаний)."""
    return inbox.log(db, user, limit=limit, status=status or "")
