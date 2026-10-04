# -*- coding: utf-8 -*-
"""
Ямастер Чек — роутер чеков (ООО «Ямастер», ymaster.ru).

Права по ролям:
  * Пользователь  — сканирует, видит ТОЛЬКО свои чеки; удаление — свои не выгруженные.
  * Бухгалтер     — видит все чеки, проверка ФНС, выгрузка в 1С, назначение
                    сотрудника (подотчётника), CSV, удаление не выгруженных.
  * Администратор — всё, включая удаление выгруженных и настройки.

Автоматизация рутины бухгалтера:
  * авто-проверка чека в ФНС сразу после сканирования (настройка auto_verify);
  * назначение сотрудника на чек (для авансовых отчётов) — сразу попадает в 1С;
  * экспорт CSV для быстрой сводки; EnterpriseData JSON/XML для 1С.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import threading
import time

from fastapi import (APIRouter, BackgroundTasks, Depends, File, HTTPException,
                     Query, UploadFile, status)
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import (ROLE_ACCOUNTANT, ROLE_ADMIN, ROLE_USER, get_current_user,
                    require_accountant, require_admin)
from ..services.scoping import (can_edit_receipt, can_view_receipt,
                                scope_receipts, target_company_id)   # v1.11.0

from ..config import settings
from ..database import get_db
from ..models import (Company, FnsLog, MappingSetting, Receipt,
                      ReceiptItem, User)
from ..schemas import (AssignBulk, ExportRequest, FetchDetailsRequest,
                       ManualReceipt, ReceiptMoveBody, ReceiptPatch,
                       ScanRequest, VerifyRequest)
from ..services import appsettings, exporter, imaging
from ..services.audit import log_action
from ..services.events import broadcast
from ..services.fns import check_receipt
from ..services.external import engine as external_engine, ExternalResult
from ..services.qr import (MultipleReceiptsError, ParsedQR, QRParseError,
                           build_qr_string, parse_qr, receipts_json)

router = APIRouter(prefix="/api/v1/receipts", tags=["Чеки"])


# --------------------------------------------------------------------------
#  Создание чека из разобранного QR (общая для всех источников)
# --------------------------------------------------------------------------
def _upsert_receipt(db: Session, parsed: ParsedQR, source: str,
                    user: User | None,
                    company_id: str | None = None) -> tuple[Receipt, bool]:
    """Создание чека с дедупликацией по ФН+ФД+ФП."""
    existing = db.query(Receipt).filter(
        Receipt.fn == parsed.fn, Receipt.fd == parsed.fd, Receipt.fp == parsed.fp
    ).first()
    if existing:
        return existing, False  # уже есть — дубликат

    # v1.2.0: чек, присланный сотрудником, сразу помечается «от кого» —
    # бухгалтеру не нужно назначать вручную; меняют только бухгалтер/админ.
    auto_assignee = ""
    if user is not None and user.role == ROLE_USER:
        auto_assignee = (user.full_name or user.username or "")[:200]

    receipt = Receipt(
        qr_data=parsed.qr_data,
        fn=parsed.fn, fd=parsed.fd, fp=parsed.fp,
        receipt_date=parsed.date_time or dt.datetime.utcnow(),
        total_sum=parsed.total_sum,
        operation=parsed.operation if parsed.operation in (1, 2) else 1,
        source=source,
        created_by=user.id if user else None,
        company_id=company_id,                     # v1.11.0: пространство клиента
        assignee=auto_assignee,
        raw_data=receipts_json(parsed),
        status="new",
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt, True


def _company_names(db: Session, ids) -> dict:
    """v1.12.3: {id: название} компаний одним запросом (для чеков)."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {c.id: c.name for c in db.query(Company).filter(Company.id.in_(ids)).all()}


def _attach_company(db: Session, d: dict) -> dict:
    """Добавить company_name к dict чека (карточка/скан)."""
    if d.get("company_id"):
        d["company_name"] = _company_names(db, [d["company_id"]]).get(d["company_id"])
    return d


def _maybe_auto_verify(background: BackgroundTasks, db: Session,
                       receipt: Receipt) -> bool:
    """Если включена настройка auto_verify — сразу ставим чек в проверку ФНС."""
    if appsettings.auto_verify_enabled(db) and receipt.status == "new":
        background.add_task(_run_verification, [receipt.id])
        return True
    return False


# --------------------------------------------------------------------------
#  POST /scan — приём строки QR
# --------------------------------------------------------------------------
@router.post("/scan", summary="Приём сканирования: строка QR-кода чека")
def scan(body: ScanRequest, background: BackgroundTasks,
         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        parsed = parse_qr(body.qr_data)
    except QRParseError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))

    # v1.11.0: чек попадает в пространство компании (админ может выбрать)
    company_id = target_company_id(db, user, body.company_id)
    receipt, created = _upsert_receipt(db, parsed, body.source, user, company_id)
    if created:
        log_action(user, "receipt_created", "receipt", receipt.id,
                   {"fn": receipt.fn, "fd": receipt.fd, "sum": receipt.total_sum})
        broadcast("receipt_created", receipt.to_dict())
        auto = _maybe_auto_verify(background, db, receipt)
        _maybe_auto_fetch(background, db, receipt)      # v1.2.0: данные из сервисов
    else:
        log_action(user, "receipt_duplicate", "receipt", receipt.id,
                   {"fn": receipt.fn, "fd": receipt.fd})
        broadcast("receipt_duplicate", receipt.to_dict())
        auto = False
        # v1.11.0: дубликат из ЧУЖОЙ компании — данные не раскрываем
        if not can_view_receipt(user, receipt):
            return {"receipt": None, "duplicate": True, "auto_verify": False,
                    "message": "Этот чек уже учтён в другой организации. "
                               "Если он должен относиться к вашей — попросите "
                               "администратора переместить его."}

    return {
        "receipt": _attach_company(db, receipt.to_dict(with_items=True)),
        "duplicate": not created,
        "auto_verify": auto,
        "message": ("Чек уже был отсканирован ранее — дубликат отсеян"
                    if not created else
                    ("Чек принят, отправлен на проверку в ФНС" if auto else "Чек принят")),
    }


# --------------------------------------------------------------------------
#  POST /scan/image — приём изображения, QR распознаёт сервер (OpenCV)
# --------------------------------------------------------------------------
@router.post("/scan/image", summary="Приём изображения чека: сервер находит QR")
async def scan_image(background: BackgroundTasks, file: UploadFile = File(...),
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

    receipt, created = _upsert_receipt(db, result.parsed, "image", user,
                                       target_company_id(db, user, None))
    if created:
        log_action(user, "receipt_created", "receipt", receipt.id,
                   {"fn": receipt.fn, "fd": receipt.fd, "source": "image"})
        broadcast("receipt_created", receipt.to_dict())
        auto = _maybe_auto_verify(background, db, receipt)
        _maybe_auto_fetch(background, db, receipt)      # v1.2.0: данные из сервисов
    else:
        auto = False
    return {
        "receipt": _attach_company(db, receipt.to_dict(with_items=True)),
        "duplicate": not created,
        "auto_verify": auto,
        "message": "Чек принят" if created else "Чек уже был отсканирован ранее",
        "codes_found": result.total_codes,
        "debug": result.debug,
    }


# --------------------------------------------------------------------------
#  POST /manual — ручной ввод реквизитов (резервный режим)
# --------------------------------------------------------------------------
@router.post("/manual", summary="Ручной ввод реквизитов чека")
def manual(body: ManualReceipt, background: BackgroundTasks,
           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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

    receipt, created = _upsert_receipt(db, parsed, "manual", user,
                                       target_company_id(db, user, getattr(body, "company_id", None)))
    if created:
        log_action(user, "receipt_created", "receipt", receipt.id, {"source": "manual"})
        broadcast("receipt_created", receipt.to_dict())
        _maybe_auto_verify(background, db, receipt)
        _maybe_auto_fetch(background, db, receipt)      # v1.2.0: данные из сервисов
    return {"receipt": _attach_company(db, receipt.to_dict(with_items=True)),
            "duplicate": not created,
            "message": "Чек принят" if created else "Чек уже есть в системе"}


# --------------------------------------------------------------------------
#  GET / — список чеков с фильтрацией (с учётом прав)
# --------------------------------------------------------------------------
@router.get("", summary="Список чеков (фильтры, пагинация)")
def list_receipts(
    status_filter: str | None = Query(None, alias="status",
                                      description="new|verifying|verified|failed"),
    fns_status: str | None = Query(None, description="valid|invalid|not_found|unknown"),
    exported: bool | None = Query(None, description="Выгружен ли чек в 1С"),
    q: str | None = Query(None, description="Поиск по ФН/ФД/ФП/сотруднику"),
    ids: str | None = Query(None, description="CSV UUID — выборка конкретных чеков (печать/экспорт)"),
    assignee: str | None = Query(None, description="Фильтр по сотруднику"),
    category: str | None = Query(None, description="Статья расходов"),
    notified: bool | None = Query(None, description="Только с уведомлениями сотрудников"),
    attention: bool | None = Query(None, description="Требуют внимания: не обработан >3 дней"),
    date_from: str | None = Query(None, description="ГГГГ-ММ-ДД"),
    date_to: str | None = Query(None, description="ГГГГ-ММ-ДД"),
    company_id: str | None = Query(None, max_length=36,
                                   description="v1.11.0: компания (только админ)"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    query = db.query(Receipt)

    # v1.7.0: выборка конкретных чеков (печать PDF) — с позициями товаров
    id_list = [x.strip() for x in (ids or "").split(",") if x.strip()][:100]
    with_items = bool(id_list)
    if id_list:
        query = query.filter(Receipt.id.in_(id_list))

    # v1.11.0: изоляция пространства (админ — все/фильтр; бухгалтер — компания;
    # пользователь — свои в своей компании)
    query = scope_receipts(query, user, company_id)

    if status_filter:
        query = query.filter(Receipt.status == status_filter)
    if fns_status:
        query = query.filter(Receipt.fns_status == fns_status)
    if exported is not None:
        query = query.filter(Receipt.exported == exported)
    if assignee:
        query = query.filter(Receipt.assignee.ilike(f"%{assignee.strip()}%"))
    if category:
        # Ищем по нормализованному полю: работает и с кириллицей в любом регистре
        query = query.filter(Receipt.category_lc.like(f"%{category.strip().casefold()}%"))
    if notified is not None:
        query = query.filter(Receipt.notified == notified)
    if attention:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=3)
        query = query.filter(Receipt.status.in_(["new", "verifying"]),
                             Receipt.created_at < cutoff)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Receipt.fn.like(like), Receipt.fd.like(like),
                                 Receipt.fp.like(like), Receipt.qr_data.like(like),
                                 Receipt.assignee.like(like)))
    if date_from:
        try:
            d = dt.datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(Receipt.receipt_date >= d)
        except ValueError:
            pass
    if date_to:
        try:
            d = dt.datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59,
                                                                  second=59)
            query = query.filter(Receipt.receipt_date <= d)
        except ValueError:
            pass

    total = query.count()
    total_sum = query.with_entities(func.coalesce(func.sum(Receipt.total_sum), 0)).scalar()
    rows = (query.order_by(Receipt.receipt_date.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    # v1.12.3: название компании для каждой строки (карта одним запросом)
    cname = _company_names(db, (r.company_id for r in rows))
    items = []
    for r in rows:
        d = r.to_dict(with_items=with_items)
        d["company_name"] = cname.get(r.company_id)
        items.append(d)
    return {
        "total": total,
        "total_sum": round(float(total_sum or 0), 2),
        "page": page,
        "page_size": page_size,
        "items": items,
    }


# --------------------------------------------------------------------------
#  Карточка / изменение / удаление
# --------------------------------------------------------------------------
@router.get("/{receipt_id}", summary="Чек с позициями")
def get_receipt(receipt_id: str,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt or not can_view_receipt(user, receipt):
        # v1.11.0: чужая компания/чужой чек — «не найден» (не раскрываем существование)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    d = receipt.to_dict(with_items=True)
    d["raw_data"] = receipt.raw_data
    _attach_company(db, d)                     # v1.12.3: название компании
    return d


@router.get("/{receipt_id}/qr.png", summary="QR-код чека как в кассовом аппарате (PNG)")
def receipt_qr_png(receipt_id: str,
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """v1.10.0: PNG с фискальной строкой чека (t=…&s=…&fn=…&i=…&fp=…&n=…) —
    в печатной версии чек выглядит как настоящий кассовый, с QR для проверки."""
    import io

    import qrcode
    from fastapi.responses import Response
    receipt = db.get(Receipt, receipt_id)
    if not receipt or not can_view_receipt(user, receipt):
        # v1.11.0: чужая компания/чужой чек — «не найден» (не раскрываем существование)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    img = qrcode.make(receipt.qr_data, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.patch("/{receipt_id}", summary="Изменение чека (сотрудник: уведомление/комментарий; бухгалтер+: всё)")
def patch_receipt(receipt_id: str, body: ReceiptPatch,
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")

    # v1.11.0: права = компания + роль. Чужая компания выглядит как «нет чека».
    if not can_view_receipt(user, receipt):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    is_staff = can_edit_receipt(user, receipt)          # бухгалтер/админ своей компании
    is_owner = (not is_staff and receipt.created_by == user.id)  # свой чек (user)
    if not is_staff and not is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Сотрудник работает только со своими чеками")

    changed = {"fields": []}

    if not is_staff:
        # Сотрудник: только галочка «Уведомляю бухгалтерию» и комментарий
        forbidden = [f for f in ("assignee", "fn", "fd", "fp", "receipt_date",
                                 "total_sum", "personal_sum", "operation",
                                 "merchant_name", "merchant_inn",
                                 "merchant_address", "cashier",
                                 "items") if getattr(body, f) is not None]
        if forbidden:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Сотрудник может изменить только уведомление и комментарий")
        if body.notified is not None:
            receipt.notified = body.notified
            changed["fields"].append("notified")
        if body.comment is not None:
            receipt.comment = body.comment.strip()[:2000]
            changed["fields"].append("comment")
    else:
        # Бухгалтер/админ: любые поля. Изменять реквизиты УЖЕ выгруженного
        # чека может только администратор (целостность данных в 1С).
        core_edit = any(getattr(body, f) is not None for f in
                        ("fn", "fd", "fp", "receipt_date", "total_sum",
                         "operation", "items"))
        if core_edit and receipt.exported and user.role != ROLE_ADMIN:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Чек уже выгружен в 1С — обратитесь к администратору")

        # Уникальность ФН+ФД+ФП при изменении реквизитов
        if any(getattr(body, f) is not None for f in ("fn", "fd", "fp")):
            new_fn = (body.fn or receipt.fn).strip()
            new_fd = (body.fd or receipt.fd).strip()
            new_fp = (body.fp or receipt.fp).strip()
            dup = (db.query(Receipt)
                   .filter(Receipt.fn == new_fn, Receipt.fd == new_fd,
                           Receipt.fp == new_fp, Receipt.id != receipt.id)
                   .first())
            if dup:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "Чек с такими ФН/ФД/ФП уже существует")
            receipt.fn, receipt.fd, receipt.fp = new_fn, new_fd, new_fp
            changed["fields"] += ["fn", "fd", "fp"]

        if body.receipt_date is not None:
            receipt.receipt_date = body.receipt_date
            changed["fields"].append("receipt_date")
        if body.total_sum is not None:
            receipt.total_sum = body.total_sum
            changed["fields"].append("total_sum")
        if body.personal_sum is not None:            # v1.8.0: личные суммы
            receipt.personal_sum = body.personal_sum
            changed["fields"].append("personal_sum")
        if body.operation in (1, 2):
            receipt.operation = body.operation
            changed["fields"].append("operation")
        for f in ("merchant_name", "merchant_inn", "merchant_address", "cashier"):
            v = getattr(body, f)
            if v is not None:
                setattr(receipt, f, v.strip()[:500])
                changed["fields"].append(f)
        if body.assignee is not None:
            receipt.assignee = body.assignee.strip()[:200]
            changed["fields"].append("assignee")
        if body.comment is not None:
            receipt.comment = body.comment.strip()[:2000]
            changed["fields"].append("comment")
        if body.notified is not None:
            receipt.notified = body.notified
            changed["fields"].append("notified")
        if body.category is not None:
            cat = body.category.strip()[:100]
            receipt.category = cat
            receipt.category_lc = cat.casefold()      # для регистронезависимого фильтра (кириллица)
            changed["fields"].append("category")
        if body.items is not None:
            receipt.items.clear()
            for pos, it in enumerate(body.items):
                receipt.items.append(ReceiptItem(
                    name=it.name.strip()[:1000],
                    quantity=it.quantity, price=it.price, total=it.total,
                    vat_rate=(it.vat_rate or "none")[:10],
                    vat_sum=it.vat_sum or 0.0, position=pos))
            changed["fields"].append(f"items({len(body.items)})")
        if changed["fields"]:
            # Ручная правка — последний и главный источник данных о чеке
            receipt.details_source = "manual_edit"
            receipt.details_fetched_at = dt.datetime.utcnow()

    db.commit()
    log_action(user, "receipt_updated", "receipt", receipt.id,
               {"fields": changed["fields"], "role": user.role})
    broadcast("receipt_updated", receipt.to_dict())
    return receipt.to_dict(with_items=True)


@router.post("/bulk-assign", summary="Назначить сотрудника на несколько чеков (бухгалтер+)")
def bulk_assign(body: AssignBulk, user: User = Depends(require_accountant),
                db: Session = Depends(get_db)):
    if not body.receipt_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Не выбраны чеки")
    updated = (scope_receipts(db.query(Receipt), user)          # v1.11.0
               .filter(Receipt.id.in_(body.receipt_ids))
               .update({Receipt.assignee: body.assignee.strip()[:200]},
                       synchronize_session=False))
    db.commit()
    log_action(user, "receipts_assigned", details={"count": updated,
                                                   "assignee": body.assignee})
    return {"ok": True, "updated": updated,
            "message": f"Сотрудник назначен, чеков обновлено: {updated}"}


# --------------------------------------------------------------------------
#  v1.11.0: Перемещение чеков между компаниями (администратор платформы).
#  «Забрать чек и передать в любую компанию»: чек уникален глобально
#  (ФН+ФД+ФП), перемещение меняет только компанию-владельца.
# --------------------------------------------------------------------------
@router.post("/move", summary="Переместить чеки в другую компанию (администратор)")
def move_receipts(body: ReceiptMoveBody, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    comp = db.get(Company, body.company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания назначения не найдена")
    if not comp.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Компания заархивирована — перемещение недоступно")
    rows = db.query(Receipt).filter(Receipt.id.in_(body.receipt_ids)).all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чеки не найдены")
    moved_ids, moved, same = [], 0, 0
    for r in rows:
        if r.company_id == comp.id:
            same += 1
            continue
        r.company_id = comp.id
        # выгрузка относилась к прежней компании — сбрасываем, чтобы выгрузку
        # новой компании бухгалтер проконтролировал сам
        r.exported = False
        r.exported_at = None
        if body.assignee is not None:
            r.assignee = body.assignee.strip()[:200]
        moved_ids.append(r.id)
        moved += 1
    db.commit()
    if moved:
        log_action(user, "receipts_moved", details={
            "count": moved, "to_company": comp.name, "ids": moved_ids[:50]})
        broadcast("receipts_moved", {"ids": moved_ids, "company_id": comp.id,
                                     "company_name": comp.name})
    msg = (f"Перемещено чеков: {moved} → {comp.name}" if moved
           else "Все выбранные чеки уже в этой компании")
    if moved:
        msg += "; флаг выгрузки в 1С сброшен"
    return {"ok": True, "moved": moved, "already_there": same,
            "company": {"id": comp.id, "name": comp.name}, "message": msg}


# --------------------------------------------------------------------------
#  v1.2.0: Получение ПОЛНЫХ данных чека из внешних источников
#  (ФНС API / proverkacheka.com / свой сервис) — с паузами 2–7 с и ротацией
# --------------------------------------------------------------------------
def _apply_external_result(db: Session, receipt: Receipt, res: ExternalResult) -> None:
    """Применение полученных данных к чеку (только непустые поля).

    v1.3.0: если бухгалтер уже правил чек вручную (details_source ==
    manual_edit) — машинные данные НЕ затирают человеческие: заполняются
    только пустые поля, реквизиты/позиции/сумма остаются как задал человек.
    """
    if not res.ok:
        receipt.fns_message = res.message
        db.commit()
        return
    human_edited = receipt.details_source == "manual_edit"

    def applyable(machine_val, human_val=None) -> bool:
        """Машинное значение применяем, если оно есть и не конфликтует с ручным."""
        if machine_val in (None, "", 0, []):
            return False
        if human_edited and human_val not in (None, "", 0, []):
            return False
        return True

    if applyable(res.date_time, receipt.receipt_date):
        receipt.receipt_date = res.date_time
    if applyable(res.total_sum, receipt.total_sum):
        receipt.total_sum = res.total_sum
    if applyable(res.operation):
        receipt.operation = res.operation
    if applyable(res.merchant_name, receipt.merchant_name):
        receipt.merchant_name = res.merchant_name
    if applyable(res.merchant_inn, receipt.merchant_inn):
        receipt.merchant_inn = res.merchant_inn
    if applyable(res.merchant_address, receipt.merchant_address):
        receipt.merchant_address = res.merchant_address
    if applyable(res.cashier, receipt.cashier):
        receipt.cashier = res.cashier
    if applyable(res.cash_sum):
        receipt.cash_sum = res.cash_sum
    if applyable(res.ecash_sum):
        receipt.ecash_sum = res.ecash_sum
    if res.found:
        receipt.fns_status = "valid"
        receipt.fns_checked_at = dt.datetime.utcnow()
        receipt.fns_message = f"Данные получены ({res.source})"
        if res.items and not (human_edited and receipt.items):
            receipt.items.clear()
            for pos, it in enumerate(res.items):
                receipt.items.append(ReceiptItem(
                    name=it.name[:1000], quantity=it.quantity,
                    price=it.price, total=it.total,
                    vat_rate=it.vat_rate[:10], vat_sum=it.vat_sum,
                    position=pos))
    if not human_edited:
        receipt.details_source = res.source or receipt.details_source
    receipt.details_fetched_at = dt.datetime.utcnow()
    try:
        raw = json.loads(receipt.raw_data or "{}")
    except ValueError:
        raw = {}
    raw["external"] = {"source": res.source, "message": res.message,
                       "fetched_at": dt.datetime.utcnow().isoformat() + "Z"}
    receipt.raw_data = json.dumps(raw, ensure_ascii=False)
    db.commit()


def _run_external_fetch(receipt_ids: list[str]) -> None:
    """Фоновый воркер: последовательно, с встроенными паузами движка."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        for rid in receipt_ids:
            receipt = db.get(Receipt, rid)
            if receipt is None:
                continue
            res = external_engine.fetch(
                db, receipt.qr_data, receipt.fn, receipt.fd, receipt.fp,
                receipt.total_sum, receipt.receipt_date)
            _apply_external_result(db, receipt, res)
            db.refresh(receipt)
            broadcast("receipt_updated", receipt.to_dict(with_items=True))
    finally:
        db.close()


def _maybe_auto_fetch(background: BackgroundTasks, db: Session, receipt: Receipt) -> bool:
    """Автозагрузка деталей после скана (настройка external_auto, по умолч. вкл)."""
    if appsettings.get_setting(db, "external_auto", "1") == "1" and receipt.status == "new":
        background.add_task(_run_external_fetch, [receipt.id])
        return True
    return False


@router.post("/{receipt_id}/fetch-details",
             summary="Получить полные данные чека из сервиса проверки (бухгалтер+)")
def fetch_details_one(receipt_id: str, background: BackgroundTasks,
                      user: User = Depends(require_accountant),
                      db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt or not can_view_receipt(user, receipt):      # v1.11.0
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    background.add_task(_run_external_fetch, [receipt.id])
    log_action(user, "external_fetch", "receipt", receipt.id, {"queued": 1})
    return {"ok": True, "queued": True,
            "message": "Запрос отправлен (пауза 2–7 с для защиты от блокировки). "
                       "Данные появятся автоматически"}


@router.post("/fetch-details",
             summary="Массовое получение данных чеков (бухгалтер+, очередь с паузами)")
def fetch_details_bulk(body: FetchDetailsRequest, background: BackgroundTasks,
                       user: User = Depends(require_accountant),
                       db: Session = Depends(get_db)):
    visible = {r.id for r in scope_receipts(
        db.query(Receipt).filter(Receipt.id.in_(body.receipt_ids)), user).all()}
    ids = [i for i in dict.fromkeys(body.receipt_ids) if i in visible]
    if not ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чеки не найдены")
    background.add_task(_run_external_fetch, ids)
    log_action(user, "external_fetch", details={"queued": len(ids)})
    return {"ok": True, "queued": len(ids),
            "message": f"В очереди чеков: {len(ids)}. Источники опрашиваются "
                       f"по очереди с паузами 2–7 с — блокировок не будет"}


@router.get("/external/status",
            summary="Состояние источников данных (бухгалтер+)")
def external_status(user: User = Depends(require_accountant),
                    db: Session = Depends(get_db)):
    cfg = external_engine._settings(db)
    return {
        "chain": external_engine.provider_chain(db),
        "engine": external_engine.status(),
        "configured": {
            "fns_api": bool(cfg.get("fns_master_token") or settings.FNS_MASTER_TOKEN),
            "proverkacheka": bool(cfg.get("proverkacheka_token")),
            "custom": bool(cfg.get("external_custom_url")),
        },
        "auto_fetch": appsettings.get_setting(db, "external_auto", "1") == "1",
    }


@router.delete("/{receipt_id}", summary="Удаление чека (по правилам ролей)")
def delete_receipt(receipt_id: str, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    if not can_view_receipt(user, receipt):                     # v1.11.0
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    if receipt.exported and user.role != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Чек уже выгружен в 1С; удалять может только администратор")
    info = {"fn": receipt.fn, "fd": receipt.fd, "fp": receipt.fp}
    db.delete(receipt)
    db.commit()
    log_action(user, "receipt_deleted", "receipt", receipt_id, info)
    broadcast("receipt_deleted", {"id": receipt_id,
                               "company_id": receipt.company_id})
    return {"ok": True}


# --------------------------------------------------------------------------
#  Проверка в ФНС (фоновая задача)
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
        receipt.status = ("verified" if result.status in ("valid", "not_found")
                          else ("failed" if result.status == "invalid" else receipt.status))
        if result.status == "unknown":
            receipt.status = "new"  # можно повторить проверку
        items = result.raw.get("items") if isinstance(result.raw, dict) else None
        if items and not receipt.items:
            for i, it in enumerate(items[:200]):
                db.add(ReceiptItem(
                    receipt_id=receipt.id,
                    name=str(it.get("name", "Позиция"))[:500],
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
                      provider=(result.raw.get("provider", "mock")
                                if isinstance(result.raw, dict) else "mock"),
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
    """Фоновое пакетное выполнение проверок."""
    threads = []
    for rid in receipt_ids:
        t = threading.Thread(target=_verify_one_sync, args=(rid,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=90)


@router.post("/verify", summary="Проверить чеки в ФНС (бухгалтер+, асинхронно)")
def verify(body: VerifyRequest, background: BackgroundTasks,
           user: User = Depends(require_accountant), db: Session = Depends(get_db)):
    ids = body.receipt_ids
    if ids:
        found = scope_receipts(
            db.query(Receipt).filter(Receipt.id.in_(ids)), user).all()   # v1.11.0
        ids = [r.id for r in found]
    else:
        found = scope_receipts(
            db.query(Receipt).filter(Receipt.status.in_(["new", "failed"])),
            user).all()
        ids = [r.id for r in found]
    if not ids:
        return {"queued": 0, "message": "Нет чеков для проверки"}
    background.add_task(_run_verification, ids)
    log_action(user, "verify_queued", details={"count": len(ids)})
    return {"queued": len(ids), "message": f"Отправлено на проверку: {len(ids)} чеков"}


@router.post("/{receipt_id}/verify", summary="Проверить один чек в ФНС (бухгалтер+)")
def verify_one(receipt_id: str, background: BackgroundTasks,
               user: User = Depends(require_accountant), db: Session = Depends(get_db)):
    receipt = db.get(Receipt, receipt_id)
    if not receipt or not can_view_receipt(user, receipt):      # v1.11.0
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чек не найден")
    background.add_task(_run_verification, [receipt.id])
    return {"queued": 1, "message": "Чек отправлен на проверку в ФНС"}


# --------------------------------------------------------------------------
#  Экспорт: EnterpriseData (1С) и CSV (сводка для бухгалтерии)
# --------------------------------------------------------------------------
@router.post("/export", summary="Экспорт чеков в 1С (EnterpriseData, бухгалтер+)")
def export_receipts(body: ExportRequest, user: User = Depends(require_accountant),
                    db: Session = Depends(get_db)):
    query = scope_receipts(db.query(Receipt), user)             # v1.11.0
    if body and body.receipt_ids:
        query = query.filter(Receipt.id.in_(body.receipt_ids))
    else:
        query = query.filter(Receipt.exported == False,  # noqa: E712
                             Receipt.status == "verified")
    receipts = query.order_by(Receipt.receipt_date)\
        .limit(settings.ONEC_BATCH_SIZE * 10).all()
    if not receipts:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Нет чеков для экспорта (проверенные чеки отсутствуют)")

    mapping = db.query(MappingSetting).filter(MappingSetting.is_active == True).all()  # noqa: E712

    if body.format == "xml":
        content = exporter.build_enterprise_data_xml(receipts, mapping, body.target_object)
        media, ext = "application/xml", "xml"
    else:
        content = exporter.build_enterprise_data_json(receipts, mapping, body.target_object)
        media, ext = "application/json", "json"

    now = dt.datetime.utcnow()
    for r in receipts:
        r.exported = True
        r.exported_at = now
    db.commit()
    log_action(user, "receipts_exported", details={
        "count": len(receipts), "format": body.format, "target": body.target_object})

    filename = f"ymaster-check-export-{now.strftime('%Y%m%d-%H%M')}.{ext}"
    return Response(content=content, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/export-csv", summary="Экспорт CSV для бухгалтерии (бухгалтер+)")
def export_csv(body: VerifyRequest | None = None,
               company_id: str | None = Query(None, max_length=36,
                                              description="v1.13.0: все чеки компании (админ)"),
               user: User = Depends(require_accountant),
               db: Session = Depends(get_db)):
    query = scope_receipts(db.query(Receipt), user, company_id)  # v1.11.0/1.13.0
    if company_id and user.role != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Выгрузка по компании доступна администратору")
    if body and body.receipt_ids:
        query = query.filter(Receipt.id.in_(body.receipt_ids))
    receipts = query.order_by(Receipt.receipt_date.desc()).limit(10000).all()

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(["Дата чека", "Сумма, ₽", "ФН", "ФД", "ФП", "Признак",
                     "Статус ФНС", "Сотрудник", "Сканеровал", "Магазин", "ИНН",
                     "Статья расходов", "Комментарий", "Уведомление", "QR",
                     "Кто добавил", "Компания"])            # v1.12.3
    cname = _company_names(db, (r.company_id for r in receipts))
    op_names = {1: "Приход", 2: "Возврат"}
    fns_names = {"valid": "Действителен", "invalid": "Недействителен",
                 "not_found": "Не найден", "unknown": "Не проверен"}
    for r in receipts:
        writer.writerow([
            r.receipt_date.strftime("%d.%m.%Y %H:%M"),
            f"{r.total_sum:.2f}".replace(".", ","),
            r.fn, r.fd, r.fp,
            op_names.get(r.operation, ""),
            fns_names.get(r.fns_status, r.fns_status),
            r.assignee or "",
            r.user.username if r.user else "",
            r.merchant_name or "",
            r.merchant_inn or "",
            r.category or "",
            r.comment or "",
            "Уведомляет" if r.notified else "",
            r.qr_data,
            (r.user.full_name or r.user.username) if r.user else "",   # v1.12.3
            cname.get(r.company_id, ""),
        ])
    log_action(user, "receipts_exported_csv", details={"count": len(receipts)})
    now = dt.datetime.utcnow()
    content = "\ufeff" + buf.getvalue()  # BOM для Excel
    filename = f"ymaster-check-{now.strftime('%Y%m%d-%H%M')}.csv"
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
