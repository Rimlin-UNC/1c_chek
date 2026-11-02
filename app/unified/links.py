"""Связи «компания — чек» помимо владельца (ADR-20, #24, фазы 1–2, T-34).

Заявка (claim) — компания пыталась учесть чек, который уже принадлежит
другой: скан QR, фото, PDF, ручной ввод или перенос из Чек-Пула получили
отказ. Сотрудник видит только «Заявка передана администратору.» (Q-22) и
никогда — число организаций. Администратор видит число и список компаний и
решает: передать чек претенденту, отклонить заявку или не учитывать её.

Прежний владелец (former_owner) — компания, у которой чек забрали
перемещением; в число организаций не входит, но виден администратору.

Правила числа (Q-18, Q-19): владелец + претенденты в статусах «ждёт решения»
и «отклонена». «Не учитывать», переданные заявки, прежние владельцы и
удалённые компании не считаются.

Запись попытки не ломает приём чека: всё в точке сохранения, сбой — в журнал
приложения, ответ сотруднику прежний (без слов о заявке). Журнал карточки
(unified_events) попытки не пишет: его видят сотрудники компании-владельца,
и событие о чужой попытке раскрыло бы число организаций. Решения
администратора пишутся событиями claim_*, которые штатным ролям не
показываются (service._viewer_events).

Модуль не импортирует service на уровне модуля: service сам вызывает
отсюда записи при перемещениях, обратные вызовы — через ленивый импорт.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..models import AuditLog, Company, Receipt, User, uid, utcnow
from ..pool.models import PoolReceipt
from ..services.receipt_identity import _insert_for
from .models import (
    LINK_CLAIM,
    LINK_COUNTED,
    LINK_DECLINED,
    LINK_FORMER_OWNER,
    LINK_OPEN,
    LINK_SOURCES,
    LINK_STATUSES,
    LINK_TRANSFERRED,
    LINK_VOID,
    REVIEW_BLOCKING,
    REVIEW_OWNERSHIP,
    UnifiedCompanyLink,
    UnifiedDocument,
    UnifiedOwnership,
)

log = logging.getLogger("ymaster")

CLAIM_NOTE = "Заявка передана администратору."
# Действие администратора → статус заявки.
DECISIONS = {"transfer": LINK_TRANSFERRED, "decline": LINK_DECLINED, "void": LINK_VOID}
# События решений по заявкам: только администратор (иначе штатная роль
# компании-владельца узнала бы о чужой заявке).
ADMIN_ONLY_EVENTS = frozenset(f"claim_{action}" for action in DECISIONS)
# То же в журнале аудита: лента дашборда бухгалтера показывает и записи без
# компании (администратор платформы) — решения по заявкам туда не попадают.
ADMIN_ONLY_AUDIT = frozenset(f"unified_claim_{action}" for action in DECISIONS)
REASON_MIN = 3
REASON_MAX = 500
# Наполнение по журналу аудита: граница «живых» заявок и уже обработанное.
LIVE_FROM_KEY = "unified_claims_live_from_audit_id"
AUDIT_DONE_KEY = "unified_claims_backfill_audit_id"
CHUNK = 200


class ForeignOwnerConflict(HTTPException):
    """409 «чек уже у другой организации» с тройкой для заявки (Q-22).

    Бросается внутри транзакции приёма. Маршрут откатывает её и записывает
    заявку отдельной транзакцией (claim_rejected) — текст отказа прежний.
    """

    def __init__(self, detail: str, fn: str | None, fd: str | None, fp: str | None,
                 company_id: str | None = None) -> None:
        super().__init__(409, detail)
        self.triple = (fn or "", fd or "", fp or "")
        self.company_id = company_id


def with_note(message: str) -> str:
    """Текст отказа сотруднику + «Заявка передана администратору.»"""
    text = (message or "").rstrip()
    if text and not text.endswith((".", "!", "?")):
        text += "."
    return f"{text} {CLAIM_NOTE}".strip()


def _chunks(values: list, size: int = CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _row(document_id: str, company_id: str, kind: str, status: str, source: str, *,
         attempts: int, first_at: datetime, last_at: datetime, user_id: str | None = None,
         last_user_id: str | None = None, decided_by: str | None = None,
         decided_at: datetime | None = None) -> dict:
    """Строка unified_company_links со всеми колонками (многострочная вставка)."""
    return {"id": uid(), "document_id": document_id, "company_id": company_id,
            "kind": kind, "status": status, "source": source, "attempts": attempts,
            "first_at": first_at, "last_at": last_at, "first_user_id": user_id,
            "last_user_id": last_user_id if last_user_id is not None else user_id,
            "decided_by": decided_by, "decided_at": decided_at}


def _attempt_set(excluded, c) -> dict:
    """Повторная попытка: счётчик растёт, последнее время и сотрудник обновляются.

    Прежний владелец снова пытается учесть чек — снова претендент; заявка по
    уже переданному чеку открывается заново (чек у компании снова забрали);
    решения «отклонить» и «не учитывать» остаются в силе. В SET все ссылки
    на колонки — значения строки до обновления (SQLite и PostgreSQL).
    """
    return {
        "attempts": c.attempts + excluded.attempts,
        "first_at": case((excluded.first_at < c.first_at, excluded.first_at), else_=c.first_at),
        "last_at": case((excluded.last_at > c.last_at, excluded.last_at), else_=c.last_at),
        "last_user_id": case((excluded.last_at >= c.last_at, excluded.last_user_id),
                             else_=c.last_user_id),
        "first_user_id": func.coalesce(c.first_user_id, excluded.first_user_id),
        "source": case((c.kind == LINK_FORMER_OWNER, excluded.source), else_=c.source),
        "status": case((c.kind == LINK_FORMER_OWNER, LINK_OPEN),
                       (c.status == LINK_TRANSFERRED, LINK_OPEN), else_=c.status),
        "kind": LINK_CLAIM,
    }


def _former_owner_set(excluded, c) -> dict:
    """Компания потеряла чек перемещением: связь становится «прежний владелец»."""
    return {"kind": LINK_FORMER_OWNER,
            "last_at": case((excluded.last_at > c.last_at, excluded.last_at), else_=c.last_at)}


def _declined_set(excluded, c) -> dict:
    """Проигравшая сторона спора о владельце: заявка отклонена решением.

    Прежний владелец становится претендентом с источником и попыткой новой
    связи (как в _attempt_set); у заявки со сканов источник и счётчик прежние.
    """
    return {"kind": LINK_CLAIM, "status": LINK_DECLINED,
            "source": case((c.kind == LINK_FORMER_OWNER, excluded.source), else_=c.source),
            "attempts": case((c.kind == LINK_FORMER_OWNER, excluded.attempts), else_=c.attempts),
            "decided_by": excluded.decided_by, "decided_at": excluded.decided_at}


def _upsert(db: Session, rows: list[dict], make_set) -> None:
    """INSERT … ON CONFLICT (document_id, company_id) пачками.

    make_set=None — DO NOTHING. В одной команде PostgreSQL не даёт обновить
    строку дважды, поэтому дубли пар схлопываются до вставки.
    """
    unique: dict[tuple[str, str], dict] = {}
    for row in rows:
        unique.setdefault((row["document_id"], row["company_id"]), row)
    if not unique:
        return
    insert = _insert_for(db)
    table = UnifiedCompanyLink.__table__
    target = [table.c.document_id, table.c.company_id]
    for chunk in _chunks(list(unique.values())):
        stmt = insert(table).values(chunk)
        if make_set is None:
            stmt = stmt.on_conflict_do_nothing(index_elements=target)
        else:
            stmt = stmt.on_conflict_do_update(index_elements=target,
                                              set_=make_set(stmt.excluded, table.c))
        db.execute(stmt)


# --------------------------------------------------------------------------
#  Записи из операций единого реестра (перемещение, спор, удаление компании)
# --------------------------------------------------------------------------
def after_move(db: Session, moved: list[tuple[str, str | None]], target_company_id: str,
               actor_id: str | None, when: datetime) -> None:
    """Перемещение: прежний владелец — связь former_owner, заявка получателя
    исполнена. Две команды на пачку — число операций не растёт с числом чеков."""
    rows = [_row(doc_id, old, LINK_FORMER_OWNER, LINK_OPEN, "move", attempts=0,
                 first_at=when, last_at=when)
            for doc_id, old in moved if old and old != target_company_id]
    _upsert(db, rows, _former_owner_set)
    satisfy_claims(db, [doc_id for doc_id, _old in moved], target_company_id, actor_id, when)


def satisfy_claims(db: Session, document_ids: list[str], company_id: str | None,
                   actor_id: str | None, when: datetime) -> int:
    """Чек стал принадлежать компании — её заявки на него исполнены."""
    if not company_id or not document_ids:
        return 0
    done = 0
    for chunk in _chunks(list(dict.fromkeys(document_ids)), 500):
        done += (db.query(UnifiedCompanyLink)
                 .filter(UnifiedCompanyLink.company_id == company_id,
                         UnifiedCompanyLink.kind == LINK_CLAIM,
                         UnifiedCompanyLink.status != LINK_TRANSFERRED,
                         UnifiedCompanyLink.document_id.in_(chunk))
                 .update({UnifiedCompanyLink.status: LINK_TRANSFERRED,
                          UnifiedCompanyLink.decided_by: actor_id,
                          UnifiedCompanyLink.decided_at: when},
                         synchronize_session=False))
    return done


def after_resolve(db: Session, document_id: str, losers: set[str], winner: str | None,
                  actor_id: str | None, when: datetime) -> None:
    """Решение спора о владельце: у проигравших — заявка «отклонена»."""
    rows = [_row(document_id, company, LINK_CLAIM, LINK_DECLINED, "archive_twin", attempts=1,
                 first_at=when, last_at=when, decided_by=actor_id, decided_at=when)
            for company in sorted(losers) if company and company != winner]
    _upsert(db, rows, _declined_set)
    satisfy_claims(db, [document_id], winner, actor_id, when)


def close_company(db: Session, company_id: str, when: datetime) -> int:
    """Компания удаляется: её заявки больше не учитываются."""
    return (db.query(UnifiedCompanyLink)
            .filter(UnifiedCompanyLink.company_id == company_id,
                    UnifiedCompanyLink.status.in_(LINK_COUNTED))
            .update({UnifiedCompanyLink.status: LINK_VOID, UnifiedCompanyLink.decided_at: when},
                    synchronize_session=False))


# --------------------------------------------------------------------------
#  Попытка учесть чужой чек (приём и перенос из пула)
# --------------------------------------------------------------------------
def _document_for_attempt(db: Session, fn: str, fd: str, fp: str, key: str):
    """Единая запись чека; чек архива регистрируется мягко (от имени системы:
    в журнале владельца не должен появиться сотрудник другой организации)."""
    from . import service

    doc = db.query(UnifiedDocument).filter(UnifiedDocument.identity_key == key).first()
    if doc is not None:
        return doc
    receipt = db.query(Receipt).filter_by(fn=fn, fd=fd, fp=fp).first()
    if receipt is not None:
        return service.register_source(db, service.REGISTRY, receipt, None,
                                       strict=True, event_type="sync")
    pool = db.query(PoolReceipt).filter_by(fn=fn, fd=fd, fp=fp).first()
    if pool is not None and pool.assigned_company_id:
        return service.register_source(db, service.POOL, pool, None,
                                       strict=True, event_type="sync")
    return None


def record_attempt(db: Session, fn: str, fd: str, fp: str, *, company_id: str | None,
                   user: User | None, source: str) -> bool:
    """Записать заявку компании на чек другой организации. Commit — за вызывающим.

    True — заявка записана (новая или повторная попытка). False — заявки нет:
    компания не указана, чек её собственный, неполная тройка или сбой (он
    откатывает только свою точку сохранения и уходит в журнал приложения).
    """
    from . import service

    if not company_id or source not in LINK_SOURCES:
        return False
    key = service.identity_key(fn, fd, fp)
    if key is None:
        return False
    user_id = user.id if user is not None else None
    try:
        with db.begin_nested():
            doc = _document_for_attempt(db, fn, fd, fp, key)
            if doc is None or doc.deleted_at is not None:
                return False
            owner = doc.ownership.company_id if doc.ownership else None
            if owner == company_id or db.get(Company, company_id) is None:
                return False
            now = utcnow()
            _upsert(db, [_row(doc.id, company_id, LINK_CLAIM, LINK_OPEN, source, attempts=1,
                              first_at=now, last_at=now, user_id=user_id)], _attempt_set)
        return True
    except HTTPException as exc:
        log.warning("Единый реестр: заявка компании %s не записана — %s", company_id, exc.detail)
        return False
    except Exception:  # noqa: BLE001 — заявка необязательна, ответ сотруднику важнее
        log.error("Единый реестр: заявка компании %s не записана — сбой", company_id, exc_info=True)
        return False


def note_attempt(db: Session, fn: str, fd: str, fp: str, *, company_id: str | None,
                 user: User | None, source: str) -> bool:
    """record_attempt и commit. Сбой фиксации — False, ответ сотруднику прежний."""
    recorded = record_attempt(db, fn, fd, fp, company_id=company_id, user=user, source=source)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — заявка необязательна
        db.rollback()
        log.error("Единый реестр: заявка компании %s не сохранена", company_id, exc_info=True)
        return False
    return recorded


def claim_rejected(db: Session, exc: ForeignOwnerConflict, *, company_id: str | None,
                   user: User | None, source: str) -> HTTPException:
    """Отказ «чек у другой организации» после отката приёма: заявка + 409.

    Вызывающий уже откатил транзакцию приёма. Заявка не записалась — тот же
    отказ без слов о заявке.
    """
    fn, fd, fp = exc.triple
    company_id = company_id or exc.company_id
    if note_attempt(db, fn, fd, fp, company_id=company_id, user=user, source=source):
        return HTTPException(409, with_note(str(exc.detail)))
    return exc


# --------------------------------------------------------------------------
#  Администратор: число организаций, список, входящие заявки, решения
# --------------------------------------------------------------------------
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _company_names(db: Session, ids) -> dict[str, str]:
    wanted = sorted({i for i in ids if i})
    names: dict[str, str] = {}
    for chunk in _chunks(wanted, 500):
        names.update((c.id, c.name) for c in db.query(Company).filter(Company.id.in_(chunk)))
    return names


def _user_names(db: Session, ids) -> dict[str, str]:
    wanted = sorted({i for i in ids if i})
    names: dict[str, str] = {}
    for chunk in _chunks(wanted, 500):
        names.update((u.id, u.full_name or u.username)
                     for u in db.query(User).filter(User.id.in_(chunk)))
    return names


def _link_payload(link: UnifiedCompanyLink, companies: dict, users: dict) -> dict:
    return {
        "link_id": link.id, "company_id": link.company_id,
        "company_name": companies.get(link.company_id),
        "company_deleted": link.company_id not in companies,
        "role": "claimant" if link.kind == LINK_CLAIM else "former_owner",
        "status": link.status, "source": link.source, "attempts": link.attempts,
        "first_at": _iso(link.first_at), "last_at": _iso(link.last_at),
        "last_user_name": users.get(link.last_user_id) if link.last_user_id else None,
        "decided_by_name": users.get(link.decided_by) if link.decided_by else None,
        "decided_at": _iso(link.decided_at),
        "counted": (link.kind == LINK_CLAIM and link.status in LINK_COUNTED
                    and link.company_id in companies),
    }


def companies_payload(db: Session, doc: UnifiedDocument) -> dict:
    """Сколько организаций учитывают чек и какие (только администратор)."""
    own = doc.ownership
    owner_id = own.company_id if own else None
    links = (db.query(UnifiedCompanyLink).filter(UnifiedCompanyLink.document_id == doc.id)
             .order_by(UnifiedCompanyLink.first_at, UnifiedCompanyLink.id).all())
    companies = _company_names(db, [owner_id, *(link.company_id for link in links)])
    users = _user_names(db, [*(link.last_user_id for link in links),
                             *(link.decided_by for link in links)])
    items = []
    if owner_id:
        items.append({"link_id": None, "company_id": owner_id,
                      "company_name": companies.get(owner_id),
                      "company_deleted": owner_id not in companies, "role": "owner",
                      "status": None, "source": None, "attempts": None,
                      "first_at": _iso(own.assigned_at), "last_at": None,
                      "last_user_name": None, "decided_by_name": None, "decided_at": None,
                      "counted": owner_id in companies})
    # Своя связь владельца (прежний владелец, которому чек вернули) строку не дублирует.
    items += [_link_payload(link, companies, users) for link in links if link.company_id != owner_id]
    return {"document_id": doc.id, "version": doc.version,
            "count": sum(1 for item in items if item["counted"]), "companies": items}


def _claims_query(db: Session):
    """Заявки по живым чекам, кроме заявок самого владельца (их исполнило
    перемещение или решение спора старой версией — после сверки)."""
    return (db.query(UnifiedCompanyLink, UnifiedDocument)
            .join(UnifiedDocument, UnifiedDocument.id == UnifiedCompanyLink.document_id)
            .outerjoin(UnifiedOwnership, UnifiedOwnership.document_id == UnifiedDocument.id)
            .filter(UnifiedCompanyLink.kind == LINK_CLAIM,
                    UnifiedDocument.deleted_at.is_(None),
                    or_(UnifiedOwnership.company_id.is_(None),
                        UnifiedOwnership.company_id != UnifiedCompanyLink.company_id)))


def open_claims_count(db: Session) -> int:
    """Счётчик для меню администратора: заявки, ждущие решения."""
    return _claims_query(db).filter(UnifiedCompanyLink.status == LINK_OPEN).count()


def claims_page(db: Session, *, status: str = LINK_OPEN, page: int = 1, per_page: int = 20) -> dict:
    """Входящие заявки для вкладки «Заявки» (только администратор)."""
    if status != "all" and status not in LINK_STATUSES:
        raise HTTPException(422, "Неизвестный статус заявки")
    query = _claims_query(db)
    if status != "all":
        query = query.filter(UnifiedCompanyLink.status == status)
    total = query.count()
    rows = (query.order_by(UnifiedCompanyLink.last_at.desc(), UnifiedCompanyLink.id)
            .offset((page - 1) * per_page).limit(per_page).all())
    owners = {doc.id: (doc.ownership.company_id if doc.ownership else None) for _link, doc in rows}
    companies = _company_names(db, [*owners.values(), *(link.company_id for link, _doc in rows)])
    users = _user_names(db, [*(link.last_user_id for link, _doc in rows),
                             *(link.decided_by for link, _doc in rows)])
    items = []
    for link, doc in rows:
        owner_id = owners[doc.id]
        item = _link_payload(link, companies, users)
        item.update({
            "document_id": doc.id, "version": doc.version,
            "receipt_date": doc.receipt_date.isoformat() if doc.receipt_date else None,
            "total_sum": doc.total_sum,
            "merchant_name": doc.merchant_name, "review_flags": doc.review_flags,
            "blocking": bool(doc.review_flags & REVIEW_BLOCKING),
            "owner_company_id": owner_id,
            "owner_company_name": companies.get(owner_id) if owner_id else None,
        })
        items.append(item)
    return {"total": total, "page": page, "per_page": per_page, "status": status, "items": items}


def decide(db: Session, link_id: str, action: str, actor: User, *,
           expected_version: int, reason: str) -> tuple[UnifiedCompanyLink, UnifiedDocument, list]:
    """Решение по заявке: передать чек, отклонить, не учитывать. Commit — за вызывающим.

    Проверка версии чека — как на экране разбора: два администратора не
    перепишут решение друг друга. Передача — обычное перемещение (журнал
    «Перенос между компаниями», флаг выгрузки сброшен). Возвращает связь,
    документ и перенесённые чеки реестра (для пересчёта политики).
    """
    from . import service

    if action not in DECISIONS:
        raise HTTPException(404, "Неизвестное действие")
    text = (reason or "").strip()
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise HTTPException(422, f"Укажите причину решения — от {REASON_MIN} до {REASON_MAX} символов")
    link = db.get(UnifiedCompanyLink, link_id)
    if link is None or link.kind != LINK_CLAIM:
        raise HTTPException(404, "Заявка не найдена")
    doc = db.get(UnifiedDocument, link.document_id)
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(404, "Чек не найден")
    service._lock_values(db, doc.fn, doc.fd, doc.fp)
    doc = (db.query(UnifiedDocument).populate_existing()
           .filter(UnifiedDocument.id == doc.id).with_for_update(of=UnifiedDocument).first())
    actor = service._recheck_actor(db, actor, ("admin",))
    link = (db.query(UnifiedCompanyLink).populate_existing()
            .filter(UnifiedCompanyLink.id == link_id).with_for_update().first())
    if link is None or link.kind != LINK_CLAIM:
        raise HTTPException(404, "Заявка не найдена")
    if doc.version != expected_version:
        raise HTTPException(409, "Версия чека изменилась — обновите страницу")
    status = DECISIONS[action]
    if link.status == LINK_TRANSFERRED:
        raise HTTPException(409, "Чек уже передан по этой заявке")
    if link.status == status:
        raise HTTPException(409, "Заявка уже в этом статусе — обновите страницу")
    old_owner = doc.ownership.company_id if doc.ownership else None
    moved: list = []
    if action == "transfer":
        if old_owner == link.company_id:
            raise HTTPException(409, "Чек уже у этой компании")
        service.move_document(db, doc.id, link.company_id, actor)
        registries, _pools = service._legacy_projections(db, doc)
        moved = registries
    now = utcnow()
    link.status = status
    link.decided_by = actor.id
    link.decided_at = now
    if action != "transfer":
        doc.version += 1
    service._add_event(db, doc, f"claim_{action}", actor, from_company=old_owner,
                       to_company=link.company_id,
                       details={"reason": text[:REASON_MAX], "link_id": link.id})
    db.flush()
    return link, doc, moved


# --------------------------------------------------------------------------
#  Сверка при старте и наполнение задним числом
# --------------------------------------------------------------------------
def reconcile_links(db: Session) -> dict:
    """Сверка связей после отката и удалений старой версией. Commit — за вызывающим.

    Заявки удалённых компаний — «не учитывать»; связи без документа удаляются;
    заявка компании, ставшей владельцем (например, старая версия перенесла
    чек), — исполнена.
    """
    now = utcnow()
    closed = (db.query(UnifiedCompanyLink)
              .filter(UnifiedCompanyLink.status.in_(LINK_COUNTED),
                      ~db.query(Company.id).filter(Company.id == UnifiedCompanyLink.company_id)
                      .exists())
              .update({UnifiedCompanyLink.status: LINK_VOID, UnifiedCompanyLink.decided_at: now},
                      synchronize_session=False))
    orphaned = (db.query(UnifiedCompanyLink)
                .filter(~db.query(UnifiedDocument.id)
                        .filter(UnifiedDocument.id == UnifiedCompanyLink.document_id).exists())
                .delete(synchronize_session=False))
    owned = (db.query(UnifiedOwnership.document_id, UnifiedOwnership.company_id)
             .join(UnifiedCompanyLink, (UnifiedCompanyLink.document_id == UnifiedOwnership.document_id)
                   & (UnifiedCompanyLink.company_id == UnifiedOwnership.company_id))
             .filter(UnifiedCompanyLink.kind == LINK_CLAIM,
                     UnifiedCompanyLink.status != LINK_TRANSFERRED).all())
    satisfied = 0
    by_company: dict[str, list[str]] = defaultdict(list)
    for document_id, company_id in owned:
        by_company[company_id].append(document_id)
    for company_id, document_ids in sorted(by_company.items()):
        satisfied += satisfy_claims(db, document_ids, company_id, None, now)
    db.flush()
    return {"links_closed": closed, "links_orphaned": orphaned, "claims_satisfied": satisfied}


def mark_live_start(db: Session) -> None:
    """Один раз: запомнить последнюю запись аудита до живых заявок.

    Отказы /scan до этой границы — история (их забирает backfill_claims),
    после неё заявки пишет сам приём. Граница не сдвигается при откатах и
    повторных обновлениях. Commit — за вызывающим.
    """
    from ..services import appsettings

    if appsettings.get_setting(db, LIVE_FROM_KEY, ""):
        return
    last = db.query(func.coalesce(func.max(AuditLog.id), 0)).scalar() or 0
    appsettings.set_setting(db, LIVE_FROM_KEY, str(int(last)))


def _audit_groups(db: Session, done: int, boundary: int) -> dict[tuple[str, str], dict]:
    """Отказы /scan из журнала аудита по парам (чек реестра, компания)."""
    groups: dict[tuple[str, str], dict] = {}
    rows = (db.query(AuditLog.id, AuditLog.entity_id, AuditLog.company_id, AuditLog.user_id,
                     AuditLog.created_at)
            .filter(AuditLog.action == "receipt_duplicate", AuditLog.entity_type == "receipt",
                    AuditLog.id > done, AuditLog.id <= boundary,
                    AuditLog.company_id.isnot(None), AuditLog.entity_id.isnot(None))
            .order_by(AuditLog.id))
    for _id, receipt_id, company_id, user_id, created in rows:
        group = groups.setdefault((receipt_id, company_id), {
            "attempts": 0, "first_at": created, "last_at": created,
            "first_user": user_id, "last_user": user_id})
        group["attempts"] += 1
        group["last_at"], group["last_user"] = created, user_id
    return groups


def backfill_claims(db: Session, *, dry_run: bool = False) -> dict:
    """Наполнить заявки задним числом. Идемпотентно; commit внутри.

    1. Журнал аудита: отказы /scan (receipt_duplicate) от компании, которая
       не владеет чеком реестра. До 1.70.85 в журнал писал только скан QR,
       поэтому история фото, PDF и ручного ввода не восстанавливается.
       Компания попытки сравнивается с текущим владельцем — приближение:
       чек могли переместить позже. Каждая запись аудита учитывается один
       раз (граница LIVE_FROM_KEY и отметка AUDIT_DONE_KEY).
    2. Исторические пары «реестр одной компании ↔ пул, закреплённый за
       другой»: если у документа есть владелец, вторая компания получает
       заявку (archive_twin). Документ на споре о владельце пропускается —
       проигравшую сторону отметит решение спора.
    """
    from ..services import appsettings
    from . import service

    boundary = int(appsettings.get_setting(db, LIVE_FROM_KEY, "0") or 0)
    done = int(appsettings.get_setting(db, AUDIT_DONE_KEY, "0") or 0)
    groups = _audit_groups(db, done, boundary)
    companies = set(_company_names(db, [company for _rid, company in groups]))
    out = {"dry_run": dry_run, "audit_rows_pending": sum(g["attempts"] for g in groups.values()),
           "audit_claims": 0, "audit_skipped": 0, "twin_claims": 0, "twin_skipped": 0}
    rows: list[dict] = []
    receipts = {}
    for chunk in _chunks(sorted({rid for rid, _company in groups}), 500):
        receipts.update((r.id, r) for r in db.query(Receipt).filter(Receipt.id.in_(chunk)))
    for (receipt_id, company_id), group in sorted(groups.items(), key=lambda kv: kv[1]["first_at"]):
        receipt = receipts.get(receipt_id)
        if receipt is None or company_id not in companies or receipt.company_id == company_id:
            out["audit_skipped"] += 1
            continue
        doc = service.document_for_source(db, service.REGISTRY, receipt.id)
        if doc is None and not dry_run:
            doc = service._register_lazily(db, receipt, None)
        if dry_run and doc is None:
            out["audit_claims"] += 1     # чек архива зарегистрируется при запуске
            continue
        owner = doc.ownership.company_id if doc is not None and doc.ownership else None
        if doc is None or doc.deleted_at is not None or owner == company_id:
            out["audit_skipped"] += 1
            continue
        out["audit_claims"] += 1
        rows.append(_row(doc.id, company_id, LINK_CLAIM, LINK_OPEN, "scan_qr",
                         attempts=group["attempts"], first_at=group["first_at"],
                         last_at=group["last_at"], user_id=group["first_user"],
                         last_user_id=group["last_user"]))
    twins: list[dict] = []
    pairs = (db.query(PoolReceipt, Receipt)
             .join(Receipt, (Receipt.fn == PoolReceipt.fn) & (Receipt.fd == PoolReceipt.fd)
                   & (Receipt.fp == PoolReceipt.fp))
             .filter(PoolReceipt.assigned_company_id.isnot(None), Receipt.company_id.isnot(None),
                     Receipt.company_id != PoolReceipt.assigned_company_id)
             .order_by(PoolReceipt.id).all())
    existing_companies = set(_company_names(db, [c for pool, receipt in pairs for c in
                                                 (pool.assigned_company_id, receipt.company_id)]))
    for pool, receipt in pairs:
        key = service.identity_key(pool.fn, pool.fd, pool.fp)
        doc = (db.query(UnifiedDocument).filter(UnifiedDocument.identity_key == key).first()
               if key else None)
        owner = doc.ownership.company_id if doc is not None and doc.ownership else None
        if (doc is None or doc.deleted_at is not None or doc.review_flags & REVIEW_OWNERSHIP
                or owner is None):
            out["twin_skipped"] += 1
            continue
        when = pool.assigned_at or receipt.created_at or utcnow()
        for company_id in sorted({pool.assigned_company_id, receipt.company_id} - {owner}):
            if company_id in existing_companies:
                out["twin_claims"] += 1
                twins.append(_row(doc.id, company_id, LINK_CLAIM, LINK_OPEN, "archive_twin",
                                  attempts=1, first_at=when, last_at=when))
    if dry_run:
        db.rollback()
        return out
    _upsert(db, rows, _attempt_set)
    _upsert(db, twins, None)
    if boundary > done:
        appsettings.set_setting(db, AUDIT_DONE_KEY, str(boundary))
    db.commit()
    return out
