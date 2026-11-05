"""Обмен с «Моим делом» по REST API (T-71, v1.70.107, ADR-37).

Адрес — https://restapi.moedelo.org, ключ компании — в заголовке md-api-key. Ключ выдаётся в «Моём деле»
(«Настройки → Интеграция с партнёрами»), один ключ открывает данные одной компании. Методы и поля — по
открытой документации API https://restapi.moedelo.org/docs (Swagger 2.0): контрагенты
/kontragents/api/v1/kontragent, акты покупок /accounting/api/v1/purchases/act (модель PurchasesActModel).

Коды ответа переводятся в MoedeloError с понятным текстом и видом ошибки (kind): auth — ключ не принят
(401), forbidden — ключ не открывает раздел (403), missing — метода или записи нет (404, 405), invalid —
«Моё дело» не приняло данные (400, 409, 422), rate — частые запросы (429), unavailable — сервер не отвечает
(5xx и сеть), bad — прочее. Ключ в журнал не пишется; текст ошибок сервиса обрезается, длинные строки
скрываются. Доверие TLS — системное хранилище плюс сертификаты Минцифры, как у банков (app/bank/http.py).
Тесты подставляют transport = httpx.MockTransport — данные только синтетические (ADR-14), в сеть не ходим.
"""
from __future__ import annotations

import re

import httpx

from ..bank import http as bank_http

BASE_URL = "https://restapi.moedelo.org"
DOCS_URL = "https://restapi.moedelo.org/docs"
TIMEOUT = 25.0

transport: httpx.BaseTransport | None = None      # тесты подставляют httpx.MockTransport (только синтетика)


class MoedeloError(Exception):
    """Ошибка обмена с «Моим делом». kind: auth | forbidden | missing | invalid | rate | unavailable | bad."""

    def __init__(self, message: str, kind: str = "bad", status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _client() -> httpx.Client:
    if transport is not None:
        return httpx.Client(transport=transport, timeout=TIMEOUT, base_url=BASE_URL)
    return httpx.Client(verify=bank_http.ssl_context(), timeout=TIMEOUT, base_url=BASE_URL)


def _texts(value: object, out: list[str], depth: int = 0) -> None:
    """Тексты ошибок из ответа: ProblemDetails (Title, Detail), Message, ValidationErrors и списки."""
    if depth > 3:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value[:10]:
            _texts(item, out, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in ("message", "title", "detail", "error", "errors", "validationerrors",
                                    "description") or depth:
                _texts(item, out, depth + 1)


def error_text(resp: httpx.Response) -> str:
    """Короткое описание ошибки «Моего дела» без служебных данных."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    parts: list[str] = []
    _texts(data, parts)
    text = "; ".join(p.strip() for p in dict.fromkeys(parts) if p and p.strip())
    return re.sub(r"[A-Za-z0-9_\-]{32,}", "…", text)[:240]           # длинные строки (ключи) не показываем


def request(method: str, path: str, key: str, *, params: dict | None = None, body: dict | None = None):
    """Запрос к «Моему делу»: разобранный JSON (словарь или список); пустой ответ — пустой словарь."""
    headers = {"Accept": "application/json", "md-api-key": key}
    try:
        with _client() as http:
            resp = http.request(method, path, headers=headers, params=params, json=body)
    except httpx.HTTPError as exc:
        raise MoedeloError("«Моё дело» не отвечает — проверьте интернет на сервере и повторите позже",
                           "unavailable") from exc
    code = resp.status_code
    if code == 401:
        raise MoedeloError("«Моё дело» не приняло ключ: он неверный или отозван. Скопируйте ключ заново "
                           "в «Моём деле»: «Настройки → Интеграция с партнёрами»", "auth", code)
    if code == 403:
        raise MoedeloError("Ключ принят, но не открывает этот раздел «Моего дела» — проверьте, что это ключ "
                           "нужной компании и что интеграция с партнёрами включена", "forbidden", code)
    if code in (404, 405):
        raise MoedeloError("«Моё дело» не нашло метод или запись", "missing", code)
    if code == 429:
        raise MoedeloError("«Моё дело» ограничило частоту запросов — повторите через минуту", "rate", code)
    if code >= 500:
        raise MoedeloError(f"«Моё дело» временно не отвечает (код {code}) — повторите позже", "unavailable", code)
    if code >= 400:
        detail = error_text(resp)
        raise MoedeloError("«Моё дело» не приняло данные" + (f": {detail}" if detail else f" (код {code})"),
                           "invalid", code)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise MoedeloError("«Моё дело» прислало ответ не в формате JSON", "bad", code) from exc
