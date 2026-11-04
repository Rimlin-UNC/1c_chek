# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru

Push-режим интеграции с 1С: чеки забирает сервер 1С (HTTP-сервис ↔ HTTP-сервис).

Защита: заголовок X-API-Token (токен задаётся администратором в настройках).
Дедупликация: 1С подтверждает загруженные чеки методом /ack, система
помечает их exported=true и повторно не отдаёт.

v1.70.96 (T-60): первичка — /primary/pull, /primary/ack, /primary/enterprise-data и файл УПД 5.03
документа /primary/{id}/upd; правила те же (токен компании — только её документы).
"""
from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import MappingSetting, Receipt
from ..primary import export1c
from ..primary.models import PrimaryDocument
from ..services import exporter
from ..services.audit import log_action

router = APIRouter(prefix="/onec/v1", tags=["Интеграция 1С"])


def _require_token(x_api_token: str | None, db: Session) -> str | None:
    """Проверить X-API-Token и вернуть область: id компании или None — все компании.

    v1.70.92 (T-57): токен компании ограничивает выгрузку и подтверждение её чеками;
    общий токен, как и раньше, видит все компании.
    """
    from ..services import onec_tokens
    try:
        return onec_tokens.resolve(db, x_api_token)
    except onec_tokens.TokenError:
        raise HTTPException(status_code=401, detail="Неверный X-API-Token") from None


def _get_mapping(db: Session) -> list[MappingSetting]:
    return db.query(MappingSetting).filter(MappingSetting.is_active == True).all()  # noqa: E712


@router.get("/ping", summary="Проверка связи 1С ↔ сервер")
def ping(x_api_token: str | None = Header(default=None, alias="X-API-Token"),
         db: Session = Depends(get_db)):
    scope = _require_token(x_api_token, db)
    query = db.query(Receipt).filter(Receipt.exported == False)  # noqa: E712
    if scope:                                          # v1.70.92: только своя компания
        query = query.filter(Receipt.company_id == scope)
    pending = query.count()
    return {
        "service": "Ямастер Чек",
        "scope": "company" if scope else "all",
        "vendor": "ООО «Ямастер» (ymaster.ru)",
        "pending_receipts": pending,
        "pending_primary": _primary_query(db, scope).count(),     # v1.70.96 (T-60)
        "server_time": dt.datetime.utcnow().isoformat() + "Z",
    }


@router.get("/receipts/pull", summary="1С забирает новые чеки (push-режим)")
def pull(x_api_token: str | None = Header(default=None, alias="X-API-Token"),
         since: str | None = Query(None, description="Получить чеки с даты ГГГГ-ММ-ДДТЧЧ:ММ:СС"),
         only_verified: bool = Query(True),
         company_id: str | None = Query(None, max_length=36,
                                        description="v1.11.0: только чеки компании"),
         limit: int = Query(default=100, ge=1, le=settings.ONEC_BATCH_SIZE),
         db: Session = Depends(get_db)):
    scope = _require_token(x_api_token, db)
    if scope and company_id and company_id != scope:   # v1.70.92 (T-57)
        raise HTTPException(403, "Токен компании не даёт доступа к чекам другой компании")
    company_id = scope or company_id

    query = db.query(Receipt).filter(Receipt.exported == False)  # noqa: E712
    if company_id:                                   # v1.11.0: пространство
        query = query.filter(Receipt.company_id == company_id)
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
    log_action(None, "onec_pull", details={"count": len(receipts), "scope": scope or "all"})
    return payload


@router.post("/receipts/ack", summary="1С подтверждает загрузку чеков")
def ack(body: dict, x_api_token: str | None = Header(default=None, alias="X-API-Token"),
        db: Session = Depends(get_db)):
    scope = _require_token(x_api_token, db)
    ids: list[str] = [str(i) for i in (body.get("ids") or [])][:1000]
    if not ids:
        raise HTTPException(422, "Ожидается массив ids")
    now = dt.datetime.utcnow()
    updated = 0
    for rid in ids:
        r = db.get(Receipt, rid)
        if r and scope and r.company_id != scope:      # v1.70.92: чужие чеки не отмечаются
            continue
        if r and not r.exported:
            r.exported = True
            r.exported_at = now
            updated += 1
    db.commit()
    log_action(None, "onec_ack", details={"count": updated, "scope": scope or "all"})
    return {"acknowledged": updated}


@router.get("/enterprise-data", summary="Выгрузка пакета EnterpriseData (1С ← сервер)")
def enterprise_data(format: str = Query("json", pattern="^(json|xml)$"),
                    target_object: str = Query("ПоступлениеТоваровУслуг"),
                    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
                    db: Session = Depends(get_db)):
    scope = _require_token(x_api_token, db)
    query = db.query(Receipt).filter(Receipt.status == "verified")
    if scope:                                          # v1.70.92: только своя компания
        query = query.filter(Receipt.company_id == scope)
    receipts = query.order_by(Receipt.receipt_date).limit(settings.ONEC_BATCH_SIZE).all()
    mapping = _get_mapping(db)
    if format == "xml":
        content = exporter.build_enterprise_data_xml(receipts, mapping, target_object)
        return Response(content=content, media_type="application/xml; charset=utf-8")
    content = exporter.build_enterprise_data_json(receipts, mapping, target_object)
    return Response(content=content, media_type="application/json; charset=utf-8")


# --------------------------------------------------------------------------
#  v1.70.96 (T-60): первичка — УПД и счета-фактуры для 1С
#  Те же правила, что у чеков: токен компании видит только её документы, общий — все;
#  забираются ещё не выгруженные; документы со статусом «Ошибки» — только по include_errors;
#  /ack отмечает выгруженными (повторно не отдаются). Формат пакета — как у чеков.
# --------------------------------------------------------------------------
def _primary_query(db: Session, scope: str | None, include_errors: bool = False, only_new: bool = True):
    query = db.query(PrimaryDocument)
    if only_new:
        query = query.filter(PrimaryDocument.exported_at.is_(None))
    if scope:
        query = query.filter(PrimaryDocument.company_id == scope)
    if not include_errors:
        query = query.filter(PrimaryDocument.status != "errors")
    return query


def _primary_ordered(query, limit: int):
    return (query.order_by(PrimaryDocument.doc_date.is_(None), PrimaryDocument.doc_date, PrimaryDocument.created_at)
            .limit(limit).all())


Token = Annotated[str | None, Header(alias="X-API-Token")]
Db = Annotated[Session, Depends(get_db)]


@router.get("/primary/pull", summary="1С забирает новые УПД и счета-фактуры")
def primary_pull(db: Db, x_api_token: Token = None,
                 include_errors: Annotated[bool, Query(description="и документы со статусом «Ошибки»")] = False,
                 company_id: Annotated[str | None, Query(max_length=36, description="только документы компании")] = None,
                 limit: Annotated[int, Query(ge=1, le=settings.ONEC_BATCH_SIZE)] = 100):
    scope = _require_token(x_api_token, db)
    if scope and company_id and company_id != scope:
        raise HTTPException(403, "Токен компании не даёт доступа к документам другой компании")
    docs = _primary_ordered(_primary_query(db, scope or company_id, include_errors), limit)
    log_action(None, "onec_primary_pull", details={"count": len(docs), "scope": scope or "all"})
    return {"provider": "Ямастер Чек (ymaster.ru)", "format": "ymaster-primary-v1",
            "documents": [export1c.pull_item(d) for d in docs], "count": len(docs), "has_more": len(docs) == limit}


@router.post("/primary/ack", summary="1С подтверждает загрузку УПД")
def primary_ack(body: dict, db: Db, x_api_token: Token = None):
    scope = _require_token(x_api_token, db)
    ids = [str(i)[:36] for i in (body.get("ids") or [])][:1000]
    if not ids:
        raise HTTPException(422, "Ожидается массив ids")
    query = db.query(PrimaryDocument).filter(PrimaryDocument.id.in_(ids), PrimaryDocument.exported_at.is_(None))
    if scope:                                          # чужие документы не отмечаются
        query = query.filter(PrimaryDocument.company_id == scope)
    now = dt.datetime.utcnow()
    docs = query.all()
    for doc in docs:
        doc.exported_at = now
    db.commit()
    log_action(None, "onec_primary_ack", details={"count": len(docs), "scope": scope or "all"})
    return {"acknowledged": len(docs)}


@router.get("/primary/enterprise-data", summary="Пакет EnterpriseData: поступления и счета-фактуры")
def primary_enterprise_data(db: Db, x_api_token: Token = None,
                            format: Annotated[str, Query(pattern="^(json|xml)$")] = "json",
                            include_errors: Annotated[bool, Query()] = False,
                            only_new: Annotated[bool, Query(description="только ещё не выгруженные")] = True):
    scope = _require_token(x_api_token, db)
    docs = _primary_ordered(_primary_query(db, scope, include_errors, only_new), settings.ONEC_BATCH_SIZE)
    if format == "xml":
        return Response(content=export1c.ed_xml(docs), media_type="application/xml; charset=utf-8")
    return Response(content=export1c.ed_json(docs), media_type="application/json; charset=utf-8")


@router.get("/primary/{doc_id}/upd", summary="Файл УПД 5.03 документа (для загрузки в 1С из файла)")
def primary_upd(doc_id: str, db: Db, x_api_token: Token = None):
    scope = _require_token(x_api_token, db)
    doc = db.get(PrimaryDocument, doc_id[:36])
    if doc is None or (scope and doc.company_id != scope):
        raise HTTPException(404, "Документ не найден")
    prepared = export1c.prepare([doc])[0]
    if "reason" in prepared:
        raise HTTPException(422, f"Файл для 1С не собран: {prepared['reason']}")
    return Response(content=prepared["data"], media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{prepared["name"]}"'})
