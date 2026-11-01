"""Экспериментальное чтение единого чека на мигрированной SQLite-копии.

Не API и не production-модель: изменившиеся исходные таблицы не синхронизируют
снимок. Пользователь получает только ту проекцию, которую ему разрешено видеть.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Receipt, User
from app.pool.models import PoolReceipt, PoolUser
from app.services.scoping import can_view_receipt
from qa.migrate_receipt_copy import _normalized
from qa.unified_integrity import pool_items_match_snapshot, pool_seller_matches_snapshot


def _origin_ids(db: Session, document_id: str, kind: str) -> list[str]:
    # Одинаковый ответ для отсутствующего и недоступного чека.
    return list(db.execute(text('SELECT origin_id FROM unified_receipt_origins '
                                'WHERE document_id=:doc AND origin_kind=:kind'),
                           {'doc': document_id, 'kind': kind}).scalars())


def _matches_identity(db: Session, document_id: str, row) -> bool:
    return bool(db.execute(text('SELECT 1 FROM unified_fiscal_documents '
                                'WHERE id=:doc AND fn=:fn AND fd=:fd AND fp=:fp'),
                           {'doc': document_id, 'fn': row.fn, 'fd': row.fd, 'fp': row.fp}).first())


def company_view(db: Session, document_id: str, user: User, company_id: str) -> dict:
    """Просмотр без объединения частных полей разных организаций и доноров.

    admin явно выбирает компанию; обычный сотрудник видит только свой чек,
    бухгалтер/руководитель — чеки своей компании. Назначенный чек пула
    доступен для подбора только бухгалтеру и администратору.
    """
    if not user.is_active or not company_id or (user.role != 'admin' and user.company_id != company_id):
        raise HTTPException(404, 'Чек не найден')
    claim = db.execute(text('SELECT company_id, registry_receipt_id, pool_origin_id '
                            'FROM unified_company_claims WHERE document_id=:doc'),
                       {'doc': document_id}).first()
    # Неоднозначное старое владение не выбирается в пользу какой-либо компании.
    if not claim or claim.company_id != company_id:
        raise HTTPException(404, 'Чек не найден')
    registry_ids = _origin_ids(db, document_id, 'registry')
    candidates = db.query(Receipt).filter(Receipt.id.in_(registry_ids),
                                          Receipt.id == claim.registry_receipt_id,
                                          Receipt.company_id == company_id).all()
    visible = [r for r in candidates if can_view_receipt(user, r) and _matches_identity(db, document_id, r)]
    if len(visible) > 1:
        raise HTTPException(409, 'Обнаружены конфликтующие записи, нужна проверка')
    if visible:
        return {'document_id': document_id, 'origin': 'registry',
                'receipt': visible[0].to_dict(with_items=True)}
    # Даже если старое назначение пула расходится с реестром, нельзя
    # предоставить чек второй компании в обход правила исключительного учёта.
    if registry_ids or user.role not in ('admin', 'accountant'):
        raise HTTPException(404, 'Чек не найден')
    pool = db.query(PoolReceipt).filter(PoolReceipt.id.in_(_origin_ids(db, document_id, 'pool')),
                                        PoolReceipt.id == claim.pool_origin_id,
                                        PoolReceipt.assigned_company_id == company_id,
                                        PoolReceipt.status == 'verified').all()
    visible_pool = [r for r in pool if _matches_identity(db, document_id, r)]
    if len(visible_pool) > 1:
        raise HTTPException(409, 'Обнаружены конфликтующие записи, нужна проверка')
    if not visible_pool:
        raise HTTPException(404, 'Чек не найден')
    # Тот же ограниченный состав, что действующий интерфейс подбора; без
    # сырых ответов ФНС, контактов, идентификатора участника и его баллов.
    from app.pool.router_company import _names_for, _row
    r = visible_pool[0]
    doc = db.execute(text('SELECT receipt_date, total_sum, operation, review_mask '
                          'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    if (not doc or doc.review_mask
            or _normalized(r.receipt_date, r.total_sum, r.operation) != tuple(doc[:3])
            or not pool_seller_matches_snapshot(db, document_id, r.id)
            or not pool_items_match_snapshot(db, r.id)
            or db.query(Receipt.id).filter_by(fn=r.fn, fd=r.fd, fp=r.fp).first()):
        raise HTTPException(409, 'Снимок чека устарел, нужна сверка')
    return {'document_id': document_id, 'origin': 'pool',
            'receipt': _row(r, _names_for(db, [r.id]).get(r.id, ''))}


def donor_view(db: Session, document_id: str, donor: PoolUser) -> dict:
    """Участник видит только собственную запись и позиции, а не учёт компании."""
    if donor.is_blocked:
        raise HTTPException(404, 'Чек не найден')
    pool = db.query(PoolReceipt).filter(PoolReceipt.id.in_(_origin_ids(db, document_id, 'pool')),
                                        PoolReceipt.pool_user_id == donor.id).all()
    visible = [r for r in pool if _matches_identity(db, document_id, r)]
    if len(visible) > 1:
        raise HTTPException(409, 'Обнаружены конфликтующие записи, нужна проверка')
    if not visible:
        raise HTTPException(404, 'Чек не найден')
    from app.pool.router_my import receipt_detail
    return {'document_id': document_id, 'origin': 'pool',
            'receipt': receipt_detail(visible[0].id, db, donor)}
