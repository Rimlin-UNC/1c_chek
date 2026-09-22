# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Push-режим интеграции с 1С: чеки забирает сервер 1С (HTTP-сервис ↔ HTTP-сервис).

Защита: заголовок X-API-Token (токен задаётся администратором в настройках).
Дедупликация: 1С подтверждает загруженные чеки методом /ack, система
помечает их exported=true и повторно не отдаёт.
"""
from __future__ import annotations

import datetime as dt
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import MappingSetting, Receipt
from ..services import exporter
from ..services.audit import log_action

router = APIRouter(prefix="/onec/v1", tags=["Интеграция 1С"])


def _require_token(x_api_token: str | None, db: Session) -> None:
    from ..models import AppSetting
    row = db.get(AppSetting, "onec_api_token")
    expected = row.value if row and row.value else settings.ONEC_API_TOKEN
    if not x_api_token or not hmac.compare_digest(x_api_token, expected):
        raise HTTPException(status_code=401, detail="Неверный X-API-Token")


def _get_mapping(db: Session) -> list[MappingSetting]:
    return db.query(MappingSetting).filter(MappingSetting.is_active == True).all()  # noqa: E712


@router.get("/ping", summary="Проверка связи 1С ↔ сервер")
def ping(x_api_token: str | None = Header(default=None, alias="X-API-Token"),
         db: Session = Depends(get_db)):
    _require_token(x_api_token, db)
    pending = db.query(Receipt).filter(Receipt.exported == False).count()  # noqa: E712
    return {
        "service": "Ямастер Чек",
        "vendor": "ООО Ямастер (ymaster.ru)",
        "pending_receipts": pending,
        "server_time": dt.datetime.utcnow().isoformat(),
    }


@router.get("/receipts/pull", summary="1С забирает новые чеки (push-режим)")
def pull(x_api_token: str | None = Header(default=None, alias="X-API-Token"),
         since: str | None = Query(None, description="Получить чеки с даты ГГГГ-ММ-ДДТЧЧ:ММ:СС"),
         only_verified: bool = Query(True),
         limit: int = Query(default=100, ge=1, le=settings.ONEC_BATCH_SIZE),
         db: Session = Depends(get_db)):
    _require_token(x_api_token, db)

    query = db.query(Receipt).filter(Receipt.exported == False)  # noqa: E712
    if only_verified:
        query = query.filter(Receipt.status == "verified")
    if since:
        try:
            d = dt.datetime.fromisoformat(since)
            query = query.filter(or_(Receipt.receipt_date > d,
                                     Receipt.created_at > d))
        except ValueError:
            raise HTTPException(422, "Некорректный параметр since (ISO 8601)")

    receipts = query.order_by(Receipt.receipt_date).limit(limit).all()
    mapping = _get_mapping(db)
    payload = exporter.build_push_payload(receipts, mapping, "ПоступлениеТоваровУслуг")
    payload["count"] = len(receipts)
    payload["has_more"] = len(receipts) == limit
    log_action(None, "onec_pull", details={"count": len(receipts)})
    return payload


@router.post("/receipts/ack", summary="1С подтверждает загрузку чеков")
def ack(body: dict, x_api_token: str | None = Header(default=None, alias="X-API-Token"),
        db: Session = Depends(get_db)):
    _require_token(x_api_token, db)
    ids: list[str] = [str(i) for i in (body.get("ids") or [])][:1000]
    if not ids:
        raise HTTPException(422, "Ожидается массив ids")
    now = dt.datetime.utcnow()
    updated = 0
    for rid in ids:
        r = db.get(Receipt, rid)
        if r and not r.exported:
            r.exported = True
            r.exported_at = now
            updated += 1
    db.commit()
    log_action(None, "onec_ack", details={"count": updated})
    return {"acknowledged": updated}


@router.get("/enterprise-data", summary="Выгрузка пакета EnterpriseData (1С ← сервер)")
def enterprise_data(format: str = Query("json", pattern="^(json|xml)$"),
                    target_object: str = Query("ПоступлениеТоваровУслуг"),
                    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
                    db: Session = Depends(get_db)):
    _require_token(x_api_token, db)
    receipts = (db.query(Receipt)
                .filter(Receipt.status == "verified")
                .order_by(Receipt.receipt_date)
                .limit(settings.ONEC_BATCH_SIZE).all())
    mapping = _get_mapping(db)
    if format == "xml":
        content = exporter.build_enterprise_data_xml(receipts, mapping, target_object)
        return Response(content=content, media_type="application/xml; charset=utf-8")
    content = exporter.build_enterprise_data_json(receipts, mapping, target_object)
    return Response(content=content, media_type="application/json; charset=utf-8")
