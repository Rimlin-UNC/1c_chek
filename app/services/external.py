# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — получение полных данных чека из внешних источников
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# v1.2.0. Источники (провайдеры):
#   fns_api        — официальное API ФНС «Открытое API проверки чека ККТ»
#                    (нужен Мастер-токен; форма ввода — Настройки → Источники);
#   proverkacheka  — proverkacheka.com, POST /api/v1/check/get
#                    (qrraw + token из личного кабинета сервиса);
#   custom         — любой сторонний сервис по контракту «POST {qrraw} → JSON»
#                    (URL задаёт администратор; подходит для проверкичека.рф
#                    и других, когда у них появится/станет известен API);
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


# ==========================================================================
#  Парсинг ответов (защитный: формы ответов у сервисов различаются)
# ==========================================================================
def _find_receipt_dict(obj, depth: int = 0):
    """Рекурсивный поиск словаря чека: содержит 'items' (list) и сумму."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    items = obj.get("items")
    if isinstance(items, list) and items and any(
        isinstance(i, dict) and ("name" in i or "itemName" in i or "onlines" not in i) for i in items
    ):
        if any(k in obj for k in ("totalSum", "total", "sum")):
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
    raw_total = float(receipt.get("totalSum") or receipt.get("total") or 0)
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
    total = money(receipt.get("totalSum") or receipt.get("total"))

    items: list[ExternalItem] = []
    for i, raw_item in enumerate(receipt.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        name = str(raw_item.get("name") or raw_item.get("itemName") or f"Позиция {i + 1}")
        qty = float(raw_item.get("quantity") or 1)
        price = money(raw_item.get("price"))
        item_total = money(raw_item.get("sum") or raw_item.get("total"))
        vat_rate = raw_item.get("nds") if isinstance(raw_item.get("nds"), (int, str)) else "none"
        if vat_rate in (0, "0"):
            vat_rate = "0"
        vat_sum = money(raw_item.get("ndsSum"))
        items.append(ExternalItem(name=name, quantity=qty, price=price,
                                  total=item_total, vat_rate=str(vat_rate), vat_sum=vat_sum))

    return ExternalResult(
        ok=True,
        source="",
        found=True,
        message="Данные чека получены",
        date_time=_parse_datetime(receipt),
        total_sum=total,
        operation=_parse_operation(receipt),
        merchant_name=str(receipt.get("user") or receipt.get("operatorName")
                          or receipt.get("retailPlace") or "")[:500],
        merchant_inn=str(receipt.get("userInn") or receipt.get("inn") or "")[:20],
        merchant_address=str(receipt.get("retailPlaceAddress") or receipt.get("address") or "")[:500],
        cashier=str(receipt.get("operator") or receipt.get("cashier") or "")[:200],
        cash_sum=money(receipt.get("cashTotalSum")) if receipt.get("cashTotalSum") else None,
        ecash_sum=money(receipt.get("ecashTotalSum")) if receipt.get("ecashTotalSum") else None,
        items=items,
        raw=receipt,
    )


# ==========================================================================
#  Отдельные провайдеры
# ==========================================================================
def fetch_proverkacheka(qr_raw: str, token: str) -> tuple[bool, str, dict]:
    """
    proverkacheka.com: POST /api/v1/check/get (qrraw + token).
    Возвращает (успех_сети, сообщение, json_ответ).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (YmasterCheck/1.2; +https://ymaster.ru)",
        "Cookie": "ENGID=1.1",
    }
    data = {"qrraw": qr_raw}
    if token:
        data["token"] = token
    try:
        resp = httpx.post(PROVERKACHEKA_URL, data=data, headers=headers,
                          timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as e:
        return False, f"Сеть: {e.__class__.__name__}", {}
    if resp.status_code in (429, 403):
        return False, f"HTTP {resp.status_code} (лимит/блокировка)", {}
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}", {}
    try:
        return True, "OK", resp.json()
    except ValueError:
        return False, "Ответ не JSON", {}


def fetch_custom(qr_raw: str, url: str) -> tuple[bool, str, dict]:
    """
    Собственный источник (контракт в docs/EXTERNAL_CHECKS.md):
    POST {url}  body: {"qrraw": "..."}  → JSON с данными чека
    (подойдёт для проверкичека.рф, когда у сервиса будет публичный API).
    """
    try:
        resp = httpx.post(url, json={"qrraw": qr_raw},
                          headers={"User-Agent": "YmasterCheck/1.2"},
                          timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as e:
        return False, f"Сеть: {e.__class__.__name__}", {}
    if resp.status_code in (429, 403):
        return False, f"HTTP {resp.status_code} (лимит/блокировка)", {}
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}", {}
    try:
        return True, "OK", resp.json()
    except ValueError:
        return False, "Ответ не JSON", {}


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
    # Валидный ответ ФНС = чек существует; позиций в этом методе нет.
    payload = {
        "document": {"receipt": {
            "totalSum": int(round(total_rub * 100)),
            "dateTime": date_time.strftime("%Y-%m-%dT%H:%M:%S") if date_time else None,
            "user": "", "userInn": "", "retailPlaceAddress": "",
            "items": [],
        }},
        "check": result.raw,
    }
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
                                "external_custom_url", "external_order",
                                "external_auto"])).all()}
        return rows

    def provider_chain(self, db) -> list[str]:
        """Порядок провайдеров с учётом настроек и наличия токенов/URL."""
        cfg = self._settings(db)
        order = [p for p in (cfg.get("external_order") or
                             "fns_api,proverkacheka,custom").split(",") if p]
        chain: list[str] = []
        for p in order:
            if p == "fns_api" and (cfg.get("fns_master_token") or settings.FNS_MASTER_TOKEN):
                chain.append(p)
            elif p == "proverkacheka" and cfg.get("proverkacheka_token"):
                chain.append(p)
            elif p == "custom" and cfg.get("external_custom_url"):
                chain.append(p)
        # mock всегда в конце — чтобы чеки проверялись даже без токенов
        chain.append("mock")
        return chain

    def is_available(self, provider: str) -> bool:
        return time.time() >= self._cooldown_until.get(provider, 0.0)

    def status(self) -> dict:
        now = time.time()
        out = {}
        for p in ("fns_api", "proverkacheka", "custom", "mock"):
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
                        elif provider == "custom":
                            net_ok, msg, data = fetch_custom(
                                qr_raw, cfg.get("external_custom_url", ""))
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
