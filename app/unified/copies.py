"""Сверка фискальных данных копий с единой записью (фаза 4а, T-37).

Зачем. Реестр и Чек-Пул хранят копии фискальных данных единой записи
(docs/single_base_design.md, раздел 12). Версия без единого слоя (1.70.81 и
раньше) после отката меняет поля и позиции реестра напрямую. Фоновая
синхронизация после проверки ФНС при сбое намеренно пропускается
(``sync_registry_safely``). В обоих случаях единая запись и копия Чек-Пула
отстают. При старте новая версия доводит копии до состояния, как если бы
синхронизация прошла.

Правила — те же, что в 1.70.82–1.70.87: реестр главный, пул — копия единой
записи.

* Копия совпадает с единой записью — ничего не делаем. Копия без позиций
  совпадает, если совпадают поля: пустой список не стирает позиции единой
  записи (так же поступает ``sync_registry``).
* Отличается реестр, а пул совпадает или его нет — изменение реестра
  принимается в единую запись и переносится в пул.
* Отличается только пул. Если у документа есть копия в реестре, пул
  перезаписывается из единой записи. Если нет, изменение пула принимается в
  единую запись.
* Реестр и пул отличаются, причём по-разному, — пометки разбора по
  расходящимся полям, ничего не перезаписывается. Если расходится только поле
  без пометки (кассир), главный — реестр.
* Признак проверки ФНС из копии сливается в единую запись (``_merge_fns``).
* Удалённые документы и документы на разборе не трогаем: их решает
  администратор.

Сравниваются фискальные факты: ФН, ФД, ФП, дата, сумма, операция, продавец,
ИНН, адрес, кассир, позиции и признак ФНС. Метки классификации — регион, город,
отрасль — не сравниваются. Их ставят классификатор при приёме и переносе и
разметка администратора Чек-Пула, а единая запись получает их при штатной
синхронизации. Если бы сверка их возвращала, ручная разметка пропадала бы после
каждого перезапуска.

Кандидатов выбирает один запрос на вид копии. В нём сравниваются фискальные
факты, признак ФНС, а у копии с позициями — число, суммы и длины названий позиций.
Когда расхождений нет, сверка стоит два запроса. Каждое исправление пишется
событием ``reconcile`` в журнал документа. Сверка идемпотентна.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import Receipt, ReceiptItem
from ..pool.models import PoolItem, PoolReceipt
from . import service
from .models import (
    REVIEW_BLOCKING,
    REVIEW_ITEMS,
    REVIEW_VERIFICATION,
    UnifiedDocument,
    UnifiedItem,
    UnifiedOrigin,
)

log = logging.getLogger("ymaster")

# Документы с такими пометками сверка не трогает: расхождение уже ждёт разбора.
# Неполная тройка — не спор (у такого документа одна копия), её сверяем.
SKIP_FLAGS = REVIEW_BLOCKING | REVIEW_VERIFICATION
_TEXT = ("fn", "fd", "fp", "merchant_name", "merchant_inn", "merchant_address", "cashier")
# Метки классификации: в сравнении копий не участвуют (см. описание модуля).
_LABELS = ("region_code", "region_city", "industry")
_FNS_FINAL = ("valid", "invalid", "not_found")
_MONEY_EPS = 0.005
_QTY_EPS = 0.000001


def _txt(col):
    return func.coalesce(col, "")


def _sources(kind: str):
    if kind == service.REGISTRY:
        return Receipt, ReceiptItem
    return PoolReceipt, PoolItem


def _item_totals(model, owner_col):
    """Число, суммы и длины названий позиций по владельцу — признак расхождения."""
    return (select(owner_col.label("oid"),
                   func.count(model.id).label("n"),
                   func.coalesce(func.sum(model.total), 0.0).label("t"),
                   func.coalesce(func.sum(model.quantity), 0.0).label("q"),
                   func.coalesce(func.sum(model.vat_sum), 0.0).label("v"),
                   func.coalesce(func.sum(func.length(model.name)), 0).label("l"))
            .group_by(owner_col).subquery())


def _differs(a, b, eps: float):
    return func.abs(func.coalesce(a, 0.0) - func.coalesce(b, 0.0)) > eps


def candidate_query(kind: str):
    """Запрос id документов, у которых копия вида ``kind`` расходится с единой записью."""
    doc = UnifiedDocument
    model, item_model = _sources(kind)
    copy_items = _item_totals(item_model, item_model.receipt_id)
    doc_items = _item_totals(UnifiedItem, UnifiedItem.document_id)
    core = or_(
        *[_txt(getattr(model, f)) != _txt(getattr(doc, f)) for f in _TEXT],
        model.receipt_date.is_distinct_from(doc.receipt_date),
        _differs(model.total_sum, doc.total_sum, _MONEY_EPS),
        func.coalesce(model.operation, 1) != func.coalesce(doc.operation, 1),
    )
    copy_n = func.coalesce(copy_items.c.n, 0)
    items = and_(copy_n > 0, or_(
        copy_n != func.coalesce(doc_items.c.n, 0),
        _differs(copy_items.c.t, doc_items.c.t, _MONEY_EPS),
        _differs(copy_items.c.q, doc_items.c.q, _QTY_EPS),
        _differs(copy_items.c.v, doc_items.c.v, _MONEY_EPS),
        func.coalesce(copy_items.c.l, 0) != func.coalesce(doc_items.c.l, 0),
    ))
    if kind == service.REGISTRY:
        fns = and_(model.fns_status.in_(_FNS_FINAL), model.fns_checked_at.isnot(None),
                   or_(doc.fns_status != model.fns_status, doc.fns_checked_at.is_(None),
                       model.fns_checked_at > doc.fns_checked_at))
    else:
        fns = and_(model.fns_confirmed.is_(True), model.fns_checked_at.isnot(None),
                   or_(doc.fns_status != "valid", doc.fns_checked_at.is_(None),
                       model.fns_checked_at > doc.fns_checked_at))
    return (select(doc.id).select_from(UnifiedOrigin)
            .join(doc, doc.id == UnifiedOrigin.document_id)
            .join(model, model.id == UnifiedOrigin.source_id)
            .outerjoin(copy_items, copy_items.c.oid == model.id)
            .outerjoin(doc_items, doc_items.c.oid == doc.id)
            .where(UnifiedOrigin.source_kind == kind,
                   doc.deleted_at.is_(None),
                   doc.review_flags.op("&")(SKIP_FLAGS) == 0,
                   or_(core, items, fns))
            .distinct())


def candidates(db: Session) -> dict[str, list[str]]:
    """Документы с расходящимися копиями по видам — для сверки и отчёта готовности."""
    return {kind: sorted(r[0] for r in db.execute(candidate_query(kind)))
            for kind in (service.REGISTRY, service.POOL)}


# --------------------------------------------------------------------------
#  Сравнение одной копии с единой записью
# --------------------------------------------------------------------------
def _doc_core(doc: UnifiedDocument) -> dict:
    return {
        **{f: getattr(doc, f) or "" for f in _TEXT},
        "receipt_date": doc.receipt_date,
        "total_sum": float(doc.total_sum or 0.0),
        "operation": int(doc.operation or 1),
    }


def _norm(core: dict) -> tuple:
    return tuple(sorted((k, service._money(v) if k == "total_sum" else v)
                        for k, v in core.items() if k not in _LABELS))


def _items_key(items) -> tuple:
    return tuple(service._item_core(i) for i in items)


class _Copy:
    """Снимок копии: поля ядра, позиции и вывод «совпадает ли с единой записью»."""

    def __init__(self, db: Session, kind: str, row: Any, doc_core: tuple, doc_items: tuple):
        self.kind, self.row = kind, row
        self.core = service._core(row, kind)
        self.items = _items_key(service._items(db, row, kind))
        self.same = _norm(self.core) == doc_core and (not self.items or self.items == doc_items)

    def variant(self) -> tuple:
        return _norm(self.core), self.items


def _divergence_flags(a: _Copy, b: _Copy) -> int:
    flags = service._compare_core(SimpleNamespace(**a.core), b.core)
    if a.items and b.items and a.items != b.items:
        flags |= REVIEW_ITEMS
    return flags


def _merge_evidence(db: Session, doc: UnifiedDocument, copies: list[_Copy]) -> bool:
    before = (doc.fns_status, doc.fns_checked_at, doc.review_flags)
    for copy in copies:
        service._merge_fns(doc, *service._fns_evidence(copy.row, copy.kind))
    if (doc.fns_status, doc.fns_checked_at, doc.review_flags) == before:
        return False
    doc.version += 1
    service._add_event(db, doc, "reconcile", None, details={"reason": "fns_evidence"})
    return True


def _mark_review(db: Session, doc: UnifiedDocument, flags: int, out: dict) -> None:
    doc.review_flags |= flags
    doc.version += 1
    service._add_event(db, doc, "reconcile", None,
                       details={"reason": "copies_diverged", "flags": flags})
    out["review"] += 1
    log.warning("Единый реестр: копии документа %s изменены по-разному — оставлено до разбора "
                "(пометки %s)", doc.id, flags)


def _reconcile_document(db: Session, doc_id: str, out: dict) -> None:
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == doc_id).with_for_update(of=UnifiedDocument).first())
    if doc is None or doc.deleted_at is not None or doc.review_flags & SKIP_FLAGS:
        return
    origins = db.query(UnifiedOrigin).filter(UnifiedOrigin.document_id == doc.id).all()
    doc_core = _norm(_doc_core(doc))
    doc_items = _items_key(sorted(doc.items, key=lambda i: (i.position or 0, i.id)))
    copies: list[_Copy] = []
    for origin in sorted(origins, key=lambda o: (o.source_kind != service.REGISTRY, o.source_id)):
        model = Receipt if origin.source_kind == service.REGISTRY else PoolReceipt
        row = db.query(model).populate_existing().filter(model.id == origin.source_id).first()
        if row is not None:
            copies.append(_Copy(db, origin.source_kind, row, doc_core, doc_items))
    regs = [c for c in copies if c.kind == service.REGISTRY]
    pools = [c for c in copies if c.kind == service.POOL]
    reg_diff = [c for c in regs if not c.same]
    pool_diff = [c for c in pools if not c.same]
    if reg_diff:
        source = reg_diff[0]
        others = [c for c in reg_diff[1:] + pool_diff if c.variant() != source.variant()]
        flags = 0
        for other in others:
            flags |= _divergence_flags(source, other)
        if flags:
            _mark_review(db, doc, flags, out)
            return
        service.sync_registry(db, source.row, None, event_type="reconcile",
                              details={"reason": "legacy_edit"})
        out["accepted"] += 1
        _merge_evidence(db, doc, pools)
        return
    if pool_diff and regs:
        service._project_pool(db, doc)
        service._add_event(db, doc, "reconcile", None,
                           details={"reason": "copy_restored", "source_kind": service.POOL,
                                    "copies": len(pool_diff)})
        out["restored"] += 1
        _merge_evidence(db, doc, copies)
        return
    if pool_diff:
        source = pool_diff[0]
        flags = 0
        for other in pool_diff[1:]:
            if other.variant() != source.variant():
                flags |= _divergence_flags(source, other)
        if flags:
            _mark_review(db, doc, flags, out)
            return
        service.sync_pool(db, source.row, None, event_type="reconcile",
                          details={"reason": "legacy_edit"})
        out["accepted"] += 1
        return
    if _merge_evidence(db, doc, copies):
        out["fns_merged"] += 1


def reconcile_copies(db: Session) -> dict:
    """Свести копии реестра и пула с единой записью. Не делает commit — решает вызывающий."""
    out = {"candidates": 0, "accepted": 0, "restored": 0, "fns_merged": 0,
           "review": 0, "conflicts": 0, "errors": 0}
    found = candidates(db)
    doc_ids = sorted(set(found[service.REGISTRY]) | set(found[service.POOL]))
    out["candidates"] = len(doc_ids)
    for doc_id in doc_ids:
        try:
            with db.begin_nested():
                _reconcile_document(db, doc_id, out)
        except HTTPException as exc:
            out["conflicts"] += 1
            log.warning("Единый реестр: копии документа %s не сведены — %s", doc_id, exc.detail)
        except Exception:  # noqa: BLE001 — один документ не должен останавливать сверку
            out["errors"] += 1
            log.error("Единый реестр: сбой сверки копий документа %s", doc_id, exc_info=True)
    db.flush()
    return out


def copies_on_startup() -> dict | None:
    """Сверка копий при старте: отдельная транзакция, сбой не останавливает сервис."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        result = reconcile_copies(db)
        db.commit()
        if any(v for k, v in result.items() if k != "candidates"):
            log.info("Единый реестр: сверка копий с единой записью — %s", result)
        return result
    except Exception:  # noqa: BLE001 — сверка не должна останавливать сервис
        db.rollback()
        log.warning("Единый реестр: сверка копий при старте пропущена", exc_info=True)
        return None
    finally:
        db.close()
