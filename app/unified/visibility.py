"""Показ числа организаций компаниям (ADR-20, #24, фаза 3, T-35).

Администратор платформы видит число и список компаний у чека всегда
(фазы 1–2). Компаниям число показывается только с его разрешения (Q-20):

* «выключено» — по умолчанию; поле ``companies_count`` компаниям не отдаётся;
* «всем компаниям»;
* «выбранным компаниям» — по флагу ``companies.show_companies_count``.

Число видят только роли из настройки (Q-21: бухгалтер и руководитель).
Сотрудник (роль «Сотрудник»), донор Чек-Пула и платный API (B2B) — никогда.
Названия других организаций компаниям не раскрываются: только число и
только по чекам, которые компания и так видит в своём реестре.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Company, User

MODE_KEY = "companies_count_mode"
ROLES_KEY = "companies_count_roles"
MODE_OFF, MODE_ALL, MODE_SELECTED = "off", "all", "selected"
MODES = (MODE_OFF, MODE_ALL, MODE_SELECTED)
# Шире не бывает: сотрудник видит только свои чеки, ему число не нужно (Q-21).
ALLOWED_ROLES = ("accountant", "manager")
# Пустое значение настройки читается как «по умолчанию» — «ни одной роли»
# хранится явной отметкой.
NO_ROLES = "none"


def mode(db: Session) -> str:
    from ..services import appsettings

    value = appsettings.get_setting(db, MODE_KEY, MODE_OFF)
    return value if value in MODES else MODE_OFF


def roles(db: Session) -> tuple[str, ...]:
    from ..services import appsettings

    raw = appsettings.get_setting(db, ROLES_KEY, ",".join(ALLOWED_ROLES))
    return tuple(r for r in ALLOWED_ROLES if r in {x.strip() for x in raw.split(",")})


def can_see_count(db: Session, user: User | None) -> bool:
    """Отдавать ли пользователю поле ``companies_count`` в реестре и карточке."""
    if user is None:
        return False
    if user.role == "admin":
        return True
    if user.role not in ALLOWED_ROLES or not user.company_id:
        return False
    current = mode(db)
    if current == MODE_OFF or user.role not in roles(db):
        return False
    if current == MODE_ALL:
        return True
    company = db.get(Company, user.company_id)
    return bool(company and company.show_companies_count)


def settings_payload(db: Session) -> dict:
    """Настройки показа для вкладки администратора."""
    companies = db.query(Company).order_by(Company.name).all()
    return {
        "mode": mode(db),
        "roles": list(roles(db)),
        "allowed_roles": list(ALLOWED_ROLES),
        "companies": [{"id": c.id, "name": c.short_name or c.name, "is_active": bool(c.is_active),
                       "enabled": bool(c.show_companies_count)} for c in companies],
    }


def update_settings(db: Session, *, new_mode: str, new_roles: list[str],
                    company_ids: list[str] | None) -> dict:
    """Сохранить режим, роли и (если передан список) флаги компаний.

    Commit — за вызывающим. ``company_ids`` — полный список компаний, которым
    число показывается в режиме «выбранным»; остальным флаг снимается.
    """
    from ..services import appsettings

    if new_mode not in MODES:
        raise ValueError("неизвестный режим показа")
    clean_roles = [r for r in ALLOWED_ROLES if r in set(new_roles)]
    appsettings.set_setting(db, MODE_KEY, new_mode)
    appsettings.set_setting(db, ROLES_KEY, ",".join(clean_roles) or NO_ROLES)
    if company_ids is not None:
        wanted = set(company_ids)
        for company in db.query(Company).all():
            company.show_companies_count = company.id in wanted
    db.flush()
    return settings_payload(db)
