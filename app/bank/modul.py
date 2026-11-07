"""Модульбанк — счета и история операций по токену (v1.70.102, ADR-32). Только чтение.

По открытой документации банка (api.modulbank.ru):
  POST {API}/account-info — компании токена и их счета: bankAccounts [{id, number, currency, category, status}];
  POST {API}/operation-history/{bankAccountId} — операции счёта: тело {from, till, skip, records}, порция до 50.
Токен выпускает владелец счёта: интернет-банк → «Услуги» → «Интеграция с банком» → «Подключение к API» →
«Сгенерировать новый», права account-info и operation-history (только чтение; operation-upload не нужен).

Поля операции: id, status (Executed — исходящая исполнена, Received — входящая; остальные ещё не проведены),
category (Debet/Credit), amount, currency, contragentName, contragentInn, contragentBankAccountNumber,
paymentPurpose, executed, docNumber, kbk, payerStatus.
"""
from __future__ import annotations

import os
import re
from datetime import date

from . import http

TITLE = "Модульбанк"
API_BASE = os.environ.get("MODULBANK_API_BASE", "https://api.modulbank.ru/v1").rstrip("/")
PAGE = 50
MAX_PAGES = 200
DONE = ("Executed", "Received")
_SKIP_CATEGORIES = ("DepositAccount", "DepositRateAccount", "ReservationAccounting")
_ID = re.compile(r"^[0-9A-Za-z\-]{1,64}$")


def _auth(token: str) -> str:
    return f"Bearer {token}"


def organization(token: str, cert_pem: str = "") -> dict:
    data = http.request("POST", f"{API_BASE}/account-info", bank=TITLE, auth=_auth(token), body={},
                        cert_pem=cert_pem)
    companies = [c for c in (data if isinstance(data, list) else [data]) if isinstance(c, dict)]
    out = []
    for company in companies:
        inn = http.digits(company.get("companyInn") or company.get("inn"), 12)
        for acc in company.get("bankAccounts") or []:
            if not isinstance(acc, dict) or acc.get("category") in _SKIP_CATEGORIES:
                continue
            if acc.get("status") in ("Closed", "Deleted", "ToClosed"):
                continue
            number = http.digits(acc.get("number"), 20)
            acc_id = str(acc.get("id") or "")
            if len(number) != 20 or not _ID.match(acc_id):
                continue
            out.append({"id": acc_id, "number": number, "tail": number[-4:], "inn": inn,
                        "company": str(company.get("companyId") or ""),
                        "currency": http.currency(acc.get("currency"), number)})
    return {"companies": [{"id": str(c.get("companyId") or ""), "name": str(c.get("companyName") or ""),
                           "inn": http.digits(c.get("companyInn") or c.get("inn"), 12)} for c in companies],
            "accounts": out}


def transaction_doc(op: dict, own_account: str) -> dict | None:
    if op.get("status") not in DONE:
        return None
    category = str(op.get("category") or "")
    if category not in ("Debet", "Debit", "Credit"):
        return None
    debit = category != "Credit"
    try:
        total = f"{abs(float(op.get('amount'))):.2f}"
    except (TypeError, ValueError):
        return None
    day = str(op.get("executed") or op.get("created") or "")[:10]
    if not day:
        return None
    other_acc = http.digits(op.get("contragentBankAccountNumber"), 20)
    doc = {"Номер": str(op.get("docNumber") or "")[:20], "Дата": day, "Сумма": total,
           "_purpose": [str(op.get("paymentPurpose") or "")], "СтатусСоставителя": str(op.get("payerStatus") or ""),
           "ПоказательКБК": str(op.get("kbk") or "")}
    if debit:
        doc.update({"ПлательщикСчет": own_account, "ПолучательСчет": other_acc, "ДатаСписано": day,
                    "ПолучательИНН": str(op.get("contragentInn") or ""),
                    "Получатель1": str(op.get("contragentName") or "")})
    else:
        doc.update({"ПолучательСчет": own_account, "ПлательщикСчет": other_acc, "ДатаПоступило": day,
                    "ПлательщикИНН": str(op.get("contragentInn") or ""),
                    "Плательщик1": str(op.get("contragentName") or "")})
    doc["_card"] = False
    return doc


def statement_docs(token: str, account: dict, lo: date, hi: date, cert_pem: str = "") -> list[dict]:
    acc_id = str(account.get("id") or "")
    if not _ID.match(acc_id):
        raise http.BankApiError("Некорректный идентификатор счёта Модульбанка", "bad")
    number = account.get("number") or ""
    docs: list[dict] = []
    for page in range(MAX_PAGES):
        data = http.request("POST", f"{API_BASE}/operation-history/{acc_id}", bank=TITLE, auth=_auth(token),
                            body={"from": lo.isoformat(), "till": hi.isoformat(), "skip": page * PAGE,
                                  "records": PAGE}, cert_pem=cert_pem)
        items = [x for x in (data if isinstance(data, list) else []) if isinstance(x, dict)]
        for op in items:
            if (doc := transaction_doc(op, number)) is not None:
                docs.append(doc)
        if len(items) < PAGE:
            break
    return docs
