"""Сбербанк (Sber API, СберБизнес) — счета и выписка своей организации (v1.70.102, ADR-32). Только чтение.

По открытой документации банка (developers.sber.ru/docs/ru/sber-api):
  POST {TOKEN} — обновление токенов: grant_type=refresh_token, client_id, client_secret, refresh_token.
       Токен доступа живёт 60 минут, refresh_token — 180 дней; при каждом обновлении банк выдаёт новую пару,
       старый refresh_token остаётся резервным 2 часа — новую пару сохраняем сразу;
  GET  {API}/ic/sso/api/v2/oauth/user-info — сведения об организации (JWT): inn и accounts [{accountNumber, bic}];
  GET  {API}/v2/statement/transactions?accountNumber&statementDate&page — выписка за день постранично
       (операция scope GET_STATEMENT_ACCOUNT). Формат операций — общий со Альфа-Банком (app/bank/fintech.py).
Доступ к своей организации: client_id и client_secret интеграции и пара токенов из личного кабинета Sber API
(или по ссылке авторизации СберБизнес ID). Канал — только взаимный TLS: нужен сертификат для API,
выпущенный банком (загружается в карточке компании, хранится зашифрованным).

Подпись JWT из user-info (ГОСТ) не проверяется: ответ получен по взаимному TLS прямо от банка, а из него
берутся только ИНН (сверка с карточкой компании) и номера счетов.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import date

from . import fintech, http

TITLE = "Сбербанк"
API_BASE = os.environ.get("SBER_API_BASE", "https://fintech.sberbank.ru:9443/fintech/api").rstrip("/")
TOKEN_URL = os.environ.get("SBER_TOKEN_URL", "https://fintech.sberbank.ru:9443/ic/sso/api/v2/oauth/token")
SCOPE_STATEMENT = "GET_STATEMENT_ACCOUNT"


def refresh(client_id: str, client_secret: str, refresh_token: str, cert_pem: str) -> dict:
    data = http.request("POST", TOKEN_URL, bank=TITLE, cert_pem=cert_pem, token_endpoint=True,
                        form={"grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
                              "refresh_token": refresh_token})
    if not isinstance(data, dict) or not data.get("access_token"):
        raise http.BankApiError(f"{TITLE}: банк не выдал токен доступа", "auth")
    return data


def _jwt_payload(text: str) -> dict:
    token = (text or "").strip().strip('"')
    parts = token.split(".")
    if len(parts) < 2:
        try:
            data = json.loads(token)               # песочница может вернуть сведения обычным JSON
        except ValueError as exc:
            raise http.BankApiError(f"{TITLE}: сведения об организации не читаются", "bad") from exc
        return data if isinstance(data, dict) else {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise http.BankApiError(f"{TITLE}: сведения об организации не читаются", "bad") from exc
    return data if isinstance(data, dict) else {}


def organization(access: str, cert_pem: str) -> dict:
    """ИНН и счета организации из user-info."""
    text = http.request("GET", f"{API_BASE}/ic/sso/api/v2/oauth/user-info", bank=TITLE, auth=f"Bearer {access}",
                        cert_pem=cert_pem, accept="application/jwt")
    info = _jwt_payload(str(text or ""))
    out = []
    for acc in info.get("accounts") or []:
        if not isinstance(acc, dict):
            continue
        number = http.digits(acc.get("accountNumber"), 20)
        if len(number) == 20:
            out.append({"id": number, "number": number, "tail": number[-4:], "currency": http.currency("", number)})
    return {"inn": http.digits(info.get("inn"), 12), "name": str(info.get("OrgName") or info.get("orgFullName") or ""),
            "accounts": out}


def statement_docs(access: str, account: dict, lo: date, hi: date, cert_pem: str) -> list[dict]:
    number = fintech.safe_account(account.get("number") or account["id"])
    return fintech.statement_docs(f"{API_BASE}/v2/statement/transactions", bank=TITLE, auth=f"Bearer {access}",
                                  cert_pem=cert_pem, account=number, lo=lo, hi=hi)
