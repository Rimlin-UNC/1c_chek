"""Банковская выписка (T-45, v1.70.98): платежи без документов и чеки к возмещению.

Бухгалтер загружает выписку в формате обмена 1С. Программа сохраняет только списания, которым нужны
документы (оплаты поставщикам и покупки по картам), и сопоставляет их с тем, что уже есть:
- покупка по карте — чек на ту же сумму за 3 дня до списания или днём позже;
- оплата поставщику — УПД или счёт-фактура того же ИНН на ту же сумму (документ за 120 дней до оплаты или
  через 60 после), документ с номером из назначения платежа (частичная оплата), чек этого продавца.
Не нашлось — платёж становится запросом в «Что нужно от клиента» (вид «платёж без документов»); покупка по
карте сотрудника — запрос ему лично. Пришёл документ — запрос закрывается сам.

Обратная сторона — чеки, которых нет в выписке: сотрудник платил своими, ему нужно вернуть деньги («к
возмещению»). Чек считается проверенным выпиской, если выписка покрывает его дату с запасом в 3 дня
(списание по карте приходит позже покупки). Решение «возмещено» или «не возмещать» — за бухгалтером.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from ..models import Company, Receipt, User, utcnow
from ..primary.models import PrimaryDocument
from ..services.policy import accountable_of
from . import parser
from .models import BankOperation, BankReimbursement, BankSettings, BankStatement

log = logging.getLogger("ymaster.bank")
CARD_BEFORE, CARD_AFTER = 3, 1           # чек — за 3 дня до списания … днём позже
DOC_BEFORE, DOC_AFTER = 120, 60          # документ — до 120 дней до оплаты или до 60 дней после
RECEIPT_DAYS = 5                         # чек продавца при оплате по счёту — ±5 дней
COVER_TAIL = 3                           # последние 3 дня выписки ждут следующую (списание приходит позже)
RETENTION = (30, 3650)
DEFAULT_RETENTION = 1095                 # 3 года — глубина налоговой проверки
MAX_CARDS = 100
# номер документа в назначении платежа: «№ 45», «N 45», «No 45», «номер 45» (кириллица — \u-кодами)
_NUMBER = re.compile(r"(?:№|(?<!\w)N[o\u043e]?\.?|(?<!\w)номер)\s*([0-9A-Za-z\u0400-\u04ff][0-9A-Za-z\u0400-\u04ff/\\\-]{0,30})",
                     re.I)


class BankError(ValueError):
    """Ошибка ввода — сообщение для пользователя (status — код ответа API)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def money(value: float | None) -> str:
    if value is None:
        return ""
    whole, frac = f"{value:,.2f}".split(".")
    return f"{whole.replace(',', ' ')},{frac} ₽"


def _close(a: float | None, b: float | None) -> bool:
    return abs(float(a or 0) - float(b or 0)) < 0.005


def _day(value: date | datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _norm_number(value: str) -> str:
    value = re.sub(r"\s+", "", value or "").upper()
    return (value.lstrip("0") or value) if value.isdigit() else value


# ------------------------------------------------------------------ настройки
def settings_of(db: Session, company_id: str) -> BankSettings:
    s = db.get(BankSettings, company_id)
    return s or BankSettings(company_id=company_id, retention_days=DEFAULT_RETENTION, cards_json="[]")


def cards_of(s: BankSettings) -> list[dict]:
    try:
        cards = json.loads(s.cards_json or "[]")
    except ValueError:
        return []
    return [c for c in cards if isinstance(c, dict) and re.fullmatch(r"\d{4}", str(c.get("tail", "")))]


def card_owner_id(db: Session, company_id: str, tail: str) -> str:
    if not tail:
        return ""
    return next((c.get("user_id", "") for c in cards_of(settings_of(db, company_id)) if c["tail"] == tail), "")


def settings_dict(db: Session, s: BankSettings) -> dict:
    cards = cards_of(s)
    ids = {c.get("user_id", "") for c in cards} - {""}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(ids))} if ids else {}
    return {"retention_days": s.retention_days or DEFAULT_RETENTION,
            "cards": [{"tail": c["tail"], "user_id": c.get("user_id", ""),
                       "user": (users[c["user_id"]].full_name or users[c["user_id"]].username)
                       if c.get("user_id") in users else ""} for c in cards]}


def save_settings(db: Session, company_id: str, data: dict, user: User) -> BankSettings:
    days = data.get("retention_days", DEFAULT_RETENTION)
    if not isinstance(days, int) or not RETENTION[0] <= days <= RETENTION[1]:
        raise BankError(f"Срок хранения — от {RETENTION[0]} до {RETENTION[1]} дней")
    cards_in = data.get("cards") or []
    if len(cards_in) > MAX_CARDS:
        raise BankError(f"Не больше {MAX_CARDS} карт")
    staff = {u.id for u in db.query(User).filter(User.company_id == company_id, User.is_active.is_(True))}
    cards, seen = [], set()
    for card in cards_in:
        tail = "".join(ch for ch in str(card.get("tail", "")) if ch.isdigit())
        if len(tail) != 4:
            raise BankError("Карта — последние 4 цифры номера")
        if tail in seen:
            raise BankError(f"Карта *{tail} указана дважды")
        owner = str(card.get("user_id") or "")
        if owner and owner not in staff:
            raise BankError(f"Владелец карты *{tail} — не сотрудник этой компании")
        seen.add(tail)
        cards.append({"tail": tail, "user_id": owner})
    s = db.get(BankSettings, company_id) or BankSettings(company_id=company_id)
    s.retention_days = days
    s.cards_json = json.dumps(cards, ensure_ascii=False)
    s.updated_by = user.id
    s.updated_at = utcnow()
    db.add(s)
    db.commit()
    purge(db, company_id)                    # срок сократили — лишнее удаляется сразу, а не при следующей выписке
    return s


# ------------------------------------------------------------------ загрузка
def import_statement(db: Session, company: Company, raw: bytes, user: User) -> dict:
    """Разобрать и сохранить выписку, сопоставить операции, создать запросы. Итоги — для бухгалтера."""
    try:
        data = parser.parse(raw)
    except parser.StatementError as exc:
        raise BankError(str(exc)) from exc
    sha = hashlib.sha256(raw).hexdigest()
    same = (db.query(BankStatement).filter(BankStatement.company_id == company.id, BankStatement.file_sha256 == sha)
            .first())
    if same:
        raise BankError(f"Эта выписка уже загружена {same.created_at:%d.%m.%Y}", 409)
    own_inn = "".join(ch for ch in (company.inn or "") if ch.isdigit())
    ops = [op for doc in data["documents"] if (op := parser.classify(doc, data["accounts"], own_inn))]
    foreign = {op["own_inn"] for op in ops if op["own_inn"]} - {own_inn}
    if own_inn and foreign and not any(op["own_inn"] == own_inn for op in ops):
        raise BankError(f"Выписка другой организации (ИНН {', '.join(sorted(foreign))}), а компания — ИНН {own_inn}. "
                        "Выберите нужную компанию", 409)
    kinds = Counter(op["kind"] for op in ops)
    statement = BankStatement(company_id=company.id, file_sha256=sha, uploaded_by=user.id, source="file",
                              account_tail=", ".join(a[-4:] for a in data["accounts"])[:40],
                              date_from=data["date_from"] or min((o["op_date"] for o in ops), default=None),
                              date_to=data["date_to"] or max((o["op_date"] for o in ops), default=None),
                              ops_total=len(ops), summary_json=json.dumps(dict(kinds), ensure_ascii=False))
    db.add(statement)
    db.flush()
    saved, duplicates, _ = store_ops(db, company.id, statement.id, ops)
    statement.ops_saved = len(saved)
    statement.duplicates = duplicates
    statement.ops_skipped = len(ops) - len(saved) - duplicates
    db.commit()
    matched = match(db, company.id, saved)
    purged = purge(db, company.id)
    requests = sync_requests(db, company.id, periods_of(saved))
    log.info("bank statement %s: %s ops, %s saved, %s matched", company.id, len(ops), len(saved), matched)
    return {"statement": statement_dict(statement), "operations": len(ops), "saved": len(saved),
            "duplicates": duplicates, "skipped": statement.ops_skipped, "matched": matched,
            "open": len(saved) - matched, "kinds": dict(kinds), "purged": purged, "requests": requests}


def _soft_key(op: dict) -> tuple | None:
    """Вторая проверка дублей — для платежей с номером: дата, сумма, номер и ИНН. Ловит ту же операцию
    из файла и из API банка, если назначение платежа в них записано по-разному (перенос строк, пробелы).
    Покупки по карте без номера так не сравниваются: два одинаковых кофе в один день — разные операции."""
    number = _norm_number(op.get("number") or "")
    if not number:
        return None
    return (op["op_date"].isoformat(), f"{op['amount']:.2f}", number, op.get("counterparty_inn") or "")


def store_ops(db: Session, company_id: str, statement_id: str, ops: list[dict]) -> tuple[list[BankOperation], int, int]:
    """Сохранить операции, которым нужны документы. Возвращает (новые, дубли из других выписок, уже были в этой).

    Общая для файла и для API банка (v1.70.99): выписка из API дополняется при каждой загрузке, поэтому
    операции, сохранённые ею раньше, — не дубли, а «уже были»."""
    known: dict[str, str] = {}
    soft: dict[tuple, str] = {}
    rows = (db.query(BankOperation.op_hash, BankOperation.statement_id, BankOperation.op_date, BankOperation.amount,
                     BankOperation.number, BankOperation.counterparty_inn)
            .filter(BankOperation.company_id == company_id))
    for digest, sid, day, amount, number, inn in rows:
        known[digest] = sid
        key = _soft_key({"op_date": day, "amount": amount or 0.0, "number": number, "counterparty_inn": inn})
        if key:
            soft.setdefault(key, sid)
    saved: list[BankOperation] = []
    duplicates = already = 0
    for op in ops:
        if op["kind"] not in parser.NEEDS_DOCUMENTS:
            continue
        digest = parser.op_hash(company_id, op)
        key = _soft_key(op)
        owner = known.get(digest) or (soft.get(key) if key else None)
        if owner is not None:
            if owner == statement_id:
                already += 1
            else:
                duplicates += 1
            continue
        known[digest] = statement_id
        if key:
            soft[key] = statement_id
        row = BankOperation(company_id=company_id, statement_id=statement_id, op_hash=digest, op_date=op["op_date"],
                            number=op["number"], amount=op["amount"], kind=op["kind"], card_tail=op["card_tail"],
                            counterparty_name=op["counterparty_name"], counterparty_inn=op["counterparty_inn"],
                            purpose=op["purpose"], status="open", updated_at=utcnow())
        db.add(row)
        saved.append(row)
    return saved, duplicates, already


def periods_of(ops: list[BankOperation]) -> set[str]:
    return {f"{op.op_date.year:04d}-{op.op_date.month:02d}" for op in ops if op.op_date}


# ------------------------------------------------------------------ сопоставление
def _used(db: Session, company_id: str) -> tuple[set[str], set[str]]:
    rows = (db.query(BankOperation.match_type, BankOperation.match_id)
            .filter(BankOperation.company_id == company_id, BankOperation.status == "matched").all())
    return ({i for t, i in rows if t == "receipt"}, {i for t, i in rows if t == "primary"})


def _receipt_note(rec: Receipt) -> str:
    shop = f" — {rec.merchant_name}" if rec.merchant_name else ""
    return f"чек на {money(rec.total_sum)} от {_day(rec.receipt_date)}{shop}"[:300]


def _doc_note(doc: PrimaryDocument, partial: bool = False) -> str:
    tail = " (по номеру из назначения платежа)" if partial else ""
    return (f"{doc.kind} № {doc.doc_number or 'без номера'} от {_day(doc.doc_date)} на "
            f"{money(doc.total_with_vat)}{tail}")[:300]


def _find_card(db: Session, op: BankOperation, used: set[str]) -> Receipt | None:
    lo = datetime.combine(op.op_date - timedelta(days=CARD_BEFORE), time.min)
    hi = datetime.combine(op.op_date + timedelta(days=CARD_AFTER + 1), time.min)
    found = (db.query(Receipt)
             .filter(Receipt.company_id == op.company_id, Receipt.operation == 1, Receipt.receipt_date >= lo,
                     Receipt.receipt_date < hi, Receipt.total_sum >= op.amount - 0.005,
                     Receipt.total_sum <= op.amount + 0.005).all())
    found = [r for r in found if r.id not in used]
    if op.counterparty_inn and any(r.merchant_inn == op.counterparty_inn for r in found):
        found = [r for r in found if r.merchant_inn == op.counterparty_inn]
    found.sort(key=lambda r: (abs((r.receipt_date.date() - op.op_date).days), r.receipt_date))
    return found[0] if found else None


def _find_transfer(db: Session, op: BankOperation, used_receipts: set[str],
                   used_docs: set[str]) -> tuple[str, object, bool] | None:
    if not op.counterparty_inn:
        return None
    lo, hi = op.op_date - timedelta(days=DOC_BEFORE), op.op_date + timedelta(days=DOC_AFTER)
    docs = (db.query(PrimaryDocument)
            .filter(PrimaryDocument.company_id == op.company_id, PrimaryDocument.seller_inn == op.counterparty_inn,
                    PrimaryDocument.doc_date >= lo, PrimaryDocument.doc_date <= hi).all())
    docs = [d for d in docs if d.id not in used_docs]
    exact = sorted((d for d in docs if _close(d.total_with_vat, op.amount)),
                   key=lambda d: abs((d.doc_date - op.op_date).days))
    if exact:
        return "primary", exact[0], False
    numbers = {_norm_number(n) for n in _NUMBER.findall(op.purpose or "")}
    by_number = [d for d in docs if d.doc_number and _norm_number(d.doc_number) in numbers]
    if by_number:
        return "primary", by_number[0], True
    lo_r = datetime.combine(op.op_date - timedelta(days=RECEIPT_DAYS), time.min)
    hi_r = datetime.combine(op.op_date + timedelta(days=RECEIPT_DAYS + 1), time.min)
    recs = (db.query(Receipt)
            .filter(Receipt.company_id == op.company_id, Receipt.operation == 1,
                    Receipt.merchant_inn == op.counterparty_inn, Receipt.receipt_date >= lo_r,
                    Receipt.receipt_date < hi_r).all())
    recs = [r for r in recs if r.id not in used_receipts and _close(r.total_sum, op.amount)]
    return ("receipt", recs[0], False) if recs else None


def match_one(db: Session, op: BankOperation, used: tuple[set[str], set[str]] | None = None) -> bool:
    """Найти документ для одной открытой операции; нашёлся — операция «сопоставлена»."""
    if op.status != "open":
        return False
    used_receipts, used_docs = used or _used(db, op.company_id)
    if op.kind == "card":
        rec = _find_card(db, op, used_receipts)
        hit = ("receipt", rec, False) if rec else None
    else:
        hit = _find_transfer(db, op, used_receipts, used_docs)
    if not hit:
        return False
    kind, obj, partial = hit
    op.status, op.match_type, op.match_id = "matched", kind, obj.id
    op.match_note = _receipt_note(obj) if kind == "receipt" else _doc_note(obj, partial)
    op.decided_by, op.updated_at = "", utcnow()
    (used_receipts if kind == "receipt" else used_docs).add(obj.id)
    return True


def match(db: Session, company_id: str, ops: list[BankOperation] | None = None) -> int:
    """Сопоставить открытые операции компании (или переданные). Возвращает число найденных."""
    if ops is None:
        ops = (db.query(BankOperation).filter(BankOperation.company_id == company_id, BankOperation.status == "open")
               .order_by(BankOperation.op_date).all())
    used = _used(db, company_id)
    found = sum(match_one(db, op, used) for op in ops)
    db.commit()
    return found


def rematch(db: Session, company_id: str) -> int:
    """Пришли новые чеки и УПД: сопоставить открытые операции и закрыть запросы по найденным."""
    ops = (db.query(BankOperation).filter(BankOperation.company_id == company_id, BankOperation.status == "open")
           .order_by(BankOperation.op_date).all())
    used = _used(db, company_id)
    hits = [op for op in ops if match_one(db, op, used)]
    db.commit()
    _refresh_requests(db, company_id, periods_of(hits))
    return len(hits)


def satisfied(db: Session, op_id: str) -> str:
    """Для «Что нужно от клиента»: почему запрос по платежу можно закрыть (пусто — ещё нет)."""
    op = db.get(BankOperation, op_id)
    if op is None:
        return ""
    if op.status == "open" and match_one(db, op):
        db.commit()
    return f"найден {op.match_note}" if op.status == "matched" else ""


# ------------------------------------------------------------------ «Что нужно от клиента»
def request_title(op: BankOperation) -> str:
    what = f": «{op.purpose[:90]}{'…' if len(op.purpose) > 90 else ''}»" if op.purpose else ""
    if op.kind == "card":
        card = f" *{op.card_tail}" if op.card_tail else ""
        return f"Чек на покупку по карте{card} на {money(op.amount)} от {_day(op.op_date)}{what}"[:500]
    number = f" (платёж № {op.number})" if op.number else ""
    who = op.counterparty_name or (f"ИНН {op.counterparty_inn}" if op.counterparty_inn else "поставщику")
    return (f"Закрывающие документы (УПД, накладная или акт) на оплату {money(op.amount)} от "
            f"{_day(op.op_date)} — {who}{number}{what}")[:500]


def open_operations(db: Session, company_id: str, start: date, end: date) -> list[BankOperation]:
    return (db.query(BankOperation)
            .filter(BankOperation.company_id == company_id, BankOperation.status == "open",
                    BankOperation.op_date >= start, BankOperation.op_date < end)
            .order_by(BankOperation.op_date, BankOperation.number).all())


def sync_requests(db: Session, company_id: str, periods: set[str]) -> int:
    """После загрузки выписки: найти недостающее за затронутые месяцы (если поиск не выключен бухгалтером)."""
    from ..docreq import service as docreq
    if not periods or not docreq.settings_of(db, company_id).auto_detect:
        return 0
    return sum(docreq.detect(db, company_id, p)["created"] for p in sorted(periods))


def _cancel_requests(db: Session, op_ids: list[str], user: User | None, note: str) -> int:
    from ..docreq import service as docreq
    from ..docreq.models import DocRequest
    if not op_ids:
        return 0
    reqs = (db.query(DocRequest).filter(DocRequest.kind == "payment", DocRequest.ref_type == "bank_op",
                                        DocRequest.ref_id.in_(op_ids), DocRequest.status == "open").all())
    for req in reqs:
        docreq.set_status(db, req, "cancelled", user, note)
    return len(reqs)


def _refresh_requests(db: Session, company_id: str, periods: set[str]) -> None:
    from ..docreq import service as docreq
    for period in sorted(periods):
        docreq.refresh(db, company_id, period)


# ------------------------------------------------------------------ решения бухгалтера
def set_operation(db: Session, op: BankOperation, action: str, user: User, note: str = "",
                  ref_type: str = "", ref_id: str = "") -> BankOperation:
    """ignore — документы не нужны; open — вернуть в работу; link — указать чек или документ вручную."""
    note = " ".join((note or "").split())[:200]
    if action == "ignore":
        if op.status == "ignored":
            return op
        op.status, op.match_type, op.match_id = "ignored", "", ""
        op.match_note = note or "документы не нужны"
        op.decided_by, op.updated_at = user.id, utcnow()
        db.commit()
        _cancel_requests(db, [op.id], user, f"Платёж не требует документов: {op.match_note}")
        return op
    if action == "open":
        op.status, op.match_type, op.match_id, op.match_note = "open", "", "", ""
        op.decided_by, op.updated_at = user.id, utcnow()
        db.commit()
        return op
    if action == "link":
        used_receipts, used_docs = _used(db, op.company_id)
        if ref_type == "receipt":
            rec = db.get(Receipt, ref_id)
            if rec is None or rec.company_id != op.company_id:
                raise BankError("Чек не найден", 404)
            if rec.id in used_receipts and op.match_id != rec.id:
                raise BankError("Этот чек уже сопоставлен с другой операцией", 409)
            op.match_note = _receipt_note(rec)
        elif ref_type == "primary":
            doc = db.get(PrimaryDocument, ref_id)
            if doc is None or doc.company_id != op.company_id:
                raise BankError("Документ не найден", 404)
            if doc.id in used_docs and op.match_id != doc.id:
                raise BankError("Этот документ уже сопоставлен с другой операцией", 409)
            op.match_note = _doc_note(doc)
        else:
            raise BankError("Укажите чек или документ")
        op.status, op.match_type, op.match_id = "matched", ref_type, ref_id
        op.decided_by, op.updated_at = user.id, utcnow()
        db.commit()
        _refresh_requests(db, op.company_id, periods_of([op]))
        return op
    raise BankError("Неизвестное действие")


def delete_statement(db: Session, statement: BankStatement, user: User) -> int:
    """Удалить выписку и её операции; открытые запросы по ним — отменить с пометкой."""
    ops = db.query(BankOperation).filter(BankOperation.statement_id == statement.id).all()
    _cancel_requests(db, [op.id for op in ops], user, "Выписка удалена бухгалтером")
    for op in ops:
        db.delete(op)
    db.delete(statement)
    db.commit()
    return len(ops)


def purge(db: Session, company_id: str | None = None, today: date | None = None) -> int:
    """Срок хранения: операции старше срока удаляются, пустые выписки — тоже. Запросы по ним — отменяются."""
    today = today or (utcnow() + timedelta(hours=3)).date()      # дата по Москве
    deleted = 0
    query = db.query(BankStatement.company_id).distinct()
    if company_id:
        query = query.filter(BankStatement.company_id == company_id)
    for (cid,) in query.all():
        border = today - timedelta(days=settings_of(db, cid).retention_days or DEFAULT_RETENTION)
        old = db.query(BankOperation).filter(BankOperation.company_id == cid, BankOperation.op_date < border).all()
        if old:
            _cancel_requests(db, [op.id for op in old], None, "Операция удалена по сроку хранения выписок")
            for op in old:
                db.delete(op)
            deleted += len(old)
        for st in db.query(BankStatement).filter(BankStatement.company_id == cid, BankStatement.date_to < border):
            if not db.query(BankOperation.id).filter(BankOperation.statement_id == st.id).first():
                db.delete(st)
    db.commit()
    return deleted


# ------------------------------------------------------------------ к возмещению
def _covered(day: date, ranges: list[tuple[date, date]]) -> bool:
    return any(lo <= day and day + timedelta(days=COVER_TAIL) <= hi for lo, hi in ranges)


def _payment_hint(rec: Receipt) -> str:
    if (rec.cash_sum or 0) > 0 and not (rec.ecash_sum or 0):
        return "наличными"
    if (rec.ecash_sum or 0) > 0 and not (rec.cash_sum or 0):
        return "картой"
    return ""


def reimbursements(db: Session, company_id: str) -> dict:
    """Чеки, которых нет в выписках: сотрудник платил своими. По сотрудникам, с решениями бухгалтера."""
    ranges = [(s.date_from, s.date_to) for s in db.query(BankStatement).filter(BankStatement.company_id == company_id)
              if s.date_from and s.date_to]
    out = {"employees": [], "pending_total": 0.0, "pending_count": 0, "covered": bool(ranges),
           "cover_tail_days": COVER_TAIL}
    if not ranges:
        return out
    lo = datetime.combine(min(r[0] for r in ranges), time.min)
    hi = datetime.combine(max(r[1] for r in ranges) + timedelta(days=1), time.min)
    used_receipts, _ = _used(db, company_id)
    decisions = {d.receipt_id: d for d in db.query(BankReimbursement).filter(BankReimbursement.company_id == company_id)}
    receipts = (db.query(Receipt)
                .filter(Receipt.company_id == company_id, Receipt.operation == 1, Receipt.receipt_date >= lo,
                        Receipt.receipt_date < hi)
                .order_by(Receipt.receipt_date).all())
    groups: dict[str, dict] = {}
    for rec in receipts:
        if rec.id in used_receipts or not _covered(rec.receipt_date.date(), ranges):
            continue
        amount = round(accountable_of(rec.total_sum, rec.personal_sum), 2)
        if amount <= 0:
            continue
        decision = decisions.get(rec.id)
        status = decision.status if decision else "pending"
        group = groups.setdefault(rec.created_by or "", {"user_id": rec.created_by or "", "items": [],
                                                         "pending_total": 0.0, "paid_total": 0.0})
        group["items"].append({"receipt_id": rec.id, "date": rec.receipt_date.isoformat(), "amount": amount,
                               "total_sum": rec.total_sum, "merchant": rec.merchant_name or "", "status": status,
                               "payment": _payment_hint(rec), "note": decision.note if decision else ""})
        if status == "pending":
            group["pending_total"] = round(group["pending_total"] + amount, 2)
            out["pending_total"] = round(out["pending_total"] + amount, 2)
            out["pending_count"] += 1
        elif status == "paid":
            group["paid_total"] = round(group["paid_total"] + amount, 2)
    ids = set(groups) - {""}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(ids))} if ids else {}
    for uid_, group in groups.items():
        user = users.get(uid_)
        group["name"] = (user.full_name or user.username) if user else "сотрудник не указан"
    out["employees"] = sorted(groups.values(), key=lambda g: (-g["pending_total"], g["name"]))
    return out


def decide(db: Session, company_id: str, receipt_id: str, status: str, user: User, note: str = "") -> dict:
    """Возмещено, не возмещать (с причиной) или вернуть в «к возмещению»."""
    rec = db.get(Receipt, receipt_id)
    if rec is None or rec.company_id != company_id:
        raise BankError("Чек не найден", 404)
    row = db.query(BankReimbursement).filter(BankReimbursement.receipt_id == receipt_id).first()
    if status == "pending":
        if row:
            db.delete(row)
            db.commit()
        return {"receipt_id": receipt_id, "status": "pending"}
    if status not in ("paid", "declined"):
        raise BankError("Неизвестное решение")
    note = " ".join((note or "").split())[:300]
    if status == "declined" and len(note) < 3:
        raise BankError("Укажите причину: например, «оплачено из подотчётных»")
    row = row or BankReimbursement(company_id=company_id, receipt_id=receipt_id)
    row.user_id = rec.created_by or ""
    row.amount = round(accountable_of(rec.total_sum, rec.personal_sum), 2)
    row.status, row.note, row.decided_by, row.decided_at = status, note, user.id, utcnow()
    db.add(row)
    db.commit()
    return {"receipt_id": receipt_id, "status": status, "note": note}


# ------------------------------------------------------------------ представление
def statement_dict(s: BankStatement, users: dict[str, User] | None = None) -> dict:
    who = (users or {}).get(s.uploaded_by or "")
    try:
        kinds = json.loads(s.summary_json or "{}")
    except ValueError:
        kinds = {}
    return {"id": s.id, "account_tail": s.account_tail, "date_from": s.date_from.isoformat() if s.date_from else None,
            "date_to": s.date_to.isoformat() if s.date_to else None, "operations": s.ops_total, "saved": s.ops_saved,
            "skipped": s.ops_skipped, "duplicates": s.duplicates, "kinds": kinds,
            "source": s.source or "file",                       # v1.70.99: file | tochka (API банка)
            "uploaded_by": (who.full_name or who.username) if who else "",
            "created_at": s.created_at.isoformat() if s.created_at else None}


def op_dict(op: BankOperation, users: dict[str, User] | None = None, owners: dict[str, str] | None = None) -> dict:
    decided = (users or {}).get(op.decided_by or "")
    owner = (users or {}).get((owners or {}).get(op.card_tail, "")) if op.card_tail else None
    return {"id": op.id, "date": op.op_date.isoformat(), "number": op.number, "amount": op.amount, "kind": op.kind,
            "kind_title": parser.KIND_TITLES.get(op.kind, op.kind), "card_tail": op.card_tail,
            "card_owner": (owner.full_name or owner.username) if owner else "",
            "counterparty_name": op.counterparty_name, "counterparty_inn": op.counterparty_inn,
            "purpose": op.purpose, "status": op.status, "match_type": op.match_type, "match_id": op.match_id,
            "match_note": op.match_note, "decided_by": (decided.full_name or decided.username) if decided else ""}


def overview(db: Session, company_id: str, status: str = "open", limit: int = 500) -> dict:
    statements = (db.query(BankStatement).filter(BankStatement.company_id == company_id)
                  .order_by(BankStatement.date_to.desc(), BankStatement.created_at.desc()).all())
    query = db.query(BankOperation).filter(BankOperation.company_id == company_id)
    if status != "all":
        query = query.filter(BankOperation.status == status)
    ops = query.order_by(BankOperation.op_date.desc(), BankOperation.number).limit(limit).all()
    totals = {st: {"count": 0, "amount": 0.0} for st in ("open", "matched", "ignored")}
    for st, amount in db.query(BankOperation.status, BankOperation.amount).filter(BankOperation.company_id == company_id):
        if st in totals:
            totals[st]["count"] += 1
            totals[st]["amount"] = round(totals[st]["amount"] + (amount or 0), 2)
    s = settings_of(db, company_id)
    owners = {c["tail"]: c.get("user_id", "") for c in cards_of(s)}
    ids = {o.decided_by for o in ops} | {s_.uploaded_by for s_ in statements} | set(owners.values())
    ids -= {""}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(ids))} if ids else {}
    refunds = reimbursements(db, company_id)
    staff = (db.query(User).filter(User.company_id == company_id, User.is_active.is_(True))
             .order_by(User.full_name, User.username).all())
    return {"statements": [statement_dict(st, users) for st in statements],
            "staff": [{"id": u.id, "name": u.full_name or u.username} for u in staff],
            "operations": [op_dict(op, users, owners) for op in ops], "status": status, "totals": totals,
            "reimburse": {"pending_total": refunds["pending_total"], "pending_count": refunds["pending_count"]},
            "settings": settings_dict(db, s)}
