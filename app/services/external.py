# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — получение полных данных чека из внешних источников
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: chek@ymaster.ru
#
# v1.70.70: источник ровно один — Открытые API ФНС (openapi.nalog.ru:8090).
# Сторонние сервисы проверки чеков (proverkacheka.com и подобные) и
# демонстрационный источник удалены из кода: чек проверяется только по
# официальному API ФНС, сверенному с комплектом «API 2.0.9 от 10.12.2024».
#
# МЕХАНИКА:
#   • обращения к ФНС строго последовательные (общий замок), пауза 2–7 с;
#   • при сетевых сбоях источник «остывает» (10 мин → 6 ч, растущая пауза);
#   • учитывается суточный лимит ФНС — 1000 запросов за 24 часа
#     (см. fns_quota.py); при исчерпании чек остаётся в очереди и
#     проверяется автоматически после сброса лимита;
#   • повторная проверка того же чека берётся из кэша (30 дней) и лимит
#     не расходует.
# ======================================================================
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings

# --- Параметры вежливости (требование ТЗ: пауза 2–7 секунд, без бана) ---
GAP_MIN_SECONDS = 2.0
GAP_MAX_SECONDS = 7.0
REQUEST_TIMEOUT = 20.0
COOLDOWN_BASE_SECONDS = 600.0        # первый «остывание» — 10 минут
COOLDOWN_MAX_SECONDS = 6 * 3600.0    # максимум 6 часов
MAX_SAME_PROVIDER_RETRIES = 2

log = logging.getLogger("ymaster")

@dataclass
class ExternalItem:
    name: str
    quantity: float = 1.0
    price: float = 0.0
    total: float = 0.0
    vat_rate: str = "none"
    vat_sum: float = 0.0


@dataclass
class ExternalResult:
    """Нормализованные данные чека от источника."""
    ok: bool
    source: str
    message: str = ""
    found: bool = False                 # чек найден в источнике
    date_time: datetime | None = None
    total_sum: float | None = None
    operation: int | None = None        # 1 приход / 2 возврат
    merchant_name: str = ""
    merchant_inn: str = ""
    merchant_address: str = ""
    cashier: str = ""
    cash_sum: float | None = None
    ecash_sum: float | None = None
    items: list[ExternalItem] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)   # v1.55.0: расширенные поля


# ==========================================================================
#  Парсинг ответов (защитный: формы ответов у сервисов различаются)
# ==========================================================================
def _lc(d: dict) -> dict:
    """Словарь с ключами в нижнем регистре (сервисы пишут totalSum/TotalSum/total_sum)."""
    return {str(k).lower(): v for k, v in d.items()}


def _find_receipt_dict(obj, depth: int = 0):
    """Рекурсивный поиск словаря чека: содержит 'items' (list) и сумму."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    low = _lc(obj)
    items = low.get("items")
    if isinstance(items, list) and items and any(isinstance(i, dict) for i in items):
        if any(k in low for k in ("totalsum", "total", "sum")):
            return obj
    for v in obj.values():
        hit = _find_receipt_dict(v, depth + 1)
        if hit is not None:
            return hit
    for v in obj.values():
        if isinstance(v, list):
            for el in v:
                hit = _find_receipt_dict(el, depth + 1)
                if hit is not None:
                    return hit
    return None


def _parse_operation(receipt: dict) -> int | None:
    op = receipt.get("operation")
    if op in (1, 2):
        return int(op)
    # v1.57.2: proverkacheka присылает «operationType» (парсеру отдаётся
    # словарь в нижнем регистре — ключ «operationtype»); раньше тип
    # приезда извлекался только из строки QR, из ответа терялся
    op = receipt.get("operationtype")
    if op in (1, 2):
        return int(op)
    t = str(receipt.get("type", "")).lower()
    if "refund" in t or "возврат" in t:
        return 2
    if t:
        return 1
    return None


def _parse_datetime(receipt: dict) -> datetime | None:
    raw = receipt.get("dateTime") or receipt.get("date_time") or receipt.get("dateTimeISO")
    if not raw:
        return None
    if isinstance(raw, (int, float)):            # unix timestamp
        try:
            return datetime.utcfromtimestamp(int(raw))
        except (ValueError, OSError, OverflowError):
            return None
    s = str(raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _money_divisor(receipt: dict, known_rub: float | None) -> float:
    """Определить делитель сумм (100 для копеек, 1 для рублей) ОДИН раз
    по итогу чека — и применить ко всем полям (позиции, наличные, НДС)."""
    low = _lc(receipt)
    raw_total = float(low.get("totalsum") or low.get("total") or 0)
    if known_rub and raw_total:
        if abs(raw_total / 100.0 - known_rub) <= max(1.0, known_rub * 0.5):
            return 100.0
        if abs(raw_total - known_rub) <= max(1.0, known_rub * 0.5):
            return 1.0
    return 100.0 if raw_total >= 100000 else 1.0


def parse_receipt_payload(data: dict, known_rub: float | None = None) -> ExternalResult:
    """Единый разбор ответа любого источника → ExternalResult."""
    receipt = _find_receipt_dict(data)
    if receipt is None:
        _keys = ", ".join(list(data.keys())[:10]) \
            if isinstance(data, dict) else type(data).__name__
        return ExternalResult(ok=True, source="", found=False,
                              message=("Источник ответил, но данных чека в ответе нет "
                                       f"(ключи ответа: {_keys or '—'})"),
                              raw=data if isinstance(data, dict) else {})

    div = _money_divisor(receipt, known_rub)
    money = lambda v: round(float(v or 0) / div, 2)   # noqa: E731
    low = _lc(receipt)

    def _g(*keys, default=None):
        """Поиск ключа по алиасам (в нижнем регистре)."""
        for k in keys:
            if low.get(k) not in (None, ""):
                return low[k]
        return default

    total = money(_g("totalsum", "total", default=0))
    dt_raw = _g("datetime", "date_time", "datetimeiso", "docdatetime")

    items: list[ExternalItem] = []
    for i, raw_item in enumerate(low.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        it = _lc(raw_item)
        name = str(it.get("name") or it.get("itemname") or it.get("nomination")
                   or f"Позиция {i + 1}")
        try:
            qty = float(it.get("quantity") or it.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1.0
        price = money(it.get("price") or it.get("itemprice"))
        item_total = money(it.get("sum") or it.get("itemsum") or it.get("total"))
        vat_rate = it.get("nds") if isinstance(it.get("nds"), (int, str)) else "none"
        if vat_rate in (0, "0"):
            vat_rate = "0"
        vat_sum = money(it.get("ndssum"))
        items.append(ExternalItem(name=name, quantity=qty, price=price,
                                  total=item_total, vat_rate=str(vat_rate), vat_sum=vat_sum))

    def _pdt(raw):
        if not raw:
            return None
        if isinstance(raw, (int, float)):
            try:
                return datetime.utcfromtimestamp(int(raw))
            except (ValueError, OSError, OverflowError):
                return None
        txt = str(raw)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%Y%m%dT%H%M"):
            try:
                return datetime.strptime(txt[:19], fmt)
            except ValueError:
                continue
        return None

    # v1.55.0: максимум полей из расширенных ответов (proverkacheka.com
    # /api/v1/check/get и совместимые): место расчётов, касса, смена,
    # налогообложение, НДС-итоги, свойства (номер заказа), регион, ОФД.
    meta = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
    meta = _lc(meta or {})
    udata = receipt.get("user_data") if isinstance(receipt.get("user_data"), dict) else {}
    props = []
    # v1.57.2: у источника «properties» бывает МАССИВОМ и ЕДИНИЧНЫМ
    # объектом — принимаем оба вида, иначе теряется «Номер заказа»
    _props_raw = receipt.get("properties")
    _props_iter = ([_props_raw] if isinstance(_props_raw, dict)
                   else (_props_raw or [] if isinstance(_props_raw, list) else []))
    for p_ in _props_iter:
        if isinstance(p_, dict) and p_.get("propertyName"):
            props.append({"name": str(p_["propertyName"])[:200],
                          "value": str(p_.get("propertyValue") or "")[:200]})
    nds_totals = []
    _ands = receipt.get("amountsReceiptNds")
    if isinstance(_ands, dict):
        for a_ in (_ands.get("amountsNds") or []):
            if isinstance(a_, dict):
                nds_totals.append({"nds": a_.get("nds"), "ndsSum": a_.get("ndsSum")})
    tax = _g("appliedtaxationtype")
    tax_names = {0: "ОСН", 1: "УСН (доходы)", 2: "УСН (доходы − расходы)",
                 4: "ЕНВД", 16: "НСПО", 32: "ПСН", 64: "ЭСХН", 128: "НПД",
                 256: "АУСН"}
    extra = {}
    for key, val in (
        ("retail_place", _g("retailplace")),
        ("kkt_reg_id", (str(_g("kktregid")).strip()
                        if isinstance(_g("kktregid"), str) else _g("kktregid"))),
        ("fiscal_drive_number", _g("fiscaldrivenumber")),
        ("fiscal_document_number", _g("fiscaldocumentnumber")),
        ("fiscal_sign", _g("fiscalsign")),
        ("shift_number", _g("shiftnumber")),
        ("request_number", _g("requestnumber")),
        ("operation_type", _g("operationtype")),
        ("number_kkt", _g("numberkkt")),
        ("region", _g("region")),
        ("nds0", _g("nds0")),
        ("taxation", tax_names.get(tax, tax) if tax is not None else None),
        ("prepaid_sum", money(_g("prepaidsum")) if _g("prepaidsum") else None),
        ("credit_sum", money(_g("creditsum")) if _g("creditsum") else None),
        ("provision_sum", money(_g("provisionsum")) if _g("provisionsum") else None),
        ("ofd_id", meta.get("ofdid")),
        ("receive_date", meta.get("receivedate")),
        ("doc_subtype", meta.get("subtype")),
        ("source_receipt_id", udata.get("id")),
        ("source_date_create", udata.get("date_create")),
        # v1.57.0: версия формата фискального документа (ФФД) и адрес
        # из metadata источника (бывает, когда в самом чеке адреса нет)
        ("ffd_version", _g("fiscaldocumentformatver")),
        ("source_address", meta.get("address")),
        # v1.57.2: контрольный знак сообщения, служебная маска и код
        # ответа источника — для полного соответствия формату API
        ("message_fiscal_sign", _g("messagefiscalsign")),
        ("redefine_mask", _g("redefine_mask")),
        ("source_code", receipt.get("code")),
    ):
        if val not in (None, "", [], {}):
            extra[key] = val
    if props:
        extra["properties"] = props
    if nds_totals:
        extra["nds_totals"] = nds_totals
    # v1.57.0: мета позиций — тип оплаты, тип товара и мера количества
    # (paymentType/productType/itemsQuantityMeasure из ответа proverkacheka)
    imeta = []
    for i_, raw_item in enumerate(low.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        it_ = _lc(raw_item)
        rec_ = {}
        for k_ in ("paymenttype", "producttype", "itemsquantitymeasure"):
            if it_.get(k_) not in (None, ""):
                rec_[k_] = it_.get(k_)
        if rec_:
            rec_["pos"] = i_ + 1
            imeta.append(rec_)
    if imeta:
        extra["items_meta"] = imeta

    return ExternalResult(
        ok=True,
        source="",
        found=True,
        message="Данные чека получены",
        date_time=_pdt(dt_raw) or _parse_datetime(receipt),
        total_sum=total,
        operation=_parse_operation(low),
        merchant_name=str(_g("user", "operatorname", "retailplace", default=""))[:500],
        merchant_inn=str(_g("userinn", "inn", default=""))[:20],
        merchant_address=str(_g("retailplaceaddress", "address", default=""))[:500],
        cashier=str(_g("operator", "cashier", default=""))[:200],
        cash_sum=money(_g("cashtotalsum")) if _g("cashtotalsum") else None,
        ecash_sum=money(_g("ecashtotalsum")) if _g("ecashtotalsum") else None,
        items=items,
        raw=receipt,
        extra=extra,
    )


# ==========================================================================
#  Отдельные провайдеры
# ==========================================================================
def fetch_fns(qr_raw: str, fn: str, fd: str, fp: str, total_rub: float,
              date_time: datetime,
              master_token: str = "") -> tuple[bool, str, dict]:
    """Проверка и получение чека в Открытых API ФНС (единственный путь).

    v1.70.70: метод GetTicket официального асинхронного сервиса ФНС отдаёт
    сразу и подтверждение существования чека, и полный документ (позиции,
    ИНН, адрес расчётов) — поэтому отдельного «проверочного» запроса нет.
    Возвращает (успех, сообщение, полезная нагрузка для разбора).
    """
    from .fns import FnsClient
    client = FnsClient(master_token=master_token or None)
    moment = date_time or datetime.now(timezone.utc).replace(tzinfo=None)
    result = client.get_ticket(fn, fd, fp, total_rub, moment)
    if not result.ok:
        # quota — не сбой источника, а исчерпанный лимит: чек в очередь
        return (False, result.message,
                {"provider": "fns", "kind": result.raw.get("kind", ""),
                 "status": result.status, "message": result.message})
    ticket = result.raw.get("ticket") or {}
    payload = {"provider": "fns", "status": result.status,
               "message": result.message, "ticket": ticket,
               "code": result.raw.get("code", "")}
    if result.status != "valid":
        return True, result.message, payload
    # Приводим документ ФНС к форме, которую понимает parse_receipt_payload:
    # в Ticket ФНС чек лежит в ключе receipt (или в корне для старых версий).
    document = ticket.get("receipt") if isinstance(ticket, dict) else None
    payload["document"] = {"receipt": document} if document else ticket
    return True, result.message, payload


# ==========================================================================
#  Движок загрузки: единственный источник — Открытые API ФНС
# ==========================================================================
class ExternalFetchEngine:
    """Загрузка данных чека из ФНС с защитой от лишних обращений.

    v1.70.70: сторонние провайдеры удалены — источник ровно один (ФНС).
    Поэтому «ротация источников» больше не нужна; остались:
      • последовательность запросов (общий замок) и пауза между ними;
      • «остывание» при сетевых сбоях, чтобы не долбить сервис;
      • учёт суточного лимита 1000 запросов (fns_quota): при исчерпании
        результат приходит со статусом quota — чек остаётся в очереди и
        проверяется автоматически после сброса лимита.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._fail_streak: dict[str, int] = {}

    # --- настройки из БД ---
    def _settings(self, db) -> dict:
        """Настройки ФНС из БД — ТОЛЬКО через расшифровку (v1.70.71).

        Раньше значения брались прямо из таблицы, а мастер-токен и AppId
        хранятся зашифрованными («enc:v1:…») — в ФНС уходила зашифрованная
        строка, и сервис отвечал отказом. Теперь читаем тем же способом,
        каким их сохраняет админка: appsettings.get_setting расшифровывает.
        """
        from . import appsettings
        rows = {key: appsettings.get_setting(db, key, "")
                for key in ("fns_master_token", "fns_client_app_id")}
        return rows

    def provider_chain(self, db) -> list[str]:
        """Источник всегда один — ФНС (оставлено для совместимости вызовов)."""
        return ["fns_api"]

    def is_available(self, provider: str = "fns_api") -> bool:
        return time.time() >= self._cooldown_until.get(provider, 0.0)

    def status(self) -> dict:
        now = time.time()
        from . import fns_quota
        cd = self._cooldown_until.get("fns_api", 0.0)
        return {"fns_api": {
            "available": now >= cd,
            "cooldown_sec": max(0, int(cd - now)),
            "fail_streak": self._fail_streak.get("fns_api", 0),
            "quota": fns_quota.stats(),
        }}

    def _polite_wait(self, provider: str) -> None:
        """Пауза между обращениями к ФНС (антибан: 2–7 с)."""
        last = self._last_call.get(provider, 0.0)
        gap = random.uniform(GAP_MIN_SECONDS, GAP_MAX_SECONDS)
        wait = last + gap - time.time()
        if wait > 0:
            log.info("⏳ %s: пауза %.1f с (антибан 2–7 с)…", provider, wait)
            time.sleep(min(wait, GAP_MAX_SECONDS))
        self._last_call[provider] = time.time()

    def _note_failure(self, message: str) -> None:
        streak = self._fail_streak.get("fns_api", 0) + 1
        self._fail_streak["fns_api"] = streak
        low = message.lower()
        if "429" in message or "403" in message or "блокир" in low \
                or "сеть недоступна" in low:
            cooldown = min(COOLDOWN_BASE_SECONDS * (2 ** (streak - 1)),
                           COOLDOWN_MAX_SECONDS)
            self._cooldown_until["fns_api"] = time.time() + cooldown

    def fetch(self, db, qr_raw: str, fn: str, fd: str, fp: str,
              total_rub: float, date_time: datetime | None) -> ExternalResult:
        """Получить данные чека из ФНС (единственный источник)."""
        known_total = total_rub or None
        log.info("🔍 ДАННЫЕ ЧЕКА [fn=%s i=%s fp=%s]: старт загрузки из ФНС "
                 "(сумма=%s руб., дата=%s)", fn, fd, fp, total_rub, date_time)

        if not self.is_available():
            left = int(self._cooldown_until["fns_api"] - time.time())
            return ExternalResult(
                ok=False, source="fns_api", found=False,
                message=f"Сервис ФНС временно недоступен, повтор через "
                        f"{left} с")

        cfg = self._settings(db)
        token = cfg.get("fns_master_token") or settings.FNS_MASTER_TOKEN
        if not token:
            log.warning("🔑 Мастер-токен ФНС не задан — проверка невозможна")
            return ExternalResult(
                ok=False, source="fns_api", found=False,
                message="Не задан Мастер-токен ФНС: укажите его в "
                        "«Настройки → Проверка чеков (ФНС)»")
        log.info("🔑 Мастер-токен ФНС: есть (…%s)", token[-4:])

        with self._lock:                      # строго последовательно
            self._polite_wait("fns_api")
            try:
                moment = date_time or datetime.now(timezone.utc).replace(
                    tzinfo=None)
                net_ok, msg, data = fetch_fns(qr_raw, fn, fd, fp, total_rub,
                                              moment, token)
            except Exception as e:            # непредвиденное — как сбой
                net_ok, msg, data = False, f"Ошибка: {e}", {}

            if not net_ok:
                if data.get("kind") == "quota":
                    # лимит — не сбой сервиса: чек ждёт сброса лимита
                    log.warning("⏸ ДАННЫЕ НЕ ПОЛУЧЕНЫ [fn=%s]: %s", fn, msg)
                    return ExternalResult(ok=False, source="fns_api",
                                          found=False, message=msg,
                                          raw={"quota": True})
                self._note_failure(msg)
                log.warning("❌ ФНС: сбой: %s", msg)
                return ExternalResult(ok=False, source="fns_api", found=False,
                                      message=msg if msg.startswith("ФНС")
                                      else f"ФНС: {msg}")

            self._fail_streak["fns_api"] = 0
            if data.get("status") in ("not_found", "invalid"):
                log.info("ℹ️ ФНС: чек не подтверждён (%s)", msg)
                return ExternalResult(ok=True, source="fns_api", found=False,
                                      message=msg, raw=data)

            parsed = parse_receipt_payload(data, known_total)
            if not parsed.found:
                log.warning("⚠️ ФНС: чек подтверждён, но позиций в ответе "
                            "нет (%s)", parsed.message)
                parsed.ok = True
                parsed.source = "fns_api"
                parsed.message = msg
                parsed.raw["engine"] = {"errors": [], "attempts": 1}
                return parsed

            parsed.source = "fns_api"
            parsed.found = True
            parsed.message = msg
            parsed.raw["engine"] = {"errors": [], "attempts": 1}
            log.info("✅ ФНС: ДАННЫЕ ЧЕКА ПОЛУЧЕНЫ (%s)", msg)
            return parsed


# Глобальный движок (единственный экземпляр на процесс — ровные паузы)
engine = ExternalFetchEngine()
