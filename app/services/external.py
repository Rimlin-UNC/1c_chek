# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — получение полных данных чека из внешних источников
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# v1.2.0. Источники (провайдеры):
#   proverkacheka  — proverkacheka.com, POST /api/v1/check/get
#                    (qrraw + token) — ЕДИНСТВЕННЫЙ рабочий источник
#                    (v1.57.0: документация сверена, забираем максимум полей);
#   fns_api        — официальное API ФНС «Открытое API проверки чека ККТ»
#                    (мастер-токен; карточка «Проверка чеков») — НА
#                    ПЕРСПЕКТИВУ: включается автоматически при появлении токена;
#   (v1.55.0: свои шлюзы выведены; v1.57.0: Приложение ФНС, Честный Знак,
#    ОФД-ру выведены из системы — сервисы перестали отвечать)
#   mock           — встроенная эмуляция для тестов и демонстрации.
#
# АНТИБАН-МЕХАНИКА (защита от блокировок):
#   • все запросы к внешним сервисам выполняются ПОСЛЕДОВАТЕЛЬНО (общий замок);
#   • между запросами случайная пауза 2–7 секунд (rand.uniform(2.0, 7.0));
#   • при 429/403 (превышение лимитов, бан) провайдер уходит в «остывание»
#     с растущей паузой 10→20→40…минут (максимум 6 часов), движок
#     автоматически переключается на следующий доступный источник;
#   • при сетевых сбоях — до 2 повторов того же источника, затем переход
#     к следующему; счётчик неудач сбрасывается после успеха;
#   • таймаут запроса 20 с; повторная проверка чека — только вне кэша.
# ======================================================================
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ..config import settings

# --- Параметры вежливости (требование ТЗ: пауза 2–7 секунд, без бана) ---
GAP_MIN_SECONDS = 2.0
GAP_MAX_SECONDS = 7.0
REQUEST_TIMEOUT = 20.0
COOLDOWN_BASE_SECONDS = 600.0        # первый «остывание» — 10 минут
COOLDOWN_MAX_SECONDS = 6 * 3600.0    # максимум 6 часов
MAX_SAME_PROVIDER_RETRIES = 2

PROVERKACHEKA_URL = "https://proverkacheka.com/api/v1/check/get"


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


def _scale_money(value: float, known_rub: float | None) -> float:
    """
    Определение масштаба суммы: источники на базе ФНС отдают копейки.
    Сверяемся с известной суммой из QR (s=…), чтобы не ошибиться.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if known_rub:
        if abs(v - known_rub * 100) <= max(1.0, known_rub * 0.5):
            return round(v / 100.0, 2)          # копейки → рубли
        if abs(v - known_rub) <= max(1.0, known_rub * 0.5):
            return round(v, 2)                   # уже рубли
    return round(v / 100.0, 2) if v >= 100000 else round(v, 2)


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
        return ExternalResult(ok=True, source="", found=False,
                              message="Источник ответил, но данных чека в ответе нет",
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
def fetch_proverkacheka(qr_raw: str, token: str) -> tuple[bool, str, dict]:
    """
    proverkacheka.com: POST /api/v1/check/get, поля qrraw + token
    (токен из личного кабинета сервиса, Cookie ENGID=1.1).
    v1.57.1: источник принимает разные форматы тела запроса — пробуем
    последовательно urlencoded → JSON → multipart; формат меняется
    автоматически, если пришёл 400/422 («неверный формат запроса»).
    В сообщении об ошибке — причина: токен/квота/формат + фрагмент ответа.
    Возвращает (успех_сети, сообщение, json_ответ).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (YmasterCheck/1.2; +https://ymaster.ru)",
        "Cookie": "ENGID=1.1",
    }
    base = {"qrraw": qr_raw}
    if token:
        base["token"] = token

    def _send(how: str):
        if how == "json":                      # Content-Type: application/json
            return httpx.post(PROVERKACHEKA_URL, json=base,
                              headers=headers, timeout=REQUEST_TIMEOUT)
        if how == "multipart":                 # multipart/form-data
            return httpx.post(PROVERKACHEKA_URL,
                              files={k: (None, v) for k, v in base.items()},
                              headers=headers, timeout=REQUEST_TIMEOUT)
        return httpx.post(PROVERKACHEKA_URL, data=base,   # application/x-www-form-urlencoded
                          headers=headers, timeout=REQUEST_TIMEOUT)

    last_err = ""
    for how in ("form", "json", "multipart"):
        try:
            resp = _send(how)
        except httpx.HTTPError as e:
            return False, f"Сеть: {e.__class__.__name__}", {}
        if resp.status_code == 401:
            return False, ("HTTP 401: токен не принят — проверьте его в личном "
                           "кабинете proverkacheka.com (Справка → API)"), {}
        if resp.status_code == 402:
            return False, ("HTTP 402: квота/тариф API исчерпаны — пополните "
                           "баланс в кабинете proverkacheka.com"), {}
        if resp.status_code in (429, 403):
            return False, f"HTTP {resp.status_code} (лимит/блокировка)", {}
        if resp.status_code in (400, 422):
            snippet = (resp.text or "")[:120].replace(chr(10), " ")
            last_err = f"{how}: HTTP {resp.status_code} {snippet}"
            continue                # формат тела не подошёл — пробуем следующий
        if resp.status_code != 200:
            snippet = (resp.text or "")[:120].replace(chr(10), " ")
            return False, f"HTTP {resp.status_code} (сервис временно недоступен?) {snippet}", {}
        try:
            return True, "OK", resp.json()
        except ValueError:
            return False, f"Ответ не JSON: {(resp.text or '')[:120]}", {}
    return False, f"Формат запроса не принят ({last_err})", {}


def fetch_fns(qr_raw: str, fn: str, fd: str, fp: str, total_rub: float,
              date_time: datetime, master_token: str) -> tuple[bool, str, dict]:
    """
    Официальное API ФНС. Мастер-токен выдаётся по заявлению (см.
    docs/knowledge/fns_auth.md). Проверка реквизитов: POST /api/2/check.
    Возвращает JSON проверки (данные позиций ФНС отдаёт по отдельному
    методу — при наличии токена движок попытается получить документ).
    """
    from .fns import FnsClient  # локальный импорт — избегаем цикла
    client = FnsClient(master_token=master_token)
    result = client.check_receipt(fn, fd, fp, total_rub, date_time)
    raw = {"provider": "fns_api", "status": result.status, "message": result.message}
    if not result.ok:
        return False, result.message, raw
    # Валидный ответ ФНС = чек существует. v1.27.0: сразу за проверкой
    # получаем ПОЛНЫЙ чек (GetTicket, SOAP KktService/0.1) — с позициями.
    payload = {
        "document": {"receipt": {
            "totalSum": int(round(total_rub * 100)),
            "dateTime": date_time.strftime("%Y-%m-%dT%H:%M:%S") if date_time else None,
            "user": "", "userInn": "", "retailPlaceAddress": "",
            "items": [],
        }},
        "check": result.raw,
    }
    try:
        gt = client.get_ticket(fn, fd, fp, total_rub, date_time)
        if gt.ok and isinstance(gt.raw.get("ticket"), dict) and gt.raw["ticket"]:
            payload["ticket"] = gt.raw["ticket"]
    except Exception:                       # GetTicket не обязан мешать проверке
        pass
    return True, result.message, payload


# ==========================================================================
#  Движок: последовательность, паузы 2–7 с, остывание, ротация
# ==========================================================================
class ExternalFetchEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._fail_streak: dict[str, int] = {}

    # --- Настройки источников из БД ---
    def _settings(self, db) -> dict:
        from ..models import AppSetting
        rows = {r.key: r.value for r in db.query(AppSetting).filter(
            AppSetting.key.in_(["fns_master_token", "proverkacheka_token",
                                "external_order", "external_auto"])).all()}
        # v1.55.0: «свои шлюзы» выведены из системы — custom-ключи в БД
        # больше не читаются и ни на что не влияют (данные не трогаем)
        return rows

    def provider_chain(self, db) -> list[str]:
        """Порядок провайдеров с учётом настроек и наличия токенов.

        v1.57.0: единственный рабочий источник — proverkacheka.com
        (POST /api/v1/check/get, qrraw + token; документация сверена).
        Приложение ФНС, Честный Знак, ОФД-ру и свои шлюзы выведены из
        системы — сервисы перестали отвечать. fns_api остаётся на
        перспективу: если в карточке «Проверка чеков» появится
        мастер-токен ФНС, источник автоматически встанет первым."""
        cfg = self._settings(db)
        order = [p for p in (cfg.get("external_order") or
                             "proverkacheka").split(",") if p]
        # v1.55.0/v1.57.0: выведенные источники игнорируем, даже если их
        # порядок сохранился в настройках
        dead = {"custom", "fns_app", "crpt", "ofd_ru"}
        order = [p for p in order if p not in dead]
        # fns_api — на перспективу (разрешение ФНС на мастер-токен)
        if (cfg.get("fns_master_token") or settings.FNS_MASTER_TOKEN) \
                and "fns_api" not in order:
            order.insert(0, "fns_api")
        chain: list[str] = []
        for p in order:
            if p in chain:
                continue                      # дедупликация порядка
            if p == "fns_api" and (cfg.get("fns_master_token") or settings.FNS_MASTER_TOKEN):
                chain.append(p)               # перспектива: мастер-токен ФНС
            elif p == "proverkacheka" and cfg.get("proverkacheka_token"):
                chain.append(p)               # основной рабочий источник
        # mock всегда в конце — чтобы чеки проверялись даже без токенов
        chain.append("mock")
        return chain

    def is_available(self, provider: str) -> bool:
        return time.time() >= self._cooldown_until.get(provider, 0.0)

    def status(self) -> dict:
        now = time.time()
        out = {}
        for p in ("fns_api", "proverkacheka", "mock"):
            cd = self._cooldown_until.get(p, 0.0)
            out[p] = {
                "available": now >= cd,
                "cooldown_sec": max(0, int(cd - now)),
                "fail_streak": self._fail_streak.get(p, 0),
            }
        return out

    def _polite_wait(self, provider: str) -> None:
        """Случайная пауза 2–7 с между запросами к источнику (антибан)."""
        last = self._last_call.get(provider, 0.0)
        gap = random.uniform(GAP_MIN_SECONDS, GAP_MAX_SECONDS)
        wait = last + gap - time.time()
        if wait > 0:
            time.sleep(min(wait, GAP_MAX_SECONDS))
        self._last_call[provider] = time.time()

    def _note_failure(self, provider: str, message: str) -> None:
        """Счётчик неудач и «остывание» при признаках блокировки."""
        streak = self._fail_streak.get(provider, 0) + 1
        self._fail_streak[provider] = streak
        if ("429" in message or "403" in message or "блокир" in message.lower()):
            cooldown = min(COOLDOWN_BASE_SECONDS * (2 ** (streak - 1)),
                           COOLDOWN_MAX_SECONDS)
            self._cooldown_until[provider] = time.time() + cooldown

    def fetch(self, db, qr_raw: str, fn: str, fd: str, fp: str,
              total_rub: float, date_time: datetime | None) -> ExternalResult:
        """
        Главная точка входа. Перебирает источники по порядку; между запросами
        — случайная пауза; при блокировке источник «остывает» и идёт ротация.
        """
        chain = self.provider_chain(db)
        errors: list[str] = []
        cfg = self._settings(db)
        known_total = total_rub or None

        for provider in chain:
            if not self.is_available(provider):
                errors.append(f"{provider}: остывание ещё {int(self._cooldown_until[provider] - time.time())} с")
                continue

            with self._lock:                      # строго последовательно
                self._polite_wait(provider)
                for attempt in range(MAX_SAME_PROVIDER_RETRIES + 1):
                    try:
                        if provider == "proverkacheka":
                            net_ok, msg, data = fetch_proverkacheka(
                                qr_raw, cfg.get("proverkacheka_token", ""))
                        elif provider == "fns_api":
                            token = cfg.get("fns_master_token") or settings.FNS_MASTER_TOKEN
                            net_ok, msg, data = fetch_fns(
                                qr_raw, fn, fd, fp, total_rub,
                                date_time or datetime.utcnow(), token)
                        else:  # mock
                            from .fns import check_mock
                            r = check_mock(fn, fd, fp, total_rub, date_time or datetime.utcnow())
                            net_ok, msg = True, r.message
                            data = {"provider": "mock", "status": r.status,
                                    "document": {"receipt": {
                                        "totalSum": int(round((total_rub or 0) * 100)),
                                        "dateTime": (date_time or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%S"),
                                        "items": [], "user": "", "userInn": "",
                                    }}}
                    except Exception as e:            # непредвиденное — как сетевой сбой
                        net_ok, msg, data = False, f"Ошибка: {e}", {}

                    if net_ok:
                        parsed = parse_receipt_payload(data, known_total)
                        # v1.26.0: источник ответил, но данных ЧЕКА в ответе
                        # нет (например, fns_api check без позиций) — это не
                        # заполнение: пробуем следующий источник, а не
                        # возвращаем «пустое» заполнение
                        if not parsed.found and provider != "mock":
                            errors.append(f"{provider}: {parsed.message}")
                            break                       # к следующему источнику
                        parsed.source = provider
                        if provider == "mock":
                            parsed.found = False
                            parsed.message = f"Источник «mock» (демо): {msg}"
                        else:
                            parsed.found = True
                            parsed.message = msg
                        self._fail_streak[provider] = 0
                        parsed.raw["engine"] = {"errors": errors, "attempts": attempt + 1}
                        return parsed

                    self._note_failure(provider, msg)
                    errors.append(f"{provider}: {msg}")
                    # блокировка/лимиты → сразу к следующему источнику
                    if "429" in msg or "403" in msg or "блокир" in msg.lower():
                        break
                    # обычный сбой → короткая пауза и повтор того же источника
                    time.sleep(random.uniform(1.0, 2.5))

        return ExternalResult(ok=False, source="", found=False,
                              message="Все источники недоступны: " + "; ".join(errors) if errors
                              else "Нет доступных источников")


# Глобальный движок (единственный экземпляр на процесс — ровные паузы)
engine = ExternalFetchEngine()
