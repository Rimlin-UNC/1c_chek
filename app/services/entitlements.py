# -*- coding: utf-8 -*-
"""Модули подписки компании (архитектурное зерно тарификации, v1.73.12).

Назначение модуля: заложить чистое разделение функциональных контуров компании,
чтобы при введении платных подписок каждый блок включался и выключался отдельно
для конкретной организации без переделки ролей и экранов:

  - ``receipts``         — работа с кассовыми чеками, авансовыми отчётами и 1С;
  - ``upd``              — электронная первичка (УПД), банковская выписка и запросы клиенту;
  - ``manager_cabinet``  — кабинет, сводка и аналитика руководителя организации;
  - ``counterparties``   — проверка контрагентов по ИНН и карточки подписантов;
  - ``contracts``        — подготовка договоров (задел под будущий платный модуль).

На первом этапе (бесплатный режим) все действующие модули включены по умолчанию.
Переопределение для отдельной компании хранится в ``app_settings`` по ключу
``company_modules:<company_id>`` (JSON-список кодов подключённых модулей) — без
миграций схемы БД. Администратор системы всегда видит все разделы независимо от
подписки конкретной компании.
"""
from __future__ import annotations

import json
from typing import Final

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
    """Список активных модулей подписки компании (по умолчанию — все действующие)."""
    if not company_id:
        return list(DEFAULT_ENABLED)
    row = db.get(AppSetting, f"{SETTING_PREFIX}{company_id}")
    if row is None or not (row.value or "").strip():
        return list(DEFAULT_ENABLED)
    try:
        raw = json.loads(row.value)
    except ValueError:
        return list(DEFAULT_ENABLED)
    if not isinstance(raw, list):
        return list(DEFAULT_ENABLED)
    return [str(code) for code in raw if str(code) in MODULES]


def set_company_modules(db: Session, company_id: str, modules: list[str]) -> list[str]:
    """Сохранить набор подключённых модулей для компании (задел для тарифного управления)."""
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


def allowed_views_for_company(db: Session, company_id: str | None,
                              base_views: list[str] | set[str],
                              *, is_admin: bool = False) -> list[str]:
    """Отфильтровать экраны роли по активным модулям подписки компании."""
    if is_admin:
        return list(base_views)
    active = set(enabled_modules_for_company(db, company_id))
    return [v for v in base_views if VIEW_MODULE.get(v, "") in ("", *active)]


def payload_for_user(db: Session, user: User | None) -> dict:
    """Сводка модулей подписки для ответа /api/v1/auth/me."""
    if user is None:
        return {"plan": "free_stage1", "enabled": [], "catalog": []}
    is_adm = user.role == ROLE_ADMIN
    cid = getattr(user, "company_id", None)
    enabled = list(MODULES.keys()) if is_adm else enabled_modules_for_company(db, cid)
    return {
        "plan": "free_stage1",
        "company_id": cid,
        "enabled": enabled,
        "view_module": dict(VIEW_MODULE),
        "catalog": [
            {
                "code": m["code"],
                "title": m["title"],
                "description": m["description"],
                "views": list(m["views"]),
                "available": bool(m["available"]),
                "active": m["code"] in enabled,
            }
            for m in MODULES.values()
        ],
    }
