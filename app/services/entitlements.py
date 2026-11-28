"""Модули подписки компании и проверка прав на модули (v1.73.12 → v1.74.0).

Назначение модуля: разделение функциональных контуров компании, чтобы каждый
блок включался, тарифицировался и контролировался отдельно для конкретной
организации без переделки ролей и экранов:

  - ``receipts``         — работа с кассовыми чеками, авансовыми отчётами и 1С;
  - ``upd``              — электронная первичка (УПД), банковская выписка и запросы клиенту;
  - ``manager_cabinet``  — кабинет, сводка и аналитика руководителя организации;
  - ``counterparties``   — проверка контрагентов по ИНН и карточки подписантов;
  - ``contracts``        — подготовка договоров (модуль с переключателем готовности в админке).

С версии v1.74.0 модуль интегрирован с корпоративным биллингом ``app.billing.service``
(тарифы, помодульные цены, Grandfathering, пробный и льготный периоды, режим чтения архива).
Администратор системы всегда видит все разделы независимо от подписки конкретной компании.
"""
from __future__ import annotations

import json
from typing import Final

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN
from ..models import AppSetting, User

SETTING_PREFIX: Final[str] = "company_modules:"

# Каталог модулей подписки: код -> описание и экраны, которыми управляет модуль.
MODULES: Final[dict[str, dict]] = {
    "receipts": {
        "code": "receipts",
        "title": "Чеки и авансовые отчёты",
        "description": "Сканирование QR и фото чеков, проверка в ФНС, АО-1, согласования и выгрузка в 1С",
        "views": ("scan", "receipts", "export", "mapping", "poolpick", "approvals"),
        "available": True,
    },
    "upd": {
        "code": "upd",
        "title": "Первичка (УПД) и сверка с банком",
        "description": "Приём и разбор УПД, банковские выписки и список недостающих документов клиента",
        "views": ("primary", "bank", "docreq"),
        "available": True,
    },
    "manager_cabinet": {
        "code": "manager_cabinet",
        "title": "Кабинет руководителя",
        "description": "Управленческая сводка расходов, НДС, контроль политики и приглашение сотрудников",
        "views": ("boss",),
        "roles": ("manager",),
        "available": True,
    },
    "counterparties": {
        "code": "counterparties",
        "title": "Проверка контрагентов",
        "description": "Досье контрагентов по ИНН, реквизиты, подписанты и банковские счета",
        "views": ("counterparties",),
        "available": True,
    },
    "contracts": {
        "code": "contracts",
        "title": "Подготовка договоров",
        "description": "Конструктор договоров и автоподстановка реквизитов контрагента (готовится к выпуску)",
        "views": ("contracts",),
        "available": False,
    },
}

DEFAULT_ENABLED: Final[tuple[str, ...]] = (
    "receipts",
    "upd",
    "manager_cabinet",
    "counterparties",
)

# Обратный индекс: какой модуль отвечает за экран (экраны вне индекса — базовые, всегда доступны роли).
VIEW_MODULE: Final[dict[str, str]] = {
    view: code
    for code, meta in MODULES.items()
    for view in meta["views"]
}


def enabled_modules_for_company(db: Session, company_id: str | None) -> list[str]:
    """Список активных модулей подписки компании с учётом живой подписки и явных настроек."""
    from ..billing import service as billing_svc

    sub_row = db.get(AppSetting, f"{billing_svc.SUB_PREFIX}{company_id}") if company_id else None
    cfg = billing_svc.get_config(db)
    if sub_row is not None or cfg["billing_mode"] == "strict":
        sub = billing_svc.get_company_subscription(db, company_id)
        return list(sub.get("enabled_modules") or [])

    if company_id:
        row = db.get(AppSetting, f"{SETTING_PREFIX}{company_id}")
        if row is not None and (row.value or "").strip():
            try:
                raw = json.loads(row.value)
                if isinstance(raw, list):
                    return [str(code) for code in raw if str(code) in MODULES]
            except ValueError:
                pass

    mod_prices = billing_svc.get_module_prices(db)
    return [code for code, m in mod_prices.items() if m.get("available", True)]


def set_company_modules(db: Session, company_id: str, modules: list[str]) -> list[str]:
    """Сохранить набор подключённых модулей для компании."""
    clean: list[str] = []
    for code in modules:
        c = str(code).strip()
        if c in MODULES and c not in clean:
            clean.append(c)
    key = f"{SETTING_PREFIX}{company_id}"
    row = db.get(AppSetting, key)
    payload = json.dumps(clean, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=key, value=payload))
    else:
        row.value = payload
    db.flush()
    return clean


def allowed_views_for_company(
    db: Session,
    company_id: str | None,
    base_views: list[str] | set[str],
    *,
    is_admin: bool = False,
) -> list[str]:
    """Отфильтровать экраны роли по активным модулям подписки компании."""
    if is_admin:
        return list(base_views)
    active = set(enabled_modules_for_company(db, company_id))
    return [v for v in base_views if VIEW_MODULE.get(v, "") in ("", *active)]


def payload_for_user(db: Session, user: User | None) -> dict:
    """Сводка модулей и подписки компании для ответа /api/v1/auth/me."""
    if user is None:
        return {"plan": "free_stage1", "enabled": [], "catalog": []}
    from ..billing import service as billing_svc

    is_adm = user.role == ROLE_ADMIN
    cid = getattr(user, "company_id", None)
    mod_prices = billing_svc.get_module_prices(db)
    sub = billing_svc.get_company_subscription(db, cid)
    enabled = list(MODULES.keys()) if is_adm else enabled_modules_for_company(db, cid)
    return {
        "plan": sub.get("plan_code") or ("admin" if is_adm else "free_stage1"),
        "plan_title": "Полный доступ администратора" if is_adm else sub.get("plan_title", ""),
        "status": "active" if is_adm else sub.get("status", "free"),
        "status_title": "Активна" if is_adm else sub.get("status_title", ""),
        "billing_mode": sub.get("billing_mode", "free_all"),
        "read_only": False if is_adm else bool(sub.get("read_only")),
        "period_end": sub.get("period_end"),
        "days_left": sub.get("days_left"),
        "warnings": [] if is_adm else list(sub.get("warnings") or []),
        "company_id": cid,
        "enabled": enabled,
        "view_module": dict(VIEW_MODULE),
        "catalog": [
            {
                "code": m["code"],
                "title": m["title"],
                "description": m["description"],
                "views": list(m["views"]),
                "available": bool((mod_prices.get(m["code"]) or {}).get("available", m["available"])),
                "monthly_kop": int((mod_prices.get(m["code"]) or {}).get("monthly_kop") or 0),
                "yearly_kop": int((mod_prices.get(m["code"]) or {}).get("yearly_kop") or 0),
                "active": m["code"] in enabled,
            }
            for m in MODULES.values()
        ],
    }


def require_company_module(
    db: Session,
    user: User | None,
    company_id: str | None,
    module_code: str,
    *,
    check_quota: bool = False,
) -> None:
    """Выбросить HTTP 402 Payment Required, если в режиме strict модуль недоступен или лимит исчерпан."""
    from ..billing import service as billing_svc

    try:
        billing_svc.check_company_module_access(
            db, user, company_id, module_code, check_quota=check_quota
        )
    except billing_svc.SubscriptionRequiredError as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "message": exc.message,
                "code": exc.code,
                "module": exc.module,
                "upgrade_hash": "#/settings/personal/st-mycompany",
            },
        ) from exc
