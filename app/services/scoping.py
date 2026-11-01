# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сервис изоляции компаний (мультикомпанийность v1.11.0).
# ЕДИНАЯ точка правил доступа: каждый запрос к чекам обязан проходить
# через эти функции — «чужое пространство» не видно никогда.
#
# Роли:
#   admin        — платформа: видит ВСЕ компании (опциональный фильтр);
#   accountant   — видит все чеки СВОЕЙ компании, может менять;
#   manager      — v1.64.0: видит все чеки СВОЕЙ компании, ТОЛЬКО просмотр
#                  (отчёт руководителя: разрезы + drill-down до чека);
#   user         — видит только СВОИ чеки внутри СВОЕЙ компании.
#
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Query, Session, object_session

from ..auth import ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_USER
from ..models import Company, Receipt, User
from ..pool.models import PoolReceipt


def _canonical_company_filter(company_id, user_id=None):
    """Если чек связан с каноном, владельцем считает единая строка владения."""
    from ..unified.models import REVIEW_OWNERSHIP, UnifiedDocument, UnifiedOrigin, UnifiedOwnership
    linked = (select(UnifiedOrigin.id)
              .where(UnifiedOrigin.source_kind == "registry",
                     UnifiedOrigin.source_id == Receipt.id)).exists()
    own_ok = (select(UnifiedOwnership.document_id)
              .join(UnifiedDocument, UnifiedDocument.id == UnifiedOwnership.document_id)
              .join(UnifiedOrigin, UnifiedOrigin.document_id == UnifiedDocument.id)
              .where(UnifiedOrigin.source_kind == "registry",
                     UnifiedOrigin.source_id == Receipt.id,
                     UnifiedDocument.deleted_at.is_(None),
                     (UnifiedDocument.review_flags.op("&")(REVIEW_OWNERSHIP)) == 0,
                     UnifiedOwnership.company_id.is_(None) if company_id is None
                     else UnifiedOwnership.company_id == company_id))
    if user_id is not None:
        own_ok = own_ok.where(UnifiedOwnership.created_by == user_id)
    return or_(~linked, own_ok.exists())


def _canonical_receipt_allowed(receipt: Receipt, user: User) -> bool:
    db = object_session(receipt)
    if db is None:
        return True
    from ..unified.models import UnifiedOrigin
    origin = (db.query(UnifiedOrigin)
              .filter(UnifiedOrigin.source_kind == "registry",
                      UnifiedOrigin.source_id == receipt.id).first())
    if origin is None:
        return True
    from ..unified.models import REVIEW_OWNERSHIP, UnifiedDocument
    doc = origin.document
    if user.role == "admin":
        return True
    if doc.deleted_at is not None or (doc.review_flags & REVIEW_OWNERSHIP):
        return False
    own = doc.ownership
    return own is not None and own.company_id == receipt.company_id


def canonical_pool_company_filter(company_id: str):
    from ..unified.models import REVIEW_OWNERSHIP, UnifiedDocument, UnifiedOrigin, UnifiedOwnership
    linked = (select(UnifiedOrigin.id)
              .where(UnifiedOrigin.source_kind == "pool",
                     UnifiedOrigin.source_id == PoolReceipt.id)).exists()
    own_ok = (select(UnifiedOwnership.document_id)
              .join(UnifiedDocument, UnifiedDocument.id == UnifiedOwnership.document_id)
              .join(UnifiedOrigin, UnifiedOrigin.document_id == UnifiedDocument.id)
              .where(UnifiedOrigin.source_kind == "pool",
                     UnifiedOrigin.source_id == PoolReceipt.id,
                     UnifiedDocument.deleted_at.is_(None),
                     (UnifiedDocument.review_flags.op("&")(REVIEW_OWNERSHIP)) == 0,
                     UnifiedOwnership.company_id == company_id)).exists()
    return or_(~linked, own_ok)


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
        query = query.filter(_canonical_company_filter(user.company_id, user.id))
    else:
        query = query.filter(_canonical_company_filter(user.company_id))
    return query


def can_view_receipt(user: User, receipt: Receipt) -> bool:
    """Доступ к отдельному чеку с учётом компании и владения.

    Пользователь без компании (platform-приглашение, старые данные) видит
    чеки «нулевого» пространства (company_id IS NULL)."""
    if user.role == ROLE_ADMIN:
        return True
    if not _canonical_receipt_allowed(receipt, user):
        return False
    if user.company_id is None:
        return receipt.company_id is None
    if receipt.company_id != user.company_id:
        return False                     # чужая компания — «не существует»
    if user.role == ROLE_ACCOUNTANT:
        return True
    if user.role == ROLE_MANAGER:        # v1.64.0: вся компания, без правки
        return True
    return receipt.created_by == user.id


def can_edit_receipt(user: User, receipt: Receipt) -> bool:
    """Бухгалтер/админ правят чеки своей компании (админ — любой)."""
    if user.role == ROLE_ADMIN:
        return True
    if user.role != ROLE_ACCOUNTANT:
        return False
    if not _canonical_receipt_allowed(receipt, user):
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
