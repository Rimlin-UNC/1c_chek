# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сервис изоляции компаний (мультикомпанийность v1.11.0).
# ЕДИНАЯ точка правил доступа: каждый запрос к чекам обязан проходить
# через эти функции — «чужое пространство» не видно никогда.
#
# Роли:
#   admin        — платформа: видит ВСЕ компании (опциональный фильтр);
#   accountant   — видит все чеки СВОЕЙ компании;
#   user         — видит только СВОИ чеки внутри СВОЕЙ компании.
#
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from sqlalchemy.orm import Query, Session

from ..auth import ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_USER
from ..models import Company, Receipt, User


def user_company_id(user: User) -> str | None:
    """Компания пользователя (у админа — None = вся платформа)."""
    return user.company_id if user.role != ROLE_ADMIN else None


def scope_receipts(query: Query, user: User,
                   company_id: str | None = None) -> Query:
    """Применить изоляцию пространства к запросу чеков.

    company_id — явный фильтр администратора («смотреть одну компанию»);
    для бухгалтера/пользователя параметр ИГНОРИРУЕТСЯ — их пространство
    определяется только их собственной компанией.
    """
    if user.role == ROLE_ADMIN:
        if company_id:
            query = query.filter(Receipt.company_id == company_id)
        return query
    if user.company_id:
        query = query.filter(Receipt.company_id == user.company_id)
    else:                                # страховка: сотрудник без компании
        query = query.filter(Receipt.company_id.is_(None))
    if user.role == ROLE_USER:
        query = query.filter(Receipt.created_by == user.id)
    return query


def can_view_receipt(user: User, receipt: Receipt) -> bool:
    """Доступ к отдельному чеку с учётом компании и владения.

    Пользователь без компании (platform-приглашение, старые данные) видит
    чеки «нулевого» пространства (company_id IS NULL)."""
    if user.role == ROLE_ADMIN:
        return True
    if user.company_id is None:
        return receipt.company_id is None
    if receipt.company_id != user.company_id:
        return False                     # чужая компания — «не существует»
    if user.role == ROLE_ACCOUNTANT:
        return True
    return receipt.created_by == user.id


def can_edit_receipt(user: User, receipt: Receipt) -> bool:
    """Бухгалтер/админ правят чеки своей компании (админ — любой)."""
    if user.role == ROLE_ADMIN:
        return True
    if user.role != ROLE_ACCOUNTANT:
        return False
    if user.company_id is None:
        return receipt.company_id is None
    return receipt.company_id == user.company_id


def target_company_id(db: Session, user: User, requested: str | None) -> str | None:
    """Компания для НОВОГО чека.

    Сотрудник/бухгалтер — всегда их компания. Администратор может указать
    company_id явно (селектор в интерфейсе); без указания чек падает без
    компании (платформенный уровень) — интерфейс подсказывает выбор.
    """
    if user.role != ROLE_ADMIN:
        return user.company_id
    if requested:
        comp = db.get(Company, requested)
        return comp.id if comp else None
    return None
