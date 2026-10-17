# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Модуль проверки подлинности чеков через API ФНС.

Реализованы два провайдера (переключаются в веб-интерфейсе или .env):
  * mock — детерминированная имитация проверки для демо/разработки;
  * fns  — официальное «Открытое API проверки чека ККТ» (openapi.nalog.ru):
      1) Получение временного токена: SOAP-операция GetMessage
         (AuthRequest → AuthAppInfo → MasterToken + AppId);
      2) Проверка чека: POST /open-api/api/2/check (JSON, заголовок token).

Для подключения реального API необходимо направить заявку в ФНС и получить
Мастер-токен (см. docs/knowledge/fns_auth.md).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..config import settings


@dataclass
class FnsResult:
    """Результат проверки чека."""
    status: str          # valid | invalid | not_found | unknown
    message: str
    raw: dict            # ответ провайдера (для raw_data и аудита)
    ok: bool             # технический успех обращения


# ==========================================================================
#  Провайдер MOCK — детерминированная имитация (демо-режим)
# ==========================================================================
def check_mock(fn: str, fd: str, fp: str, total_sum: float,
               receipt_date: datetime) -> FnsResult:
    """
    Имитация ответа ФНС: результат стабилен для одной тройки ФН+ФД+ФП,
    ~85% чеков «найдены и корректны».
    """
    h = int(hashlib.sha256(f"{fn}|{fd}|{fp}".encode()).hexdigest(), 16)
    bucket = h % 100
    time.sleep(0.15)  # имитация сетевой задержки
    if bucket < 85:
        return FnsResult(
            status="valid",
            message="Чек найден в ФИАС ФНС, реквизиты корректны (демо-проверка)",
            raw={"provider": "mock", "code": 0, "found": True},
            ok=True,
        )
    if bucket < 93:
        return FnsResult(
            status="not_found",
            message="Чек не найден: возможно, данные ещё не поступили от ОФД (демо)",
            raw={"provider": "mock", "code": 1, "found": False},
            ok=True,
        )
    return FnsResult(
        status="invalid",
        message="Контрольная сумма ФП не совпадает — чек недействителен (демо)",
        raw={"provider": "mock", "code": 2, "found": True, "valid": False},
        ok=True,
    )


# ==========================================================================
#  Провайдер FNS — реальное API ФНС
# ==========================================================================
_SOAP_AUTH_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:msg="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0"
               xmlns:auth="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">
  <soap:Body>
    <msg:GetMessageRequest>
      <msg:Message>
        <auth:AuthRequest>
          <auth:AuthAppInfo>
            <auth:MasterToken>{master_token}</auth:MasterToken>
          </auth:AuthAppInfo>
        </auth:AuthRequest>
      </msg:Message>
    </msg:GetMessageRequest>
  </soap:Body>
</soap:Envelope>"""


# ==========================================================================
#  v1.27.0: Получение ПОЛНЫХ данных чека (GetTicket) — SOAP-сервис KKT.
#  Протокол (по образцу QortexDevs/fnsapi, официальная документация ФНС):
#    1) сессионный токен из AuthService (как выше) → заголовок
#       FNS-OpenApi-Token (+ FNS-OpenApi-UserToken — base64 user_id);
#    2) SendMessage(GetTicketRequest) → MessageId;
#    3) опрос GetMessage(MessageId) до ProcessingStatus=COMPLETED;
#    4) в Result.Message — JSON-СТРОКА с полным чеком (позиции, ИНН…).
# ==========================================================================
_SOAP_KKT_SEND_TEMPLATE = """<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <msg:SendMessageRequest xmlns:msg="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0">
      <msg:Message>
        <kkt:{request_type}TicketRequest xmlns:kkt="urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0">
          <kkt:{request_type}TicketInfo>
            <kkt:Sum>{sum}</kkt:Sum>
            <kkt:Date>{date}</kkt:Date>
            <kkt:Fn>{fn}</kkt:Fn>
            <kkt:TypeOperation>{operation}</kkt:TypeOperation>
            <kkt:FiscalDocumentId>{fd}</kkt:FiscalDocumentId>
            <kkt:FiscalSign>{fp}</kkt:FiscalSign>
          </kkt:{request_type}TicketInfo>
        </kkt:{request_type}TicketRequest>
      </msg:Message>
    </msg:SendMessageRequest>
  </soap:Body>
</soap:Envelope>"""

_SOAP_KKT_GET_TEMPLATE = """<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <msg:GetMessageRequest xmlns:msg="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0">
      <msg:MessageId>{message_id}</msg:MessageId>
    </msg:GetMessageRequest>
  </soap:Body>
</soap:Envelope>"""


def ET_tostring_compat(el) -> str:
    """ET.tostring с защитой от отсутствия кодировки по умолчанию."""
    import xml.etree.ElementTree as ET
    return ET.tostring(el, encoding="unicode")


def _xml_findtext_ns(xml_text: str, local_name: str) -> str | None:
    """Текст ПЕРВОГО элемента с заданным ЛОКАЛЬНЫМ именем (без NS)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit('}', 1)[-1] == local_name and (el.text or '').strip():
            return el.text.strip()
    return None


def _xml_find_element_ns(xml_text: str, local_name: str):
    """Первый элемент с заданным ЛОКАЛЬНЫМ именем (без NS) или None."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit('}', 1)[-1] == local_name:
            return el
    return None


class FnsClient:
    """Клиент «Открытого API проверки чека ККТ» (openapi.nalog.ru)."""

    def __init__(self, api_base: str | None = None, master_token: str | None = None,
                 client_app_id: str | None = None, timeout: int | None = None):
        self.api_base = (api_base or settings.FNS_API_BASE).rstrip("/")
        # Приоритет: явный аргумент → настройка из админки (БД) → переменная окружения
        self.master_token = master_token or _db_master_token() or settings.FNS_MASTER_TOKEN
        self.client_app_id = client_app_id or settings.FNS_CLIENT_APP_ID
        self.timeout = timeout or settings.FNS_TIMEOUT_SECONDS
        self._temp_token: str | None = None
        self._temp_token_issued_at: float = 0.0

    # --- Временный токен (SOAP) ----------------------------------------
    def _get_temp_token(self) -> str:
        # Кэш токена на 6 часов
        if self._temp_token and (time.time() - self._temp_token_issued_at) < 6 * 3600:
            return self._temp_token
        url = f"{self.api_base}/open-api/AuthService/0.1"
        body = _SOAP_AUTH_TEMPLATE.format(master_token=self.master_token)
        resp = httpx.post(
            url, content=body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8",
                     "ClientAppId": self.client_app_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        text = resp.text
        # Извлекаем <Token>...</Token> из SOAP-ответа
        start = text.find("<Token>")
        end = text.find("</Token>")
        if start == -1 or end == -1:
            raise RuntimeError(
                f"ФНС: не удалось получить временный токен. Ответ: {text[:300]}"
            )
        self._temp_token = text[start + 7:end].strip()
        self._temp_token_issued_at = time.time()
        return self._temp_token

    # --- Полные данные чека (GetTicket, v1.27.0) -------------------------
    def get_ticket(self, fn: str, fd: str, fp: str, total_sum: float,
                   receipt_date: datetime, operation: int = 1,
                   user_id: str = "ymaster-check") -> FnsResult:
        """
        Официальный метод GetTicket (SOAP KktService/0.1): ФНС возвращает
        ПОЛНЫЙ чек — позиции, ИНН, адрес и т.д. Сумма — в КОПЕЙКАХ,
        дата — %Y-%m-%dT%H:%M:%S (протокол fnsapi/документация ФНС).
        """
        if not self.master_token:
            return FnsResult("unknown", "Не задан Мастер-токен ФНС",
                             {"provider": "fns", "error": "no_master_token"}, False)
        url = f"{self.api_base}/open-api/ais3/KktService/0.1"
        send_body = _SOAP_KKT_SEND_TEMPLATE.format(
            request_type="Get", sum=int(round((total_sum or 0) * 100)),
            date=(receipt_date or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%S"),
            fn=fn, operation=1 if operation != 2 else 2, fd=fd, fp=fp)
        user_token = base64.b64encode(user_id.encode("ascii")).decode("ascii")

        def _headers(tok: str) -> dict:
            return {"Content-Type": "text/xml; charset=utf-8",
                    "FNS-OpenApi-Token": tok,
                    "FNS-OpenApi-UserToken": user_token,
                    "ClientAppId": self.client_app_id}

        try:
            token = self._get_temp_token()
            resp = httpx.post(url, content=send_body.encode("utf-8"),
                              headers=_headers(token), timeout=self.timeout)
            if resp.status_code in (401, 403):            # токен протух
                self._temp_token = None
                token = self._get_temp_token()
                resp = httpx.post(url, content=send_body.encode("utf-8"),
                                  headers=_headers(token), timeout=self.timeout)
            if resp.status_code != 200:
                return FnsResult("unknown", f"ФНС GetTicket: HTTP {resp.status_code}",
                                 {"provider": "fns"}, False)
            message_id = _xml_findtext_ns(resp.text, "MessageId")
            if not message_id:
                return FnsResult("unknown", "ФНС GetTicket: нет MessageId в ответе",
                                 {"provider": "fns", "raw": resp.text[:300]}, False)
            # Опрос результата: до 15 попыток с паузой 2 с (как в fnsapi)
            get_body_tpl = _SOAP_KKT_GET_TEMPLATE
            for _ in range(15):
                time.sleep(2)
                poll = httpx.post(url, content=get_body_tpl.format(
                    message_id=message_id).encode("utf-8"),
                    headers=_headers(token), timeout=self.timeout)
                if poll.status_code != 200:
                    return FnsResult("unknown",
                                     f"ФНС GetTicket: HTTP {poll.status_code} при опросе",
                                     {"provider": "fns"}, False)
                status = _xml_findtext_ns(poll.text, "ProcessingStatus") or ""
                if status != "COMPLETED":
                    continue
                result_el = _xml_find_element_ns(poll.text, "Result")
                fault_el = _xml_find_element_ns(poll.text, "Fault")
                if fault_el is not None:
                    msg = (fault_el[0].text or "ошибка ФНС") if len(fault_el) else "ошибка ФНС"
                    return FnsResult("not_found", f"ФНС GetTicket: {msg}",
                                     {"provider": "fns"}, True)
                if result_el is None:
                    return FnsResult("unknown", "ФНС GetTicket: нет Result в ответе",
                                     {"provider": "fns", "raw": poll.text[:300]}, False)
                code = _xml_findtext_ns(ET_tostring_compat(result_el), "Code")
                msg_text = _xml_findtext_ns(ET_tostring_compat(result_el), "Message")
                if str(code) != "0":
                    return FnsResult("not_found",
                                     f"ФНС GetTicket: код {code}, {msg_text or 'без описания'}",
                                     {"provider": "fns"}, True)
                # Message — JSON-строка с полным чеком
                ticket = json.loads(msg_text) if msg_text else {}
                return FnsResult("valid", "Полные данные чека получены из ФНС",
                                 {"provider": "fns", "ticket": ticket}, True)
            return FnsResult("unknown", "ФНС GetTicket: таймаут ожидания результата",
                             {"provider": "fns"}, False)
        except httpx.HTTPError as e:
            return FnsResult("unknown", f"Сеть при GetTicket: {e.__class__.__name__}",
                             {"provider": "fns"}, False)
        except (ValueError, KeyError) as e:
            return FnsResult("unknown", f"Разбор ответа GetTicket: {e}",
                             {"provider": "fns"}, False)

    # --- Проверка чека ---------------------------------------------------

    def check_receipt(self, fn: str, fd: str, fp: str, total_sum: float,
                      receipt_date: datetime) -> FnsResult:
        if not self.master_token:
            return FnsResult(
                status="unknown",
                message="Не задан Мастер-токен ФНС. Укажите его в настройках или "
                        "переключите провайдера на «mock».",
                raw={"provider": "fns", "error": "no_master_token"},
                ok=False,
            )
        url = f"{self.api_base}/open-api/api/2/check"
        payload = [{
            "date": receipt_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "sum": int(round(total_sum * 100)),
            "fn": fn,
            "fd": fd,
            "fp": fp,
            "type": "income" if total_sum >= 0 else "incomeRefund",
        }]
        try:
            token = self._get_temp_token()
            resp = httpx.post(
                url, json=payload,
                headers={"token": token, "ClientAppId": self.client_app_id},
                timeout=self.timeout,
            )
            # Токен мог протухнуть — обновляем и пробуем ещё раз
            if resp.status_code in (401, 403):
                self._temp_token = None
                token = self._get_temp_token()
                resp = httpx.post(
                    url, json=payload,
                    headers={"token": token, "ClientAppId": self.client_app_id},
                    timeout=self.timeout,
                )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") \
                else {"raw": resp.text[:500]}
            if resp.status_code == 200:
                code = data.get("Code", 0)
                if code == 0:
                    return FnsResult("valid", "Чек найден в базе ФНС, ФП совпадает",
                                     {"provider": "fns", **data}, True)
                return FnsResult("not_found", f"ФНС: {data.get('Message', 'чек не найден')}",
                                 {"provider": "fns", **data}, True)
            return FnsResult("unknown", f"ФНС API вернул HTTP {resp.status_code}",
                             {"provider": "fns", **data}, False)
        except httpx.HTTPError as e:
            return FnsResult("unknown", f"Ошибка сети при обращении к ФНС: {e}",
                             {"provider": "fns", "error": str(e)}, False)
        except Exception as e:
            return FnsResult("unknown", f"Непредвиденная ошибка проверки: {e}",
                             {"provider": "fns", "error": str(e)}, False)


# ==========================================================================
#  Фабрика провайдера + кэш результатов
# ==========================================================================
def _db_master_token() -> str:
    """Мастер-токен ФНС, сохранённый администратором в настройках (v1.2.0)."""
    try:
        from ..models import AppSetting
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            from .appsettings import get_setting
            return get_setting(db, "fns_master_token", "")
        finally:
            db.close()
    except Exception:
        return ""


def get_provider() -> str:
    from ..models import AppSetting
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        row = db.get(AppSetting, "fns_provider")
        return (row.value if row and row.value else settings.FNS_PROVIDER)
    finally:
        db.close()


def check_receipt(fn: str, fd: str, fp: str, total_sum: float,
                  receipt_date: datetime, cached_result: str | None = None,
                  cached_at: datetime | None = None) -> FnsResult:
    """
    Проверка чека с учётом кэша: свежий успешный результат (в пределах
    FNS_CACHE_TTL_DAYS) не отправляется повторно — экономия лимитов API.
    """
    provider = get_provider()
    ttl_ok = (
        cached_result in ("valid", "invalid", "not_found")
        and cached_at is not None
        and (datetime.utcnow() - cached_at).days < settings.FNS_CACHE_TTL_DAYS
    )
    if ttl_ok:
        return FnsResult(cached_result, "Результат из кэша (повторная проверка не требовалась)",
                         {"provider": provider, "cached": True}, True)

    if provider == "fns":
        client = FnsClient()
        return client.check_receipt(fn, fd, fp, total_sum, receipt_date)
    return check_mock(fn, fd, fp, total_sum, receipt_date)
