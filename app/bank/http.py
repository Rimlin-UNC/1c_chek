"""Общий обмен с банками по API (v1.70.102, ADR-32): ошибки, TLS и сертификат клиента.

Все банки, кроме Точки (у неё свой клиент app/bank/tochka.py с 1.70.99), ходят через request():
  * доверие — системное хранилище плюс сертификаты Минцифры (app/certs/russian_trusted_ca.pem);
  * сертификат клиента (взаимный TLS — Сбер, Альфа-Банк) подключается к SSL-контексту запроса.
    Python читает сертификат и ключ только из файла, поэтому PEM пишется во временный файл с правами 0600
    и удаляется сразу после загрузки в контекст; в памяти остаётся только SSL-контекст;
  * коды ответа банка переводятся в BankApiError с понятным текстом и видом ошибки (kind): auth — доступ
    истёк или отозван, forbidden — нет прав на выписки, rate — частые запросы, unavailable — банк не
    отвечает, tls — не прошла проверка сертификата, bad — прочее.

Повтора «без проверки сертификата» нет и не будет: по этому каналу идут ключи и банковские данные.
Ключи в журнал не пишутся; текст ошибок банка обрезается, длинные строки (токены) скрываются.
Тесты подставляют transport = httpx.MockTransport — данные только синтетические (ADR-14), в сеть не ходим.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import tempfile
import time
from collections import OrderedDict

import httpx

log = logging.getLogger("ymaster.bank.http")

TIMEOUT = 25.0
_CA_RU = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs",
                      "russian_trusted_ca.pem")
_CTX_CACHE: OrderedDict[str, ssl.SSLContext] = OrderedDict()
_CTX_MAX = 16

transport: httpx.BaseTransport | None = None      # тесты подставляют httpx.MockTransport (только синтетика)
sleep = time.sleep                                # тесты подменяют, чтобы не ждать


class BankApiError(Exception):
    """Ошибка обмена с банком. kind: auth | forbidden | rate | unavailable | tls | bad."""

    def __init__(self, message: str, kind: str = "bad", status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _load_client_cert(ctx: ssl.SSLContext, pem: str) -> None:
    fd, path = tempfile.mkstemp(prefix="ymbank-", suffix=".pem")     # права 0600 — только процесс программы
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(pem)
        ctx.load_cert_chain(path)
    finally:
        os.unlink(path)


def ssl_context(cert_pem: str = "") -> ssl.SSLContext:
    """Системное хранилище + сертификаты Минцифры; при необходимости — сертификат клиента (взаимный TLS)."""
    key = hashlib.sha256(cert_pem.encode()).hexdigest() if cert_pem else "-"
    ctx = _CTX_CACHE.get(key)
    if ctx is not None:
        _CTX_CACHE.move_to_end(key)
        return ctx
    ctx = ssl.create_default_context()
    if os.path.exists(_CA_RU):
        ctx.load_verify_locations(cafile=_CA_RU)
    else:
        log.warning("банк: нет %s — сертификаты Минцифры не подключены", _CA_RU)
    if cert_pem:
        try:
            _load_client_cert(ctx, cert_pem)
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise BankApiError("Сертификат клиента не читается — загрузите его заново", "tls") from exc
    _CTX_CACHE[key] = ctx
    while len(_CTX_CACHE) > _CTX_MAX:
        _CTX_CACHE.popitem(last=False)
    return ctx


def client(cert_pem: str = "") -> httpx.Client:
    if transport is not None:
        return httpx.Client(transport=transport, timeout=TIMEOUT)
    return httpx.Client(verify=ssl_context(cert_pem), timeout=TIMEOUT)


def error_text(resp: httpx.Response) -> str:
    """Короткое описание ошибки банка без служебных данных (форматы Сбера, Альфы, Т-Банка, Модульбанка)."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [str(data.get(k) or "") for k in ("error_description", "errorMessage", "message", "error", "cause")]
    for key in ("Errors", "errors", "checks"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                parts.append(str(item.get("message") or item.get("errorMessage") or item.get("errorCode") or ""))
    text = "; ".join(p for p in dict.fromkeys(parts) if p and not isinstance(p, (dict, list)))
    return re.sub(r"[A-Za-z0-9_\-]{40,}", "…", text)[:200]           # длинные строки (токены) не показываем


def request(method: str, url: str, *, bank: str, auth: str = "", params: dict | None = None,
            form: dict | None = None, body: dict | None = None, cert_pem: str = "", accept: str = "application/json",
            token_endpoint: bool = False, empty_404: bool = False, headers: dict | None = None):
    """Запрос к банку. auth — готовое значение заголовка Authorization («Bearer …», «ApiKey …», «Basic …»).
    Ответ: разобранный JSON (словарь или список) либо текст, если accept — не JSON. empty_404 — «нет данных»."""
    hdrs = {"Accept": accept, **(headers or {})}
    if auth:
        hdrs["Authorization"] = auth
    try:
        with client(cert_pem) as http:
            resp = http.request(method, url, headers=hdrs, params=params, data=form, json=body)
    except BankApiError:
        raise
    except httpx.HTTPError as exc:
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text:
            raise BankApiError(f"Не удалось проверить сертификат сервера банка ({bank}). Обновите программу — "
                               "сертификаты Минцифры идут в её составе", "tls") from exc
        if "certificate" in text.lower() or "handshake" in text.lower() or "SSL" in text:
            raise BankApiError(f"{bank}: банк не принял защищённое соединение — проверьте сертификат клиента "
                               "(для API банка он обязателен)", "tls") from exc
        raise BankApiError(f"{bank}: банк недоступен — повторим позже", "unavailable") from exc
    code = resp.status_code
    if token_endpoint and code in (400, 401):
        detail = error_text(resp)
        raise BankApiError(f"{bank}: банк не принял данные входа: " + (detail or f"код {code}") +
                           ". Проверьте ключи или подключите банк заново", "auth", code)
    if code == 401:
        raise BankApiError(f"{bank}: банк не принял доступ — срок действия истёк или ключ отозван", "auth", code)
    if code == 403:
        raise BankApiError(f"{bank}: банк отказал в доступе — у ключа нет прав на счета и выписки. Выпустите "
                           "ключ с доступом к выпискам и подключите банк заново", "forbidden", code)
    if code == 404 and empty_404:
        return None
    if code == 429:
        raise BankApiError(f"{bank}: банк ограничил частоту запросов — повторим позже", "rate", code)
    if code >= 500:
        raise BankApiError(f"{bank}: банк временно не отвечает (код {code}) — повторим позже", "unavailable", code)
    if code >= 400:
        detail = error_text(resp)
        raise BankApiError(f"{bank}: банк вернул ошибку {code}" + (f": {detail}" if detail else ""), "bad", code)
    if "json" not in accept:
        return resp.text
    if not resp.content:
        return {}
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise BankApiError(f"{bank}: банк прислал ответ не в формате JSON", "bad", code) from exc


def digits(value: object, size: int = 0) -> str:
    out = "".join(ch for ch in str(value or "") if ch.isdigit())
    return out[:size] if size else out


CURRENCIES = {"643": "RUB", "810": "RUB", "RUR": "RUB", "RUB": "RUB", "840": "USD", "978": "EUR", "156": "CNY",
              "398": "KZT", "933": "BYN", "949": "TRY", "784": "AED", "356": "INR"}


def currency(value: object, account: str = "") -> str:
    """Буквенный код валюты: из поля банка («643», «RUR», «USD») или из 6–8 цифр номера счёта."""
    raw = str(value or "").strip().upper()
    if raw:
        return CURRENCIES.get(raw, raw[:3])
    code = digits(account)[5:8]
    return CURRENCIES.get(code, "RUB" if not code else code)
