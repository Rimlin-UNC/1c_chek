"""Закрытие выписки документами (T-79, #63, v1.70.113, ADR-43): какие расходы и поступления закрыты УПД.

Расход — списание поставщику (bank_operations, вид transfer) ↔ входящий УПД компании (покупатель — компания,
продавец — получатель платежа). Поступление (bank_incomes) ↔ исходящий УПД (продавец — компания, покупатель —
плательщик). Покупки по карте по-прежнему закрывают кассовые чеки (bank.service.match).

Подбор внутри контрагента, по шагам:
1. ручные привязки бухгалтера (bank_links; у списаний — и прежняя ручная «привязка» документа);
2. номер документа из назначения платежа;
3. та же сумма одного документа — ближайший по дате в окне «120 дней до — 60 дней после» оплаты;
4. сумма набора из 2–4 документов (или набора оплат за один документ) — только если такой набор один;
5. остаток — по очереди дат (FIFO): ранние оплаты закрывают ранние документы, как автоматическое погашение
   задолженности в 1С.
Платёж может закрываться несколькими документами, документ — несколькими платежами. Закрыт — документы покрывают
сумму с точностью до копейки; частично — часть. Автоматический подбор не хранится, он пересчитывается; у списаний
итог сводится к статусу операции: закрытый платёж больше не запрос «без документов», частично закрытый остаётся им
с пометкой. Решения бухгалтера («не нужны», ручная привязка) пересчёт не трогает.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations

from sqlalchemy.orm import Session

from ..models import Company, User, utcnow
from ..primary.models import PrimaryDocument
from . import service
from .models import BankIncome, BankLink, BankOperation

EPS = 0.005                               # копейка: суммы сравниваются с округлением до 2 знаков
WINDOW_BEFORE, WINDOW_AFTER = service.DOC_BEFORE, service.DOC_AFTER
SET_MAX, SET_POOL = 4, 12                 # набор — до 4 документов из 12 ближайших по дате
FUNCTIONS = ("СЧФ", "СЧФДОП", "ДОП")
SIDES = ("expense", "income")
HOW = {"manual": "привязал бухгалтер", "number": "номер в назначении платежа", "sum": "та же сумма",
       "set": "сумма нескольких документов", "fifo": "по очереди дат (FIFO)", "receipt": "кассовый чек"}
STATUS_TITLES = {"closed": "закрыт", "partial": "закрыт частично", "open": "без документов",
                 "not_needed": "документы не нужны"}


def _r(value: float | None) -> float:
    return round(float(value or 0), 2)


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _doc_title(doc: PrimaryDocument) -> str:
    day = doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else "без даты"
    return f"{doc.kind} № {doc.doc_number or 'без номера'} от {day}"


@dataclass
class Pay:
    id: str
    day: date
    amount: float
    inn: str
    name: str
    number: str
    purpose: str
    rest: float = 0.0
    links: list = field(default_factory=list)           # [(doc_id, сумма, как)]


@dataclass
class Doc:
    id: str
    day: date
    amount: float
    inn: str
    name: str
    number: str
    title: str
    rest: float = 0.0
    links: list = field(default_factory=list)           # [(pay_id, сумма, как)]


def _give(pay: Pay, doc: Doc, how: str, limit: float | None = None) -> float:
    amount = min(pay.rest, doc.rest, limit if limit is not None else pay.rest)
    amount = _r(amount)
    if amount <= EPS:
        return 0.0
    pay.rest, doc.rest = _r(pay.rest - amount), _r(doc.rest - amount)
    pay.links.append((doc.id, amount, how))
    doc.links.append((pay.id, amount, how))
    return amount


def _near(pay: Pay, doc: Doc) -> bool:
    return pay.day - timedelta(days=WINDOW_BEFORE) <= doc.day <= pay.day + timedelta(days=WINDOW_AFTER)


def _numbers(purpose: str) -> set[str]:
    return {service._norm_number(n) for n in service._NUMBER.findall(purpose or "")}


def _one_set(target: float, pool: list) -> tuple | None:
    """Единственный набор из 2–SET_MAX элементов с суммой target (иначе None: неоднозначно или нет)."""
    found = None
    for size in range(2, min(SET_MAX, len(pool)) + 1):
        for combo in combinations(pool, size):
            if abs(sum(x.rest for x in combo) - target) <= EPS:
                if found is not None:
                    return None
                found = combo
    return found


def pin(pays: dict[str, Pay], docs: dict[str, Doc], pins: list[tuple[str, str, float | None]]) -> None:
    """1. Ручные привязки — первыми и по всей компании (бухгалтер вправе привязать документ другого контрагента)."""
    for pay_id, doc_id, limit in pins:
        if pay_id in pays and doc_id in docs:
            _give(pays[pay_id], docs[doc_id], "manual", limit)


def allocate(pays: list[Pay], docs: list[Doc], pins: list[tuple[str, str, float | None]] | None = None) -> None:
    """Весь подбор для одного контрагента с нуля (ручные привязки + шаги 2–5) — для проверок и простых случаев."""
    for p in pays:
        p.rest, p.links = _r(p.amount), []
    for d in docs:
        d.rest, d.links = _r(d.amount), []
    pin({p.id: p for p in pays}, {d.id: d for d in docs}, pins or [])
    auto_allocate(pays, docs)


def auto_allocate(pays: list[Pay], docs: list[Doc]) -> None:
    """Шаги 2–5 внутри одного контрагента по остаткам после ручных привязок; меняет rest и links на месте."""
    pays.sort(key=lambda x: (x.day, x.id))
    docs.sort(key=lambda x: (x.day, x.id))
    for p in pays:                                                      # 2. номер из назначения платежа
        numbers = _numbers(p.purpose)
        if not numbers:
            continue
        for d in docs:
            if p.rest <= EPS:
                break
            if d.rest > EPS and d.number and service._norm_number(d.number) in numbers:
                _give(p, d, "number")
    for p in pays:                                                      # 3. та же сумма одного документа
        if p.rest <= EPS:
            continue
        same = [d for d in docs if d.rest > EPS and abs(d.rest - p.rest) <= EPS and _near(p, d)]
        if same:
            same.sort(key=lambda d: abs((d.day - p.day).days))
            _give(p, same[0], "sum")
    for p in pays:                                                      # 4а. платёж = набор документов
        if p.rest <= EPS or abs(p.rest - p.amount) > EPS:
            continue
        pool = sorted((d for d in docs if d.rest > EPS and abs(d.rest - d.amount) <= EPS and _near(p, d)),
                      key=lambda d: abs((d.day - p.day).days))[:SET_POOL]
        combo = _one_set(p.rest, pool)
        for d in combo or ():
            _give(p, d, "set")
    for d in docs:                                                      # 4б. документ = набор оплат
        if d.rest <= EPS or abs(d.rest - d.amount) > EPS:
            continue
        pool = sorted((p for p in pays if p.rest > EPS and abs(p.rest - p.amount) <= EPS and _near(p, d)),
                      key=lambda p: abs((d.day - p.day).days))[:SET_POOL]
        combo = _one_set(d.rest, pool)
        for p in combo or ():
            _give(p, d, "set")
    i = j = 0                                                           # 5. остаток — по очереди дат
    open_pays = [p for p in pays if p.rest > EPS]
    open_docs = [d for d in docs if d.rest > EPS]
    while i < len(open_pays) and j < len(open_docs):
        _give(open_pays[i], open_docs[j], "fifo")
        if open_pays[i].rest <= EPS:
            i += 1
        if open_docs[j].rest <= EPS:
            j += 1


# ------------------------------------------------------------------ данные компании
def _company_inn(company: Company) -> str:
    return _digits(company.inn)


def _documents(db: Session, company: Company) -> tuple[list[PrimaryDocument], list[PrimaryDocument]]:
    """(входящие — расходы, исходящие — продажи). Без ИНН у компании все документы считаются входящими."""
    inn = _company_inn(company)
    rows = (db.query(PrimaryDocument)
            .filter(PrimaryDocument.company_id == company.id, PrimaryDocument.function.in_(FUNCTIONS),
                    PrimaryDocument.doc_date.isnot(None)).all())
    incoming, outgoing = [], []
    for doc in rows:
        if (doc.total_with_vat or 0) <= EPS:
            continue
        seller, buyer = _digits(doc.seller_inn), _digits(doc.buyer_inn)
        if inn and seller == inn and buyer != inn:
            outgoing.append(doc)
        elif not inn or buyer == inn or not buyer:
            incoming.append(doc)
    return incoming, outgoing


@dataclass
class Result:
    pays: dict[str, Pay]
    docs: dict[str, Doc]
    rows: dict[str, object]                 # id → BankOperation | BankIncome
    documents: dict[str, PrimaryDocument]


def compute(db: Session, company: Company, side: str) -> Result:
    """Подбор по всей сохранённой истории компании (период отчёта — только фильтр показа)."""
    incoming, outgoing = _documents(db, company)
    if side == "expense":
        rows = (db.query(BankOperation)
                .filter(BankOperation.company_id == company.id, BankOperation.kind == "transfer").all())
        documents = incoming
        doc_inn = lambda d: _digits(d.seller_inn)                     # noqa: E731
        doc_name = lambda d: d.seller_name                            # noqa: E731
    else:
        rows = db.query(BankIncome).filter(BankIncome.company_id == company.id).all()
        documents = outgoing
        doc_inn = lambda d: _digits(d.buyer_inn)                      # noqa: E731
        doc_name = lambda d: d.buyer_name                             # noqa: E731
    pays: dict[str, Pay] = {}
    for row in rows:
        if side == "expense" and (row.status == "ignored" or (row.status == "matched" and row.match_type == "receipt")):
            continue                        # не нужны документы или закрыт кассовым чеком — не участвует в подборе
        inn = _digits(row.counterparty_inn)
        if not inn:
            continue
        pays[row.id] = Pay(row.id, row.op_date, _r(row.amount), inn, row.counterparty_name or "", row.number or "",
                           row.purpose or "")
    docs = {d.id: Doc(d.id, d.doc_date, _r(d.total_with_vat), doc_inn(d), doc_name(d) or "", d.doc_number or "",
                      _doc_title(d)) for d in documents}
    pins: list[tuple[str, str, float | None]] = [
        (ln.operation_id, ln.document_id, ln.amount)
        for ln in db.query(BankLink).filter(BankLink.company_id == company.id, BankLink.side == side)
        .order_by(BankLink.created_at)]
    if side == "expense":                  # прежняя ручная привязка документа к списанию (до 1.70.113)
        pins += [(r.id, r.match_id, None) for r in rows
                 if r.status == "matched" and r.match_type == "primary" and r.decided_by and r.id in pays]
    for p in pays.values():
        p.rest = _r(p.amount)
    for d in docs.values():
        d.rest = _r(d.amount)
    pin(pays, docs, pins)
    groups_p: dict[str, list[Pay]] = defaultdict(list)
    groups_d: dict[str, list[Doc]] = defaultdict(list)
    for p in pays.values():
        groups_p[p.inn].append(p)
    for d in docs.values():
        groups_d[d.inn].append(d)
    for inn, group in groups_p.items():
        auto_allocate(group, groups_d.get(inn, []))
    return Result(pays, docs, {r.id: r for r in rows}, {d.id: d for d in documents})


def _status(amount: float, rest: float) -> str:
    if rest <= EPS:
        return "closed"
    return "partial" if amount - rest > EPS else "open"


def _note(result: Result, pay: Pay) -> str:
    """Один документ закрывает платёж целиком — прежняя запись («УПД № 45 от … на …»), чтобы старые операции и
    запросы не меняли текст; несколько документов или частичное закрытие — список с суммами."""
    if len(pay.links) == 1 and pay.rest <= EPS:
        doc_id, _amount, how = pay.links[0]
        doc = result.documents.get(doc_id)
        if doc is not None:
            return service._doc_note(doc, partial=(how == "number"))
    parts = [f"{result.docs[doc_id].title} — {service.money(amount)}" for doc_id, amount, _how in pay.links]
    text = "; ".join(parts)
    if pay.rest > EPS and pay.links:
        text = f"частично: {text}; осталось {service.money(pay.rest)}"
    elif len(pay.links) > 1:
        text = f"закрыт документами: {text}"
    return text[:300]


def apply(result: Result) -> tuple[int, set[str], int]:
    """Итог подбора — в статусы списаний (решения бухгалтера не трогаются).
    Возвращает (изменено строк, затронутые месяцы, сколько платежей стало закрытыми)."""
    changed, periods, newly = 0, set(), 0
    for row_id, row in result.rows.items():
        if not isinstance(row, BankOperation):
            continue
        if row.status == "ignored" or (row.status == "matched" and (row.decided_by or row.match_type == "receipt")):
            continue
        pay = result.pays.get(row_id)
        if pay is None:
            continue
        if pay.rest <= EPS and pay.links:
            main = max(pay.links, key=lambda x: x[1])[0]
            new = ("matched", "primary", main, _note(result, pay))
        else:
            new = ("open", "", "", _note(result, pay) if pay.links else "")
        if (row.status, row.match_type, row.match_id, row.match_note) != new:
            if row.status != "matched" and new[0] == "matched":
                newly += 1
            row.status, row.match_type, row.match_id, row.match_note = new
            row.decided_by, row.updated_at = "", utcnow()
            changed += 1
            periods.add(f"{row.op_date.year:04d}-{row.op_date.month:02d}")
    return changed, periods, newly


def settle_expense(db: Session, company: Company, refresh: bool = True) -> tuple[Result, int, set[str], int]:
    """Пересчитать списания компании и записать статусы; refresh — сразу закрыть запросы закрытых платежей."""
    expense = compute(db, company, "expense")
    changed, periods, newly = apply(expense)
    db.commit()
    db.info.setdefault("closing_settled", set()).add(company.id)
    if refresh and periods:
        service._refresh_requests(db, company.id, periods)
    return expense, changed, periods, newly


def settle_once(db: Session, company_id: str) -> None:
    """Для проверок запросов «Что нужно от клиента»: пересчитать компанию один раз за сессию (запрос к серверу)."""
    if company_id in db.info.get("closing_settled", set()):
        return
    company = db.get(Company, company_id)
    if company is not None:
        settle_expense(db, company, refresh=False)


def settle(db: Session, company: Company) -> dict:
    """Пересчитать сверку компании: статусы списаний записываются, запросы закрытых платежей закрываются сами."""
    expense, changed, _periods, newly = settle_expense(db, company)
    income = compute(db, company, "income")
    return {"changed": changed, "matched": newly,
            "expense": _summary(expense, None, None, side="expense", db=db, company=company),
            "income": _summary(income, None, None, side="income")}


# ------------------------------------------------------------------ отчёт
def _in_period(day: date | None, start: date | None, end: date | None) -> bool:
    return day is not None and (start is None or day >= start) and (end is None or day <= end)


def _card_rows(db: Session, company: Company, start: date | None, end: date | None) -> list[BankOperation]:
    """Списания вне подбора по УПД: покупки по карте, «документы не нужны», закрытые чеком и переводы без ИНН."""
    rows = db.query(BankOperation).filter(BankOperation.company_id == company.id).all()
    return [r for r in rows if (r.kind == "card" or (r.kind == "transfer" and (
        r.status == "ignored" or (r.status == "matched" and r.match_type == "receipt")
        or not _digits(r.counterparty_inn)))) and _in_period(r.op_date, start, end)]


def _summary(result: Result, start: date | None, end: date | None, *, side: str, db: Session | None = None,
             company: Company | None = None) -> dict:
    total = closed = open_amount = 0.0
    counts = {"closed": 0, "partial": 0, "open": 0, "not_needed": 0}
    for pay in result.pays.values():
        if not _in_period(pay.day, start, end):
            continue
        total += pay.amount
        closed += pay.amount - pay.rest
        open_amount += pay.rest
        counts[_status(pay.amount, pay.rest)] += 1
    if side == "expense" and db is not None and company is not None:
        for row in _card_rows(db, company, start, end):          # чеки по картам и «не нужны» — тоже расходы
            if row.status == "ignored":
                counts["not_needed"] += 1
                continue
            total += row.amount or 0
            if row.status == "matched":
                closed += row.amount or 0
                counts["closed"] += 1
            else:
                open_amount += row.amount or 0
                counts["open"] += 1
    percent = round(closed / total * 100, 1) if total > EPS else 100.0
    return {"count": sum(counts.values()) - counts["not_needed"], "amount": _r(total), "closed_amount": _r(closed),
            "open_amount": _r(open_amount), "percent": percent, **{f"{k}_count": v for k, v in counts.items()}}


def _pay_row(result: Result, pay: Pay, side: str) -> dict:
    row = result.rows[pay.id]
    return {"id": pay.id, "side": side, "date": pay.day.isoformat(), "number": pay.number, "amount": pay.amount,
            "counterparty_name": pay.name, "counterparty_inn": pay.inn, "purpose": pay.purpose,
            "kind": getattr(row, "kind", "income"), "closed": _r(pay.amount - pay.rest), "rest": pay.rest,
            "status": _status(pay.amount, pay.rest),
            "manual": any(how == "manual" for _d, _a, how in pay.links),
            "links": [{"doc_id": doc_id, "title": result.docs[doc_id].title, "date": result.docs[doc_id].day.isoformat(),
                       "amount": amount, "how": how} for doc_id, amount, how in pay.links]}


def _card_row(row: BankOperation) -> dict:
    status = "not_needed" if row.status == "ignored" else ("closed" if row.status == "matched" else "open")
    links = ([{"doc_id": row.match_id, "title": row.match_note, "date": "", "amount": _r(row.amount), "how": "receipt"}]
             if row.status == "matched" else [])
    return {"id": row.id, "side": "expense", "date": row.op_date.isoformat(), "number": row.number or "",
            "amount": _r(row.amount), "counterparty_name": row.counterparty_name or "",
            "counterparty_inn": row.counterparty_inn or "", "purpose": row.purpose or "", "kind": row.kind,
            "closed": _r(row.amount) if status == "closed" else 0.0,
            "rest": 0.0 if status != "open" else _r(row.amount), "status": status, "manual": bool(row.decided_by),
            "links": links, "note": row.match_note if row.status == "ignored" else ""}


def _counterparties(result: Result, start: date | None, end: date | None) -> list[dict]:
    """Сальдо по контрагентам за период: оплачено и документов; разница — аванс без документов или долг."""
    agg: dict[str, dict] = {}
    for pay in result.pays.values():
        if _in_period(pay.day, start, end):
            a = agg.setdefault(pay.inn, {"inn": pay.inn, "name": pay.name, "payments": 0.0, "documents": 0.0})
            a["payments"] = _r(a["payments"] + pay.amount)
    for doc in result.docs.values():
        if _in_period(doc.day, start, end):
            a = agg.setdefault(doc.inn, {"inn": doc.inn, "name": doc.name, "payments": 0.0, "documents": 0.0})
            a["documents"] = _r(a["documents"] + doc.amount)
            a["name"] = a["name"] or doc.name
    out = []
    for a in agg.values():
        a["balance"] = _r(a["payments"] - a["documents"])
        out.append(a)
    out.sort(key=lambda a: (-abs(a["balance"]), a["name"]))
    return out


def _side(db: Session, company: Company, result: Result, side: str, start: date | None, end: date | None) -> dict:
    rows = [_pay_row(result, pay, side) for pay in result.pays.values() if _in_period(pay.day, start, end)]
    if side == "expense":
        rows += [_card_row(r) for r in _card_rows(db, company, start, end)]
    order = {"open": 0, "partial": 1, "closed": 2, "not_needed": 3}
    rows.sort(key=lambda r: (order[r["status"]], r["date"]), reverse=False)
    unpaid = [{"id": d.id, "title": d.title, "date": d.day.isoformat(), "counterparty_name": d.name,
               "counterparty_inn": d.inn, "amount": d.amount, "paid": _r(d.amount - d.rest), "rest": d.rest}
              for d in result.docs.values() if d.rest > EPS and _in_period(d.day, start, end)]
    unpaid.sort(key=lambda d: d["date"])
    return {"summary": _summary(result, start, end, side=side, db=db, company=company), "rows": rows,
            "unpaid": unpaid, "counterparties": _counterparties(result, start, end)}


def report(db: Session, company: Company, start: date | None = None, end: date | None = None) -> dict:
    """Отчёт за период; перед ним — пересчёт (статусы списаний и запросы приводятся к свежей сверке)."""
    expense, changed, _periods, _newly = settle_expense(db, company)
    income = compute(db, company, "income")
    has_incomes = db.query(BankIncome.id).filter(BankIncome.company_id == company.id).first() is not None
    return {"company": {"id": company.id, "name": company.short_name or company.name, "inn": _company_inn(company)},
            "period": {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None},
            "expense": _side(db, company, expense, "expense", start, end),
            "income": _side(db, company, income, "income", start, end),
            "has_incomes": has_incomes, "changed": changed, "how": HOW, "statuses": STATUS_TITLES}


def to_csv(data: dict, side: str) -> bytes:
    """Таблица для бухгалтера: точка с запятой, UTF-8 с меткой — открывается в Excel без мастера импорта."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Дата", "Номер", "Контрагент", "ИНН", "Сумма", "Закрыто", "Остаток", "Статус", "Документы"])
    for row in data[side]["rows"]:
        docs = "; ".join(f"{ln['title']} — {ln['amount']:.2f} ({HOW.get(ln['how'], ln['how'])})" for ln in row["links"])
        writer.writerow([row["date"], row["number"], row["counterparty_name"], row["counterparty_inn"],
                         f"{row['amount']:.2f}".replace(".", ","), f"{row['closed']:.2f}".replace(".", ","),
                         f"{row['rest']:.2f}".replace(".", ","), STATUS_TITLES.get(row["status"], row["status"]), docs])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ ручная привязка
class ClosingError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _row(db: Session, company: Company, side: str, operation_id: str):
    model = BankOperation if side == "expense" else BankIncome
    row = db.get(model, operation_id)
    if row is None or row.company_id != company.id or (side == "expense" and row.kind != "transfer"):
        raise ClosingError("Платёж не найден", 404)
    return row


def candidates(db: Session, company: Company, side: str, operation_id: str) -> dict:
    """Документы для ручной привязки: сначала того же контрагента и с остатком, ближние по дате."""
    row = _row(db, company, side, operation_id)
    result = compute(db, company, side)
    inn = _digits(row.counterparty_inn)
    linked = {ln.document_id for ln in db.query(BankLink).filter(BankLink.side == side,
                                                                  BankLink.operation_id == row.id)}
    items = []
    for doc in result.docs.values():
        same = doc.inn == inn
        if not same and doc.id not in linked:
            continue
        items.append({"id": doc.id, "title": doc.title, "date": doc.day.isoformat(), "counterparty_name": doc.name,
                      "counterparty_inn": doc.inn, "amount": doc.amount, "rest": doc.rest,
                      "linked": doc.id in linked, "same_counterparty": same})
    items.sort(key=lambda d: (not d["linked"], d["rest"] <= EPS, abs((date.fromisoformat(d["date"]) - row.op_date).days)))
    return {"operation": {"id": row.id, "date": row.op_date.isoformat(), "amount": _r(row.amount),
                          "counterparty_name": row.counterparty_name, "counterparty_inn": inn},
            "items": items[:100]}


def link(db: Session, company: Company, side: str, operation_id: str, document_ids: list[str], user: User) -> int:
    """Ручная привязка: заменяет прежние ручные привязки этого платежа; документы — той же компании и стороны."""
    row = _row(db, company, side, operation_id)
    incoming, outgoing = _documents(db, company)
    allowed = {d.id for d in (incoming if side == "expense" else outgoing)}
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        raise ClosingError("Выберите документы")
    bad = [i for i in ids if i not in allowed]
    if bad:
        raise ClosingError("Документ не найден или относится к другой стороне сверки", 404)
    db.query(BankLink).filter(BankLink.side == side, BankLink.operation_id == row.id).delete(synchronize_session=False)
    for doc_id in ids:
        db.add(BankLink(company_id=company.id, side=side, operation_id=row.id, document_id=doc_id,
                        created_by=user.id))
    if side == "expense" and row.status == "ignored":
        row.status, row.match_note, row.decided_by = "open", "", ""
    db.commit()
    return len(ids)


def unlink(db: Session, company: Company, side: str, operation_id: str) -> int:
    """Снять ручные привязки платежа: дальше подбирает программа."""
    row = _row(db, company, side, operation_id)
    n = db.query(BankLink).filter(BankLink.side == side, BankLink.operation_id == row.id).delete(
        synchronize_session=False)
    if side == "expense" and row.status == "matched" and row.match_type == "primary" and row.decided_by:
        row.status, row.match_type, row.match_id, row.match_note, row.decided_by = "open", "", "", "", ""
        n += 1
    db.commit()
    return n
