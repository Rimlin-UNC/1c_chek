"""Т-Банк (Т-Бизнес, T-API) — счета и выписка по токену (v1.70.102, ADR-32). Только чтение.

По открытой документации банка (developer.tbank.ru):
  GET {API}/api/v4/bank-accounts — счета компании: accountNumber, name, currency, bankBik, accountType;
  GET {API}/api/v1/statement?accountNumber&from&till&limit&cursor — операции за период порциями до 5000;
      пока в ответе есть nextCursor — запрашиваем следующую порцию с cursor=nextCursor.
Доступ — токен, который владелец счёта выпускает сам: Т-Бизнес → «Интеграции» → «Выпуск токена», доступ
«Счета и выписки» (информация о счетах и об операциях компании). Токен привязан банком к ИНН и КПП компании.

Поля операции: operationId, operationDate, typeOfOperation (Debit/Credit), operationStatus (авторизация или
проведённая транзакция — берём только проведённые), accountAmount/rubleAmount, payPurpose, description,
documentNumber, counterParty/payer/receiver {name, inn, account|acct}, cardNumber, tax {kbk, payerStatus}.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from . import http

TITLE = "Т-Банк"
API_BASE = os.environ.get("TBANK_API_BASE", "https://business.tbank.ru/openapi").rstrip("/")
LIMIT = 5000
MAX_PAGES = 50
_MSK = timezone(timedelta(hours=3))


def _auth(token: str) -> str:
    return f"Bearer {token}"


def accounts(token: str, cert_pem: str = "") -> list[dict]:
    data = http.request("GET", f"{API_BASE}/api/v4/bank-accounts", bank=TITLE, auth=_auth(token), cert_pem=cert_pem)
    items = data if isinstance(data, list) else (data or {}).get("accounts") or []
    out = []
    for acc in items:
        if not isinstance(acc, dict):
            continue
        number = http.digits(acc.get("accountNumber"), 20)
        if len(number) != 20:
            continue
        out.append({"id": number, "number": number, "tail": number[-4:],
                    "currency": http.currency(acc.get("currency"), number)})
    return out


def _msk_day(value: object) -> str:
    """Дата операции по Москве: «2026-10-05T21:30:00Z» → 2026-10-06; дата без времени — как есть."""
    text = str(value or "").strip()
    if len(text) <= 10:
        return text[:10]
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    if moment.tzinfo is None:
        return moment.date().isoformat()
    return moment.astimezone(_MSK).date().isoformat()


def transaction_doc(op: dict, own_account: str) -> dict | None:
    """Операция Т-Банка → документ в полях формата обмена с 1С (дальше — общий классификатор)."""
    if str(op.get("operationStatus") or "").lower().startswith("auth"):
        return None                                # авторизация по карте ещё не проведена — придёт транзакцией
    kind = str(op.get("typeOfOperation") or "").lower()
    if kind not in ("debit", "credit"):
        return None
    debit = kind == "debit"
    amount = op.get("accountAmount")
    if op.get("rubleAmount") not in (None, ""):
        amount = op.get("rubleAmount")             # валютный счёт — сумма в рублях
    if amount in (None, ""):
        amount = op.get("operationAmount")
    try:
        total = f"{abs(float(amount)):.2f}"
    except (TypeError, ValueError):
        return None
    party = op.get("counterParty") or op.get("receiver" if debit else "payer") or {}
    other_acc = http.digits(party.get("account") or party.get("acct"), 20)
    day = _msk_day(op.get("chargeDate") or op.get("drawDate") or op.get("operationDate"))
    if not day:
        return None
    purpose = str(op.get("payPurpose") or op.get("description") or "")
    card = str(op.get("cardNumber") or "")
    if card and card[-4:].isdigit() and card[-4:] not in purpose:
        purpose = f"{purpose} (карта {card})".strip()          # хвост карты — для сверки с чеками сотрудника
    tax = op.get("tax") or {}
    doc = {"Номер": str(op.get("documentNumber") or "")[:20], "Дата": str(op.get("docDate") or day)[:10],
           "Сумма": total, "_purpose": [purpose], "СтатусСоставителя": str(tax.get("payerStatus") or ""),
           "ПоказательКБК": str(tax.get("kbk") or "")}
    if debit:
        doc.update({"ПлательщикСчет": own_account, "ПолучательСчет": other_acc, "ДатаСписано": day,
                    "ПолучательИНН": str(party.get("inn") or ""), "Получатель1": str(party.get("name") or "")})
    else:
        doc.update({"ПолучательСчет": own_account, "ПлательщикСчет": other_acc, "ДатаПоступило": day,
                    "ПлательщикИНН": str(party.get("inn") or ""), "Плательщик1": str(party.get("name") or "")})
    doc["_card"] = bool(card)
    return doc


def statement_docs(token: str, account: dict, lo: date, hi: date, cert_pem: str = "") -> list[dict]:
    number = account.get("number") or account["id"]
    params = {"accountNumber": number, "from": f"{lo.isoformat()}T00:00:00+03:00",
              "till": f"{hi.isoformat()}T23:59:59+03:00", "limit": LIMIT}
    docs: list[dict] = []
    for _ in range(MAX_PAGES):
        data = http.request("GET", f"{API_BASE}/api/v1/statement", bank=TITLE, auth=_auth(token), params=params,
                            cert_pem=cert_pem) or {}
        for op in data.get("operations") or []:
            if isinstance(op, dict) and (doc := transaction_doc(op, number)):
                docs.append(doc)
        cursor = str(data.get("nextCursor") or "")
        if not cursor or cursor == params.get("cursor"):
            break
        params = {**params, "cursor": cursor[:100]}
    return docs
