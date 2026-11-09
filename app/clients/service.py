"""Рабочее место аутсорсера (T-50, v1.70.100, ADR-30): клиенты бухгалтера, переключение клиента, сводка.

Переключение клиента. Интерфейс бухгалтера шлёт заголовок X-Ym-Company с выбранной компанией. Проверка —
в одной точке: auth.get_current_user → activate(). Компания должна быть назначена бухгалтеру
администратором и быть активной, иначе ответ 403. Тогда на время запроса users.company_id подменяется
значением без истории изменений (set_committed_value): прежние правила изоляции — чеки, первичка, «что
нужно от клиента», банк, выгрузка — работают в компании клиента без правок, а в базу подмена не попадает:
UPDATE строится только по изменённым колонкам, у подменённой колонки истории нет. Сессия своя на каждый
запрос, expire_on_commit выключен — подмена живёт до конца запроса и не переживает его. Администратор
заголовок не использует (у него явный выбор компании), остальные роли его игнорируют.

Сводка — по всем компаниям, доступным пользователю: администратору — по всем активным, бухгалтеру — по
своей и назначенным. Чеки, спорные по принадлежности (единый реестр), бухгалтер не видит и в счёт не идут.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..approvals import service as approvals
from ..approvals.models import AdvanceApproval, ApprovalSettings
from ..auth import ROLE_ACCOUNTANT, ROLE_ADMIN
from ..bank.models import BankConnection, BankOperation
from ..bank.providers import TITLES as BANK_TITLES
from ..docreq import service as docreq
from ..docreq.models import DocRequest, DocRequestEvent, DocRequestSettings
from ..models import Company, Receipt, User
from ..primary.models import PrimaryDocument
from ..services.scoping import registry_visible_filter
from .models import AccountantClient

HEADER = "X-Ym-Company"
HOME_ATTR = "_ym_home_company"      # своя компания бухгалтера на время подмены (атрибут объекта, не колонка)
ATTENTION_DAYS = 3                  # как на дашборде: чек «висит» в проверке дольше 3 дней
MAX_CLIENTS = 500
MAX_BULK = 200                      # v1.72.0 (T-89): сколько компаний можно отметить за один раз
BAD_CONN = ("reauth", "error")
# вес признака в очереди внимания: просрочка и ошибки — выше, «к выгрузке» — только сведение
WEIGHTS = {"overdue": 5, "fns_bad": 3, "upd_errors": 3, "approvals_overdue": 3, "bank_open": 2,
           "attention": 2, "policy": 2, "escalated": 2, "open": 1}


class ClientsError(Exception):
    """Ошибка доступа или ввода — сообщение для пользователя (status — код ответа API)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ кто какие компании ведёт
def client_ids(db: Session, user_id: str) -> list[str]:
    """Компании-клиенты бухгалтера, назначенные администратором (без своей)."""
    stmt = (select(AccountantClient.company_id).where(AccountantClient.user_id == user_id)
            .order_by(AccountantClient.created_at, AccountantClient.id))
    return list(db.execute(stmt).scalars())


def home_company_id(user: User) -> str | None:
    """Своя компания пользователя — и во время подмены на клиента."""
    return getattr(user, HOME_ATTR, user.company_id)


def allowed_company_ids(db: Session, user: User) -> list[str] | None:
    """Компании, доступные пользователю; None — все (администратор)."""
    if user.role == ROLE_ADMIN:
        return None
    home = home_company_id(user)
    ids = [home] if home else []
    if user.role == ROLE_ACCOUNTANT:
        ids += [cid for cid in client_ids(db, user.id) if cid not in ids]
    return ids


def activate(db: Session, user: User, requested: str | None) -> bool:
    """Переключить запрос бухгалтера на компанию клиента; True — подмена выполнена."""
    cid = (requested or "").strip()
    if not cid or user.role != ROLE_ACCOUNTANT:
        return False
    home = home_company_id(user)
    if cid == home:
        return False
    if len(cid) > 36 or cid not in client_ids(db, user.id):
        raise ClientsError("Нет доступа к компании клиента — выберите клиента заново", 403)
    company = db.get(Company, cid)
    if company is None or not company.is_active:
        raise ClientsError("Компания клиента отключена — выберите клиента заново", 403)
    setattr(user, HOME_ATTR, home)
    set_committed_value(user, "company_id", cid)
    return True


def company_title(company: Company) -> str:
    return company.short_name or company.name


def clients_of(db: Session, user: User) -> list[dict]:
    """Компании для переключателя бухгалтера: своя — первой, затем назначенные; отключённые — нет."""
    ids = allowed_company_ids(db, user)
    if not ids:
        return []
    rows = {c.id: c for c in db.execute(select(Company).where(Company.id.in_(ids))).scalars()}
    home = home_company_id(user)
    out = []
    for cid in ids:
        company = rows.get(cid)
        if company is not None and company.is_active:
            out.append({"id": company.id, "name": company_title(company), "inn": company.inn or "",
                        "home": cid == home})
    return out


def access_of(db: Session, accountant: User) -> list[str]:
    return client_ids(db, accountant.id)


def check_client_limit(db: Session, user: User, extra: int = 1) -> None:
    """Предел клиентов бухгалтера (T-84): лишний клиент не создаётся и не назначается."""
    have = len(client_ids(db, user.id))
    if have + extra > MAX_CLIENTS:
        raise ClientsError(f"У бухгалтера уже {have} клиентов — предел {MAX_CLIENTS}. "
                           "Снимите лишних в «Пользователи» → «🏢 Клиенты».")


def adopt_client(db: Session, user: User, company_id: str) -> None:
    """Бухгалтер завёл клиента сам (T-84): связь появляется в той же транзакции.

    Иначе бухгалтер создал бы компанию, которую сам же не видит: доступ к клиенту
    выдаётся только строкой accountant_clients (ADR-30). Свою компанию не добавляем.
    """
    if user.role != ROLE_ACCOUNTANT or not company_id or company_id == user.company_id:
        return
    exists = (db.query(AccountantClient)
              .filter(AccountantClient.user_id == user.id,
                      AccountantClient.company_id == company_id).first())
    if exists is None:
        db.add(AccountantClient(user_id=user.id, company_id=company_id, granted_by=user.id))


def set_access(db: Session, admin: User, accountant: User, company_ids: list[str]) -> dict:
    """Заменить список клиентов бухгалтера (только администратор). Своя компания в список не входит."""
    if accountant.role != ROLE_ACCOUNTANT:
        raise ClientsError("Клиентов назначают только бухгалтеру", 409)
    wanted: list[str] = []
    for raw in company_ids:
        cid = str(raw or "").strip()
        if cid and cid not in wanted and cid != accountant.company_id:
            wanted.append(cid)
    if len(wanted) > MAX_CLIENTS:
        raise ClientsError(f"Не больше {MAX_CLIENTS} компаний на одного бухгалтера")
    if any(len(cid) > 36 for cid in wanted):
        raise ClientsError("Компания не найдена", 404)
    found = set(db.execute(select(Company.id).where(Company.id.in_(wanted))).scalars()) if wanted else set()
    if len(found) != len(wanted):
        raise ClientsError("Компания не найдена", 404)
    current = {row.company_id: row for row in db.execute(
        select(AccountantClient).where(AccountantClient.user_id == accountant.id)).scalars()}
    added = [cid for cid in wanted if cid not in current]
    removed = [cid for cid in current if cid not in wanted]
    for cid in removed:
        db.delete(current[cid])
    for cid in added:
        db.add(AccountantClient(user_id=accountant.id, company_id=cid, granted_by=admin.id))
    db.commit()
    return {"company_ids": wanted, "added": len(added), "removed": len(removed)}


# ------------------------------------------------------------------ сводка по клиентам
def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _days_left(due: datetime, now: datetime) -> int:
    """Полных дней до срока: 0 — меньше суток (до «часа икс»); после срока — минус полные дни просрочки,
    но не меньше 1 по модулю (просрочено на 5 минут — −1, на 3 дня 2 часа — −3)."""
    seconds = (due - now).total_seconds()
    if seconds >= 0:
        return int(seconds // 86400)
    return -max(1, int(-seconds // 86400))


def month_title(period: str) -> str:
    """«Октябрь 2026» — для заголовков и выбора периода (в docreq — строчными, внутри фразы)."""
    title = docreq.period_title(period)
    return title[:1].upper() + title[1:]


def _grouped(db: Session, stmt) -> dict[str, int]:
    return {cid: int(count or 0) for cid, count in db.execute(stmt)}


def _blank(company: Company, home: str | None) -> dict:
    return {
        "id": company.id, "name": company_title(company), "inn": company.inn or "", "home": company.id == home,
        "receipts": {"count": 0, "sum": 0.0, "attention": 0, "fns_bad": 0, "policy": 0, "to_export": 0},
        "primary": {"errors": 0, "to_export": 0},
        "docreq": {"open": 0, "overdue": 0, "escalated": 0, "deadline": "", "days_left": 0},
        # v1.71.4 (T-87): список банков — каждый со своим состоянием, загрузкой и ошибкой
        "bank": {"open": 0, "status": "none", "last_sync_at": "", "last_error": "", "banks": []},
        # v1.71.3 (T-86): согласования авансовых отчётов — как на своём экране, но сразу по всем клиентам
        "approvals": {"enabled": False, "todo": 0, "overdue": 0, "work": 0, "month": 0},
        "score": 0, "level": "ok",
    }


def overview(db: Session, user: User, period: str | None = None, now: datetime | None = None) -> dict:
    """Все клиенты в одном окне: недостающие документы, просрочки, платежи без документов, непроверенные
    чеки, ошибки УПД, готовность к выгрузке и последняя загрузка из банка — по каждой компании."""
    now = now or datetime.now(timezone.utc)
    period = period or docreq.default_period(now)
    try:
        docreq.parse_period(period)
    except docreq.DocRequestError as exc:
        raise ClientsError(str(exc)) from exc
    ids = allowed_company_ids(db, user)
    stmt = select(Company).where(Company.is_active.is_(True))
    if ids is not None:
        stmt = stmt.where(Company.id.in_(ids or [""]))
    companies = sorted(db.execute(stmt).scalars(), key=lambda c: company_title(c).lower())
    home = home_company_id(user)
    rows = {c.id: _blank(c, home) for c in companies}
    cids = list(rows)
    result = {"period": period, "period_title": month_title(period), "companies": [], "totals": {},
              "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if cids:
        _fill(db, user, rows, cids, period, now)
    for row in rows.values():
        r, p, d, b = row["receipts"], row["primary"], row["docreq"], row["bank"]
        ap = row["approvals"]
        parts = {"overdue": d["overdue"], "fns_bad": r["fns_bad"], "upd_errors": p["errors"],
                 "approvals_overdue": ap["overdue"], "bank_open": b["open"], "attention": r["attention"],
                 "policy": r["policy"], "escalated": d["escalated"], "open": d["open"]}
        row["score"] = sum(WEIGHTS[k] * v for k, v in parts.items()) + (3 if b["status"] in BAD_CONN else 0)
        bad = d["overdue"] or r["fns_bad"] or p["errors"] or ap["overdue"] or b["status"] in BAD_CONN
        row["level"] = "bad" if bad else ("warn" if row["score"] else "ok")
    ordered = sorted(rows.values(), key=lambda x: (-x["score"], x["name"].lower()))
    result["companies"] = ordered
    result["totals"] = _totals(ordered)
    return result


def _fill(db: Session, user: User, rows: dict[str, dict], cids: list[str], period: str, now: datetime) -> None:
    d0, d1 = docreq.period_start(period), docreq.period_end(period)
    t0, t1 = datetime.combine(d0, time.min), datetime.combine(d1, time.min)
    visible = [] if user.role == ROLE_ADMIN else [registry_visible_filter()]
    base = [Receipt.company_id.in_(cids), *visible]
    in_period = [Receipt.receipt_date >= t0, Receipt.receipt_date < t1]

    def receipts_count(*where) -> dict[str, int]:
        return _grouped(db, select(Receipt.company_id, func.count(Receipt.id))
                        .where(*base, *where).group_by(Receipt.company_id))

    totals = db.execute(select(Receipt.company_id, func.count(Receipt.id),
                               func.coalesce(func.sum(Receipt.total_sum), 0.0))
                        .where(*base, *in_period).group_by(Receipt.company_id))
    for cid, count, amount in totals:
        rows[cid]["receipts"]["count"] = int(count or 0)
        rows[cid]["receipts"]["sum"] = round(float(amount or 0.0), 2)
    cutoff = _naive_utc(now) - timedelta(days=ATTENTION_DAYS)
    metrics = {
        "attention": receipts_count(Receipt.status.in_(["new", "verifying"]), Receipt.created_at < cutoff),
        "fns_bad": receipts_count(Receipt.fns_status.in_(["invalid", "not_found"]), *in_period),
        "policy": receipts_count(Receipt.policy_flags != "", *in_period),
        "to_export": receipts_count(Receipt.status == "verified", Receipt.exported.is_(False), *in_period),
    }
    for key, values in metrics.items():
        for cid, count in values.items():
            rows[cid]["receipts"][key] = count

    doc_where = [PrimaryDocument.company_id.in_(cids), PrimaryDocument.doc_date >= d0, PrimaryDocument.doc_date < d1]
    for key, extra in (("errors", [PrimaryDocument.status == "errors"]),
                       ("to_export", [PrimaryDocument.status != "errors", PrimaryDocument.exported_at.is_(None)])):
        stmt = (select(PrimaryDocument.company_id, func.count(PrimaryDocument.id))
                .where(*doc_where, *extra).group_by(PrimaryDocument.company_id))
        for cid, count in _grouped(db, stmt).items():
            rows[cid]["primary"][key] = count

    req_where = [DocRequest.company_id.in_(cids), DocRequest.period == period, DocRequest.status == "open"]
    open_reqs = _grouped(db, select(DocRequest.company_id, func.count(DocRequest.id))
                         .where(*req_where).group_by(DocRequest.company_id))
    escalated = _grouped(db, select(DocRequest.company_id, func.count(func.distinct(DocRequest.id)))
                         .join(DocRequestEvent, DocRequestEvent.request_id == DocRequest.id)
                         .where(*req_where, DocRequestEvent.kind == "escalated").group_by(DocRequest.company_id))
    saved = {s.company_id: s for s in db.execute(
        select(DocRequestSettings).where(DocRequestSettings.company_id.in_(cids))).scalars()}
    for cid in cids:
        settings = saved.get(cid) or docreq.settings_of(db, cid)
        due = docreq.deadline(period, settings)
        info = rows[cid]["docreq"]
        info["open"] = open_reqs.get(cid, 0)
        info["escalated"] = escalated.get(cid, 0)
        info["deadline"] = due.isoformat()
        info["days_left"] = _days_left(due, now)
        info["overdue"] = info["open"] if now >= due else 0

    bank_open = _grouped(db, select(BankOperation.company_id, func.count(BankOperation.id))
                         .where(BankOperation.company_id.in_(cids), BankOperation.status == "open",
                                BankOperation.op_date >= d0, BankOperation.op_date < d1)
                         .group_by(BankOperation.company_id))
    for cid, count in bank_open.items():
        rows[cid]["bank"]["open"] = count
    # v1.70.102 (ADR-32): банков у компании может быть несколько — в сводке худшее состояние, последняя
    # проверка и первая ошибка с названием банка.
    # v1.71.4 (T-87): список банков — каждый со своим состоянием, загрузкой и ошибкой, худший первым:
    # по нему карточка клиента показывает «какой банк сломался и что именно случилось», а не одну строку.
    rank = {"reauth": 4, "error": 3, "awaiting": 2, "new": 1, "active": 0}
    for conn in db.execute(select(BankConnection).where(BankConnection.company_id.in_(cids))).scalars():
        info = rows[conn.company_id]["bank"]
        status = conn.status or "new"
        title = BANK_TITLES.get(conn.bank, conn.bank)
        last = conn.last_sync_at.isoformat() + "Z" if conn.last_sync_at else ""
        info["banks"].append({"bank": conn.bank, "title": title, "status": status,
                              "error": (conn.last_error or "")[:200],
                              "last_error": f"{title}: {conn.last_error}"[:200] if conn.last_error else "",
                              "last_sync_at": last, "method": conn.method or "",
                              "accounts": _accounts_count(conn.accounts_enc)})
        if info["status"] == "none" or rank.get(status, 1) > rank.get(info["status"], 1):
            info["status"] = status
        if last > info["last_sync_at"]:
            info["last_sync_at"] = last
        if conn.last_error and not info["last_error"]:
            info["last_error"] = f"{title}: {conn.last_error}"[:200]
    for cid in cids:
        info = rows[cid]["bank"]
        info["banks"].sort(key=lambda b: (-rank.get(b["status"], 1), b["title"]))
    _approvals(db, rows, cids, user, now, datetime.combine(docreq.period_start(period), time.min))


def _accounts_count(value: str) -> int:
    """Сколько счетов компании подключено в этом банке (accounts_enc — [{id, tail, currency, status}])."""
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError):
        return 0
    return len(rows) if isinstance(rows, list) else 0


def _approvals(db: Session, rows: dict[str, dict], cids: list[str], user: User, now: datetime,
               month_start: datetime) -> None:
    """v1.71.3 (T-86): согласования авансовых отчётов в сводке — по компаниям, где согласование включено.

    «Ждут моего хода» — то же правило, что в списке «Ждут меня» на экране «Согласования» (approvals.turn_filter):
    второе мнение разошлось бы с экраном. Считаются только отчёты компаний, доступных пользователю (ADR-30:
    бухгалтер видит компании, назначенные администратором, и не видит спорные чеки единого реестра).
    """
    enabled = {cid for (cid,) in db.execute(select(ApprovalSettings.company_id)
                                            .where(ApprovalSettings.company_id.in_(cids),
                                                   ApprovalSettings.enabled.is_(True)))}
    if not enabled:
        return
    ids = sorted(enabled)
    for cid in ids:
        rows[cid]["approvals"]["enabled"] = True
    todo = _grouped(db, select(AdvanceApproval.company_id, func.count(AdvanceApproval.id))
                    .where(or_(*approvals.turn_filter(user)), AdvanceApproval.company_id.in_(ids))
                    .group_by(AdvanceApproval.company_id))
    overdue = _grouped(db, select(AdvanceApproval.company_id, func.count(AdvanceApproval.id))
                       .where(or_(*approvals.turn_filter(user)), AdvanceApproval.company_id.in_(ids),
                              AdvanceApproval.deadline_at.isnot(None), AdvanceApproval.deadline_at <= _naive_utc(now))
                       .group_by(AdvanceApproval.company_id))
    work = _grouped(db, select(AdvanceApproval.company_id, func.count(AdvanceApproval.id))
                    .where(AdvanceApproval.company_id.in_(ids),
                           AdvanceApproval.status.in_(("draft", "pending")))
                    .group_by(AdvanceApproval.company_id))
    month = _grouped(db, select(AdvanceApproval.company_id, func.count(AdvanceApproval.id))
                     .where(AdvanceApproval.company_id.in_(ids), AdvanceApproval.created_at >= month_start)
                     .group_by(AdvanceApproval.company_id))
    for cid in ids:
        info = rows[cid]["approvals"]
        info["todo"], info["overdue"] = todo.get(cid, 0), overdue.get(cid, 0)
        info["work"], info["month"] = work.get(cid, 0), month.get(cid, 0)


# ------------------------------------------------------------------ массовые действия (T-89, v1.72.0)
BULK_ACTIONS = ("remind", "archive", "restore", "assign")
BULK_TITLES = {"remind": "напомнили о документах", "archive": "отправили в архив",
               "restore": "вернули из архива", "assign": "назначили сотрудника"}


def bulk_targets(db: Session, user: User, company_ids: list[str]) -> tuple[list[Company], list[dict]]:
    """Компании пачки, которые пользователю можно менять, и отказы по остальным (причина — для окна).

    Права те же, что у одиночных действий: администратор — любая компания, бухгалтер — только назначенная
    ему администратором и активная (ADR-30). Чужую компанию не «пропускаем молча»: она попадает в отказы
    с причиной, иначе бухгалтер считает, что напомнил клиенту, а напоминания не было.
    """
    allowed = allowed_company_ids(db, user)
    found = {c.id: c for c in db.execute(select(Company).where(Company.id.in_(company_ids or [""]))).scalars()}
    ok, refused = [], []
    for cid in dict.fromkeys(company_ids):
        company = found.get(cid)
        if company is None:
            refused.append({"company_id": cid, "ok": False, "error": "компания не найдена"})
        elif allowed is not None and cid not in allowed:
            refused.append({"company_id": cid, "ok": False, "error": "компания не назначена этому бухгалтеру"})
        else:
            ok.append(company)
    return ok, refused


def _totals(rows: list[dict]) -> dict:
    out = {"companies": len(rows), "bad": 0, "warn": 0, "ok": 0, "receipts": 0, "sum": 0.0, "attention": 0,
           "fns_bad": 0, "policy": 0, "to_export": 0, "upd_errors": 0, "upd_to_export": 0, "open": 0,
           "overdue": 0, "escalated": 0, "bank_open": 0, "approvals_todo": 0, "approvals_overdue": 0}
    for row in rows:
        out[row["level"]] += 1
        r, p, d, b = row["receipts"], row["primary"], row["docreq"], row["bank"]
        out["receipts"] += r["count"]
        out["sum"] = round(out["sum"] + r["sum"], 2)
        for key in ("attention", "fns_bad", "policy", "to_export"):
            out[key] += r[key]
        out["upd_errors"] += p["errors"]
        out["upd_to_export"] += p["to_export"]
        out["open"] += d["open"]
        out["overdue"] += d["overdue"]
        out["escalated"] += d["escalated"]
        out["bank_open"] += b["open"]
        out["approvals_todo"] += row["approvals"]["todo"]
        out["approvals_overdue"] += row["approvals"]["overdue"]
    return out


def period_choices(now: datetime | None = None, count: int = 6) -> list[dict]:
    """Последние месяцы для выбора периода: отчётный (прошлый) — первым, затем текущий и ранние."""
    current = docreq.default_period(now)
    months = [docreq.shift_period(current, 1), current] + [docreq.shift_period(current, -k) for k in range(1, count)]
    return [{"period": p, "title": month_title(p)} for p in months]

