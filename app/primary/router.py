"""Первичка: API загрузки и просмотра электронных УПД (T-53, v1.70.94).

Доступ — бухгалтер и администратор; изоляция компаний — service.scope/visible.
Принимаются XML и ZIP-архивы оператора ЭДО (из архива берутся только .xml, подписи
и печатные формы пропускаются). Пределы: 50 документов за раз, 5 МБ на XML,
20 МБ на архив и 50 МБ распакованного объёма — защита от «zip-бомб».
"""
from __future__ import annotations

import io
import zipfile
import zlib
from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import require_accountant
from ..database import get_db
from ..models import User
from . import service, upd
from .models import PrimaryDocument

router = APIRouter(prefix="/api/v1/primary", tags=["primary"])

Accountant = Annotated[User, Depends(require_accountant)]
Db = Annotated[Session, Depends(get_db)]

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_UNPACKED = 50 * 1024 * 1024
STATUSES = ("ok", "warnings", "errors")


def _unpack(name: str, data: bytes) -> list[tuple[str, bytes | None, str]]:
    """Архив → [(имя, байты | None, причина отказа)]; не архив — как есть."""
    if not data.startswith(b"PK"):
        return [(name, data, "")]
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [(name, None, "Архив повреждён")]
    out, unpacked = [], 0
    for info in archive.infolist():
        inner = info.filename
        if info.is_dir() or not inner.lower().endswith(".xml") or inner.startswith("__MACOSX/"):
            continue
        label = f"{name}/{inner}"[:255]
        if len(out) >= service.MAX_FILES:
            out.append((label, None, "В архиве больше 50 документов — остальные не загружены"))
            break
        if info.file_size > upd.MAX_BYTES or unpacked + info.file_size > MAX_UNPACKED:
            out.append((label, None, "Файл в архиве слишком большой"))
            continue
        try:                        # зашифрованный, повреждённый, неизвестное сжатие
            with archive.open(info) as fh:
                body = fh.read(upd.MAX_BYTES + 1)
        except (RuntimeError, NotImplementedError, EOFError, OSError, zipfile.BadZipFile, zlib.error):
            out.append((label, None, "Файл в архиве не читается: зашифрован или повреждён"))
            continue
        unpacked += len(body)       # по фактически прочитанному: заявленный размер можно подделать
        if unpacked > MAX_UNPACKED:
            out.append((label, None, "Архив слишком большой после распаковки"))
            break
        out.append((label, body, ""))
    return out or [(name, None, "В архиве нет XML-документов")]


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


@router.get("")
def list_documents(user: Accountant, db: Db,
                   company_id: Annotated[str | None, Query()] = None,
                   status: Annotated[str | None, Query()] = None,
                   q: Annotated[str | None, Query(max_length=100)] = None,
                   date_from: Annotated[date | None, Query()] = None,
                   date_to: Annotated[date | None, Query()] = None,
                   limit: Annotated[int, Query(ge=1, le=500)] = 100,
                   offset: Annotated[int, Query(ge=0)] = 0) -> dict:
    query = service.scope(db.query(PrimaryDocument), user, company_id)
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
