"""Клиент API Точка Банка (v1.70.99, ADR-29): OAuth 2.0, клиенты, счета и выписки — только чтение.

Адреса и поля — по открытой спецификации банка (developers.tochka.com, OpenAPI tochka-api):
  POST {AUTH}/token — токены: client_credentials (приложение), authorization_code (после входа клиента),
       refresh_token (продление; каждый раз приходит новый refresh_token — сохраняем его);
  POST {API}/consent/v1.0/consents — разрешение на чтение (consentId), затем клиент входит в банк:
  GET  {AUTH}/authorize?client_id&response_type=code&state&redirect_uri&scope&consent_id;
  GET  {API}/open-banking/v1.0/customers и /accounts — компания (ИНН → customerCode) и её счета;
  POST {API}/open-banking/v1.0/statements — заказ выписки (statementId), банк готовит её асинхронно;
  GET  {API}/open-banking/v1.0/accounts/{accountId}/statements/{statementId} — когда status = Ready.

TLS: enter.tochka.com работает только на сертификатах Минцифры (Russian Trusted CA), которых нет в
стандартных хранилищах. Доверие к ним добавляется к системному (app/certs/russian_trusted_ca.pem).
Повтора «без проверки сертификата» нет и не будет: по этому каналу идут секреты и банковские данные.

Секреты в журнал не пишутся; текст ошибок банка обрезается и не содержит токенов.
"""
from __future__ import annotations

import logging
import os
import re
import ssl
import time
from datetime import date, datetime
from urllib.parse import quote, urlencode

import httpx

from .http import BankApiError

log = logging.getLogger("ymaster.bank.tochka")

AUTH_BASE = os.environ.get("TOCHKA_AUTH_BASE", "https://enter.tochka.com/connect").rstrip("/")
API_BASE = os.environ.get("TOCHKA_API_BASE", "https://enter.tochka.com/uapi").rstrip("/")
SCOPE = "accounts customers statements"           # только чтение: платежей и СБП нет
PERMISSIONS = ("ReadAccountsBasic", "ReadAccountsDetail", "ReadStatements", "ReadTransactionsBasic",
               "ReadTransactionsCredits", "ReadTransactionsDebits", "ReadTransactionsDetail", "ReadCustomerData")
CONSENT_DAYS = 365                                # разрешение на год; продлить — «Переподключить»
TIMEOUT = 20.0
POLL_DELAYS = (1, 1, 2, 2, 3, 3, 4, 4)            # ≈20 с ждём готовности выписки, дальше — следующей проверкой
_CA_RU = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs",
                      "russian_trusted_ca.pem")
_ID = re.compile(r"^[0-9A-Za-z._\-/]{1,64}$")     # accountId вида «40702810…/044525104», statementId

transport: httpx.BaseTransport | None = None      # тесты подставляют httpx.MockTransport (только синтетика)
sleep = time.sleep                                # тесты подменяют, чтобы не ждать
_ssl: ssl.SSLContext | None = None


class TochkaError(BankApiError):
    """Ошибка обмена с Точкой. kind: auth | forbidden | rate | unavailable | tls | bad.
    v1.70.102: частный случай общей ошибки банков (app/bank/http.py) — загрузка ловит их одинаково."""


def ssl_context() -> ssl.SSLContext:
    """Системное хранилище плюс корневой и выпускающий сертификаты Минцифры (как у бота MAX)."""
    ctx = ssl.create_default_context()
    if os.path.exists(_CA_RU):
        ctx.load_verify_locations(cafile=_CA_RU)
    else:
        log.warning("Точка: нет %s — проверка сертификата банка не пройдёт", _CA_RU)
    return ctx


def _client() -> httpx.Client:
    global _ssl
    if transport is not None:
        return httpx.Client(transport=transport, timeout=TIMEOUT)
    if _ssl is None:
        _ssl = ssl_context()
    return httpx.Client(verify=_ssl, timeout=TIMEOUT)


def _safe_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID.match(value) or ".." in value:
        raise TochkaError(f"Некорректный идентификатор ({what})", "bad")
    return value


def _error_text(resp: httpx.Response) -> str:
    """Короткое описание ошибки банка без служебных данных."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [str(data.get(k) or "") for k in ("error_description", "message", "error")]
    for item in data.get("Errors") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("message") or item.get("errorCode") or ""))
    text = "; ".join(p for p in dict.fromkeys(parts) if p)
    return re.sub(r"[A-Za-z0-9_\-]{40,}", "…", text)[:200]   # длинные строки (токены) не показываем


def _request(method: str, url: str, *, token: str = "", form: dict | None = None, body: dict | None = None,
             token_endpoint: bool = False) -> dict:
    # только заголовки из спецификации: компания определяется счётом (accountId), отдельный заголовок не нужен
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _client() as client:
            resp = client.request(method, url, headers=headers, data=form, json=body)
    except httpx.HTTPError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            raise TochkaError("Не удалось проверить сертификат банка (Минцифры). Обновите программу — "
                              "сертификаты идут в её составе", "tls") from exc
        raise TochkaError("Банк недоступен — повторим позже", "unavailable") from exc
    code = resp.status_code
    if token_endpoint and code in (400, 401):
        detail = _error_text(resp)
        raise TochkaError("Банк не принял данные входа: " + (detail or f"код {code}") +
                          ". Проверьте client_id и client_secret или подключите банк заново", "auth", code)
    if code == 401:
        raise TochkaError("Банк не принял доступ: срок действия истёк или доступ отозван", "auth", code)
    if code == 403:
        raise TochkaError("Банк отказал в доступе: нет разрешения на счета и выписки — подключите банк заново "
                          "и разрешите чтение выписок", "forbidden", code)
    if code == 429:
        raise TochkaError("Банк ограничил частоту запросов — повторим позже", "rate", code)
    if code >= 500:
        raise TochkaError(f"Банк временно не отвечает (код {code}) — повторим позже", "unavailable", code)
    if code >= 400:
        detail = _error_text(resp)
        raise TochkaError(f"Банк вернул ошибку {code}" + (f": {detail}" if detail else ""), "bad", code)
    try:
        data = resp.json()
    except ValueError as exc:
        raise TochkaError("Банк прислал ответ не в формате JSON", "bad", code) from exc
    if not isinstance(data, dict):
        raise TochkaError("Банк прислал ответ неожиданного вида", "bad", code)
    return data


def _items(data: dict, key: str) -> list[dict]:
    """Data.<key> — список объектов (в ответах банка бывает и одиночный объект)."""
    value = (data.get("Data") or {}).get(key)
    if isinstance(value, dict):
        return [value]
    return [x for x in value or [] if isinstance(x, dict)]


# ------------------------------------------------------------------ OAuth 2.0
def token(grant: dict) -> dict:
    data = _request("POST", f"{AUTH_BASE}/token", form=grant, token_endpoint=True)
    if not data.get("access_token"):
        raise TochkaError("Банк не выдал токен доступа", "auth")
    return data


def app_token(client_id: str, client_secret: str) -> str:
    """Токен самого приложения — нужен только чтобы создать разрешение."""
    return token({"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials",
                  "scope": SCOPE})["access_token"]


def create_consent(app_access: str, expires: datetime) -> dict:
    body = {"Data": {"permissions": list(PERMISSIONS),
                     "expirationDateTime": expires.strftime("%Y-%m-%dT%H:%M:%S+00:00")}}
    items = _items(_request("POST", f"{API_BASE}/consent/v1.0/consents", token=app_access, body=body), "Consent")
    if not items or not items[0].get("consentId"):
        raise TochkaError("Банк не создал разрешение на доступ", "bad")
    return {"consent_id": str(items[0]["consentId"])[:128], "status": str(items[0].get("status") or "")}


def authorize_url(client_id: str, redirect_uri: str, state: str, consent_id: str) -> str:
    query = {"client_id": client_id, "response_type": "code", "state": state, "redirect_uri": redirect_uri,
             "scope": SCOPE, "consent_id": consent_id}
    return f"{AUTH_BASE}/authorize?{urlencode(query, quote_via=quote)}"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Код из адреса возврата (живёт 5 минут) → access_token (24 часа) и refresh_token (30 дней)."""
    return token({"client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code",
                  "scope": SCOPE, "code": code, "redirect_uri": redirect_uri})


def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return token({"client_id": client_id, "client_secret": client_secret, "grant_type": "refresh_token",
                  "refresh_token": refresh_token})


# ------------------------------------------------------------------ данные
def customers(access: str) -> list[dict]:
    return _items(_request("GET", f"{API_BASE}/open-banking/v1.0/customers", token=access), "Customer")


def accounts(access: str) -> list[dict]:
    """Все счета, доступные по токену; у каждого — customerCode компании (отбор — на нашей стороне)."""
    return _items(_request("GET", f"{API_BASE}/open-banking/v1.0/accounts", token=access), "Account")


def init_statement(access: str, account_id: str, start: date, end: date) -> dict:
    body = {"Data": {"Statement": {"accountId": _safe_id(account_id, "счёт"), "startDateTime": start.isoformat(),
                                   "endDateTime": end.isoformat()}}}
    items = _items(_request("POST", f"{API_BASE}/open-banking/v1.0/statements", token=access, body=body),
                   "Statement")
    if not items or not items[0].get("statementId"):
        raise TochkaError("Банк не принял заказ выписки", "bad")
    return items[0]


def get_statement(access: str, account_id: str, statement_id: str) -> dict:
    # accountId — «номер/БИК»: банк ждёт его в пути как есть (так в примерах документации и в SDK)
    url = (f"{API_BASE}/open-banking/v1.0/accounts/{_safe_id(account_id, 'счёт')}/statements/"
           f"{_safe_id(statement_id, 'выписка')}")
    items = _items(_request("GET", url, token=access), "Statement")
    return items[0] if items else {}


def wait_statement(access: str, account_id: str, statement_id: str) -> dict | None:
    """Выписка, когда банк её подготовит; не успел за ≈20 с — None (заберём при следующей проверке)."""
    for delay in (0, *POLL_DELAYS):
        if delay:
            sleep(delay)
        st = get_statement(access, account_id, statement_id)
        status = st.get("status")
        if status == "Ready":
            return st
        if status == "Error":
            raise TochkaError("Банк не смог сформировать выписку — повторим позже", "unavailable")
    return None


def account_number(account_id: str) -> str:
    """Номер счёта из accountId «40702810…/044525104» (цифры до косой черты)."""
    return "".join(ch for ch in (account_id or "").split("/", 1)[0] if ch.isdigit())[:20]


def transaction_doc(txn: dict, own_account: str) -> dict | None:
    """Транзакция банка → документ в полях формата обмена с 1С: дальше её разбирает тот же классификатор,
    что и файл (parser.classify), — правила одни, а одна и та же операция из файла и из API не задвоится."""
    if txn.get("status") not in (None, "", "Booked"):      # Pending — ещё не проведена, придёт позже
        return None
    debit = txn.get("creditDebitIndicator") == "Debit"
    amount_obj = txn.get("Amount") or {}
    amount = amount_obj.get("amount")
    if (amount_obj.get("currency") or "RUB") != "RUB":
        amount = amount_obj.get("amountNat")              # валютный счёт — сумма в рублях по курсу ЦБ
    if amount in (None, ""):
        return None
    party = txn.get("CreditorParty" if debit else "DebtorParty") or {}
    other = txn.get("CreditorAccount" if debit else "DebtorAccount") or {}
    other_acc = str(other.get("identification") or "") if other.get("schemeName") != "RU.CBR.PAN" else ""
    day = str(txn.get("documentProcessDate") or "")[:10]
    tax = txn.get("TaxFields") or {}
    doc = {"Номер": str(txn.get("documentNumber") or "")[:20], "Дата": day, "Сумма": f"{float(amount):.2f}",
           "_purpose": [str(txn.get("description") or "")],
           "СтатусСоставителя": str(tax.get("originatorStatus") or ""), "ПоказательКБК": str(tax.get("kbk") or "")}
    if debit:
        doc.update({"ПлательщикСчет": own_account, "ПолучательСчет": other_acc, "ДатаСписано": day,
                    "ПолучательИНН": str(party.get("inn") or ""), "Получатель1": str(party.get("name") or "")})
    else:
        doc.update({"ПолучательСчет": own_account, "ПлательщикСчет": other_acc, "ДатаПоступило": day,
                    "ПлательщикИНН": str(party.get("inn") or ""), "Плательщик1": str(party.get("name") or "")})
    doc["_card"] = txn.get("transactionTypeCode") == "Банковские карты"
    return doc
