"""Пакет для других учётных систем (T-68, v1.70.100): всё за отчётный месяц одним архивом.

Внутри: чеки с позициями, первичка со строками, банковские операции, которым нужны документы, и
возмещения сотрудникам — каждая таблица в JSON (машинный формат: даты ISO, числа с точкой) и в CSV
(для Excel и загрузки в учётные системы: точка с запятой, UTF-8 с меткой, десятичная запятая); файлы УПД
5.03 по схеме ФНС — тем же генератором, что выгрузка в 1С (их принимают 1С, «Моё дело» и другие системы
через ЭДО); manifest.json с составом и контрольными суммами и FORMAT.md с описанием полей.

В пакет попадает только то, что пользователь вправе видеть: компания — своя (у бухгалтера — выбранный
клиент), чеки, спорные по принадлежности в едином реестре, не включаются ни у кого. Банковских счетов в
пакете нет — программа их не хранит (минимум банковских данных, ADR-28).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..bank import service as bank
from ..bank.models import BankOperation
from ..config import settings
from ..docreq import service as docreq
from ..models import Company, Receipt, User
from ..primary import export1c
from ..primary.models import PrimaryDocument
from ..services.scoping import registry_visible_filter

FORMAT = "ymaster-chek-package"
FORMAT_VERSION = "1.0"
MAX_RECEIPTS = 50000
OPERATIONS = {1: "приход", 2: "возврат прихода", 3: "расход", 4: "возврат расхода"}
FNS = {"valid": "действителен", "invalid": "недействителен", "not_found": "не найден", "unknown": "не проверен",
       "pending": "проверяется"}
BANK_STATUS = {"open": "нет документов", "matched": "документ найден", "ignored": "документы не нужны"}
BANK_KIND = {"card": "покупка по карте", "transfer": "оплата поставщику"}
REFUND = {"pending": "к возмещению", "paid": "возмещено", "declined": "не возмещать"}

RECEIPT_COLUMNS = [
    ("id", "Идентификатор"), ("date", "Дата и время"), ("operation", "Признак расчёта"), ("total_sum", "Сумма, ₽"),
    ("fn", "ФН"), ("fd", "ФД"), ("fp", "ФП"), ("fns_status", "Проверка ФНС"), ("merchant_name", "Продавец"),
    ("merchant_inn", "ИНН продавца"), ("category", "Статья расходов"), ("assignee", "Сотрудник"),
    ("personal_sum", "Личные расходы, ₽"), ("comment", "Комментарий"), ("exported", "Выгружен в 1С"),
    ("source", "Источник"),
]
ITEM_COLUMNS = [
    ("receipt_id", "Чек"), ("position", "№"), ("name", "Наименование"), ("quantity", "Количество"),
    ("unit", "Единица"), ("price", "Цена, ₽"), ("total", "Сумма, ₽"), ("vat_rate", "Ставка НДС"),
    ("vat_sum", "НДС, ₽"), ("product_group", "Товарная группа"),
]
DOC_COLUMNS = [
    ("id", "Идентификатор"), ("kind", "Вид"), ("function", "Функция"), ("number", "Номер"), ("date", "Дата"),
    ("correction_number", "Исправление №"), ("seller_name", "Продавец"), ("seller_inn", "ИНН продавца"),
    ("seller_kpp", "КПП продавца"), ("buyer_name", "Покупатель"), ("buyer_inn", "ИНН покупателя"),
    ("currency", "Валюта"), ("total_without_vat", "Без НДС, ₽"), ("vat_total", "НДС, ₽"),
    ("total_with_vat", "С НДС, ₽"), ("status", "Проверка"), ("exported", "Выгружен в 1С"), ("upd_file", "Файл УПД"),
]
LINE_COLUMNS = [
    ("document_id", "Документ"), ("position", "№"), ("name", "Наименование"), ("unit_name", "Единица"),
    ("quantity", "Количество"), ("price", "Цена"), ("sum_without_vat", "Без НДС"), ("vat_rate", "Ставка НДС"),
    ("vat_sum", "НДС"), ("sum_with_vat", "С НДС"), ("item_code", "Код товара"),
]
BANK_COLUMNS = [
    ("id", "Идентификатор"), ("date", "Дата"), ("number", "Номер документа"), ("amount", "Сумма, ₽"),
    ("kind", "Вид"), ("counterparty_name", "Получатель"), ("counterparty_inn", "ИНН получателя"),
    ("purpose", "Назначение"), ("status", "Документы"), ("match_note", "Найденный документ"),
]
REFUND_COLUMNS = [
    ("employee", "Сотрудник"), ("date", "Дата чека"), ("merchant", "Продавец"), ("payment", "Оплата"),
    ("total_sum", "Сумма чека, ₽"), ("amount", "К возмещению, ₽"), ("status", "Решение"), ("note", "Примечание"),
]
MONEY_KEYS = {"total_sum", "personal_sum", "price", "total", "vat_sum", "total_without_vat", "vat_total",
              "total_with_vat", "sum_without_vat", "sum_with_vat", "amount"}


class PackageError(Exception):
    """Ошибка ввода — сообщение для пользователя (status — код ответа API)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _iso_dt(value: datetime | None) -> str:
    return value.replace(microsecond=0).isoformat() if value else ""


def _iso_d(value: date | None) -> str:
    return value.isoformat() if value else ""


def _num(value) -> float | None:
    return None if value is None else round(float(value), 6)


def _csv_value(key: str, value) -> str:
    """Значение для CSV: даты — ДД.ММ.ГГГГ, деньги — с запятой, логическое — «да»/«нет»."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)) and key in MONEY_KEYS:
        return f"{float(value):.2f}".replace(".", ",")
    if isinstance(value, float):
        return f"{value:g}".replace(".", ",")
    text = str(value)
    if key == "date" and len(text) >= 10 and text[4] == "-" and text[7] == "-":
        day = f"{text[8:10]}.{text[5:7]}.{text[0:4]}"
        return day + (f" {text[11:16]}" if len(text) >= 16 else "")
    return text


def to_csv(rows: list[dict], columns: list[tuple[str, str]]) -> bytes:
    buf = io.StringIO()
    out = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    out.writerow([title for _, title in columns])
    for row in rows:
        out.writerow([_csv_value(key, row.get(key)) for key, _ in columns])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _json(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


# ------------------------------------------------------------------ данные за месяц
def receipts_of(db: Session, company_id: str, start: datetime, end: datetime) -> tuple[list[dict], list[dict]]:
    stmt = (select(Receipt).options(selectinload(Receipt.items))
            .where(Receipt.company_id == company_id, Receipt.receipt_date >= start, Receipt.receipt_date < end,
                   registry_visible_filter())
            .order_by(Receipt.receipt_date, Receipt.id).limit(MAX_RECEIPTS + 1))
    found = list(db.execute(stmt).scalars())
    if len(found) > MAX_RECEIPTS:
        raise PackageError(f"За месяц больше {MAX_RECEIPTS} чеков — обратитесь к администратору")
    receipts, items = [], []
    for r in found:
        receipts.append({
            "id": r.id, "date": _iso_dt(r.receipt_date), "operation": OPERATIONS.get(r.operation, str(r.operation)),
            "total_sum": _num(r.total_sum), "fn": r.fn, "fd": r.fd, "fp": r.fp,
            "fns_status": FNS.get(r.fns_status, r.fns_status), "merchant_name": r.merchant_name or "",
            "merchant_inn": r.merchant_inn or "", "category": r.category or "", "assignee": r.assignee or "",
            "personal_sum": _num(r.personal_sum or 0.0), "comment": r.comment or "", "exported": bool(r.exported),
            "source": r.source or "", "qr": r.qr_data or "",
        })
        for it in sorted(r.items, key=lambda x: (x.position or 0, x.id)):
            items.append({
                "receipt_id": r.id, "position": it.position, "name": it.name or "", "quantity": _num(it.quantity),
                "unit": it.unit or "", "price": _num(it.price), "total": _num(it.total), "vat_rate": it.vat_rate or "",
                "vat_sum": _num(it.vat_sum), "product_group": it.product_group or "",
            })
    return receipts, items


def documents_of(db: Session, company_id: str, start: date, end: date) -> list[PrimaryDocument]:
    stmt = (select(PrimaryDocument).options(selectinload(PrimaryDocument.lines))
            .where(PrimaryDocument.company_id == company_id, PrimaryDocument.doc_date >= start,
                   PrimaryDocument.doc_date < end)
            .order_by(PrimaryDocument.doc_date, PrimaryDocument.doc_number, PrimaryDocument.id))
    return list(db.execute(stmt).scalars())


def _doc_row(doc: PrimaryDocument, upd_file: str) -> dict:
    return {
        "id": doc.id, "kind": doc.kind or "", "function": doc.function or "", "number": doc.doc_number or "",
        "date": _iso_d(doc.doc_date), "correction_number": doc.correction_number or "",
        "seller_name": doc.seller_name or "", "seller_inn": doc.seller_inn or "", "seller_kpp": doc.seller_kpp or "",
        "buyer_name": doc.buyer_name or "", "buyer_inn": doc.buyer_inn or "", "currency": doc.currency or "643",
        "total_without_vat": _num(doc.total_without_vat), "vat_total": _num(doc.vat_total),
        "total_with_vat": _num(doc.total_with_vat), "status": doc.status or "", "exported": doc.exported_at is not None,
        "upd_file": upd_file,
    }


def _line_rows(doc: PrimaryDocument) -> list[dict]:
    return [{"document_id": doc.id, "position": ln.position, "name": ln.name or "", "unit_name": ln.unit_name or "",
             "quantity": _num(ln.quantity), "price": _num(ln.price), "sum_without_vat": _num(ln.sum_without_vat),
             "vat_rate": ln.vat_rate or "", "vat_sum": _num(ln.vat_sum), "sum_with_vat": _num(ln.sum_with_vat),
             "item_code": ln.item_code or ""} for ln in sorted(doc.lines, key=lambda x: (x.position or 0, x.id))]


def operations_of(db: Session, company_id: str, start: date, end: date) -> list[dict]:
    stmt = (select(BankOperation).where(BankOperation.company_id == company_id, BankOperation.op_date >= start,
                                        BankOperation.op_date < end)
            .order_by(BankOperation.op_date, BankOperation.id))
    return [{"id": op.id, "date": _iso_d(op.op_date), "number": op.number or "", "amount": _num(op.amount),
             "kind": BANK_KIND.get(op.kind, op.kind), "counterparty_name": op.counterparty_name or "",
             "counterparty_inn": op.counterparty_inn or "", "purpose": op.purpose or "",
             "status": BANK_STATUS.get(op.status, op.status), "match_note": op.match_note or ""}
            for op in db.execute(stmt).scalars()]


def refunds_of(db: Session, company_id: str, start: date, end: date) -> list[dict]:
    data = bank.reimbursements(db, company_id)
    out = []
    for group in data.get("employees", []):
        for item in group.get("items", []):
            day = str(item.get("date") or "")[:10]
            if not day or not start.isoformat() <= day < end.isoformat():
                continue
            out.append({"employee": group.get("name", ""), "date": day, "merchant": item.get("merchant", ""),
                        "payment": item.get("payment", ""), "total_sum": _num(item.get("total_sum")),
                        "amount": _num(item.get("amount")), "status": REFUND.get(item.get("status"), item.get("status")),
                        "note": item.get("note", "")})
    return out


# ------------------------------------------------------------------ сборка пакета
def file_name(company: Company, period: str) -> str:
    inn = "".join(ch for ch in (company.inn or "") if ch.isdigit()) or "company"
    return f"ymaster-package-{inn}-{period}.zip"


def build(db: Session, company: Company, period: str, user: User, now: datetime | None = None) -> tuple[str, bytes, dict]:
    """Архив пакета за отчётный месяц: (имя файла, байты ZIP, manifest)."""
    try:
        docreq.parse_period(period)
    except docreq.DocRequestError as exc:
        raise PackageError(str(exc)) from exc
    now = now or datetime.now(timezone.utc)
    d0, d1 = docreq.period_start(period), docreq.period_end(period)
    t0, t1 = datetime.combine(d0, time.min), datetime.combine(d1, time.min)
    receipts, items = receipts_of(db, company.id, t0, t1)
    docs = documents_of(db, company.id, d0, d1)
    ready = export1c.prepare(docs, now) if docs else []
    upd_names = {r["doc"].id: f"upd/{r['name']}" for r in ready if "name" in r}
    skipped = [{"document_id": r["doc"].id, "number": r["doc"].doc_number or "", "date": _iso_d(r["doc"].doc_date),
                "reason": r["reason"]} for r in ready if "reason" in r]
    doc_rows = [_doc_row(doc, upd_names.get(doc.id, "")) for doc in docs]
    line_rows = [row for doc in docs for row in _line_rows(doc)]
    operations = operations_of(db, company.id, d0, d1)
    refunds = refunds_of(db, company.id, d0, d1)

    files: list[tuple[str, bytes]] = [
        ("receipts.json", _json(receipts)), ("receipts.csv", to_csv(receipts, RECEIPT_COLUMNS)),
        ("receipt_items.json", _json(items)), ("receipt_items.csv", to_csv(items, ITEM_COLUMNS)),
        ("primary_documents.json", _json(doc_rows)), ("primary_documents.csv", to_csv(doc_rows, DOC_COLUMNS)),
        ("primary_lines.json", _json(line_rows)), ("primary_lines.csv", to_csv(line_rows, LINE_COLUMNS)),
        ("bank_operations.json", _json(operations)), ("bank_operations.csv", to_csv(operations, BANK_COLUMNS)),
        ("reimbursements.json", _json(refunds)), ("reimbursements.csv", to_csv(refunds, REFUND_COLUMNS)),
    ]
    files += [(f"upd/{r['name']}", r["data"]) for r in ready if "name" in r]
    files.append(("FORMAT.md", format_text().encode("utf-8")))
    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "generator": f"{settings.APP_NAME} {settings.APP_VERSION}",
        "generated_at": now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "company": {"name": company.name, "inn": company.inn or ""},
        "period": {"month": period, "title": docreq.period_title(period), "from": d0.isoformat(),
                   "to": (d1 - timedelta(days=1)).isoformat()},
        "counts": {"receipts": len(receipts), "receipt_items": len(items), "primary_documents": len(doc_rows),
                   "primary_lines": len(line_rows), "upd_files": len(upd_names), "bank_operations": len(operations),
                   "reimbursements": len(refunds)},
        "skipped_upd": skipped,
        "files": [{"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files],
        "notes": ["Чеки, спорные по принадлежности в едином реестре, в пакет не включены.",
                  "Номеров банковских счетов в пакете нет: программа их не хранит."],
        "exported_by": user.username,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", _json(manifest))
        for name, data in files:
            archive.writestr(name, data)
    return file_name(company, period), buf.getvalue(), manifest


def format_text() -> str:
    """FORMAT.md — описание пакета: файлы, поля и как загрузить в другие системы."""
    def table(columns: list[tuple[str, str]]) -> str:
        return "\n".join(f"| `{key}` | {title} |" for key, title in columns)

    return f"""# Пакет «Ямастер Чек» для других учётных систем

Формат `{FORMAT}`, версия {FORMAT_VERSION}. Один архив — одна компания и один отчётный месяц.

## Файлы

| Файл | Что внутри |
|---|---|
| `manifest.json` | состав пакета: компания, месяц, число записей, контрольные суммы SHA-256, пропущенные УПД с причиной |
| `receipts.json`, `receipts.csv` | кассовые чеки месяца |
| `receipt_items.json`, `receipt_items.csv` | позиции чеков (связь — поле `receipt_id`) |
| `primary_documents.json`, `primary_documents.csv` | первичка: УПД и счета-фактуры по дате документа |
| `primary_lines.json`, `primary_lines.csv` | строки первичных документов (связь — поле `document_id`) |
| `bank_operations.json`, `bank_operations.csv` | списания по выпискам, которым нужны документы, и найден ли документ |
| `reimbursements.json`, `reimbursements.csv` | чеки сотрудников вне выписки — к возмещению |
| `upd/*.xml` | файлы УПД 5.03 по схеме ФНС (windows-1251), имя — идентификатор файла по формату ФНС |

## Правила записи

- JSON — UTF-8; даты — ГГГГ-ММ-ДД, дата и время — по ISO 8601, например 2026-10-10T12:00:00 (время продажи
  по чеку); числа — с точкой.
- CSV — разделитель «;», UTF-8 с меткой BOM (Excel открывает сразу), десятичная запятая, даты — ДД.ММ.ГГГГ;
  первая строка — названия колонок по-русски, порядок колонок — как в таблицах ниже.
- Деньги — в рублях с копейками. Пустое значение — пустая строка (JSON — `""` или `null`).

## Чеки — `receipts`

| Поле | Колонка CSV |
|---|---|
{table(RECEIPT_COLUMNS)}

В JSON есть ещё поле `qr` — исходная строка QR-кода чека (по ней чек можно проверить в ФНС).

## Позиции чеков — `receipt_items`

| Поле | Колонка CSV |
|---|---|
{table(ITEM_COLUMNS)}

## Первичка — `primary_documents`

| Поле | Колонка CSV |
|---|---|
{table(DOC_COLUMNS)}

## Строки первички — `primary_lines`

| Поле | Колонка CSV |
|---|---|
{table(LINE_COLUMNS)}

## Банк — `bank_operations`

| Поле | Колонка CSV |
|---|---|
{table(BANK_COLUMNS)}

## Возмещения — `reimbursements`

| Поле | Колонка CSV |
|---|---|
{table(REFUND_COLUMNS)}

## Как загрузить

- **1С** — файлы УПД из папки `upd` или выгрузка в 1С на экране «Выгрузка в 1С» (EnterpriseData, API для 1С).
- **«Моё дело»** — входящие УПД принимаются в формате ФНС: файлы из папки `upd` (через ЭДО или загрузкой
  документа); банковская выписка — в формате обмена с 1С из интернет-банка. Таблицы CSV — для сверки и
  ручного ввода авансовых отчётов.
- **Другие системы** — JSON или CSV по описанию выше; связи между таблицами — по полям `receipt_id` и
  `document_id`, проверка целостности — по контрольным суммам в `manifest.json`.
"""
