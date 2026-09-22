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


class FnsClient:
    """Клиент «Открытого API проверки чека ККТ» (openapi.nalog.ru)."""

    def __init__(self, api_base: str | None = None, master_token: str | None = None,
                 client_app_id: str | None = None, timeout: int | None = None):
        self.api_base = (api_base or settings.FNS_API_BASE).rstrip("/")
        self.master_token = master_token or settings.FNS_MASTER_TOKEN
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
