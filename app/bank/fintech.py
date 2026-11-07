"""Выписка в формате «transactions + rurTransfer» (v1.70.102, ADR-32): Сбер (Sber API) и Альфа-Банк (Alfa API).

Оба банка отдают выписку по счёту за один день постранично: GET …/statement/transactions?accountNumber=
&statementDate=ГГГГ-ММ-ДД&page=N. В ответе — список transactions и ссылки _links; есть ссылка rel=next —
есть следующая страница. Ссылке банка мы не переходим (адрес строим сами, меняя только номер страницы):
так ответ банка не может направить запрос с ключами по чужому адресу.

Поля операции (по спецификациям банков): direction DEBIT/CREDIT, amount и amountRub {amount, currencyName},
number, documentDate, operationDate, paymentPurpose, uuid/operationId, rurTransfer {payer…, payee…,
departmentalInfo {kbk, drawerStatus101 …}}. Операция переводится в поля формата обмена с 1С — дальше её
разбирает тот же классификатор, что и файл (parser.classify), поэтому одна и та же операция из файла и из
API не задвоится.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

from . import http

MAX_PAGES = 100                                  # 100 страниц по 100–1000 операций — с большим запасом на день


def days(lo: date, hi: date) -> list[date]:
    out, cur = [], lo
    while cur <= hi:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _amount(txn: dict) -> str | None:
    amount = txn.get("amount") or {}
    rub = txn.get("amountRub") or {}
    value = amount.get("amount")
    if str(amount.get("currencyName") or "RUB").upper() not in ("RUB", "RUR", "810", "643") and rub.get("amount") \
            not in (None, ""):
        value = rub.get("amount")                 # валютная операция — сумма в рублях по курсу ЦБ
    if value in (None, ""):
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return None


def transaction_doc(txn: dict, own_account: str, day: date) -> dict | None:
    """Операция выписки → документ в полях формата обмена с 1С (как у файла выписки)."""
    direction = str(txn.get("direction") or "").upper()
    if direction not in ("DEBIT", "CREDIT"):
        return None
    amount = _amount(txn)
    if amount is None:
        return None
    rur = txn.get("rurTransfer") or {}
    tax = rur.get("departmentalInfo") or {}
    when = day.isoformat()
    doc = {"Номер": str(txn.get("number") or "")[:20], "Дата": str(txn.get("documentDate") or when)[:10],
           "Сумма": amount, "_purpose": [str(txn.get("paymentPurpose") or "")],
           "СтатусСоставителя": str(tax.get("drawerStatus101") or ""), "ПоказательКБК": str(tax.get("kbk") or "")}
    if direction == "DEBIT":
        doc.update({"ПлательщикСчет": own_account, "ПолучательСчет": http.digits(rur.get("payeeAccount"), 20),
                    "ДатаСписано": when, "ПолучательИНН": str(rur.get("payeeInn") or ""),
                    "Получатель1": str(rur.get("payeeName") or ""), "ПлательщикИНН": str(rur.get("payerInn") or "")})
    else:
        doc.update({"ПолучательСчет": own_account, "ПлательщикСчет": http.digits(rur.get("payerAccount"), 20),
                    "ДатаПоступило": when, "ПлательщикИНН": str(rur.get("payerInn") or ""),
                    "Плательщик1": str(rur.get("payerName") or ""), "ПолучательИНН": str(rur.get("payeeInn") or "")})
    doc["_card"] = False
    return doc


def _has_next(data: dict) -> bool:
    return any(isinstance(link, dict) and str(link.get("rel") or "").lower() == "next"
               for link in data.get("_links") or [])


def day_statement(url: str, *, bank: str, auth: str, cert_pem: str, account: str, day: date) -> list[dict]:
    """Все операции счёта за день: страницы по порядку, пока банк даёт ссылку на следующую."""
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = http.request("GET", url, bank=bank, auth=auth, cert_pem=cert_pem, empty_404=True,
                            params={"accountNumber": account, "statementDate": day.isoformat(), "page": page})
        if not isinstance(data, dict):            # 404 «выписка за дату не найдена» — операций в этот день не было
            break
        out.extend(t for t in data.get("transactions") or [] if isinstance(t, dict))
        if not _has_next(data):
            break
    return out


def statement_docs(url: str, *, bank: str, auth: str, cert_pem: str, account: str, lo: date, hi: date) -> list[dict]:
    """Документы за период: по дням (так устроена выписка в API обоих банков)."""
    docs = []
    for day in days(lo, hi):
        for txn in day_statement(url, bank=bank, auth=auth, cert_pem=cert_pem, account=account, day=day):
            doc = transaction_doc(txn, account, day)
            if doc is not None:
                docs.append(doc)
    return docs


def safe_account(number: str) -> str:
    """Номер счёта для адреса запроса: ровно 20 цифр (иначе — ошибка, а не запрос по странному адресу)."""
    value = http.digits(number)
    if len(value) != 20:
        raise http.BankApiError("Некорректный номер счёта в ответе банка", "bad")
    return quote(value)
