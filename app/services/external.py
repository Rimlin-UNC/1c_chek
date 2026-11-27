# ======================================================================
# Ямастер Чек — получение полных данных чека из внешних источников
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: chek@ymaster.ru
#
# v1.70.70+: источник ровно один — Открытые API ФНС (openapi.nalog.ru:8090).
# Сторонние сервисы проверки чеков (proverkacheka.com и подобные) и
# демонстрационный источник удалены из кода: чек проверяется только по
# официальному API ФНС, сверенному с комплектом «API 2.0.9 от 10.12.2024».
#
# МЕХАНИКА:
#   • сначала проверяется локальная БД (receipts, unified_documents,
#     pool_receipts, fns_log) — 0 запросов и 0 мс паузы на уже известный чек;
#   • обращения к ФНС последовательные (общий замок), пауза 2–7 с;
#   • при сетевых сбоях источник «остывает» (10 мин → 6 ч, растущая пауза);
#   • учитывается суточный лимит ФНС — 1000 запросов за 24 часа
#     (см. fns_quota.py); при исчерпании чек остаётся в очереди и
#     проверяется автоматически после сброса лимита;
#   • из одного ответа GetTicketResponse (RawData=true) извлекаются ВСЕ
#     реквизиты ФФД 1.05/1.1/1.2.
# ======================================================================
from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings

# --- Параметры вежливости (требование ТЗ: пауза 2–7 секунд, без бана) ---
GAP_MIN_SECONDS = 2.0
GAP_MAX_SECONDS = 7.0
REQUEST_TIMEOUT = 20.0
COOLDOWN_BASE_SECONDS = 600.0        # первое «остывание» — 10 минут
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
    operation: int | None = None        # 1..4 по тегу 1054 ФФД
    merchant_name: str = ""
    merchant_inn: str = ""
    merchant_address: str = ""
    cashier: str = ""
    cash_sum: float | None = None
    ecash_sum: float | None = None
    items: list[ExternalItem] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)   # v1.55.0+: расширенные поля ФФД


# ==========================================================================
#  Парсинг ответов (защитный: формы ответов различаются по версиям ФФД)
# ==========================================================================
def _lc(d: dict) -> dict:
    """Словарь с ключами в нижнем регистре (totalSum/TotalSum/total_sum)."""
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
    if op in (1, 2, 3, 4):
        return int(op)
    op = receipt.get("operationtype")
    if op in (1, 2, 3, 4):
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
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(
                int(raw), timezone.utc).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(s[:19], fmt)        # noqa: DTZ007
        except ValueError:
            continue
    return None


def _money_divisor(receipt: dict, known_rub: float | None) -> float:
    """Определить делитель сумм (100 для копеек, 1 для рублей) по итогу чека."""
    low = _lc(receipt)
    raw_total = float(low.get("totalsum") or low.get("total") or 0)
    if known_rub and raw_total:
        if abs(raw_total / 100.0 - known_rub) <= max(1.0, known_rub * 0.5):
            return 100.0
        if abs(raw_total - known_rub) <= max(1.0, known_rub * 0.5):
            return 1.0
    return 100.0 if raw_total >= 100000 else 1.0


_FLAT_NDS_HEADER_KEYS: tuple[tuple[str, str], ...] = (
    ("nds0", "0"),
    ("nds5", "5"),
    ("nds7", "7"),
    ("nds10", "10"),
    ("nds18", "18"),
    ("nds20", "20"),
    ("nds22", "22"),
    ("nds5105", "5/105"),
    ("nds7107", "7/107"),
    ("nds10110", "10/110"),
    ("nds18118", "18/118"),
    ("nds20120", "20/120"),
    ("nds22122", "22/122"),
    ("ndsno", "none"),
)


def parse_receipt_payload(data: dict, known_rub: float | None = None) -> ExternalResult:
    """Единый разбор ответа источника → ExternalResult со всеми тегами ФФД."""
    from . import fns_refs, receipt_card

    receipt = _find_receipt_dict(data)
    if receipt is None:
        _keys = ", ".join(list(data.keys())[:10]) \
            if isinstance(data, dict) else type(data).__name__
        return ExternalResult(
            ok=True, source="", found=False,
            message=("Источник ответил, но данных чека в ответе нет "
                     f"(ключи ответа: {_keys or '—'})"),
            raw=data if isinstance(data, dict) else {},
        )

    outer_ticket = (data.get("ticket")
                    if isinstance(data, dict) and isinstance(data.get("ticket"), dict)
                    else {})
    outer_low = _lc(outer_ticket) if outer_ticket else {}

    div = _money_divisor(receipt, known_rub)
    money = lambda v: round(float(v or 0) / div, 2)      # noqa: E731
    low = _lc(receipt)

    def _g(*keys, default=None):
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
        vat_sum = money(it.get("ndssum") or it.get("ndssumm"))
        items.append(ExternalItem(name=name, quantity=qty, price=price,
                                  total=item_total, vat_rate=str(vat_rate)[:10],
                                  vat_sum=vat_sum))

    def _pdt(raw):
        if not raw:
            return None
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(
                    int(raw), timezone.utc).replace(tzinfo=None)
            except (ValueError, OSError, OverflowError):
                return None
        txt = str(raw)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%Y%m%dT%H%M"):
            try:
                return datetime.strptime(txt[:19], fmt)   # noqa: DTZ007
            except ValueError:
                continue
        return None

    meta = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
    meta = _lc(meta or {})
    udata = receipt.get("user_data") if isinstance(receipt.get("user_data"), dict) else {}

    # Поддержка и {propertyName, propertyValue}, и {key, value} из Ticket ФНС,
    # а также propertiesUser (тег 1084) и propertiesData (тег 1192)
    props = receipt_card._extract_props_list(
        receipt.get("properties"),
        receipt.get("propertiesUser") or low.get("propertiesuser"),
    )
    props_data = str(_g("propertiesdata") or "")[:200]
    if props_data and not any(p["value"] == props_data for p in props):
        props.append({"name": "Доп. реквизит чека (1192)", "value": props_data})

    nds_totals = []
    seen_nds: set[str] = set()
    _ands = receipt.get("amountsReceiptNds") or low.get("amountsreceiptnds")
    if isinstance(_ands, dict):
        for a_ in (_ands.get("amountsNds") or _lc(_ands).get("amountsnds") or []):
            if isinstance(a_, dict):
                raw_n = a_.get("nds")
                nds_totals.append({"nds": raw_n, "ndsSum": a_.get("ndsSum")})
                seen_nds.add(fns_refs.vat_canonical(raw_n, coded=True))
    for flat_key, canon_rate in _FLAT_NDS_HEADER_KEYS:
        flat_val = low.get(flat_key)
        if flat_val not in (None, "", 0) and canon_rate not in seen_nds:
            nds_totals.append({"nds": canon_rate, "ndsSum": money(flat_val)})
            seen_nds.add(canon_rate)

    tax = _g("appliedtaxationtype", "taxationtype")
    tax_human = fns_refs.taxation_name(tax) if tax is not None else None

    ofd_id = (outer_low.get("ofdid") or outer_low.get("ofd")
              or meta.get("ofdid"))
    ticket_id = outer_low.get("id")
    ticket_kind = outer_low.get("kind") or meta.get("subtype")

    extra = {}
    for key, val in (
        ("retail_place", _g("retailplace", "retailplacename")),
        ("seller_kpp", _g("userkpp", "kpp")),
        ("seller_email", _g("selleraddress")),
        ("sender_address", _g("senderaddress")),
        ("fns_site", _g("fnssite")),
        ("cashier_inn", _g("operatorinn")),
        ("buyer_inn", _g("buyerinn")),
        ("kkt_reg_id", (str(_g("kktregid")).strip()
                        if isinstance(_g("kktregid"), str) else _g("kktregid"))),
        ("fiscal_drive_number", _g("fiscaldrivenumber")),
        ("fiscal_document_number", _g("fiscaldocumentnumber")),
        ("fiscal_sign", _g("fiscalsign")),
        ("shift_number", _g("shiftnumber")),
        ("request_number", _g("requestnumber")),
        ("operation_type", _g("operationtype")),
        ("number_kkt", _g("numberkkt")),
        ("machine_number", _g("machinenumber")),
        ("internet_sign", _g("internetsign")),
        ("payment_agent_type", _g("paymentagenttype")),
        ("receipt_code", _g("receiptcode", "bsocode")),
        ("region", _g("region")),
        ("nds0", _g("nds0")),
        ("taxation", tax_human),
        ("prepaid_sum", money(_g("prepaidsum")) if _g("prepaidsum") else None),
        ("credit_sum", money(_g("creditsum")) if _g("creditsum") else None),
        ("provision_sum", money(_g("provisionsum")) if _g("provisionsum") else None),
        ("ofd_id", ofd_id),
        ("ofd_inn", _g("ofdin", "ofdinn")),
        ("ofd_name", _g("ofdname")),
        ("ticket_id", ticket_id),
        ("ticket_kind", ticket_kind),
        ("receive_date", meta.get("receivedate")),
        ("doc_subtype", meta.get("subtype") or outer_low.get("kind")),
        ("source_receipt_id", udata.get("id")),
        ("source_date_create", udata.get("date_create")),
        ("ffd_version", _g("fiscaldocumentformatver")),
        ("source_address", meta.get("address")),
        ("message_fiscal_sign", _g("messagefiscalsign")),
        ("redefine_mask", _g("redefine_mask", "redefinemask")),
        ("properties_data", props_data),
        ("source_code", receipt.get("code") or (data.get("code") if isinstance(data, dict) else None)),
    ):
        if val not in (None, "", [], {}):
            extra[key] = val
    if props:
        extra["properties"] = props
    if nds_totals:
        extra["nds_totals"] = nds_totals

    ind_rec = receipt_card._industry_details(
        receipt.get("industryReceiptDetails") or low.get("industryreceiptdetails"))
    if ind_rec:
        extra["industry_details"] = ind_rec

    imeta = []
    for i_, raw_item in enumerate(low.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        it_ = _lc(raw_item)
        rec_ = {}
        for k_ in ("paymenttype", "producttype", "itemsquantitymeasure"):
            if it_.get(k_) not in (None, ""):
                rec_[k_] = it_.get(k_)
        mk = receipt_card._marking(
            raw_item.get("productCodeData") or raw_item.get("itemsProductCode")
            or raw_item.get("marking") or it_.get("productcodedata"),
            raw_item.get("productCodeNew") or it_.get("productcodenew"),
        )
        if mk:
            rec_["marking"] = mk
        sup = receipt_card._extract_supplier(raw_item)
        if sup:
            rec_["supplier"] = sup
        ag = receipt_card._extract_agent(raw_item)
        if ag:
            rec_["agent"] = ag
        idet = receipt_card._industry_details(
            raw_item.get("itemsIndustryDetails") or raw_item.get("industryDetails")
            or it_.get("itemsindustrydetails"))
        if idet:
            rec_["industry_details"] = idet
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


def _infer_operation(qr_raw: str, explicit: int | None = None) -> int:
    """Определить признак расчёта (1..4 по тегу 1054 ФФД) из QR или аргумента."""
    if explicit in (1, 2, 3, 4):
        return int(explicit)
    if qr_raw:
        try:
            from .qr import parse_qr
            parsed_qr = parse_qr(qr_raw)
            if parsed_qr.operation in (1, 2, 3, 4):
                return int(parsed_qr.operation)
        except Exception:                                # noqa: BLE001
            m = re.search(r"(?:^|[&?])n=([1-4])(?:$|[&])", str(qr_raw))
            if m:
                return int(m.group(1))
    return 1


def _ticket_to_payload(result) -> dict:
    """Привести ответ FnsResult к словарю для parse_receipt_payload."""
    ticket = result.raw.get("ticket") if isinstance(result.raw, dict) else {}
    ticket = ticket if isinstance(ticket, dict) else {}
    payload: dict = {
        "provider": "fns",
        "status": result.status,
        "message": result.message,
        "ticket": ticket,
        "code": result.raw.get("code", "") if isinstance(result.raw, dict) else "",
    }
    if isinstance(result.raw, dict) and result.raw.get("checked_fingerprint"):
        payload["checked_fingerprint"] = result.raw["checked_fingerprint"]
    if isinstance(result.raw, dict) and result.raw.get("cached"):
        payload["cached"] = True
        payload["cache_source"] = result.raw.get("cache_source", "db")
    if result.status == "valid":
        document = ticket.get("receipt") if isinstance(ticket, dict) else None
        payload["document"] = {"receipt": document} if document else ticket
    return payload


# ==========================================================================
#  Отдельные провайдеры
# ==========================================================================
def fetch_fns(qr_raw: str, fn: str, fd: str, fp: str, total_rub: float,
              date_time: datetime,
              master_token: str = "",
              *,
              operation: int | None = None,
              db=None,
              force: bool = False) -> tuple[bool, str, dict]:
    """Проверка и получение полного чека в Открытых API ФНС (с кэшем БД)."""
    from . import fns
    op = _infer_operation(qr_raw, operation)
    moment = date_time or datetime.now(timezone.utc).replace(tzinfo=None)

    pre = fns.validate_ticket_request(fn, fd, fp, total_rub, moment, op)
    if pre is not None:
        return True, pre.message, _ticket_to_payload(pre)

    t0 = time.monotonic()
    with fns.inflight_lock(fn, fd, fp):
        if not force:
            if db is not None:
                hit = fns.find_cached_in_db(
                    db, fn, fd, fp, total_rub, moment, op, require_items=True)
                if hit is not None and hit.status == "valid":
                    return True, hit.message, _ticket_to_payload(hit)
            try:
                from ..database import SessionLocal
                fresh_db = SessionLocal()
                try:
                    hit = fns.find_cached_in_db(
                        fresh_db, fn, fd, fp, total_rub, moment, op,
                        require_items=True)
                    if hit is not None and hit.status == "valid":
                        return True, hit.message, _ticket_to_payload(hit)
                finally:
                    fresh_db.close()
            except Exception:                            # noqa: BLE001
                log.debug("fetch_fns local db lookup skipped", exc_info=True)

        client = (fns.shared_client(master_token=master_token or None, db=db)
                  if fns.FnsClient is getattr(fns, "_DEFAULT_FNS_CLIENT_CLS", None)
                  else fns.FnsClient(master_token=master_token or None))
        result = client.get_ticket(fn, fd, fp, total_rub, moment, operation=op)
    duration_ms = int((time.monotonic() - t0) * 1000)

    if not result.ok:
        return (False, result.message,
                {"provider": "fns", "kind": result.raw.get("kind", ""),
                 "status": result.status, "message": result.message,
                 "duration_ms": duration_ms})
    payload = _ticket_to_payload(result)
    payload["duration_ms"] = duration_ms
    return True, result.message, payload


_DEFAULT_FETCH_FNS = fetch_fns


# ==========================================================================
#  Движок загрузки: единственный источник — Открытые API ФНС
# ==========================================================================
class ExternalFetchEngine:
    """Загрузка данных чека из ФНС с защитой от лишних обращений."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._fail_streak: dict[str, int] = {}

    def _settings(self, db) -> dict:
        """Настройки ФНС из БД — ТОЛЬКО через расшифровку (v1.70.71)."""
        from . import appsettings
        rows = {key: appsettings.get_setting(db, key, "")
                for key in ("fns_master_token", "fns_client_app_id")}
        return rows

    def provider_chain(self, db) -> list[str]:
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

    def _finalize_parsed(self, data: dict, msg: str,
                         known_total: float | None) -> ExternalResult:
        """Разобрать ответ источника и приложить метаданные/ticket."""
        import copy
        clean_ticket = (copy.deepcopy(data["ticket"])
                        if isinstance(data.get("ticket"), dict) and data["ticket"]
                        else None)
        parsed = parse_receipt_payload(data, known_total)
        parsed.raw = dict(parsed.raw) if isinstance(parsed.raw, dict) else {}
        if clean_ticket is not None:
            parsed.raw["_fns_ticket"] = clean_ticket
        if data.get("checked_fingerprint"):
            parsed.raw["checked_fingerprint"] = data["checked_fingerprint"]
        if data.get("duration_ms") is not None:
            parsed.raw["duration_ms"] = data["duration_ms"]
        if data.get("cached"):
            parsed.raw["cached"] = True
            parsed.raw["cache_source"] = data.get("cache_source", "db")
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

    def fetch(self, db, qr_raw: str, fn: str, fd: str, fp: str,
              total_rub: float, date_time: datetime | None) -> ExternalResult:
        """Получить данные чека из локальной БД или ФНС (единственный источник)."""
        from . import fns
        known_total = total_rub or None
        op = _infer_operation(qr_raw)
        use_default_fns = fetch_fns is _DEFAULT_FETCH_FNS

        # 0) Мгновенная проверка локальной БД до паузы антибана и проверки токена:
        #    0 запросов к ФНС и 0 мс задержки, если чек уже подтверждён с позициями.
        if use_default_fns and db is not None:
            hit = fns.find_cached_in_db(
                db, fn, fd, fp, total_rub, date_time, op, require_items=True)
            if hit is not None and hit.status == "valid":
                return self._finalize_parsed(
                    _ticket_to_payload(hit), hit.message, known_total)

        log.info("🔍 ДАННЫЕ ЧЕКА [fn=%s i=%s fp=%s]: старт загрузки из ФНС "
                 "(сумма=%s руб., дата=%s)", fn, fd, fp, total_rub, date_time)

        inflight = fns.inflight_lock(fn, fd, fp) if use_default_fns else None
        if inflight is not None:
            inflight.acquire()
        try:
            if use_default_fns:
                try:
                    from ..database import SessionLocal
                    fresh_db = SessionLocal()
                    try:
                        hit = fns.find_cached_in_db(
                            fresh_db, fn, fd, fp, total_rub, date_time, op,
                            require_items=True)
                        if hit is not None and hit.status == "valid":
                            return self._finalize_parsed(
                                _ticket_to_payload(hit), hit.message,
                                known_total)
                    finally:
                        fresh_db.close()
                except Exception:                        # noqa: BLE001
                    log.debug("engine local db lookup skipped", exc_info=True)

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

            with self._lock:
                self._polite_wait("fns_api")
                try:
                    moment = date_time or datetime.now(timezone.utc).replace(
                        tzinfo=None)
                    if use_default_fns:
                        net_ok, msg, data = fetch_fns(
                            qr_raw, fn, fd, fp, total_rub, moment, token,
                            operation=op, db=db)
                    else:
                        net_ok, msg, data = fetch_fns(
                            qr_raw, fn, fd, fp, total_rub, moment, token)
                except Exception as e:
                    net_ok, msg, data = False, f"Ошибка: {e}", {}

                if not net_ok:
                    if data.get("kind") == "quota":
                        log.warning("⏸ ДАННЫЕ НЕ ПОЛУЧЕНЫ [fn=%s]: %s", fn, msg)
                        return ExternalResult(ok=False, source="fns_api",
                                              found=False, message=msg,
                                              raw={"quota": True})
                    self._note_failure(msg)
                    log.warning("❌ ФНС: сбой: %s", msg)
                    return ExternalResult(ok=False, source="fns_api",
                                          found=False,
                                          message=msg if msg.startswith("ФНС")
                                          else f"ФНС: {msg}")

                self._fail_streak["fns_api"] = 0
                if data.get("status") in ("not_found", "invalid"):
                    log.info("ℹ️ ФНС: чек не подтверждён (%s)", msg)
                    return ExternalResult(ok=True, source="fns_api",
                                          found=False, message=msg, raw=data)

                return self._finalize_parsed(data, msg, known_total)
        finally:
            if inflight is not None:
                inflight.release()


engine = ExternalFetchEngine()
