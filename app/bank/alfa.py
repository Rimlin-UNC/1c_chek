"""Альфа-Банк (Alfa API) — профиль организации и выписка по ключу API (v1.70.102, ADR-32). Только чтение.

Адреса и поля — по официальному SDK банка (github.com/alfacomdevelopment/alfa-api-sdk, лицензия MIT):
  GET {API}/jp/v2/customer-info — профиль организации: inn и accounts [{number, currencyCode, type}];
  GET {API}/statement/transactions?accountNumber&statementDate&page — выписка за день постранично (до 1000
      операций на странице); формат операций — общий со Сбером (app/bank/fintech.py).
Авторизация (там же): «Authorization: ApiKey <ключ API>» или «Authorization: Bearer <токен доступа>».
Ключ выдаёт Альфа-Бизнес Онлайн после подключения Alfa API (заявка на alfa_api@alfabank.ru); для части
методов банк требует сертификат клиента (взаимный TLS, сертификат по запросу CSR) — его можно загрузить
в карточке компании. Токен доступа OAuth — это JWT из трёх частей: такой ключ уходит как Bearer.
"""
from __future__ import annotations

import os
import re
from datetime import date

from . import fintech, http

TITLE = "Альфа-Банк"
API_BASE = os.environ.get("ALFA_API_BASE", "https://baas.alfabank.ru/api").rstrip("/")
_JWT = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*$")


def auth(key: str) -> str:
    """Токен доступа (JWT) — Bearer; ключ API из интернет-банка — ApiKey."""
    return f"Bearer {key}" if _JWT.match(key or "") else f"ApiKey {key}"


def organization(key: str, cert_pem: str = "") -> dict:
    data = http.request("GET", f"{API_BASE}/jp/v2/customer-info", bank=TITLE, auth=auth(key), cert_pem=cert_pem) or {}
    out = []
    for acc in data.get("accounts") or []:
        if not isinstance(acc, dict):
            continue
        number = http.digits(acc.get("number"), 20)
        if len(number) == 20:
            out.append({"id": number, "number": number, "tail": number[-4:],
                        "currency": http.currency(acc.get("currencyCode"), number)})
    return {"inn": http.digits(data.get("inn"), 12), "name": str(data.get("shortName") or data.get("fullName") or ""),
            "accounts": out}


def statement_docs(key: str, account: dict, lo: date, hi: date, cert_pem: str = "") -> list[dict]:
    number = fintech.safe_account(account.get("number") or account["id"])
    return fintech.statement_docs(f"{API_BASE}/statement/transactions", bank=TITLE, auth=auth(key),
                                  cert_pem=cert_pem, account=number, lo=lo, hi=hi)
