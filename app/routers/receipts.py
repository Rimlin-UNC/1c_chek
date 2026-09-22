# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Роутер чеков: приём сканирований, список, проверка ФНС, экспорт.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

from fastapi import (APIRouter, BackgroundTasks, Depends, File, HTTPException,
                     Query, UploadFile, status)
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..database import get_db
from ..models import FnsLog, MappingSetting, Receipt, ReceiptItem, User
from ..schemas import ExportRequest, ManualReceipt, ScanRequest, VerifyRequest
from ..services import exporter, imaging
from ..services.audit import log_action
from ..services.events import broadcast
from ..services.fns import check_receipt
from ..services.qr import (MultipleReceiptsError, ParsedQR, QRParseError,
                           build_qr_string, parse_qr, receipts_json)

router = APIRouter(prefix="/api/v1/receipts", tags=["Чеки"])


# --------------------------------------------------------------------------
#  Создание чека из разобранного QR (общая для всех источников)
# --------------------------------------------------------------------------
def _upsert_receipt(db: Session, parsed: ParsedQR, source: str,
                    user: User | None, image_path: str | None = None):
    """Создание чека с дедупликацией по ФН+ФД+ФП."""
    existing = db.query(Receipt).filter(
        Receipt.fn == parsed.fn, Receipt.fd == parsed.fd, Receipt.fp == parsed.fp
    ).first()
    if existing:
        return existing, False  # уже есть — дубликат

    receipt = Receipt(
        qr_data=parsed.qr_data,
        fn=parsed.fn, fd=parsed.fd, fp=parsed.fp,
        receipt_date=parsed.date_time or dt.datetime.utcnow(),
        total_sum=parsed.total_sum,
        operation=parsed.operation if parsed.operation in (1, 2) else 1,
        source=source,
        created_by=user.id if user else None,
        raw_data=receipts_json(parsed),
        image_path=image_path,
        status="new",
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt, True


# --------------------------------------------------------------------------
#  POST /scan — приём строки QR
# --------------------------------------------------------------------------
@router.post("/scan", summary="Приём сканирования: строка QR-кода чека")
def scan(body: ScanRequest, user: User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    try:
        parsed = parse_qr(body.qr_data)
    except QRParseError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    receipt, created = _upsert_receipt(db, parsed, body.source, user)
    if created:
        log_action(user, "receipt_created", "receipt", receipt.id,
                   {"fn": receipt.fn, "fd": receipt.fd, "sum": receipt.total_sum})
        broadcast("receipt_created", receipt.to_dict())
    else:
        log_action(user, "receipt_duplicate", "receipt", receipt.id,
                   {"fn": receipt.fn, "fd": receipt.fd})
        broadcast("receipt_duplicate", receipt.to_dict())

    return {
        "receipt": receipt.to_dict(with_items=True),
        "duplicate": not created,
        "message": ("Чек уже был отсканирован ранее — дубликат отсеян"
                    if not created else "Чек принят"),
    }


# --------------------------------------------------------------------------
#  POST /scan/image — приём изображения, QR распознаёт сервер (OpenCV)
# --------------------------------------------------------------------------
@router.post("/scan/image", summary="Приём изображения чека: сервер находит QR")
async def scan_image(file: UploadFile = File(...),
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    raw = await file.read()
    if len(raw) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Файл больше {settings.MAX_UPLOAD_MB} МБ")
    try:
        result = imaging.decode_qr_image(raw)
    except MultipleReceiptsError as e:
        raise HTTPException(
            status.HTTP_300_MULTIPLE_CHOICES,
            {"message": "На изображении найдено несколько разных чеков",
             "variants": [p.to_dict() for p in e.parsed]},
        )
    except (QRParseError, ValueError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    receipt, created = _upsert_receipt(db, result.parsed, "image", user)
    if created:
        log_action(user, "receipt_created", "receipt", receipt.id,
                   {"fn": receipt.fn, "fd": receipt.fd, "source": "image"})
        broadcast("receipt_created", receipt.to_dict())
    return {
        "receipt": receipt.to_dict(with_items=True),
        "duplicate": not created,
        "message": "Чек принят" if created else "Чек уже был отсканирован ранее",
        "codes_found": result.total_codes,
        "debug": result.debug,
    }


# --------------------------------------------------------------------------
#  POST /manual — ручной ввод реквизитов (резервный режим)
# --------------------------------------------------------------------------
@router.post("/manual", summary="Ручной ввод реквизитов чека")
def manual(body: ManualReceipt, user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    import datetime as dt
    date_time = None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%d.%m.%y %H:%M"):
        try:
            date_time = dt.datetime.strptime(body.date_time.strip(), fmt)
            break
        except ValueError:
            continue
    if date_time is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Некорректный формат даты. Пример: 22.09.2025 14:30")

    qr_data = build_qr_string(date_time, body.total_sum, body.fn, body.fd,
                              body.fp, body.operation)
    try:
        parsed = parse_qr(qr_data)
    except QRParseError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    receipt, created = _upsert_receipt(db, parsed, "manual", user)
    if created:
        log_action(user, "receipt_created", "receipt", receipt.id, {"source": "manual"})
        broadcast("receipt_created", receipt.to_dict())
    return {"receipt": receipt.to_dict(with_items=True), "duplicate": not created,
            "message": "Чек принят" if created else "Чек уже есть в системе"}


# --------------------------------------------------------------------------
#  GET / — список чеков с фильтрацией
# --------------------------------------------------------------------------
@router.get("", summary="Список чеков (фильтры, пагинация)")
def list_receipts(
    status: str | None = Query(None, description="new|verifying|verified|failed"),
    fns_status: str | None = Query(None, description="valid|invalid|not_found|unknown"),
    exported: bool | None = Query(None, description="Выгружен ли чек в 1С"),
    q: str | None = Query(None, description="Поиск по ФН/ФД/ФП"),
    date_from: str | None = Query(None, description="ГГГГ-ММ-ДД"),
    date_to: str | None = Query(None, description="ГГГГ-ММ-ДД"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    query = db.query(Receipt)
    if status:
        query = query.filter(Receipt.status == status)
    if fns_status:
        query = query.filter(Receipt.fns_status == fns_status)
    if exported is not None:
        query = query.filter(Receipt.exported == exported)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Receipt.fn.like(like), Receipt.fd.like(like),
                                 Receipt.fp.like(like), Receipt.qr_data.like(like)))
    if date_from:
        try:
            d = dt.datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(Receipt.receipt_date >= d)
        except ValueError:
            pass
    if date_to:
        try:
            d = dt.datetime.strptime(date_to, "%Y-%m-%d")
            d = d.replace(hour=23, minute=59, second=59)
            query = query.filter(Receipt.receipt_date <= d)
        except ValueError:
            pass

    total = query.count()
    total_sum = query.with_entities(func.coalesce(func.sum(Receipt.total_sum), 0)).scalar()
    rows = (query.order_by(Receipt.receipt_date.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total,
        "total_sum": round(float(total_sum or 0), 2),
        "page": page,
        "page_size": page_size,
        "items": [r.to_dict() for r in rows],
    }


# --------------------------------------------------------------------------
#  GET /{id} — карточка чека
# --------------------------------------------------------------------------
@router.get("/{receipt_id}", summary="Чек с позициями")
def get_receipt(receipt_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    d = receipt.to_dict(with_items=True)
    d["raw_data"] = receipt.raw_data
    return d


# --------------------------------------------------------------------------
#  DELETE /{id}
# --------------------------------------------------------------------------
@router.delete("/{receipt_id}", summary="Удаление чека")
def delete_receipt(receipt_id: str, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    if receipt.exported and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Чек уже выгружен в 1С; удалять может только администратор")
    info = {"fn": receipt.fn, "fd": receipt.fd, "fp": receipt.fp}
    db.delete(receipt)
    db.commit()
    log_action(user, "receipt_deleted", "receipt", receipt_id, info)
    broadcast("receipt_deleted", {"id": receipt_id})
    return {"ok": True}


# --------------------------------------------------------------------------
#  Проверка в ФНС (фоновая задача + очередь)
# --------------------------------------------------------------------------
def _verify_one_sync(receipt_id: str) -> None:
    """Синхронное выполнение проверки (для фонового потока)."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        receipt = db.get(Receipt, receipt_id)
        if not receipt:
            return
        receipt.status = "verifying"
        db.commit()
        broadcast("receipt_verifying", {"id": receipt.id, "status": "verifying"})

        t0 = time.monotonic()
        result = check_receipt(receipt.fn, receipt.fd, receipt.fp,
                               receipt.total_sum, receipt.receipt_date,
                               receipt.fns_status, receipt.fns_checked_at)
        duration = int((time.monotonic() - t0) * 1000)

        receipt.fns_status = result.status
        receipt.fns_message = result.message
        receipt.fns_checked_at = dt.datetime.utcnow()
        receipt.status = "verified" if result.status in ("valid", "not_found") else \
                         ("failed" if result.status == "invalid" else receipt.status)
        if result.status == "unknown":
            receipt.status = "new"  # можно повторить проверку
        # Сохраняем позиции из ответа ФНС (если провайдер их вернул)
        items = result.raw.get("items") if isinstance(result.raw, dict) else None
        if items and not receipt.items:
            for i, it in enumerate(items[:200]):
                db.add(ReceiptItem(
                    receipt_id=receipt.id,
                    name=str(it.get("name", "Позиция")) [:500],
                    quantity=float(it.get("quantity", 1) or 1),
                    price=float(it.get("price", 0) or 0),
                    total=float(it.get("sum", 0) or 0),
                    vat_rate=str(it.get("vat_rate", "none") or "none")[:10],
                    vat_sum=float(it.get("vat_sum", 0) or 0),
                    position=i,
                ))
        raw = json.loads(receipt.raw_data or "{}")
        raw["fns"] = result.raw
        receipt.raw_data = json.dumps(raw, ensure_ascii=False)
        db.commit()

        db.add(FnsLog(receipt_id=receipt.id,
                      request_data=json.dumps({"fn": receipt.fn, "fd": receipt.fd,
                                               "fp": receipt.fp}, ensure_ascii=False),
                      response_data=json.dumps(result.raw, ensure_ascii=False)[:4000],
                      provider=result.raw.get("provider", "mock") if isinstance(result.raw, dict) else "mock",
                      ok=result.ok, duration_ms=duration))
        db.commit()
        broadcast("receipt_verified", receipt.to_dict())
    except Exception as e:  # не роняем воркер
        try:
            db.rollback()
            r = db.get(Receipt, receipt_id)
            if r and r.status == "verifying":
                r.status = "new"
                r.fns_message = f"Ошибка проверки: {e}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _run_verification(receipt_ids: list[str]) -> None:
    """Фоновое пакетное выполнение проверок (по одной в потоке)."""
    threads = []
    for rid in receipt_ids:
        t = threading.Thread(target=_verify_one_sync, args=(rid,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=60)


@router.post("/verify", summary="Проверить чеки в ФНС (асинхронно)")
def verify(body: VerifyRequest, background: BackgroundTasks,
           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ids = body.receipt_ids
    if ids:
        found = db.query(Receipt).filter(Receipt.id.in_(ids)).all()
        ids = [r.id for r in found]
    else:
        found = db.query(Receipt).filter(Receipt.status.in_(["new", "failed"])).all()
        ids = [r.id for r in found]
    if not ids:
        return {"queued": 0, "message": "Нет чеков для проверки"}
    background.add_task(_run_verification, ids)
    log_action(user, "verify_queued", details={"count": len(ids)})
    return {"queued": len(ids), "message": f"Отправлено на проверку: {len(ids)} чеков"}


@router.post("/{receipt_id}/verify", summary="Проверить один чек в ФНС")
def verify_one(receipt_id: str, background: BackgroundTasks,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    background.add_task(_run_verification, [receipt.id])
    return {"queued": 1, "message": "Чек отправлен на проверку в ФНС"}


# --------------------------------------------------------------------------
#  Экспорт EnterpriseData
# --------------------------------------------------------------------------
@router.post("/export", summary="Экспорт чеков в 1С (EnterpriseData)")
def export_receipts(body: ExportRequest, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    query = db.query(Receipt)
    if body.receipt_ids:
        query = query.filter(Receipt.id.in_(body.receipt_ids))
    else:
        query = query.filter(Receipt.exported == False, Receipt.status == "verified")  # noqa: E712
    receipts = query.order_by(Receipt.receipt_date).limit(settings.ONEC_BATCH_SIZE * 10).all()
    if not receipts:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Нет чеков для экспорта (проверенные чеки отсутствуют)")

    mapping = db.query(MappingSetting).filter(MappingSetting.is_active == True).all()  # noqa: E712

    if body.format == "xml":
        content = exporter.build_enterprise_data_xml(receipts, mapping, body.target_object)
        media = "application/xml"
        ext = "xml"
    else:
        content = exporter.build_enterprise_data_json(receipts, mapping, body.target_object)
        media = "application/json"
        ext = "json"

    # Помечаем как выгруженные
    now = dt.datetime.utcnow()
    for r in receipts:
        r.exported = True
        r.exported_at = now
    db.commit()
    log_action(user, "receipts_exported", details={
        "count": len(receipts), "format": body.format,
        "target": body.target_object})

    filename = f"ymaster-check-export-{now.strftime('%Y%m%d-%H%M')}.{ext}"
    return Response(
        content=content, media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
