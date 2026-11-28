"""Сервис корпоративного управления подписками, прайс-листом, квотами и счетами (v1.74.0).

Ключевые принципы (Enterprise B2B SaaS):
1. Все денежные суммы хранятся и рассчитываются строго в целых копейках (int) —
   без погрешностей плавающей точки.
2. Иммутабельность цен для действующих договоров (Grandfathering):
   при изменении цены тарифа или модуля администратор выбирает политику
   ``grandfather`` (действующие подписки сохраняют ``locked_price_kop``) либо
   ``next_renewal`` (новая цена применяется со следующего продления).
3. Льготный период (Grace Period) и защита данных клиента (No Data Loss):
   после ``period_end`` действует ``grace_until``; после полного истечения в
   режиме ``strict`` блокируются только новые пишущие/аналитические операции
   неоплаченных модулей (HTTP 402), а чтение и выгрузка архива остаются открыты.
4. Безопасное хранение без миграций схемы БД: конфигурация, версии тарифов,
   промокоды, подписки организаций и счета хранятся в ``app_settings``, а
   неизменяемый след всех действий пишется в ``audit_log``.
"""
from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN
from ..counterparties.models import Counterparty
from ..models import AppSetting, AuditLog, Company, Receipt, User, iso_z
from ..primary.models import PrimaryDocument
from ..services import entitlements

KEY_CONFIG: Final[str] = "billing:config"
KEY_MODULES: Final[str] = "billing:module_prices"
KEY_PLANS: Final[str] = "billing:plans"
KEY_PROMOS: Final[str] = "billing:promos"
KEY_INVOICES: Final[str] = "billing:invoices_index"
SUB_PREFIX: Final[str] = "billing:sub:"

BILLING_MODES: Final[tuple[str, ...]] = ("free_all", "soft_notice", "strict")
VAT_MODES: Final[dict[str, tuple[str, int]]] = {
    "none": ("Без НДС (УСН)", 0),
    "vat5": ("В т.ч. НДС 5%", 5),
    "vat7": ("В т.ч. НДС 7%", 7),
    "vat20": ("В т.ч. НДС 20%", 20),
}
CYCLES_MONTHS: Final[dict[str, int]] = {
    "monthly": 1,
    "quarterly": 3,
    "yearly": 12,
}
CYCLE_TITLES: Final[dict[str, str]] = {
    "monthly": "1 мес.",
    "quarterly": "3 мес.",
    "yearly": "12 мес.",
}
STATUS_TITLES: Final[dict[str, str]] = {
    "free": "Бесплатный доступ",
    "trial": "Пробный период",
    "active": "Активна",
    "grace": "Льготный период",
    "expired": "Истекла (режим чтения)",
    "partner": "Партнёрский доступ",
    "suspended": "Приостановлена",
}

_CODE_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_PROMO_RE = re.compile(r"^[A-Z0-9_-]{2,32}$")
MAX_KOP: Final[int] = 100_000_000_00  # 100 млн ₽ потолок от опечаток

DEFAULT_CONFIG: Final[dict] = {
    "billing_mode": "free_all",           # free_all | soft_notice | strict
    "trial_days": 14,
    "grace_days": 7,
    "annual_discount_pct": 17,
    "vat_mode": "none",                   # none | vat5 | vat7 | vat20
    "currency": "RUB",
    "price_change_policy": "grandfather",  # grandfather | next_renewal
    "seller_name": "ООО «Ямастер»",
    "seller_inn": "7700000000",
    "seller_kpp": "770001001",
    "seller_address": "Российская Федерация",
    "seller_bank": "АО «ТБанк»",
    "seller_bik": "044525974",
    "seller_account": "40702810000000000000",
    "seller_corr_account": "30101810145250000974",
    "invoice_prefix": "ДВ",
    "next_invoice_seq": 1,
    "support_email": "chek@ymaster.ru",
}

DEFAULT_MODULE_PRICES: Final[dict[str, dict]] = {
    "receipts": {
        "code": "receipts",
        "monthly_kop": 149_000,       # 1 490 ₽/мес
        "yearly_kop": 1_490_000,      # 14 900 ₽/год
        "monthly_quota": 500,         # чеков в месяц (0 = безлимит)
        "quota_unit": "чеков/мес",
        "available": True,
        "price_version": 1,
        "price_history": [],
    },
    "upd": {
        "code": "upd",
        "monthly_kop": 199_000,       # 1 990 ₽/мес
        "yearly_kop": 1_990_000,      # 19 900 ₽/год
        "monthly_quota": 300,         # УПД в месяц
        "quota_unit": "УПД/мес",
        "available": True,
        "price_version": 1,
        "price_history": [],
    },
    "manager_cabinet": {
        "code": "manager_cabinet",
        "monthly_kop": 99_000,        # 990 ₽/мес
        "yearly_kop": 990_000,        # 9 900 ₽/год
        "monthly_quota": 0,
        "quota_unit": "без лимита",
        "available": True,
        "price_version": 1,
        "price_history": [],
    },
    "counterparties": {
        "code": "counterparties",
        "monthly_kop": 79_000,        # 790 ₽/мес
        "yearly_kop": 790_000,        # 7 900 ₽/год
        "monthly_quota": 200,
        "quota_unit": "контрагентов",
        "available": True,
        "price_version": 1,
        "price_history": [],
    },
    "contracts": {
        "code": "contracts",
        "monthly_kop": 129_000,       # 1 290 ₽/мес
        "yearly_kop": 1_290_000,      # 12 900 ₽/год
        "monthly_quota": 100,
        "quota_unit": "договоров/мес",
        "available": False,
        "price_version": 1,
        "price_history": [],
    },
}

DEFAULT_PLANS: Final[list[dict]] = [
    {
        "code": "start_receipts",
        "title": "Старт: Чеки",
        "badge": "",
        "description": "Кассовые чеки, проверка ФНС, авансовые отчёты АО-1 и выгрузка в 1С",
        "modules": ["receipts"],
        "monthly_kop": 149_000,
        "quarterly_kop": 419_000,
        "yearly_kop": 1_490_000,
        "limits": {
            "users": 5,
            "receipts_monthly": 500,
            "upd_monthly": 0,
            "counterparties_monthly": 0,
        },
        "active": True,
        "sort_order": 10,
        "price_version": 1,
        "price_history": [],
    },
    {
        "code": "upd_bank",
        "title": "Первичка и Банк",
        "badge": "",
        "description": "Приём и печать электронных УПД, банковские выписки и запросы документов",
        "modules": ["upd"],
        "monthly_kop": 199_000,
        "quarterly_kop": 559_000,
        "yearly_kop": 1_990_000,
        "limits": {
            "users": 5,
            "receipts_monthly": 0,
            "upd_monthly": 300,
            "counterparties_monthly": 0,
        },
        "active": True,
        "sort_order": 20,
        "price_version": 1,
        "price_history": [],
    },
    {
        "code": "accounting_pro",
        "title": "Бухгалтерия ПРО",
        "badge": "Популярный",
        "description": "Полный контур бухгалтера: чеки, АО-1, УПД, сверка с банком и контрагенты",
        "modules": ["receipts", "upd", "counterparties"],
        "monthly_kop": 349_000,
        "quarterly_kop": 979_000,
        "yearly_kop": 3_490_000,
        "limits": {
            "users": 15,
            "receipts_monthly": 1500,
            "upd_monthly": 1000,
            "counterparties_monthly": 500,
        },
        "active": True,
        "sort_order": 30,
        "price_version": 1,
        "price_history": [],
    },
    {
        "code": "corp_full",
        "title": "Корпоративный Полный",
        "badge": "Максимум",
        "description": "Все модули платформы включая Кабинет руководителя и повышенные квоты",
        "modules": ["receipts", "upd", "manager_cabinet", "counterparties"],
        "monthly_kop": 499_000,
        "quarterly_kop": 1_399_000,
        "yearly_kop": 4_990_000,
        "limits": {
            "users": 50,
            "receipts_monthly": 5000,
            "upd_monthly": 3000,
            "counterparties_monthly": 2000,
        },
        "active": True,
        "sort_order": 40,
        "price_version": 1,
        "price_history": [],
    },
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now_iso() -> str:
    return iso_z(_utcnow())


def _parse_iso(val: str | None) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _read_json(db: Session, key: str, default):
    row = db.get(AppSetting, key)
    if row is None or not (row.value or "").strip():
        return copy.deepcopy(default)
    try:
        return json.loads(row.value)
    except ValueError:
        return copy.deepcopy(default)


def _write_json(db: Session, key: str, data) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=payload))
    else:
        row.value = payload
    db.flush()


def _validate_kop(val: int, field_label: str) -> int:
    if not isinstance(val, int) or isinstance(val, bool) or val < 0 or val > MAX_KOP:
        raise ValueError(f"Некорректная сумма поля «{field_label}»")
    return val


def format_rub(kop: int | None) -> str:
    """Форматирование копеек в читаемую строку в рублях без float."""
    if kop is None:
        return "0 ₽"
    sign = "-" if kop < 0 else ""
    abs_kop = abs(int(kop))
    rub, rem = divmod(abs_kop, 100)
    rub_str = f"{rub:,}".replace(",", " ")
    if rem:
        return f"{sign}{rub_str},{rem:02d} ₽"
    return f"{sign}{rub_str} ₽"


# --------------------------------------------------------------------------
#  Конфигурация, цены модулей и пакетные тарифы
# --------------------------------------------------------------------------
def get_config(db: Session) -> dict:
    raw = _read_json(db, KEY_CONFIG, DEFAULT_CONFIG)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    if cfg["billing_mode"] not in BILLING_MODES:
        cfg["billing_mode"] = "free_all"
    if cfg["vat_mode"] not in VAT_MODES:
        cfg["vat_mode"] = "none"
    if cfg["price_change_policy"] not in ("grandfather", "next_renewal"):
        cfg["price_change_policy"] = "grandfather"
    return cfg


def update_config(db: Session, patch: dict) -> tuple[dict, dict]:
    """Обновить глобальные настройки биллинга; возвращает (old_cfg, new_cfg)."""
    old_cfg = get_config(db)
    cfg = copy.deepcopy(old_cfg)
    if "billing_mode" in patch and patch["billing_mode"] is not None:
        mode = str(patch["billing_mode"]).strip()
        if mode not in BILLING_MODES:
            raise ValueError("Недопустимый режим тарификации")
        cfg["billing_mode"] = mode
    if "trial_days" in patch and patch["trial_days"] is not None:
        td = int(patch["trial_days"])
        if not 0 <= td <= 365:
            raise ValueError("Пробный период должен быть от 0 до 365 дней")
        cfg["trial_days"] = td
    if "grace_days" in patch and patch["grace_days"] is not None:
        gd = int(patch["grace_days"])
        if not 0 <= gd <= 90:
            raise ValueError("Льготный период должен быть от 0 до 90 дней")
        cfg["grace_days"] = gd
    if "annual_discount_pct" in patch and patch["annual_discount_pct"] is not None:
        ad = int(patch["annual_discount_pct"])
        if not 0 <= ad <= 60:
            raise ValueError("Годовая скидка должна быть от 0 до 60%")
        cfg["annual_discount_pct"] = ad
    if "vat_mode" in patch and patch["vat_mode"] is not None:
        vm = str(patch["vat_mode"]).strip()
        if vm not in VAT_MODES:
            raise ValueError("Неизвестная ставка НДС")
        cfg["vat_mode"] = vm
    if "price_change_policy" in patch and patch["price_change_policy"] is not None:
        pol = str(patch["price_change_policy"]).strip()
        if pol not in ("grandfather", "next_renewal"):
            raise ValueError("Неизвестная политика изменения цен")
        cfg["price_change_policy"] = pol
    for str_key in (
        "seller_name", "seller_inn", "seller_kpp", "seller_address",
        "seller_bank", "seller_bik", "seller_account", "seller_corr_account",
        "invoice_prefix", "support_email",
    ):
        if str_key in patch and patch[str_key] is not None:
            cfg[str_key] = str(patch[str_key]).strip()[:300]
    _write_json(db, KEY_CONFIG, cfg)
    return old_cfg, cfg


def get_module_prices(db: Session) -> dict[str, dict]:
    stored = _read_json(db, KEY_MODULES, {})
    out: dict[str, dict] = {}
    for code, meta in entitlements.MODULES.items():
        base = copy.deepcopy(DEFAULT_MODULE_PRICES.get(code, {
            "code": code,
            "monthly_kop": 99_000,
            "yearly_kop": 990_000,
            "monthly_quota": 0,
            "quota_unit": "",
            "available": bool(meta.get("available", True)),
            "price_version": 1,
            "price_history": [],
        }))
        if isinstance(stored, dict) and isinstance(stored.get(code), dict):
            base.update(stored[code])
        base["code"] = code
        base["title"] = meta["title"]
        base["description"] = meta["description"]
        base["views"] = list(meta["views"])
        out[code] = base
    return out


def update_module_price(
    db: Session,
    code: str,
    *,
    monthly_kop: int,
    yearly_kop: int,
    monthly_quota: int,
    available: bool,
    apply_policy: str | None = None,
    actor: str = "admin",
) -> dict:
    if code not in entitlements.MODULES:
        raise ValueError("Неизвестный модуль подписки")
    _validate_kop(monthly_kop, "Цена за месяц")
    _validate_kop(yearly_kop, "Цена за год")
    if monthly_kop > 0 and yearly_kop > monthly_kop * 12:
        raise ValueError(
            "Годовая цена модуля не может превышать 12 месячных платежей (проверьте ввод)"
        )
    if not isinstance(monthly_quota, int) or monthly_quota < 0 or monthly_quota > 10_000_000:
        raise ValueError("Некорректный месячный лимит модуля")

    stored = _read_json(db, KEY_MODULES, {})
    if not isinstance(stored, dict):
        stored = {}
    cur = get_module_prices(db)[code]
    price_changed = (
        cur["monthly_kop"] != monthly_kop or cur["yearly_kop"] != yearly_kop
    )
    history = list(cur.get("price_history") or [])
    version = int(cur.get("price_version") or 1)
    if price_changed:
        history.append({
            "version": version,
            "monthly_kop": cur["monthly_kop"],
            "yearly_kop": cur["yearly_kop"],
            "changed_at": _now_iso(),
            "changed_by": actor,
        })
        history = history[-20:]
        version += 1

    stored[code] = {
        "code": code,
        "monthly_kop": monthly_kop,
        "yearly_kop": yearly_kop,
        "monthly_quota": monthly_quota,
        "quota_unit": cur.get("quota_unit", ""),
        "available": bool(available),
        "price_version": version,
        "price_history": history,
        "updated_at": _now_iso(),
    }
    _write_json(db, KEY_MODULES, stored)

    policy = apply_policy or get_config(db)["price_change_policy"]
    if price_changed and policy == "next_renewal":
        _schedule_price_refresh_for_module(db, code)
    return get_module_prices(db)[code]


def get_plans(db: Session, *, include_inactive: bool = True) -> list[dict]:
    stored = _read_json(db, KEY_PLANS, None)
    plans = copy.deepcopy(DEFAULT_PLANS) if not isinstance(stored, list) or not stored else stored
    plans.sort(key=lambda p: (int(p.get("sort_order") or 100), str(p.get("code") or "")))
    if not include_inactive:
        return [p for p in plans if p.get("active", True)]
    return plans


def get_plan_map(db: Session) -> dict[str, dict]:
    return {p["code"]: p for p in get_plans(db, include_inactive=True)}


def upsert_plan(
    db: Session,
    payload: dict,
    *,
    apply_policy: str | None = None,
    actor: str = "admin",
) -> dict:
    code = str(payload.get("code") or "").strip().lower()
    if not _CODE_RE.fullmatch(code):
        raise ValueError("Код тарифа должен содержать 2–40 латинских символов, цифр или '_'")
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 120:
        raise ValueError("Укажите название тарифа (до 120 символов)")

    modules = [
        str(m).strip()
        for m in (payload.get("modules") or [])
        if str(m).strip() in entitlements.MODULES
    ]
    modules = list(dict.fromkeys(modules))
    if not modules:
        raise ValueError("Выберите хотя бы один модуль, входящий в тариф")

    monthly_kop = _validate_kop(int(payload.get("monthly_kop") or 0), "Цена за 1 мес.")
    quarterly_kop = _validate_kop(
        int(payload.get("quarterly_kop") if payload.get("quarterly_kop") is not None else monthly_kop * 3),
        "Цена за 3 мес.",
    )
    yearly_kop = _validate_kop(
        int(payload.get("yearly_kop") if payload.get("yearly_kop") is not None else monthly_kop * 10),
        "Цена за 12 мес.",
    )
    if monthly_kop > 0 and yearly_kop > monthly_kop * 12:
        raise ValueError("Годовая цена тарифа не может превышать 12 месячных платежей")
    if monthly_kop > 0 and quarterly_kop > monthly_kop * 3:
        raise ValueError("Квартальная цена тарифа не может превышать 3 месячных платежа")

    raw_limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    limits = {
        "users": max(0, min(100_000, int(raw_limits.get("users") or 0))),
        "receipts_monthly": max(0, min(10_000_000, int(raw_limits.get("receipts_monthly") or 0))),
        "upd_monthly": max(0, min(10_000_000, int(raw_limits.get("upd_monthly") or 0))),
        "counterparties_monthly": max(0, min(10_000_000, int(raw_limits.get("counterparties_monthly") or 0))),
    }

    plans = get_plans(db, include_inactive=True)
    existing = next((p for p in plans if p["code"] == code), None)
    version = int(existing.get("price_version") or 1) if existing else 1
    history = list(existing.get("price_history") or []) if existing else []
    price_changed = bool(
        existing and (
            existing.get("monthly_kop") != monthly_kop
            or existing.get("quarterly_kop") != quarterly_kop
            or existing.get("yearly_kop") != yearly_kop
        )
    )
    if price_changed and existing is not None:
        history.append({
            "version": version,
            "monthly_kop": existing.get("monthly_kop", 0),
            "quarterly_kop": existing.get("quarterly_kop", 0),
            "yearly_kop": existing.get("yearly_kop", 0),
            "changed_at": _now_iso(),
            "changed_by": actor,
        })
        history = history[-20:]
        version += 1

    item = {
        "code": code,
        "title": title,
        "badge": str(payload.get("badge") or "").strip()[:40],
        "description": str(payload.get("description") or "").strip()[:400],
        "modules": modules,
        "monthly_kop": monthly_kop,
        "quarterly_kop": quarterly_kop,
        "yearly_kop": yearly_kop,
        "limits": limits,
        "active": bool(payload.get("active", True)),
        "sort_order": int(payload.get("sort_order") if payload.get("sort_order") is not None else (existing or {}).get("sort_order", 50)),
        "price_version": version,
        "price_history": history,
        "updated_at": _now_iso(),
    }
    if existing is not None:
        idx = plans.index(existing)
        plans[idx] = item
    else:
        plans.append(item)
    _write_json(db, KEY_PLANS, plans)

    policy = apply_policy or get_config(db)["price_change_policy"]
    if price_changed and policy == "next_renewal":
        _schedule_price_refresh_for_plan(db, code)
    return item


def _iter_stored_subscriptions(db: Session) -> list[tuple[str, dict]]:
    rows = db.scalars(
        select(AppSetting).where(AppSetting.key.like(f"{SUB_PREFIX}%"))
    ).all()
    out: list[tuple[str, dict]] = []
    for r in rows:
        cid = r.key[len(SUB_PREFIX):]
        if not cid:
            continue
        try:
            d = json.loads(r.value or "{}")
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append((cid, d))
    return out


def _schedule_price_refresh_for_plan(db: Session, plan_code: str) -> None:
    """Политика next_renewal: помечает активные подписки плана к переходу на новую цену при продлении."""
    for cid, sub in _iter_stored_subscriptions(db):
        if sub.get("plan_code") == plan_code and sub.get("price_locked"):
            sub["price_locked"] = False
            sub["pending_new_price"] = True
            _write_json(db, f"{SUB_PREFIX}{cid}", sub)


def _schedule_price_refresh_for_module(db: Session, module_code: str) -> None:
    for cid, sub in _iter_stored_subscriptions(db):
        addons = sub.get("addon_modules") or []
        if module_code in addons and sub.get("price_locked"):
            sub["price_locked"] = False
            sub["pending_new_price"] = True
            _write_json(db, f"{SUB_PREFIX}{cid}", sub)


# --------------------------------------------------------------------------
#  Промокоды
# --------------------------------------------------------------------------
def get_promos(db: Session) -> list[dict]:
    raw = _read_json(db, KEY_PROMOS, [])
    return raw if isinstance(raw, list) else []


def create_or_update_promo(db: Session, payload: dict) -> dict:
    code = str(payload.get("code") or "").strip().upper()
    if not _PROMO_RE.fullmatch(code):
        raise ValueError("Код промокода: 2–32 символа A–Z, 0–9, '-' или '_'")
    kind = str(payload.get("discount_kind") or "percent").strip()
    if kind not in ("percent", "fixed_kop"):
        raise ValueError("Тип скидки должен быть percent или fixed_kop")
    val = int(payload.get("discount_value") or 0)
    if kind == "percent" and not 0 <= val <= 100:
        raise ValueError("Процент скидки промокода должен быть от 0 до 100%")
    if kind == "fixed_kop":
        _validate_kop(val, "Скидка промокода")
    bonus_days = int(payload.get("bonus_trial_days") or 0)
    if not 0 <= bonus_days <= 365:
        raise ValueError("Бонусные дни должны быть от 0 до 365")
    max_uses = max(0, int(payload.get("max_uses") or 0))
    valid_until = str(payload.get("valid_until") or "").strip()[:30]
    if valid_until and _parse_iso(valid_until) is None:
        raise ValueError("Некорректная дата окончания промокода")

    promos = get_promos(db)
    existing = next((p for p in promos if p.get("code") == code), None)
    entry = {
        "code": code,
        "title": str(payload.get("title") or "").strip()[:120] or f"Промокод {code}",
        "discount_kind": kind,
        "discount_value": val,
        "bonus_trial_days": bonus_days,
        "valid_until": valid_until,
        "max_uses": max_uses,
        "used_count": int((existing or {}).get("used_count") or 0),
        "active": bool(payload.get("active", True)),
        "created_at": (existing or {}).get("created_at") or _now_iso(),
    }
    if existing is not None:
        promos[promos.index(existing)] = entry
    else:
        promos.insert(0, entry)
    _write_json(db, KEY_PROMOS, promos)
    return entry


def deactivate_promo(db: Session, code: str) -> dict:
    norm = code.strip().upper()
    promos = get_promos(db)
    existing = next((p for p in promos if p.get("code") == norm), None)
    if existing is None:
        raise LookupError("Промокод не найден")
    existing["active"] = False
    _write_json(db, KEY_PROMOS, promos)
    return existing


def validate_promo(db: Session, code: str | None) -> dict | None:
    if not code or not code.strip():
        return None
    norm = code.strip().upper()
    promos = get_promos(db)
    promo = next((p for p in promos if p.get("code") == norm), None)
    if promo is None or not promo.get("active", True):
        raise ValueError("Промокод не найден или отключён")
    if promo.get("valid_until"):
        exp = _parse_iso(promo["valid_until"])
        if exp and _utcnow() > exp + timedelta(days=1):
            raise ValueError("Срок действия промокода истёк")
    max_uses = int(promo.get("max_uses") or 0)
    if max_uses > 0 and int(promo.get("used_count") or 0) >= max_uses:
        raise ValueError("Лимит активаций промокода исчерпан")
    return promo


def _consume_promo(db: Session, code: str | None) -> None:
    if not code:
        return
    norm = code.strip().upper()
    promos = get_promos(db)
    for p in promos:
        if p.get("code") == norm:
            p["used_count"] = int(p.get("used_count") or 0) + 1
            _write_json(db, KEY_PROMOS, promos)
            return


# --------------------------------------------------------------------------
#  Расчёт стоимости (Quote), Подписки компаний и Использование квот
# --------------------------------------------------------------------------
def catalog_price_for_selection(
    db: Session,
    *,
    plan_code: str | None,
    addon_modules: list[str] | None,
    billing_cycle: str = "monthly",
) -> tuple[int, list[str], dict]:
    """Базовая стоимость по текущему прайс-листу, итоговый список модулей и лимиты."""
    if billing_cycle not in CYCLES_MONTHS:
        raise ValueError("Неизвестный расчётный период")
    months = CYCLES_MONTHS[billing_cycle]
    plan_map = get_plan_map(db)
    mod_prices = get_module_prices(db)

    included: list[str] = []
    limits = {
        "users": 0,
        "receipts_monthly": 0,
        "upd_monthly": 0,
        "counterparties_monthly": 0,
    }
    base_kop = 0
    if plan_code:
        plan = plan_map.get(plan_code)
        if plan is None:
            raise ValueError("Тарифный план не найден")
        included.extend(plan.get("modules") or [])
        limits.update(plan.get("limits") or {})
        if billing_cycle == "monthly":
            base_kop += int(plan.get("monthly_kop") or 0)
        elif billing_cycle == "quarterly":
            base_kop += int(plan.get("quarterly_kop") or 0)
        else:
            base_kop += int(plan.get("yearly_kop") or 0)

    clean_addons: list[str] = []
    for m in addon_modules or []:
        mc = str(m).strip()
        if mc in mod_prices and mc not in included and mc not in clean_addons:
            clean_addons.append(mc)
    for mc in clean_addons:
        mp = mod_prices[mc]
        if billing_cycle == "yearly":
            base_kop += int(mp.get("yearly_kop") or 0)
        else:
            base_kop += int(mp.get("monthly_kop") or 0) * months
        q = int(mp.get("monthly_quota") or 0)
        if mc == "receipts" and q and not limits["receipts_monthly"]:
            limits["receipts_monthly"] = q
        elif mc == "upd" and q and not limits["upd_monthly"]:
            limits["upd_monthly"] = q
        elif mc == "counterparties" and q and not limits["counterparties_monthly"]:
            limits["counterparties_monthly"] = q

    all_modules = included + clean_addons
    return base_kop, all_modules, limits


def quote_subscription(
    db: Session,
    *,
    company_id: str | None = None,
    plan_code: str | None = None,
    addon_modules: list[str] | None = None,
    billing_cycle: str = "monthly",
    promo_code: str | None = None,
    custom_discount_pct: int | None = None,
    custom_price_kop: int | None = None,
) -> dict:
    """Точный серверный расчёт стоимости подписки или счёта."""
    cfg = get_config(db)
    months = CYCLES_MONTHS.get(billing_cycle)
    if months is None:
        raise ValueError("Выберите период: monthly, quarterly или yearly")

    sub_raw = _read_raw_subscription(db, company_id) if company_id else None
    catalog_kop, modules, limits = catalog_price_for_selection(
        db, plan_code=plan_code, addon_modules=addon_modules, billing_cycle=billing_cycle
    )
    if not modules:
        raise ValueError("Выберите тарифный план или хотя бы один модуль")

    # Если у компании действует Grandfathering на тот же план/период/модули — держим её цену
    used_grandfathered = False
    base_kop = catalog_kop
    if (
        custom_price_kop is None
        and sub_raw
        and sub_raw.get("price_locked")
        and sub_raw.get("locked_price_kop") is not None
        and sub_raw.get("plan_code") == (plan_code or "")
        and (sub_raw.get("addon_modules") or []) == [m for m in (addon_modules or []) if m not in (get_plan_map(db).get(plan_code or "", {}).get("modules") or [])]
        and sub_raw.get("billing_cycle") == billing_cycle
    ):
        base_kop = int(sub_raw["locked_price_kop"])
        used_grandfathered = True

    if custom_price_kop is not None:
        base_kop = _validate_kop(int(custom_price_kop), "Индивидуальная цена")

    discount_pct = (
        int(custom_discount_pct)
        if custom_discount_pct is not None
        else int((sub_raw or {}).get("custom_discount_pct") or 0)
    )
    discount_pct = max(0, min(100, discount_pct))
    discount_kop = (base_kop * discount_pct) // 100

    promo = validate_promo(db, promo_code) if promo_code else None
    promo_discount_kop = 0
    bonus_days = 0
    if promo is not None:
        after_custom = max(base_kop - discount_kop, 0)
        if promo["discount_kind"] == "percent":
            promo_discount_kop = (after_custom * int(promo["discount_value"])) // 100
        else:
            promo_discount_kop = min(after_custom, int(promo["discount_value"]))
        bonus_days = int(promo.get("bonus_trial_days") or 0)

    total_discount_kop = min(base_kop, discount_kop + promo_discount_kop)
    total_kop = max(base_kop - total_discount_kop, 0)

    vat_label, vat_rate = VAT_MODES.get(cfg["vat_mode"], VAT_MODES["none"])
    vat_kop = (total_kop * vat_rate) // (100 + vat_rate) if vat_rate > 0 else 0

    return {
        "plan_code": plan_code or "",
        "addon_modules": [m for m in (addon_modules or []) if m in entitlements.MODULES],
        "modules": modules,
        "limits": limits,
        "billing_cycle": billing_cycle,
        "cycle_title": CYCLE_TITLES[billing_cycle],
        "months": months,
        "catalog_kop": catalog_kop,
        "subtotal_kop": base_kop,
        "discount_kop": total_discount_kop,
        "discount_pct": discount_pct,
        "promo_discount_kop": promo_discount_kop,
        "promo_code": promo["code"] if promo else "",
        "bonus_trial_days": bonus_days,
        "vat_mode": cfg["vat_mode"],
        "vat_label": vat_label,
        "vat_kop": vat_kop,
        "total_kop": total_kop,
        "total_formatted": format_rub(total_kop),
        "monthly_equivalent_kop": total_kop // months,
        "grandfathered": used_grandfathered,
    }


def _read_raw_subscription(db: Session, company_id: str | None) -> dict | None:
    if not company_id:
        return None
    row = db.get(AppSetting, f"{SUB_PREFIX}{company_id}")
    if row is None or not (row.value or "").strip():
        return None
    try:
        d = json.loads(row.value)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def company_monthly_usage(db: Session, company_id: str | None) -> dict:
    """Фактическое потребление квот компании за текущий календарный месяц UTC."""
    now = _utcnow()
    month_start = datetime(now.year, now.month, 1)
    period_key = f"{now.year:04d}-{now.month:02d}"
    if not company_id:
        return {
            "period": period_key,
            "receipts": 0,
            "upd": 0,
            "counterparties": 0,
            "users": 0,
        }
    rc_cnt = db.scalar(
        select(func.count()).select_from(Receipt).where(
            Receipt.company_id == company_id,
            Receipt.created_at >= month_start,
        )
    ) or 0
    upd_cnt = db.scalar(
        select(func.count()).select_from(PrimaryDocument).where(
            PrimaryDocument.company_id == company_id,
            PrimaryDocument.created_at >= month_start,
        )
    ) or 0
    cp_cnt = db.scalar(
        select(func.count()).select_from(Counterparty).where(
            Counterparty.company_id == company_id,
        )
    ) or 0
    usr_cnt = db.scalar(
        select(func.count()).select_from(User).where(
            User.company_id == company_id,
            User.is_active.is_(True),
        )
    ) or 0
    return {
        "period": period_key,
        "receipts": int(rc_cnt),
        "upd": int(upd_cnt),
        "counterparties": int(cp_cnt),
        "users": int(usr_cnt),
    }


def get_company_subscription(db: Session, company_id: str | None) -> dict:
    """Полное вычисленное состояние подписки, модулей и лимитов компании."""
    cfg = get_config(db)
    mod_prices = get_module_prices(db)
    plan_map = get_plan_map(db)
    raw = _read_raw_subscription(db, company_id)
    now = _utcnow()

    # Явное переопределение модулей через entitlements.set_company_modules (Этап 1)
    explicit_override_row = (
        db.get(AppSetting, f"{entitlements.SETTING_PREFIX}{company_id}")
        if company_id else None
    )
    has_explicit_modules = bool(
        explicit_override_row and (explicit_override_row.value or "").strip()
    )

    if raw is None:
        default_mods = [
            code for code, m in mod_prices.items() if m.get("available", True)
        ]
        if has_explicit_modules and explicit_override_row is not None:
            try:
                parsed = json.loads(explicit_override_row.value)
                if isinstance(parsed, list):
                    default_mods = [str(x) for x in parsed if str(x) in entitlements.MODULES]
            except ValueError:
                pass
        active_mods = default_mods if cfg["billing_mode"] != "strict" or has_explicit_modules else []
        status = "free" if cfg["billing_mode"] != "strict" else "expired"
        usage = company_monthly_usage(db, company_id)
        warnings: list[str] = []
        if status == "expired":
            warnings.append("Платная подписка не оформлена — доступен просмотр архива (режим чтения).")
        return {
            "company_id": company_id,
            "billing_mode": cfg["billing_mode"],
            "status": status,
            "status_title": STATUS_TITLES.get(status, status),
            "plan_code": "",
            "plan_title": "Базовый доступ" if status == "free" else "Подписка не подключена",
            "billing_cycle": "monthly",
            "addon_modules": [],
            "enabled_modules": active_mods,
            "read_only": status == "expired" and cfg["billing_mode"] == "strict",
            "locked_price_kop": 0,
            "catalog_price_kop": 0,
            "price_locked": False,
            "grandfathered": False,
            "custom_discount_pct": 0,
            "started_at": None,
            "period_end": None,
            "grace_until": None,
            "days_left": None,
            "limits": {"users": 0, "receipts_monthly": 0, "upd_monthly": 0, "counterparties_monthly": 0},
            "usage": usage,
            "contract_number": "",
            "contract_date": "",
            "admin_note": "",
            "warnings": warnings,
        }

    stored_status = str(raw.get("status") or "active")
    plan_code = str(raw.get("plan_code") or "")
    plan = plan_map.get(plan_code)
    cycle = str(raw.get("billing_cycle") or "monthly")
    if cycle not in CYCLES_MONTHS:
        cycle = "monthly"
    addons = [str(m) for m in (raw.get("addon_modules") or []) if str(m) in entitlements.MODULES]

    plan_mods = list(plan.get("modules") or []) if plan else []
    configured_mods = list(dict.fromkeys(plan_mods + addons))
    if raw.get("custom_modules") is not None and isinstance(raw.get("custom_modules"), list):
        configured_mods = [str(m) for m in raw["custom_modules"] if str(m) in entitlements.MODULES]

    period_end_dt = _parse_iso(raw.get("period_end"))
    grace_until_dt = _parse_iso(raw.get("grace_until"))
    if period_end_dt is not None and grace_until_dt is None and cfg["grace_days"] > 0:
        grace_until_dt = period_end_dt + timedelta(days=int(cfg["grace_days"]))

    eff_status = stored_status
    days_left: int | None = None
    if stored_status in ("partner", "suspended", "free"):
        eff_status = stored_status
    elif period_end_dt is not None:
        delta_sec = (period_end_dt - now).total_seconds()
        days_left = max(0, int((delta_sec + 86399) // 86400)) if delta_sec > 0 else 0
        if now <= period_end_dt:
            eff_status = "trial" if stored_status == "trial" else "active"
        elif grace_until_dt is not None and now <= grace_until_dt:
            eff_status = "grace"
        else:
            eff_status = "expired"

    # Какие модули реально открыты прямо сейчас
    if cfg["billing_mode"] == "free_all" and not raw.get("enforce_in_free_mode"):
        enabled_mods = configured_mods or [
            c for c, m in mod_prices.items() if m.get("available", True)
        ]
        read_only = False
    elif eff_status in ("active", "trial", "grace", "partner", "free"):
        enabled_mods = configured_mods
        read_only = False
    else:
        # expired / suspended
        if cfg["billing_mode"] == "strict":
            enabled_mods = []
            read_only = True
        else:
            enabled_mods = configured_mods
            read_only = False

    # Базовые лимиты тарифа + переопределения компании
    try:
        cat_kop, _, base_limits = catalog_price_for_selection(
            db, plan_code=plan_code or None, addon_modules=addons, billing_cycle=cycle
        )
    except ValueError:
        cat_kop = 0
        base_limits = {"users": 0, "receipts_monthly": 0, "upd_monthly": 0, "counterparties_monthly": 0}
    custom_limits = raw.get("custom_limits") if isinstance(raw.get("custom_limits"), dict) else {}
    limits = dict(base_limits)
    for k in ("users", "receipts_monthly", "upd_monthly", "counterparties_monthly"):
        if custom_limits.get(k) is not None:
            limits[k] = max(0, int(custom_limits[k]))

    locked_kop = int(raw.get("locked_price_kop") if raw.get("locked_price_kop") is not None else cat_kop)
    price_locked = bool(raw.get("price_locked", True))
    grandfathered = bool(price_locked and cat_kop > 0 and locked_kop != cat_kop)

    usage = company_monthly_usage(db, company_id)
    warnings = []
    if eff_status == "trial" and days_left is not None and days_left <= 5:
        warnings.append(f"Пробный период завершается через {days_left} дн. — оформите счёт заранее.")
    elif eff_status == "grace":
        grace_days_left = (
            max(0, int(((grace_until_dt - now).total_seconds() + 86399) // 86400))
            if grace_until_dt else 0
        )
        warnings.append(
            f"Оплаченный период истёк — действует льготный доступ ещё {grace_days_left} дн. без блокировки работы."
        )
    elif eff_status == "expired":
        warnings.append(
            "Срок подписки истёк — все ранее загруженные документы доступны для чтения и выгрузки; для добавления новых продлите подписку."
        )
    elif eff_status == "suspended":
        warnings.append("Подписка организации приостановлена администратором.")

    for metric_key, lim_key, label in (
        ("receipts", "receipts_monthly", "чеков"),
        ("upd", "upd_monthly", "УПД"),
        ("counterparties", "counterparties_monthly", "контрагентов"),
    ):
        lim_val = int(limits.get(lim_key) or 0)
        used_val = int(usage.get(metric_key) or 0)
        if lim_val > 0 and used_val >= lim_val:
            warnings.append(f"Достигнут месячный лимит {label}: {used_val} из {lim_val}.")
        elif lim_val > 0 and used_val >= int(lim_val * 0.8):
            warnings.append(f"Израсходовано более 80% месячного лимита {label}: {used_val} из {lim_val}.")

    plan_title = (
        plan["title"]
        if plan
        else ("Партнёрский тариф" if eff_status == "partner" else "Индивидуальный набор модулей")
    )
    return {
        "company_id": company_id,
        "billing_mode": cfg["billing_mode"],
        "status": eff_status,
        "stored_status": stored_status,
        "status_title": STATUS_TITLES.get(eff_status, eff_status),
        "plan_code": plan_code,
        "plan_title": plan_title,
        "plan_version": int(raw.get("plan_version") or (plan or {}).get("price_version") or 1),
        "billing_cycle": cycle,
        "cycle_title": CYCLE_TITLES.get(cycle, cycle),
        "addon_modules": addons,
        "configured_modules": configured_mods,
        "enabled_modules": enabled_mods,
        "read_only": read_only,
        "locked_price_kop": locked_kop,
        "locked_price_formatted": format_rub(locked_kop),
        "catalog_price_kop": cat_kop,
        "price_locked": price_locked,
        "grandfathered": grandfathered,
        "custom_discount_pct": int(raw.get("custom_discount_pct") or 0),
        "started_at": raw.get("started_at"),
        "period_end": iso_z(period_end_dt) if period_end_dt else None,
        "grace_until": iso_z(grace_until_dt) if grace_until_dt else None,
        "days_left": days_left,
        "limits": limits,
        "usage": usage,
        "contract_number": str(raw.get("contract_number") or ""),
        "contract_date": str(raw.get("contract_date") or ""),
        "admin_note": str(raw.get("admin_note") or ""),
        "warnings": warnings,
    }


def assign_company_subscription(
    db: Session,
    company_id: str,
    payload: dict,
    *,
    actor: str = "admin",
) -> dict:
    """Назначить или изменить подписку компании (администратор)."""
    comp = db.get(Company, company_id)
    if comp is None:
        raise LookupError("Компания не найдена")
    cfg = get_config(db)
    prev = _read_raw_subscription(db, company_id) or {}

    status = str(payload.get("status") or prev.get("status") or "active").strip()
    if status not in STATUS_TITLES:
        raise ValueError("Недопустимый статус подписки")

    plan_code = str(payload.get("plan_code") if "plan_code" in payload else prev.get("plan_code") or "").strip()
    plan_map = get_plan_map(db)
    if plan_code and plan_code not in plan_map:
        raise ValueError("Тарифный план не найден")

    cycle = str(payload.get("billing_cycle") or prev.get("billing_cycle") or "monthly").strip()
    if cycle not in CYCLES_MONTHS:
        raise ValueError("Недопустимый расчётный период")

    addons = [
        str(m).strip()
        for m in (payload.get("addon_modules") if "addon_modules" in payload else (prev.get("addon_modules") or []))
        if str(m).strip() in entitlements.MODULES
    ]
    plan_mods = list(plan_map[plan_code].get("modules") or []) if plan_code else []
    addons = [m for m in dict.fromkeys(addons) if m not in plan_mods]

    custom_discount = int(
        payload.get("custom_discount_pct")
        if payload.get("custom_discount_pct") is not None
        else (prev.get("custom_discount_pct") or 0)
    )
    if not 0 <= custom_discount <= 100:
        raise ValueError("Индивидуальная скидка должна быть от 0 до 100%")

    try:
        cat_kop, all_mods, _ = catalog_price_for_selection(
            db, plan_code=plan_code or None, addon_modules=addons, billing_cycle=cycle
        )
    except ValueError:
        cat_kop = 0
        all_mods = list(dict.fromkeys(plan_mods + addons))

    if payload.get("locked_price_kop") is not None:
        locked_kop = _validate_kop(int(payload["locked_price_kop"]), "Фиксированная цена")
    elif prev.get("locked_price_kop") is not None and payload.get("keep_locked_price"):
        locked_kop = int(prev["locked_price_kop"])
    else:
        locked_kop = max(0, cat_kop - (cat_kop * custom_discount) // 100)

    now = _utcnow()
    if "period_end" in payload and payload["period_end"]:
        pe_dt = _parse_iso(str(payload["period_end"]))
        if pe_dt is None:
            raise ValueError("Некорректная дата окончания периода")
    elif payload.get("duration_days") is not None:
        days = int(payload["duration_days"])
        if not 0 <= days <= 3650:
            raise ValueError("Длительность должна быть от 0 до 3650 дней")
        pe_dt = now + timedelta(days=days)
    elif status == "trial" and not prev.get("period_end"):
        pe_dt = now + timedelta(days=int(cfg["trial_days"]))
    elif prev.get("period_end"):
        pe_dt = _parse_iso(prev.get("period_end"))
    elif status in ("partner", "free"):
        pe_dt = None
    else:
        pe_dt = now + timedelta(days=30 * CYCLES_MONTHS[cycle])

    grace_dt = (
        pe_dt + timedelta(days=int(cfg["grace_days"]))
        if pe_dt is not None and int(cfg["grace_days"]) > 0
        else pe_dt
    )

    raw_custom_limits = payload.get("custom_limits")
    custom_limits = prev.get("custom_limits") if not isinstance(raw_custom_limits, dict) else {
        k: max(0, int(raw_custom_limits[k]))
        for k in ("users", "receipts_monthly", "upd_monthly", "counterparties_monthly")
        if raw_custom_limits.get(k) is not None
    }

    record = {
        "company_id": company_id,
        "status": status,
        "plan_code": plan_code,
        "plan_version": int((plan_map.get(plan_code) or {}).get("price_version") or 1),
        "billing_cycle": cycle,
        "addon_modules": addons,
        "locked_price_kop": locked_kop,
        "price_locked": bool(payload.get("price_locked", True)),
        "custom_discount_pct": custom_discount,
        "custom_limits": custom_limits or {},
        "started_at": prev.get("started_at") or _now_iso(),
        "period_end": iso_z(pe_dt) if pe_dt else "",
        "grace_until": iso_z(grace_dt) if grace_dt else "",
        "contract_number": str(
            payload.get("contract_number") if "contract_number" in payload else (prev.get("contract_number") or "")
        ).strip()[:80],
        "contract_date": str(
            payload.get("contract_date") if "contract_date" in payload else (prev.get("contract_date") or "")
        ).strip()[:20],
        "admin_note": str(
            payload.get("admin_note") if "admin_note" in payload else (prev.get("admin_note") or "")
        ).strip()[:500],
        "enforce_in_free_mode": bool(payload.get("enforce_in_free_mode", prev.get("enforce_in_free_mode", False))),
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    _write_json(db, f"{SUB_PREFIX}{company_id}", record)
    # Синхронизируем модули в entitlements для консистентности
    if all_mods:
        entitlements.set_company_modules(db, company_id, all_mods)
    return get_company_subscription(db, company_id)


def extend_company_subscription(
    db: Session,
    company_id: str,
    *,
    add_days: int = 0,
    target_date: str | None = None,
    reason: str = "",
    actor: str = "admin",
) -> dict:
    """Продлить подписку компании на +N дней или до указанной даты с фиксацией причины."""
    comp = db.get(Company, company_id)
    if comp is None:
        raise LookupError("Компания не найдена")
    cfg = get_config(db)
    now = _utcnow()
    raw = _read_raw_subscription(db, company_id)
    if raw is None:
        # Если у компании ещё не было записи подписки — открываем активный доступ
        raw = {
            "company_id": company_id,
            "status": "active",
            "plan_code": "accounting_pro",
            "plan_version": 1,
            "billing_cycle": "monthly",
            "addon_modules": [],
            "locked_price_kop": 0,
            "price_locked": True,
            "custom_discount_pct": 0,
            "custom_limits": {},
            "started_at": _now_iso(),
        }
    cur_end = _parse_iso(raw.get("period_end"))
    base_dt = cur_end if (cur_end and cur_end > now) else now
    if target_date:
        new_end = _parse_iso(target_date)
        if new_end is None:
            raise ValueError("Некорректная целевая дата продления")
    else:
        if not 1 <= int(add_days) <= 3650:
            raise ValueError("Количество дней продления должно быть от 1 до 3650")
        new_end = base_dt + timedelta(days=int(add_days))

    if raw.get("status") in ("expired", "grace", "free"):
        raw["status"] = "active"
    raw["period_end"] = iso_z(new_end)
    raw["grace_until"] = iso_z(new_end + timedelta(days=int(cfg["grace_days"])))
    if reason.strip():
        raw["admin_note"] = reason.strip()[:500]
    raw["updated_at"] = _now_iso()
    raw["updated_by"] = actor
    _write_json(db, f"{SUB_PREFIX}{company_id}", raw)
    return get_company_subscription(db, company_id)


def cleanup_company_billing(db: Session, company_id: str) -> None:
    """Удалить привязки подписки и счета при полном удалении компании (AuditLog сохраняется)."""
    for key in (f"{SUB_PREFIX}{company_id}", f"{entitlements.SETTING_PREFIX}{company_id}"):
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    inv_items = _read_json(db, KEY_INVOICES, [])
    if isinstance(inv_items, list):
        filtered = [x for x in inv_items if x.get("company_id") != company_id]
        if len(filtered) != len(inv_items):
            _write_json(db, KEY_INVOICES, filtered)
    db.flush()


# --------------------------------------------------------------------------
#  Счета на оплату и заявки от организаций
# --------------------------------------------------------------------------
def list_invoices(db: Session, *, company_id: str | None = None) -> list[dict]:
    items = _read_json(db, KEY_INVOICES, [])
    if not isinstance(items, list):
        return []
    if company_id:
        items = [x for x in items if x.get("company_id") == company_id]
    return items


def get_invoice(db: Session, invoice_id: str) -> dict | None:
    for inv in list_invoices(db):
        if inv.get("id") == invoice_id:
            return inv
    return None


def create_invoice(
    db: Session,
    *,
    company_id: str,
    plan_code: str | None,
    addon_modules: list[str] | None,
    billing_cycle: str = "monthly",
    promo_code: str | None = None,
    custom_discount_pct: int | None = None,
    custom_price_kop: int | None = None,
    comment: str = "",
    created_by: str = "",
    origin: str = "client",
) -> dict:
    comp = db.get(Company, company_id)
    if comp is None:
        raise LookupError("Компания не найдена")
    q = quote_subscription(
        db,
        company_id=company_id,
        plan_code=plan_code,
        addon_modules=addon_modules,
        billing_cycle=billing_cycle,
        promo_code=promo_code,
        custom_discount_pct=custom_discount_pct,
        custom_price_kop=custom_price_kop,
    )
    cfg = get_config(db)
    seq = int(cfg.get("next_invoice_seq") or 1)
    now = _utcnow()
    inv_number = f"{cfg.get('invoice_prefix') or 'ДВ'}-{now.year}-{seq:04d}"
    cfg["next_invoice_seq"] = seq + 1
    _write_json(db, KEY_CONFIG, cfg)

    if promo_code:
        _consume_promo(db, promo_code)

    plan_map = get_plan_map(db)
    mod_prices = get_module_prices(db)
    line_items: list[dict] = []
    if q["plan_code"] and q["plan_code"] in plan_map:
        pl = plan_map[q["plan_code"]]
        line_items.append({
            "title": f"Подписка DocVer — тариф «{pl['title']}» ({q['cycle_title']})",
            "qty": 1,
            "unit": "лиц.",
        })
    for mc in q["addon_modules"]:
        if mc in mod_prices and mc not in (plan_map.get(q["plan_code"], {}).get("modules") or []):
            line_items.append({
                "title": f"Дополнительный модуль «{mod_prices[mc]['title']}» ({q['cycle_title']})",
                "qty": 1,
                "unit": "модуль",
            })

    import uuid
    inv = {
        "id": str(uuid.uuid4()),
        "number": inv_number,
        "status": "issued",           # issued | paid | cancelled
        "origin": origin,             # client | admin
        "company_id": comp.id,
        "company_name": comp.name,
        "company_inn": comp.inn or "",
        "plan_code": q["plan_code"],
        "addon_modules": q["addon_modules"],
        "modules": q["modules"],
        "billing_cycle": q["billing_cycle"],
        "cycle_title": q["cycle_title"],
        "months": q["months"],
        "bonus_trial_days": q["bonus_trial_days"],
        "promo_code": q["promo_code"],
        "subtotal_kop": q["subtotal_kop"],
        "discount_kop": q["discount_kop"],
        "vat_mode": q["vat_mode"],
        "vat_label": q["vat_label"],
        "vat_kop": q["vat_kop"],
        "total_kop": q["total_kop"],
        "total_formatted": q["total_formatted"],
        "line_items": line_items,
        "seller_snapshot": {
            "name": cfg["seller_name"],
            "inn": cfg["seller_inn"],
            "kpp": cfg["seller_kpp"],
            "address": cfg["seller_address"],
            "bank": cfg["seller_bank"],
            "bik": cfg["seller_bik"],
            "account": cfg["seller_account"],
            "corr_account": cfg["seller_corr_account"],
        },
        "comment": comment.strip()[:400],
        "created_at": _now_iso(),
        "created_by": created_by,
        "paid_at": None,
        "payment_ref": "",
    }
    all_invs = list_invoices(db)
    all_invs.insert(0, inv)
    _write_json(db, KEY_INVOICES, all_invs[:1000])
    return inv


def mark_invoice_paid(
    db: Session,
    invoice_id: str,
    *,
    payment_ref: str = "",
    actor: str = "admin",
) -> tuple[dict, dict]:
    """Подтвердить оплату счёта и автоматически активировать/продлить подписку компании."""
    all_invs = list_invoices(db)
    inv = next((x for x in all_invs if x.get("id") == invoice_id), None)
    if inv is None:
        raise LookupError("Счёт не найден")
    if inv.get("status") == "paid":
        sub = get_company_subscription(db, inv["company_id"])
        return inv, sub
    if inv.get("status") == "cancelled":
        raise ValueError("Отменённый счёт нельзя отметить оплаченным")

    inv["status"] = "paid"
    inv["paid_at"] = _now_iso()
    inv["payment_ref"] = payment_ref.strip()[:120]
    inv["paid_by"] = actor
    _write_json(db, KEY_INVOICES, all_invs)

    cid = inv["company_id"]
    now = _utcnow()
    cur_raw = _read_raw_subscription(db, cid)
    cur_end = _parse_iso((cur_raw or {}).get("period_end"))
    start_from = cur_end if (cur_end and cur_end > now) else now
    months = int(inv.get("months") or 1)
    bonus_days = int(inv.get("bonus_trial_days") or 0)
    new_end = start_from + timedelta(days=30 * months + bonus_days)

    sub = assign_company_subscription(
        db,
        cid,
        {
            "status": "active",
            "plan_code": inv.get("plan_code") or "",
            "addon_modules": inv.get("addon_modules") or [],
            "billing_cycle": inv.get("billing_cycle") or "monthly",
            "locked_price_kop": int(inv.get("total_kop") or 0),
            "price_locked": True,
            "period_end": iso_z(new_end),
        },
        actor=actor,
    )
    return inv, sub


def cancel_invoice(db: Session, invoice_id: str, *, actor: str = "admin") -> dict:
    all_invs = list_invoices(db)
    inv = next((x for x in all_invs if x.get("id") == invoice_id), None)
    if inv is None:
        raise LookupError("Счёт не найден")
    if inv.get("status") == "paid":
        raise ValueError("Оплаченный счёт нельзя отменить")
    inv["status"] = "cancelled"
    inv["cancelled_at"] = _now_iso()
    inv["cancelled_by"] = actor
    _write_json(db, KEY_INVOICES, all_invs)
    return inv


def render_invoice_html(inv: dict) -> str:
    """Сформировать чистый печатный HTML-бланк счёта на оплату по стандарту РФ."""
    s = inv.get("seller_snapshot") or DEFAULT_CONFIG
    esc = html.escape
    created_date = str(inv.get("created_at") or "")[:10]
    status = inv.get("status") or "issued"
    badge = (
        '<span class="stamp paid">ОПЛАЧЕНО</span>'
        if status == "paid"
        else ('<span class="stamp cancelled">ОТМЕНЁН</span>' if status == "cancelled" else "")
    )
    items = inv.get("line_items") or [{"title": "Подписка на программное обеспечение DocVer", "qty": 1, "unit": "лиц."}]
    rows_html = ""
    for idx, it in enumerate(items, 1):
        price_str = inv["total_formatted"] if idx == 1 else "включено"
        rows_html += (
            f"<tr><td>{idx}</td><td>{esc(str(it.get('title') or ''))}</td>"
            f"<td class='num'>{int(it.get('qty') or 1)}</td>"
            f"<td>{esc(str(it.get('unit') or 'шт.'))}</td>"
            f"<td class='num'>{esc(price_str)}</td></tr>"
        )
    discount_row = (
        f"<tr><td colspan='4' class='right'>Скидка:</td><td class='num'>−{esc(format_rub(inv.get('discount_kop') or 0))}</td></tr>"
        if int(inv.get("discount_kop") or 0) > 0 else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Счёт № {esc(str(inv.get('number') or ''))}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; color:#0f172a; margin:32px auto; max-width:820px; padding:0 20px; line-height:1.45; }}
  .toolbar {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; padding:10px 14px; background:#f1f5f9; border-radius:10px; }}
  .btn {{ background:#1d4ed8; color:#fff; border:0; border-radius:8px; padding:8px 16px; font-weight:600; cursor:pointer; }}
  table.bank {{ width:100%; border-collapse:collapse; margin-bottom:20px; font-size:13px; }}
  table.bank td {{ border:1px solid #334155; padding:6px 8px; vertical-align:top; }}
  h1 {{ font-size:20px; margin:16px 0 8px; border-bottom:2px solid #0f172a; padding-bottom:8px; }}
  .parties {{ font-size:13.5px; margin:12px 0 18px; }}
  .parties div {{ margin-bottom:6px; }}
  table.items {{ width:100%; border-collapse:collapse; font-size:13.5px; margin-bottom:14px; }}
  table.items th, table.items td {{ border:1px solid #334155; padding:7px 9px; }}
  table.items th {{ background:#f8fafc; text-align:left; }}
  .num {{ text-align:right; white-space:nowrap; }}
  .right {{ text-align:right; font-weight:600; }}
  .stamp {{ display:inline-block; padding:4px 10px; border-radius:6px; font-size:12px; font-weight:700; margin-left:10px; }}
  .stamp.paid {{ background:#dcfce7; color:#166534; border:1px solid #16a34a; }}
  .stamp.cancelled {{ background:#fee2e2; color:#991b1b; border:1px solid #dc2626; }}
  .sign {{ margin-top:34px; display:flex; justify-content:space-between; font-size:13.5px; }}
  @media print {{ .toolbar {{ display:none; }} body {{ margin:0; }} }}
</style></head><body>
<div class="toolbar">
  <span>Счёт на оплату подписки DocVer № <b>{esc(str(inv.get('number') or ''))}</b></span>
  <button class="btn" onclick="window.print()">🖨 Печать / Сохранить PDF</button>
</div>
<table class="bank">
  <tr>
    <td colspan="2">Банк получателя: <b>{esc(str(s.get('bank') or ''))}</b></td>
    <td>БИК<br>Сч. №</td>
    <td>{esc(str(s.get('bik') or ''))}<br>{esc(str(s.get('corr_account') or ''))}</td>
  </tr>
  <tr>
    <td>ИНН {esc(str(s.get('inn') or ''))}</td>
    <td>КПП {esc(str(s.get('kpp') or ''))}</td>
    <td rowspan="2">Сч. №</td>
    <td rowspan="2">{esc(str(s.get('account') or ''))}</td>
  </tr>
  <tr><td colspan="2">Получатель: <b>{esc(str(s.get('name') or ''))}</b></td></tr>
</table>
<h1>Счёт на оплату № {esc(str(inv.get('number') or ''))} от {esc(created_date)} {badge}</h1>
<div class="parties">
  <div><b>Поставщик:</b> {esc(str(s.get('name') or ''))}, ИНН {esc(str(s.get('inn') or ''))}, КПП {esc(str(s.get('kpp') or ''))}, {esc(str(s.get('address') or ''))}</div>
  <div><b>Покупатель:</b> {esc(str(inv.get('company_name') or ''))}{', ИНН ' + esc(str(inv.get('company_inn'))) if inv.get('company_inn') else ''}</div>
  <div><b>Назначение платежа:</b> Оплата по счёту № {esc(str(inv.get('number') or ''))} от {esc(created_date)} за право использования ПО DocVer ({esc(str(inv.get('vat_label') or 'Без НДС'))}).</div>
</div>
<table class="items">
  <thead><tr><th style="width:36px">№</th><th>Наименование</th><th style="width:60px">Кол-во</th><th style="width:60px">Ед.</th><th style="width:130px">Сумма</th></tr></thead>
  <tbody>
    {rows_html}
    {discount_row}
    <tr><td colspan="4" class="right">Итого к оплате:</td><td class="num"><b>{esc(str(inv.get('total_formatted') or ''))}</b></td></tr>
    <tr><td colspan="4" class="right">{esc(str(inv.get('vat_label') or 'Без НДС'))}:</td><td class="num">{esc(format_rub(inv.get('vat_kop') or 0)) if inv.get('vat_kop') else '—'}</td></tr>
  </tbody>
</table>
<p style="font-size:12.5px;color:#475569">Оплата настоящего счёта означает согласие с условиями лицензионного договора-оферты DocVer. Доступ к выбранным модулям активируется на {int(inv.get('months') or 1)} мес.</p>
<div class="sign">
  <div>Руководитель ______________________</div>
  <div>Главный бухгалтер ______________________</div>
</div>
</body></html>"""


# --------------------------------------------------------------------------
#  Аналитика администратора (MRR / ARR / Воронка / Журнал аудита)
# --------------------------------------------------------------------------
def admin_overview(db: Session) -> dict:
    cfg = get_config(db)
    mod_prices = get_module_prices(db)
    plans = get_plans(db, include_inactive=True)
    promos = get_promos(db)
    invoices = list_invoices(db)

    companies = db.scalars(
        select(Company).order_by(Company.is_active.desc(), Company.name.asc())
    ).all()

    mrr_kop = 0
    counts = {
        "total_companies": len(companies),
        "active": 0,
        "trial": 0,
        "grace": 0,
        "expired": 0,
        "partner": 0,
        "free": 0,
        "suspended": 0,
        "expiring_7d": 0,
        "expiring_30d": 0,
        "grandfathered": 0,
        "open_invoices": sum(1 for inv in invoices if inv.get("status") == "issued"),
        "paid_invoices": sum(1 for inv in invoices if inv.get("status") == "paid"),
    }

    company_rows: list[dict] = []
    for comp in companies:
        sub = get_company_subscription(db, comp.id)
        st = sub["status"]
        counts[st] = counts.get(st, 0) + 1
        if sub.get("grandfathered"):
            counts["grandfathered"] += 1
        dl = sub.get("days_left")
        if st in ("active", "trial") and dl is not None:
            if dl <= 7:
                counts["expiring_7d"] += 1
            if dl <= 30:
                counts["expiring_30d"] += 1
        if st in ("active", "grace") and int(sub.get("locked_price_kop") or 0) > 0:
            months = CYCLES_MONTHS.get(sub.get("billing_cycle") or "monthly", 1)
            mrr_kop += int(sub["locked_price_kop"]) // months
        company_rows.append({
            "company_id": comp.id,
            "company_name": comp.short_name or comp.name,
            "full_name": comp.name,
            "inn": comp.inn or "",
            "is_active": bool(comp.is_active),
            "subscription": sub,
        })

    audit_rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == "billing")
        .order_by(AuditLog.id.desc())
        .limit(60)
    ).all()
    events = [a.to_dict() for a in audit_rows]

    return {
        "config": cfg,
        "vat_modes": [
            {"code": k, "title": v[0], "rate": v[1]} for k, v in VAT_MODES.items()
        ],
        "metrics": {
            "mrr_kop": mrr_kop,
            "mrr_formatted": format_rub(mrr_kop),
            "arr_kop": mrr_kop * 12,
            "arr_formatted": format_rub(mrr_kop * 12),
            **counts,
        },
        "module_prices": list(mod_prices.values()),
        "plans": plans,
        "promos": promos,
        "companies": company_rows,
        "invoices": invoices[:150],
        "events": events,
    }


def client_billing_view(db: Session, company_id: str) -> dict:
    """Данные подписки и витрины тарифов для бухгалтера / руководителя организации."""
    comp = db.get(Company, company_id)
    if comp is None:
        raise LookupError("Компания не найдена")
    cfg = get_config(db)
    sub = get_company_subscription(db, company_id)
    mod_prices = get_module_prices(db)
    plans = get_plans(db, include_inactive=False)
    invoices = list_invoices(db, company_id=company_id)[:30]
    return {
        "company": {
            "id": comp.id,
            "name": comp.short_name or comp.name,
            "full_name": comp.name,
            "inn": comp.inn or "",
        },
        "billing_mode": cfg["billing_mode"],
        "vat_mode": cfg["vat_mode"],
        "vat_label": VAT_MODES.get(cfg["vat_mode"], VAT_MODES["none"])[0],
        "support_email": cfg["support_email"],
        "subscription": sub,
        "plans": plans,
        "modules": [m for m in mod_prices.values() if m.get("available", True)],
        "coming_soon_modules": [m for m in mod_prices.values() if not m.get("available", True)],
        "invoices": invoices,
    }


# --------------------------------------------------------------------------
#  Серверный страж модулей и лимитов (HTTP 402 Payment Required)
# --------------------------------------------------------------------------
class SubscriptionRequiredError(Exception):
    """Операция недоступна без активного модуля подписки или при исчерпании квоты."""

    def __init__(self, message: str, *, code: str = "subscription_required", module: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.module = module


def check_company_module_access(
    db: Session,
    user: User | None,
    company_id: str | None,
    module_code: str,
    *,
    check_quota: bool = False,
) -> None:
    """Проверить право компании выполнить операцию модуля в режиме strict."""
    if user is None or user.role == ROLE_ADMIN:
        return
    cfg = get_config(db)
    if cfg["billing_mode"] != "strict":
        return
    cid = company_id or getattr(user, "company_id", None)
    sub = get_company_subscription(db, cid)
    mod_title = (entitlements.MODULES.get(module_code) or {}).get("title") or module_code

    if sub["status"] == "suspended":
        raise SubscriptionRequiredError(
            "Подписка вашей организации приостановлена администратором. Просмотр архива остаётся доступен.",
            code="subscription_suspended",
            module=module_code,
        )
    if module_code not in set(sub.get("enabled_modules") or []):
        if sub["status"] == "expired":
            raise SubscriptionRequiredError(
                f"Срок подписки истёк — модуль «{mod_title}» переведён в режим чтения архива. "
                "Оформите продление в разделе «Настройки → Ваша компания».",
                code="subscription_expired",
                module=module_code,
            )
        raise SubscriptionRequiredError(
            f"Модуль «{mod_title}» не входит в текущую подписку вашей организации. "
            "Подключите его в разделе «Настройки → Ваша компания».",
            code="module_not_included",
            module=module_code,
        )

    if check_quota:
        quota_map = {
            "receipts": ("receipts", "receipts_monthly", "чеков"),
            "upd": ("upd", "upd_monthly", "документов УПД"),
            "counterparties": ("counterparties", "counterparties_monthly", "контрагентов"),
        }
        if module_code in quota_map:
            u_key, l_key, label = quota_map[module_code]
            lim = int((sub.get("limits") or {}).get(l_key) or 0)
            used = int((sub.get("usage") or {}).get(u_key) or 0)
            if lim > 0 and used >= lim:
                raise SubscriptionRequiredError(
                    f"Исчерпан месячный лимит {label} по вашему тарифу ({used} из {lim}). "
                    "Расширьте тариф или обратитесь к администратору.",
                    code="quota_exceeded",
                    module=module_code,
                )
