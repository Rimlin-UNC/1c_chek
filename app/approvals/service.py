"""Согласование авансовых отчётов (T-46, v1.70.112, ADR-24, ADR-42): маршрут, подпись, сроки, уведомления.

Маршрут: сотрудник → руководитель → бухгалтер. Сотрудник подписывает отчёт, руководитель и бухгалтер
согласуют или отклоняют. Подпись — простая электронная подпись в программе (63-ФЗ, ст. 9): вход под своей
учётной записью, явное подтверждение действия над конкретным содержимым (SHA-256 снимка) и запись в журнале
с адресом и временем. Этап без ответа за срок компании (по умолчанию 72 часа) считается согласованным —
отдельным событием журнала, и отчёт идёт дальше. Пороги автосогласования: руководитель — по сумме, бухгалтер —
по сумме, если все чеки подтверждены ФНС и без пометок политики расходов. Своё не согласуют: сотрудник не
бывает согласующим собственного отчёта; нет руководителя, кроме самого сотрудника, — этап пропускается
(событие «пропущен»). Уведомления (Telegram, MAX) отправляет минутный тик — запрос пользователя не ждёт сети.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..clients.models import AccountantClient
from ..config import settings as app_settings
from ..models import Company, Receipt, User, iso_z, uid, utcnow
from .models import AdvanceApproval, AdvanceApprovalEvent, ApprovalSettings

log = logging.getLogger("ymaster.approvals")

STAGES = ("employee", "manager", "accountant")
STAGE_TITLES = {"employee": "сотрудник", "manager": "руководитель", "accountant": "бухгалтер"}
STATUSES = ("draft", "pending", "approved", "rejected", "cancelled")
STATUS_TITLES = {"draft": "ждёт подписи сотрудника", "pending": "на согласовании", "approved": "согласован",
                 "rejected": "отклонён", "cancelled": "отменён"}
# действия людей — простая электронная подпись (ПЭП); остальные события записывает программа
PEP_ACTIONS = ("signed", "approved", "rejected", "cancelled")
SYSTEM_ACTIONS = ("created", "auto_approved", "threshold_approved", "skipped", "completed", "notified")
CHANNELS = ("telegram", "max")
DEFAULTS = {"enabled": False, "timeout_hours": 72, "manager_limit": 0.0, "accountant_limit": 0.0,
            "channels": "telegram,max"}
TIMEOUT_RANGE = (24, 168)
LIMIT_MAX = 10_000_000.0
MAX_RECEIPTS = 500
TICK_BATCH = 50


def msk(moment: datetime) -> datetime:
    """Наивное UTC → наивное московское (UTC+3, без перехода на летнее время с 2014 года)."""
    return moment + timedelta(hours=3)


class ApprovalError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ настройки компании
def settings_of(db: Session, company_id: str) -> ApprovalSettings:
    row = db.get(ApprovalSettings, company_id)
    return row if row is not None else ApprovalSettings(company_id=company_id, **DEFAULTS)


def is_enabled(db: Session, company_id: str | None) -> bool:
    if not company_id:
        return False
    row = db.get(ApprovalSettings, company_id)
    return bool(row and row.enabled)


def _channels(s: ApprovalSettings) -> list[str]:
    return [c for c in (s.channels or "").split(",") if c in CHANNELS]


def settings_dict(s: ApprovalSettings) -> dict:
    return {"enabled": bool(s.enabled), "timeout_hours": int(s.timeout_hours or 72),
            "manager_limit": float(s.manager_limit or 0), "accountant_limit": float(s.accountant_limit or 0),
            "channels": _channels(s), "updated_at": iso_z(s.updated_at)}


def save_settings(db: Session, company_id: str, data: dict, user: User) -> ApprovalSettings:
    hours = int(data.get("timeout_hours", 72))
    if not TIMEOUT_RANGE[0] <= hours <= TIMEOUT_RANGE[1]:
        raise ApprovalError(f"Срок ответа — от {TIMEOUT_RANGE[0]} до {TIMEOUT_RANGE[1]} часов", 422)
    limits = {}
    for key in ("manager_limit", "accountant_limit"):
        value = round(float(data.get(key, 0) or 0), 2)
        if not 0 <= value <= LIMIT_MAX:
            raise ApprovalError("Порог автосогласования — от 0 до 10 000 000 ₽", 422)
        limits[key] = value
    channels = [c for c in CHANNELS if c in (data.get("channels") or [])]
    row = db.get(ApprovalSettings, company_id)
    if row is None:
        row = ApprovalSettings(company_id=company_id)
        db.add(row)
    row.enabled = bool(data.get("enabled"))
    row.timeout_hours = hours
    row.manager_limit, row.accountant_limit = limits["manager_limit"], limits["accountant_limit"]
    row.channels = ",".join(channels)
    row.updated_at, row.updated_by = utcnow(), user.id
    db.commit()
    return row


# ------------------------------------------------------------------ состав отчёта
def _day(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApprovalError(f"{field}: укажите дату ГГГГ-ММ-ДД", 422) from None
    if parsed.isoformat() != value:
        raise ApprovalError(f"{field}: укажите дату ГГГГ-ММ-ДД", 422)
    return parsed


def employee_receipts(db: Session, company_id: str, employee: User, date_from: str, date_to: str,
                      receipt_ids: list[str] | None = None) -> list[Receipt]:
    """Чеки сотрудника за период: подотчётное лицо — его ФИО или логин (так их помечает программа при
    добавлении чека сотрудником), либо добавил он сам без подотчётного лица."""
    d_from, d_to = _day(date_from, "Начало периода"), _day(date_to, "Конец периода")
    if d_from > d_to:
        raise ApprovalError("Дата начала позже даты окончания", 422)
    names = {n.strip().casefold()[:200] for n in (employee.full_name or "", employee.username or "") if n.strip()}
    mine = or_(Receipt.assignee_lc.in_(names) if names else False,
               and_(Receipt.created_by == employee.id, or_(Receipt.assignee == "", Receipt.assignee.is_(None))))
    query = db.query(Receipt).filter(Receipt.company_id == company_id, mine,
                                     Receipt.receipt_date >= datetime.combine(d_from, datetime.min.time()),
                                     Receipt.receipt_date < datetime.combine(d_to + timedelta(days=1),
                                                                             datetime.min.time()))
    if receipt_ids:
        query = query.filter(Receipt.id.in_(list(receipt_ids)[:MAX_RECEIPTS]))
    return query.order_by(Receipt.receipt_date, Receipt.id).limit(MAX_RECEIPTS).all()


def _amount(r: Receipt) -> float:
    """К учёту: сумма чека без личных покупок; возврат прихода — минусом."""
    value = float(r.total_sum or 0) - float(r.personal_sum or 0)
    return round(-value if r.operation == 2 else value, 2)


def _item(r: Receipt) -> dict:
    return {"id": r.id, "date": r.receipt_date.strftime("%Y-%m-%dT%H:%M") if r.receipt_date else "",
            "merchant": (r.merchant_name or "")[:200], "inn": r.merchant_inn or "",
            "category": r.category or "", "sum": round(float(r.total_sum or 0), 2),
            "personal": round(float(r.personal_sum or 0), 2), "amount": _amount(r),
            "return": r.operation == 2, "fns": r.fns_status or "unknown", "flags": r.policy_flags or ""}


def build_snapshot(company: Company, employee: User, date_from: str, date_to: str, purpose: str,
                   receipts: list[Receipt]) -> dict:
    items = [_item(r) for r in receipts]
    return {"v": 1,
            "company": {"id": company.id, "name": company.short_name or company.name, "inn": company.inn or ""},
            "employee": {"id": employee.id, "name": employee.full_name or employee.username,
                         "username": employee.username},
            "period": {"from": date_from, "to": date_to}, "purpose": purpose,
            "items": items, "count": len(items), "total": round(sum(i["amount"] for i in items), 2)}


def canonical(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_of(approval: AdvanceApproval) -> dict:
    try:
        return json.loads(approval.snapshot or "{}")
    except ValueError:
        return {}


def changes(db: Session, approval: AdvanceApproval) -> list[dict]:
    """Чеки, изменённые после формирования отчёта: подпись остаётся под снимком, но согласующий видит разницу."""
    items = snapshot_of(approval).get("items") or []
    live = {r.id: r for r in db.query(Receipt).filter(Receipt.id.in_([i["id"] for i in items]))} if items else {}
    out = []
    for item in items:
        r = live.get(item["id"])
        if r is None or r.company_id != approval.company_id:
            out.append({"id": item["id"], "kind": "removed", "merchant": item.get("merchant", "")})
        elif _amount(r) != item["amount"] or (r.fns_status or "unknown") != item["fns"]:
            out.append({"id": r.id, "kind": "changed", "merchant": item.get("merchant", ""),
                        "was": item["amount"], "now": _amount(r), "fns_was": item["fns"],
                        "fns_now": r.fns_status or "unknown"})
    return out


def compliant(db: Session, approval: AdvanceApproval) -> bool:
    """Для порога бухгалтера: все чеки подтверждены ФНС, без пометок политики расходов и без правок."""
    items = snapshot_of(approval).get("items") or []
    return bool(items) and all(i["fns"] == "valid" and not i["flags"] for i in items) \
        and not changes(db, approval)


# ------------------------------------------------------------------ участники и права
def _user_name(user: User | None) -> str:
    return (user.full_name or user.username) if user else ""


def approvers(db: Session, approval: AdvanceApproval, stage: str | None = None) -> list[User]:
    """Кто может решать на этапе (без самого сотрудника). Администратор вправе действовать за бухгалтера,
    но в списке получателей уведомлений он только тогда, когда у компании нет бухгалтера."""
    stage = stage or approval.stage
    base = db.query(User).filter(User.is_active.is_(True), User.id != approval.employee_id)
    if stage == "manager":
        return base.filter(User.role == "manager", User.company_id == approval.company_id) \
            .order_by(User.full_name, User.username).all()
    if stage == "accountant":
        assigned = [row.user_id for row in db.query(AccountantClient.user_id)
                    .filter(AccountantClient.company_id == approval.company_id)]
        return base.filter(User.role == "accountant",
                           or_(User.company_id == approval.company_id, User.id.in_(assigned or [""]))) \
            .order_by(User.full_name, User.username).all()
    if stage == "employee":
        employee = db.get(User, approval.employee_id)
        return [employee] if employee is not None and employee.is_active else []
    return []


def _notify_list(db: Session, approval: AdvanceApproval, stage: str) -> list[User]:
    people = approvers(db, approval, stage)
    if stage == "accountant" and not people:
        people = db.query(User).filter(User.role == "admin", User.is_active.is_(True),
                                       User.id != approval.employee_id).all()
    return people


def can_view(user: User, approval: AdvanceApproval) -> bool:
    if user.role == "admin":
        return True
    if user.id in (approval.employee_id, approval.created_by):
        return True
    return user.role in ("manager", "accountant") and user.company_id == approval.company_id


def turn_filter(user: User, company_id: str | None = None) -> list:
    """v1.71.3 (T-86): «мой ход» — подпись своего черновика или решение на моём этапе.

    Одно правило на два места: список «Ждут меня» на экране «Согласования» и плитка «Согласования»
    в сводке клиентов. `company_id` — только для администратора (у него явный выбор компании в шапке).
    """
    draft = and_(AdvanceApproval.status == "draft", AdvanceApproval.employee_id == user.id)
    stage = {"manager": "manager", "accountant": "accountant", "admin": "accountant"}.get(user.role)
    if stage is None:
        return [draft]
    turn = and_(AdvanceApproval.status == "pending", AdvanceApproval.stage == stage,
                AdvanceApproval.employee_id != user.id)
    if user.role != "admin":
        turn = and_(turn, AdvanceApproval.company_id == user.company_id)
    elif company_id:
        turn = and_(turn, AdvanceApproval.company_id == company_id)
    return [draft, turn]


def can_act(user: User, approval: AdvanceApproval) -> bool:
    """Решение на текущем этапе: сотрудник — подпись своего отчёта; руководитель и бухгалтер — своей компании."""
    if approval.status == "draft" and approval.stage == "employee":
        return user.id == approval.employee_id
    if approval.status != "pending" or user.id == approval.employee_id:
        return False
    if approval.stage == "manager":
        return user.role == "manager" and user.company_id == approval.company_id
    if approval.stage == "accountant":
        return user.role == "admin" or (user.role == "accountant" and user.company_id == approval.company_id)
    return False


def can_cancel(user: User, approval: AdvanceApproval) -> bool:
    if approval.status not in ("draft", "pending"):
        return False
    if user.id in (approval.employee_id, approval.created_by):
        return True
    return user.role == "admin" or (user.role == "accountant" and user.company_id == approval.company_id)


# ------------------------------------------------------------------ журнал
def _seal_payload(prev: str, e: AdvanceApprovalEvent) -> str:
    return "|".join([prev, e.approval_id, str(e.seq), e.stage, e.action, e.actor_id, e.actor_role,
                     e.doc_hash, e.comment, e.ip, e.created_at.strftime("%Y-%m-%dT%H:%M:%S")])


def _seal(prev: str, e: AdvanceApprovalEvent) -> str:
    key = (app_settings.SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, _seal_payload(prev, e).encode("utf-8"), hashlib.sha256).hexdigest()


def events_of(db: Session, approval_id: str) -> list[AdvanceApprovalEvent]:
    return (db.query(AdvanceApprovalEvent).filter(AdvanceApprovalEvent.approval_id == approval_id)
            .order_by(AdvanceApprovalEvent.seq).all())


def verify(events: list[AdvanceApprovalEvent]) -> dict:
    """Цепочка журнала цела: номера идут подряд, каждая печать сходится с предыдущей."""
    prev = ""
    for index, e in enumerate(events, start=1):
        if e.seq != index or not hmac.compare_digest(e.seal or "", _seal(prev, e)):
            return {"ok": False, "broken_at": index}
        prev = e.seal
    return {"ok": True, "broken_at": 0}


def _event(db: Session, approval: AdvanceApproval, action: str, *, actor: User | None = None, comment: str = "",
           meta: dict | None = None, now: datetime | None = None, stage: str | None = None) -> AdvanceApprovalEvent:
    last = (db.query(AdvanceApprovalEvent).filter(AdvanceApprovalEvent.approval_id == approval.id)
            .order_by(AdvanceApprovalEvent.seq.desc()).first())
    meta = meta or {}
    e = AdvanceApprovalEvent(
        approval_id=approval.id, company_id=approval.company_id, seq=(last.seq + 1) if last else 1,
        stage=approval.stage if stage is None else stage, action=action,
        actor_id=actor.id if actor else "", actor_name=_user_name(actor)[:200], actor_role=actor.role if actor else "",
        comment=(comment or "")[:2000], doc_hash=approval.doc_hash,
        ip=str(meta.get("ip") or "")[:64], user_agent=str(meta.get("user_agent") or "")[:300],
        created_at=(now or utcnow()).replace(microsecond=0))
    e.seal = _seal(last.seal if last else "", e)
    db.add(e)
    db.flush()
    return e


# ------------------------------------------------------------------ маршрут
def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _enter(db: Session, approval: AdvanceApproval, stage: str, now: datetime) -> None:
    """Перевести отчёт на этап; пропуск и пороги — сразу, отдельными событиями журнала."""
    s = settings_of(db, approval.company_id)
    approval.stage, approval.stage_started_at = stage, now
    approval.deadline_at = now + timedelta(hours=int(s.timeout_hours or 72))
    if stage == "manager":
        if not approvers(db, approval, "manager"):
            _event(db, approval, "skipped", now=now,
                   comment="в компании нет руководителя, кроме самого сотрудника, — этап пропущен")
            return _enter(db, approval, "accountant", now)
        if s.manager_limit and approval.total <= s.manager_limit:
            _event(db, approval, "threshold_approved", now=now,
                   comment=f"сумма {_money(approval.total)} не больше порога руководителя {_money(s.manager_limit)}")
            return _enter(db, approval, "accountant", now)
    if stage == "accountant" and s.accountant_limit and approval.total <= s.accountant_limit \
            and compliant(db, approval):
        _event(db, approval, "threshold_approved", now=now,
               comment=f"сумма {_money(approval.total)} не больше порога бухгалтера {_money(s.accountant_limit)}; "
                       "все чеки подтверждены ФНС, без пометок и правок")
        return _complete(db, approval, now)
    return None


def _complete(db: Session, approval: AdvanceApproval, now: datetime) -> None:
    approval.status, approval.stage, approval.deadline_at, approval.finished_at = "approved", "", None, now
    _event(db, approval, "completed", now=now, stage="", comment="отчёт согласован всеми этапами")


def _advance(db: Session, approval: AdvanceApproval, now: datetime) -> None:
    if approval.stage == "employee":
        approval.status = "pending"
        _enter(db, approval, "manager", now)
    elif approval.stage == "manager":
        _enter(db, approval, "accountant", now)
    else:
        _complete(db, approval, now)


def _check_hash(approval: AdvanceApproval, doc_hash: str) -> None:
    if sha256(approval.snapshot or "") != approval.doc_hash:
        raise ApprovalError("Содержимое отчёта не сходится с хешем — подпись невозможна; сообщите администратору", 409)
    if not hmac.compare_digest((doc_hash or "").lower(), approval.doc_hash):
        raise ApprovalError("Отчёт изменился с момента открытия — обновите страницу и проверьте снова", 409)


def create(db: Session, actor: User, company: Company, employee: User, date_from: str, date_to: str, *,
           purpose: str = "", receipt_ids: list[str] | None = None, previous_id: str = "",
           meta: dict | None = None, now: datetime | None = None) -> AdvanceApproval:
    from ..services import ao_numbers
    if not is_enabled(db, company.id):
        raise ApprovalError("Согласование авансовых отчётов в компании выключено — включает администратор", 409)
    if employee.company_id != company.id or not employee.is_active or employee.role not in (
            "user", "manager", "accountant"):
        raise ApprovalError("Сотрудник не найден в компании", 404)
    receipts = employee_receipts(db, company.id, employee, date_from, date_to, receipt_ids)
    if not receipts:
        raise ApprovalError("За период нет чеков сотрудника — отчёт не из чего собрать", 404)
    purpose = (purpose or "").strip()[:500]
    text = canonical(build_snapshot(company, employee, date_from, date_to, purpose, receipts))
    now = now or utcnow()
    approval = AdvanceApproval(id=uid(), company_id=company.id, employee_id=employee.id,
                               employee_name=_user_name(employee)[:200], date_from=date_from, date_to=date_to,
                               purpose=purpose, receipts_count=len(receipts),
                               total=round(sum(_amount(r) for r in receipts), 2), snapshot=text, doc_hash=sha256(text),
                               status="draft", stage="employee", stage_started_at=now, previous_id=previous_id or "",
                               created_by=actor.id, created_at=now, updated_at=now)
    record = ao_numbers.reserve(db, company.id, msk(now).date().isoformat(), approval.id)
    approval.number = ao_numbers.number_text(record)
    db.add(approval)
    db.flush()
    _event(db, approval, "created", actor=actor, meta=meta, now=now,
           comment=f"{approval.number}: чеков {approval.receipts_count}, к учёту {_money(approval.total)}")
    db.commit()
    return approval


def sign(db: Session, approval: AdvanceApproval, actor: User, doc_hash: str, *, meta: dict | None = None,
         now: datetime | None = None) -> AdvanceApproval:
    if approval.status != "draft" or approval.stage != "employee":
        raise ApprovalError("Отчёт уже подписан или закрыт", 409)
    if actor.id != approval.employee_id:
        raise ApprovalError("Подписывает только сотрудник — подотчётное лицо", 403)
    _check_hash(approval, doc_hash)
    now = now or utcnow()
    _event(db, approval, "signed", actor=actor, meta=meta, now=now,
           comment="подписан простой электронной подписью: вход в программу, подтверждение и журнал")
    approval.updated_at = now
    _advance(db, approval, now)
    db.commit()
    return approval


def decide(db: Session, approval: AdvanceApproval, actor: User, decision: str, doc_hash: str, comment: str = "", *,
           meta: dict | None = None, now: datetime | None = None) -> AdvanceApproval:
    if approval.status != "pending" or approval.stage not in ("manager", "accountant"):
        raise ApprovalError("Отчёт не ждёт решения", 409)
    if not can_act(actor, approval):
        if actor.id == approval.employee_id:
            raise ApprovalError("Свой отчёт согласует другой сотрудник", 403)
        raise ApprovalError(f"Этап «{STAGE_TITLES[approval.stage]}» — решение не за вами", 403)
    _check_hash(approval, doc_hash)
    comment = (comment or "").strip()
    if decision == "reject" and len(comment) < 3:
        raise ApprovalError("Напишите причину отказа — её увидит сотрудник", 422)
    now = now or utcnow()
    approval.updated_at = now
    if decision == "approve":
        _event(db, approval, "approved", actor=actor, meta=meta, now=now, comment=comment)
        _advance(db, approval, now)
    else:
        _event(db, approval, "rejected", actor=actor, meta=meta, now=now, comment=comment)
        approval.status, approval.deadline_at, approval.finished_at = "rejected", None, now
    db.commit()
    return approval


def cancel(db: Session, approval: AdvanceApproval, actor: User, comment: str = "", *, meta: dict | None = None,
           now: datetime | None = None) -> AdvanceApproval:
    if not can_cancel(actor, approval):
        raise ApprovalError("Отменить нельзя: отчёт закрыт или нет прав", 403 if approval.status in (
            "draft", "pending") else 409)
    now = now or utcnow()
    _event(db, approval, "cancelled", actor=actor, meta=meta, now=now, comment=(comment or "").strip())
    approval.status, approval.deadline_at, approval.finished_at, approval.updated_at = "cancelled", None, now, now
    db.commit()
    return approval


# ------------------------------------------------------------------ сроки и уведомления (минутный тик)
def expire(db: Session, now: datetime | None = None, company_id: str | None = None) -> int:
    """Этап без ответа за срок компании считается согласованным (ADR-24) — отдельное событие журнала.
    company_id — только одна компания (ручной запуск, тесты); тик обходит все."""
    now = now or utcnow()
    query = db.query(AdvanceApproval).filter(AdvanceApproval.status == "pending",
                                             AdvanceApproval.deadline_at.isnot(None),
                                             AdvanceApproval.deadline_at <= now)
    if company_id:
        query = query.filter(AdvanceApproval.company_id == company_id)
    due = query.order_by(AdvanceApproval.deadline_at).limit(TICK_BATCH).all()
    done = 0
    for approval in due:
        hours = int(settings_of(db, approval.company_id).timeout_hours or 72)
        try:
            _event(db, approval, "auto_approved", now=now,
                   comment=f"нет ответа {hours} ч — этап считается согласованным (ADR-24)")
            approval.updated_at = now
            _advance(db, approval, now)
            db.commit()
            done += 1
        except (StaleDataError, IntegrityError):
            db.rollback()                                    # в эту секунду решили вручную — решение важнее
        except Exception:                                    # noqa: BLE001 — один отчёт не держит остальные
            db.rollback()
            log.exception("approval %s: автосогласование не записано", approval.id)
    return done


def _notice_key(approval: AdvanceApproval) -> str:
    """Какое уведомление положено отчёту сейчас (пусто — никакого); без запросов к базе."""
    if approval.status == "draft":
        return "" if approval.created_by == approval.employee_id else f"draft:{approval.id}"
    if approval.status == "pending" and approval.stage_started_at:
        return f"{approval.stage}:{approval.stage_started_at:%Y%m%d%H%M%S}"
    if approval.status in ("approved", "rejected"):
        return f"done:{approval.status}"
    return ""


def _notice(db: Session, approval: AdvanceApproval) -> tuple[list[User], str]:
    """Получатели и текст уведомления для текущего состояния отчёта."""
    from ..pool import mailer
    link = f"{mailer.base_url(db)}/#/approvals"
    head = (f"Авансовый отчёт {approval.number} — {approval.employee_name}, "
            f"{_ru(approval.date_from)}–{_ru(approval.date_to)}, чеков {approval.receipts_count}, "
            f"к учёту {_money(approval.total)}.")
    if approval.status == "draft":
        return approvers(db, approval, "employee"), f"{head}\nОтчёт подготовлен для вас — проверьте и подпишите: {link}"
    if approval.status == "pending":
        due = msk(approval.deadline_at) if approval.deadline_at else None
        when = f"до {due:%d.%m.%Y %H:%M} (МСК)" if due else ""
        return (_notify_list(db, approval, approval.stage),
                f"{head}\nЖдёт вашего согласования ({STAGE_TITLES[approval.stage]}) {when}. "
                f"Без ответа в срок этап считается согласованным. Открыть: {link}")
    employee = db.get(User, approval.employee_id)
    people = [employee] if employee is not None and employee.is_active else []
    if approval.status == "approved":
        return people, f"{head}\nОтчёт согласован. Открыть: {link}"
    last = (db.query(AdvanceApprovalEvent).filter(AdvanceApprovalEvent.approval_id == approval.id,
                                                  AdvanceApprovalEvent.action == "rejected")
            .order_by(AdvanceApprovalEvent.seq.desc()).first())
    why = f": «{last.comment}» — {last.actor_name} ({STAGE_TITLES.get(last.stage, last.stage)})" if last else ""
    return people, f"{head}\nОтчёт отклонён{why}. Открыть: {link}"


def _ru(day: str) -> str:
    return f"{day[8:10]}.{day[5:7]}.{day[:4]}" if len(day) == 10 else day


def notify(db: Session, now: datetime | None = None, company_id: str | None = None, limit: int = TICK_BATCH) -> int:
    """Отправить уведомления, которых ещё не было (каждое — один раз), и записать итог в журнал.

    Смотрим недавно изменённые отчёты (свежие — первыми): смена этапа меняет ключ уведомления; отчёты, чьё
    уведомление уже ушло, отсеиваются по ключу без запросов к базе."""
    from ..docreq import service as docreq
    since = (now or utcnow()) - timedelta(days=7)
    query = db.query(AdvanceApproval).filter(AdvanceApproval.status.in_(("draft", "pending", "approved", "rejected")),
                                             AdvanceApproval.updated_at >= since)
    if company_id:
        query = query.filter(AdvanceApproval.company_id == company_id)
    rows = query.order_by(AdvanceApproval.updated_at.desc()).limit(limit * 4).all()
    ready = None
    sent = 0
    for approval in rows:
        key = _notice_key(approval)
        if not key or approval.notified_key == key:
            continue
        if sent >= limit:
            break
        people, text = _notice(db, approval)
        if ready is None:
            ready = docreq.channel_status(db)
        channels = _channels(settings_of(db, approval.company_id))
        done, missed = [], []
        for person in people:
            targets = []
            if "telegram" in channels and ready["telegram"]["ready"] and person.telegram_chat_id:
                targets.append(("telegram", person.telegram_chat_id))
            if "max" in channels and ready["max"]["ready"] and person.max_user_id:
                targets.append(("max", person.max_user_id))
            ok = []
            for channel, target in targets:
                success, _err = SENDERS[channel](db, target, text, "Согласование авансового отчёта")
                if success:
                    ok.append("Telegram" if channel == "telegram" else "MAX")
            (done if ok else missed).append(f"{_user_name(person)} ({', '.join(ok)})" if ok else _user_name(person))
        comment = ("отправлено: " + "; ".join(done)) if done else "никому не отправлено"
        if missed:
            comment += "; без канала или с ошибкой отправки: " + "; ".join(missed)
        if not people:
            comment = "получателей нет"
        try:
            # отметка — через ORM: версия строки растёт, и решение, принятое в ту же секунду, получит 409, а не
            # раздвоит журнал (номер записи к тому же уникален); тик повторит уведомление через минуту
            approval.notified_key = key
            _event(db, approval, "notified", comment=comment, stage=approval.stage, now=now)
            db.commit()
            sent += 1
        except (StaleDataError, IntegrityError):
            db.rollback()                                    # отчёт изменился в эту секунду — следующий тик
        except Exception:                                    # noqa: BLE001
            db.rollback()
            log.exception("approval %s: итог уведомления не записан", approval.id)
    return sent


def _send_telegram(db: Session, target: str, text: str, subject: str) -> tuple[bool, str]:
    from ..docreq import service as docreq
    return docreq.SENDERS["telegram"](db, target, text, subject)


def _send_max(db: Session, target: str, text: str, subject: str) -> tuple[bool, str]:
    from ..docreq import service as docreq
    return docreq.SENDERS["max"](db, target, text, subject)


SENDERS = {"telegram": _send_telegram, "max": _send_max}


def tick(db: Session, now: datetime | None = None) -> dict:
    return {"expired": expire(db, now), "notified": notify(db, now)}


# ------------------------------------------------------------------ представление
def brief(approval: AdvanceApproval, now: datetime | None = None) -> dict:
    now = now or utcnow()
    left = None
    if approval.status == "pending" and approval.deadline_at:
        left = max(0, int((approval.deadline_at - now).total_seconds() // 60))
    return {"id": approval.id, "number": approval.number, "company_id": approval.company_id,
            "employee_id": approval.employee_id, "employee_name": approval.employee_name,
            "date_from": approval.date_from, "date_to": approval.date_to, "purpose": approval.purpose,
            "receipts_count": approval.receipts_count, "total": approval.total, "status": approval.status,
            "status_title": STATUS_TITLES.get(approval.status, approval.status), "stage": approval.stage,
            "stage_title": STAGE_TITLES.get(approval.stage, ""), "deadline_at": iso_z(approval.deadline_at),
            "minutes_left": left, "created_at": iso_z(approval.created_at), "finished_at": iso_z(approval.finished_at),
            "doc_hash": approval.doc_hash}


def event_dict(e: AdvanceApprovalEvent) -> dict:
    return {"seq": e.seq, "stage": e.stage, "stage_title": STAGE_TITLES.get(e.stage, ""), "action": e.action,
            "pep": e.action in PEP_ACTIONS and bool(e.actor_id), "actor_name": e.actor_name,
            "actor_role": e.actor_role, "comment": e.comment, "doc_hash": e.doc_hash, "ip": e.ip,
            "created_at": iso_z(e.created_at), "seal": e.seal[:16]}
