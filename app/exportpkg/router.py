"""API пакета для других учётных систем (T-68, v1.70.100).

GET /api/v1/export/package?period=ГГГГ-ММ — ZIP за месяц: JSON, CSV, файлы УПД 5.03, manifest.json, FORMAT.md.
Бухгалтер — своя компания (или выбранный клиент, ADR-30), администратор — выбранная компания.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, require_accountant
from ..clients import service as clients
from ..database import get_db
from ..docreq import service as docreq
from ..models import Company, User
from ..services.audit import log_action
from . import service

router = APIRouter(prefix="/api/v1/export", tags=["Выгрузка"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]


def _company(db: Session, user: User, company_id: str | None) -> Company:
    """Компания запроса: администратор — выбранная; бухгалтер — своя, выбранный клиент или любая из
    назначенных ему компаний (пакет прямо с экрана «Клиенты», ADR-30)."""
    if user.role == ROLE_ADMIN:
        if not company_id:
            raise HTTPException(400, "Выберите компанию")
        company = db.get(Company, company_id)
    else:
        target = company_id or user.company_id
        allowed = set(clients.allowed_company_ids(db, user) or []) | {user.company_id}
        if not target or target not in allowed:
            raise HTTPException(404, "Компания не найдена")
        company = db.get(Company, target)
        if company is not None and not company.is_active:
            company = None
    if company is None:
        raise HTTPException(404, "Компания не найдена")
    return company


@router.get("/package", summary="Пакет для других учётных систем: JSON, CSV и УПД 5.03 за месяц (ZIP)")
def package(user: Accountant, db: Db, period: Annotated[str | None, Query(max_length=7)] = None,
            company_id: Annotated[str | None, Query(max_length=36)] = None) -> Response:
    company = _company(db, user, company_id)
    month = period or docreq.default_period()
    try:
        name, data, manifest = service.build(db, company, month, user)
    except service.PackageError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    log_action(user, "export_package", "company", company.id,
               {"company": company.name, "period": month, **manifest["counts"]})
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
