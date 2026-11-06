"""Первичка: API загрузки и просмотра электронных УПД (T-53, v1.70.94; T-61, v1.70.95).

Доступ — бухгалтер и администратор; изоляция компаний — service.scope/visible.
Принимаются XML и ZIP-архивы оператора ЭДО (из архива берутся только .xml, подписи
и печатные формы пропускаются). Пределы: 50 документов за раз, 5 МБ на XML,
20 МБ на архив и 50 МБ распакованного объёма — защита от «zip-бомб».

С 1.70.95 — печатные формы: POST /recognize (PDF, скан, фото → черновик, база не трогается),
POST /confirm (сверенный черновик или ручной ввод → документ), GET /ocr (готово ли распознавание
сканов), POST /ocr/models (скачать языковые модели — только администратор).

С 1.70.96 — выгрузка в 1С (T-60, export1c.py): POST /export/check (что уйдёт и почему пропущено,
база не меняется), POST /export (файлы УПД 5.03 для «Загрузить → Из файла» в 1С или пакет
EnterpriseData; документы отмечаются выгруженными). Для обмена без участия человека — /onec/v1/primary/*.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import require_accountant, require_admin
from ..database import get_db
from ..models import User
from ..services.audit import log_action
from . import export1c, ocr, service, upd
from .models import PrimaryDocument

router = APIRouter(prefix="/api/v1/primary", tags=["primary"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]
MONEY = 1e13                 # предел сумм: больше не бывает, а мусор не должен ломать базу
DateType = date              # поле «date» в DraftIn перекрыло бы имя типа внутри класса


class PartyIn(BaseModel):
    name: str = Field("", max_length=500)
    inn: str = Field("", max_length=20)
    kpp: str = Field("", max_length=20)


class LineIn(BaseModel):
    position: int | None = Field(None, ge=0, le=100000)
    name: str = Field("", max_length=1000)
    unit_code: str = Field("", max_length=10)
    unit_name: str = Field("", max_length=40)
    quantity: float | None = Field(None, ge=-MONEY, le=MONEY)
    price: float | None = Field(None, ge=-MONEY, le=MONEY)
    sum_without_vat: float | None = Field(None, ge=-MONEY, le=MONEY)
    vat_rate: str = Field("none", max_length=40)
    vat_sum: float | None = Field(None, ge=-MONEY, le=MONEY)
    sum_with_vat: float | None = Field(None, ge=-MONEY, le=MONEY)
    item_code: str = Field("", max_length=100)


class TotalsIn(BaseModel):
    without_vat: float | None = Field(None, ge=-MONEY, le=MONEY)
    vat: float | None = Field(None, ge=-MONEY, le=MONEY)
    with_vat: float | None = Field(None, ge=-MONEY, le=MONEY)


class DraftIn(BaseModel):
    function: Literal["СЧФДОП", "ДОП", "СЧФ"] = "СЧФДОП"
    number: str = Field("", max_length=100)
    date: DateType | None = None
    correction_number: str = Field("", max_length=20)
    correction_date: DateType | None = None
    currency: str = Field("643", max_length=3)
    seller: PartyIn = Field(default_factory=PartyIn)
    buyer: PartyIn = Field(default_factory=PartyIn)
    totals: TotalsIn = Field(default_factory=TotalsIn)
    lines: list[LineIn] = Field(default_factory=list, max_length=500)


class ConfirmIn(BaseModel):
    source: Literal["pdf", "scan", "manual"]
    token: str = Field("", max_length=200)
    file_name: str = Field("", max_length=255)
    file_sha256: str = Field("", max_length=64)
    company_id: str | None = Field(None, max_length=36)
    draft: DraftIn


class ExportIn(BaseModel):
    """v1.70.96 (T-60): выгрузка в 1С — файлы УПД 5.03 (upd) или пакет EnterpriseData (json, xml)."""
    ids: list[Annotated[str, Field(max_length=36)]] = Field(default_factory=list, max_length=500)
    format: Literal["upd", "json", "xml"] = "upd"
    company_id: str | None = Field(None, max_length=36)
    include_errors: bool = False
    date_from: DateType | None = None
    date_to: DateType | None = None

MAX_ZIP_BYTES = service.MAX_ZIP_BYTES
STATUSES = ("ok", "warnings", "errors")
_unpack = service.unpack          # v1.70.104: разбор архива переехал в service — им пользуется и приём из почты


@router.post("/upload")
async def upload(user: Accountant, db: Db, files: Annotated[list[UploadFile], File()],
                 company_id: Annotated[str | None, Form()] = None) -> dict:
    if not files:
        raise HTTPException(400, "Выберите файлы УПД (XML или ZIP)")
    items: list[tuple[str, bytes | None, str]] = []
    for upload_file in files:
        data = await upload_file.read(MAX_ZIP_BYTES + 1)
        name = upload_file.filename or "документ.xml"
        if len(data) > MAX_ZIP_BYTES:
            items.append((name, None, "Файл больше 20 МБ"))
            continue
        items.extend(_unpack(name, data))
    if len(items) > service.MAX_FILES:
        raise HTTPException(400, f"Не больше {service.MAX_FILES} документов за одну загрузку")
    results = [service.accept(db, user, body, label, company_id) if body is not None
               else {"file": label, "result": "rejected", "message": reason, "issues": []}
               for label, body, reason in items]
    counts = Counter(r["result"] for r in results)
    return {"results": results, "created": counts["created"], "duplicates": counts["duplicate"],
            "rejected": counts["rejected"]}


@router.post("/recognize")
def recognize(user: Accountant, db: Db, file: Annotated[UploadFile, File()],
              company_id: Annotated[str | None, Form()] = None) -> dict:
    """PDF, скан или фото печатной формы → черновик для сверки (синхронно: распознавание —
    в пуле потоков, сам сервер не блокируется)."""
    data = file.file.read(MAX_ZIP_BYTES + 1)
    if not data:
        raise HTTPException(400, "Файл пустой")
    if len(data) > MAX_ZIP_BYTES:
        raise HTTPException(400, "Файл больше 20 МБ")
    try:
        return service.recognize_file(db, user, data, file.filename or "документ", company_id)
    except service.RecognizeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/confirm")
def confirm(user: Accountant, db: Db, body: ConfirmIn) -> dict:
    try:
        return service.confirm(db, user, body.model_dump())
    except service.RecognizeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


class SecondModeIn(BaseModel):
    mode: Literal["auto", "always", "off"]


@router.get("/ocr", dependencies=[Depends(require_accountant)])
def ocr_status(db: Db) -> dict:
    return {**ocr.status(), "second_mode": service.second_mode(db)}


@router.put("/ocr/second")
def ocr_second(user: Annotated[User, Depends(require_admin)], db: Db, body: SecondModeIn) -> dict:
    """v1.70.109 (T-77): режим второго прочтения — по сомнению, всегда или выключено."""
    service.set_second_mode(db, body.mode)
    log_action(user, "primary_ocr_second", details={"mode": body.mode})
    return {**ocr.status(), "second_mode": service.second_mode(db)}


@router.post("/ocr/models", dependencies=[Depends(require_admin)])
def ocr_models() -> dict:
    try:
        return ocr.download_models()
    except ocr.OcrError as exc:     # 424, а не 502: браузер повторяет 502 сам — три скачивания подряд
        raise HTTPException(424, str(exc)) from exc


@router.get("")
def list_documents(user: Accountant, db: Db,
                   company_id: Annotated[str | None, Query()] = None,
                   status: Annotated[str | None, Query()] = None,
                   q: Annotated[str | None, Query(max_length=100)] = None,
                   date_from: Annotated[date | None, Query()] = None,
                   date_to: Annotated[date | None, Query()] = None,
                   exported: Annotated[bool | None, Query(description="v1.70.96: выгружен ли в 1С")] = None,
                   limit: Annotated[int, Query(ge=1, le=500)] = 100,
                   offset: Annotated[int, Query(ge=0)] = 0) -> dict:
    query = service.scope(db.query(PrimaryDocument), user, company_id)
    if exported is not None:
        query = query.filter(PrimaryDocument.exported_at.isnot(None) if exported
                             else PrimaryDocument.exported_at.is_(None))
    if date_from:
        query = query.filter(PrimaryDocument.doc_date >= date_from)
    if date_to:
        query = query.filter(PrimaryDocument.doc_date <= date_to)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(PrimaryDocument.doc_number.ilike(like), PrimaryDocument.seller_name.ilike(like),
                                 PrimaryDocument.seller_inn.like(like), PrimaryDocument.buyer_name.ilike(like)))
    counts = dict(query.with_entities(PrimaryDocument.status, func.count()).group_by(PrimaryDocument.status).all())
    if status in STATUSES:
        query = query.filter(PrimaryDocument.status == status)
    total = query.count()
    rows = (query.order_by(PrimaryDocument.doc_date.is_(None), PrimaryDocument.doc_date.desc(),
                           PrimaryDocument.created_at.desc())
            .offset(offset).limit(limit).all())
    return {"items": [service.brief(d) for d in rows], "total": total,
            "summary": {s: int(counts.get(s, 0)) for s in STATUSES}}


# ------------------------------------------------------------------ выгрузка в 1С (T-60, v1.70.96)
def _selection(db: Session, user: User, body: ExportIn) -> list[PrimaryDocument]:
    return service.export_selection(db, user, ids=body.ids, company_id=body.company_id,
                                    include_errors=body.include_errors, date_from=body.date_from,
                                    date_to=body.date_to)


@router.post("/export/check")
def export_check(user: Accountant, db: Db, body: ExportIn) -> dict:
    """Что уйдёт в 1С: сколько документов готово и почему пропущены остальные. База не меняется."""
    docs = _selection(db, user, body)
    skipped: list[dict] = []
    count = len(docs)
    if body.format == "upd":
        prepared = export1c.prepare(docs)
        skipped = [{"id": p["doc"].id, "title": service.title(p["doc"]), "reason": p["reason"]}
                   for p in prepared if "reason" in p]
        count = len(prepared) - len(skipped)
    errors = 0 if body.include_errors else service.errors_left(db, user, ids=body.ids, company_id=body.company_id)
    return {"count": count, "skipped": skipped, "already_exported": sum(1 for d in docs if d.exported_at),
            "errors_excluded": errors, "limit": service.EXPORT_LIMIT, "format": body.format}


@router.post("/export")
def export_documents(user: Accountant, db: Db, body: ExportIn) -> Response:
    """Скачать файл для 1С и отметить документы выгруженными (повторно — только отмеченные вручную)."""
    docs = _selection(db, user, body)
    if not docs:
        raise HTTPException(404, "Нет документов для выгрузки: новые уже выгружены, а с ошибками не выгружаются")
    stamp = datetime.now(export1c.MSK)
    skipped = 0
    if body.format == "upd":
        prepared = export1c.prepare(docs, stamp)
        ready = [p for p in prepared if "data" in p]
        skipped = len(prepared) - len(ready)
        if not ready:
            reasons = "; ".join(f"{service.title(p['doc'])}: {p['reason']}" for p in prepared[:3])
            raise HTTPException(422, f"Файл для 1С не собран — {reasons}")
        docs = [p["doc"] for p in ready]
        if len(ready) == 1:
            content, media, name = ready[0]["data"], "application/xml", ready[0]["name"]
        else:
            content, media = export1c.zip_files(ready), "application/zip"
            name = f"upd-dlya-1c-{stamp:%Y%m%d-%H%M}.zip"
    elif body.format == "xml":
        content, media = export1c.ed_xml(docs).encode("utf-8"), "application/xml; charset=utf-8"
        name = f"enterprise-data-pervichka-{stamp:%Y%m%d-%H%M}.xml"
    else:
        content, media = export1c.ed_json(docs).encode("utf-8"), "application/json; charset=utf-8"
        name = f"enterprise-data-pervichka-{stamp:%Y%m%d-%H%M}.json"
    service.mark_exported(db, docs, stamp.astimezone(timezone.utc).replace(tzinfo=None))
    log_action(user, "primary_exported", details={"count": len(docs), "skipped": skipped, "format": body.format})
    return Response(content=content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-Exported": str(len(docs)), "X-Skipped": str(skipped)})


def _visible(db: Session, user: User, doc_id: str) -> PrimaryDocument:
    doc = db.get(PrimaryDocument, doc_id)
    if doc is None or not service.visible(user, doc):
        raise HTTPException(404, "Документ не найден")
    return doc


@router.get("/{doc_id}")
def get_document(doc_id: str, user: Accountant, db: Db) -> dict:
    return service.detail(_visible(db, user, doc_id))


@router.delete("/{doc_id}")
def delete_document(doc_id: str, user: Accountant, db: Db) -> dict:
    doc = _visible(db, user, doc_id)
    db.delete(doc)
    db.commit()
    return {"ok": True}
