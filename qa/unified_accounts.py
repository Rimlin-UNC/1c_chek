"""Экспериментальный учёт единого чека на отдельной SQLite-копии.

Не подключён к API. Вызывающий аутентифицирует пользователя и завершает
транзакцию. Фискальная запись и позиции не копируются в учёт компании.
"""
from __future__ import annotations

import math
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Company, Receipt, User, utcnow
from app.pool.models import PoolItem, PoolReceipt
from app.services.receipt_identity import lock_identity
from qa.migrate_receipt_copy import _normalized
from qa.unified_integrity import pool_items_match_snapshot


def _document(db: Session, document_id: str):
    return db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                           'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()



def promote_pool_claim(db: Session, document_id: str, company_id: str, user: User) -> bool:
    """Создать частную учётную привязку к выбранному чеку; True если создана.

    Идемпотентно, без вызова ФНС и без повторного списания квоты. Никакие
    персональные данные донора или сырой ответ источника не копируются.
    """
    if not user.is_active or user.role not in ('admin', 'accountant'):
        raise HTTPException(403, 'Для учёта нужны права бухгалтера')
    if not company_id or (user.role != 'admin' and user.company_id != company_id):
        raise HTTPException(403, 'Учёт доступен только для своей компании')
    document = _document(db, document_id)
    if not document:
        raise HTTPException(404, 'Чек не найден')
    fn, fd, fp, receipt_date, total_sum, operation, mask = document
    lock_identity(db, fn, fd, fp)
    db.expire_all()
    if _document(db, document_id) != document:
        raise HTTPException(409, 'Снимок чека изменился, нужна повторная сверка')
    company = db.query(Company).filter(Company.id == company_id).with_for_update().first()
    if not company or not company.is_active:
        raise HTTPException(403, 'Компания не найдена или отключена')
    claim = db.execute(text('SELECT company_id, pool_origin_id, registry_receipt_id '
                            'FROM unified_company_claims WHERE document_id=:id'), {'id': document_id}).first()
    if not claim or claim.company_id != company_id:
        raise HTTPException(404, 'Чек не найден')
    existing = db.execute(text('SELECT company_id, legacy_receipt_id, pool_origin_id, fns_status, fns_checked_at '
                               'FROM unified_company_accounts WHERE document_id=:id'), {'id': document_id}).first()
    if existing:
        if (existing.company_id, existing.legacy_receipt_id, existing.pool_origin_id) != (
                claim.company_id, claim.registry_receipt_id, claim.pool_origin_id):
            raise HTTPException(409, 'Учётная привязка требует сверки')
        if existing.legacy_receipt_id:
            legacy = db.get(Receipt, existing.legacy_receipt_id)
            if not legacy or legacy.company_id != company_id or (legacy.fn, legacy.fd, legacy.fp) != (fn, fd, fp):
                raise HTTPException(409, 'Старая учётная запись изменилась, нужна сверка')
            if existing.pool_origin_id:
                linked_pool = db.get(PoolReceipt, existing.pool_origin_id)
                if not linked_pool or linked_pool.assigned_company_id != company_id:
                    raise HTTPException(409, 'Назначение пула изменилось, нужна сверка')
            return False
    if mask or claim.registry_receipt_id or not claim.pool_origin_id:
        raise HTTPException(409, 'Чек требует сверки перед учётом')
    origin = db.execute(text("SELECT 1 FROM unified_receipt_origins WHERE document_id=:doc "
                             "AND origin_kind='pool' AND origin_id=:pool"),
                        {'doc': document_id, 'pool': claim.pool_origin_id}).first()
    if not origin:
        raise HTTPException(409, 'Связь с пулом изменилась, нужна сверка')
    pool = db.get(PoolReceipt, claim.pool_origin_id)
    if (not pool or pool.assigned_company_id != company_id or pool.status != 'verified'
            or (pool.fn, pool.fd, pool.fp) != (fn, fd, fp)
            or _normalized(pool.receipt_date, pool.total_sum, pool.operation) != (receipt_date, total_sum, operation)
            or not pool_items_match_snapshot(db, claim.pool_origin_id)):
        raise HTTPException(409, 'Сохранённый чек или его позиции изменились, нужна сверка')
    if db.query(Receipt.id).filter_by(fn=fn, fd=fd, fp=fp).first():
        raise HTTPException(409, 'Чек уже зарегистрирован в реестре')
    if existing:
        if existing.fns_status == 'valid' and (
                not pool.fns_confirmed or pool.fns_checked_at is None
                or pool.fns_checked_at.isoformat(' ') != existing.fns_checked_at):
            raise HTTPException(409, 'Подтверждение ФНС изменилось, нужна сверка')
        return False
    # Ручное одобрение и старые чеки не являются доказательством проверки ФНС.
    confirmed = bool(pool.fns_confirmed and pool.fns_checked_at is not None)
    checked = pool.fns_checked_at.isoformat(' ') if confirmed else None
    db.execute(text('INSERT INTO unified_company_accounts '
                    '(document_id, company_id, pool_origin_id, created_by, created_at, '
                    'status, fns_status, fns_checked_at) '
                    'VALUES (:doc, :company, :pool, :user, :created, :status, :fns, :checked)'),
               {'doc': document_id, 'company': company_id, 'pool': pool.id, 'user': user.id,
                'created': utcnow().isoformat(' '), 'status': 'verified' if confirmed else 'pending',
                'fns': 'valid' if confirmed else 'unknown', 'checked': checked})
    _record_event(db, document_id, user.id, 'created', '')
    return True


def _record_event(db: Session, document_id: str, actor_id: str, event: str, fields: str) -> None:
    db.execute(text('INSERT INTO unified_company_account_events '
                    '(id, document_id, actor_id, event_type, fields, created_at) '
                    'VALUES (:id, :doc, :actor, :event, :fields, :created)'),
               {'id': str(uuid4()), 'doc': document_id, 'actor': actor_id, 'event': event,
                'fields': fields, 'created': utcnow().isoformat(' ')})


def account_view(db: Session, document_id: str, company_id: str, user: User) -> dict:
    """Вернуть только разрешённую учётную проекцию компании на снимке."""
    if not user.is_active or not company_id or (user.role != 'admin' and user.company_id != company_id):
        raise HTTPException(404, 'Чек не найден')
    account = db.execute(text('SELECT company_id, legacy_receipt_id, pool_origin_id, created_by, '
                              'status, fns_status, fns_checked_at, assignee, comment, category, '
                              'personal_sum, exported FROM unified_company_accounts WHERE document_id=:id'),
                         {'id': document_id}).first()
    claim = db.execute(text('SELECT company_id, registry_receipt_id, pool_origin_id '
                            'FROM unified_company_claims WHERE document_id=:id'), {'id': document_id}).first()
    if not account or not claim or account.company_id != company_id or (
            account.company_id, account.legacy_receipt_id, account.pool_origin_id) != tuple(claim):
        raise HTTPException(404, 'Чек не найден')
    if account.legacy_receipt_id:
        # Действующие права и приватные поля старого чека остаются источником истины.
        from qa.unified_receipt_preview import company_view
        return company_view(db, document_id, user, company_id)
    if user.role not in ('admin', 'accountant', 'manager') and not (
            user.role == 'user' and account.created_by == user.id):
        raise HTTPException(404, 'Чек не найден')
    doc = _document(db, document_id)
    pool = db.get(PoolReceipt, account.pool_origin_id)
    if not doc or not pool:
        raise HTTPException(404, 'Чек не найден')
    fn, fd, fp, receipt_date, total_sum, operation, mask = doc
    origin = db.execute(text("SELECT 1 FROM unified_receipt_origins WHERE document_id=:doc "
                             "AND origin_kind='pool' AND origin_id=:pool"),
                        {'doc': document_id, 'pool': pool.id}).first()
    if (mask or not origin or pool.assigned_company_id != company_id or pool.status != 'verified'
            or (pool.fn, pool.fd, pool.fp) != (fn, fd, fp)
            or _normalized(pool.receipt_date, pool.total_sum, pool.operation) != (receipt_date, total_sum, operation)
            or not pool_items_match_snapshot(db, pool.id)):
        raise HTTPException(409, 'Снимок чека устарел, нужна сверка')
    if db.query(Receipt.id).filter_by(fn=fn, fd=fd, fp=fp).first():
        raise HTTPException(409, 'Чек появился в старом реестре, нужна сверка')
    if account.fns_status == 'valid' and (not pool.fns_confirmed or pool.fns_checked_at is None
                                          or pool.fns_checked_at.isoformat(' ') != account.fns_checked_at):
        raise HTTPException(409, 'Подтверждение ФНС изменилось, нужна сверка')
    items = db.query(PoolItem).filter_by(receipt_id=pool.id).order_by(PoolItem.id).all()
    return {'document_id': document_id, 'origin': 'unified_pool', 'receipt': {
        'fn': fn, 'fd': fd, 'fp': fp, 'receipt_date': receipt_date, 'total_sum': total_sum,
        'operation': operation, 'status': account.status, 'fns_status': account.fns_status,
        'fns_checked_at': account.fns_checked_at, 'assignee': account.assignee,
        'comment': account.comment, 'category': account.category,
        'personal_sum': account.personal_sum, 'exported': bool(account.exported),
        'merchant_name': pool.merchant_name or '', 'merchant_inn': pool.merchant_inn or '',
        'merchant_address': pool.merchant_address or '',
        'items': [{'name': item.name, 'quantity': item.quantity, 'price': item.price,
                   'total': item.total, 'vat_rate': item.vat_rate, 'vat_sum': item.vat_sum}
                  for item in items],
    }}


def update_account_private(db: Session, document_id: str, company_id: str, user: User, *,
                           comment: str | None = None, assignee: str | None = None,
                           category: str | None = None, personal_sum: float | None = None) -> bool:
    """Изменить только личные поля компании и записать событие без значений."""
    if not user.is_active or user.role not in ('admin', 'accountant'):
        raise HTTPException(403, 'Для изменения нужны права бухгалтера')
    if not company_id or (user.role != 'admin' and user.company_id != company_id):
        raise HTTPException(403, 'Изменение доступно только для своей компании')
    doc = _document(db, document_id)
    if not doc:
        raise HTTPException(404, 'Чек не найден')
    lock_identity(db, doc.fn, doc.fd, doc.fp)
    db.expire_all()
    if _document(db, document_id) != doc:
        raise HTTPException(409, 'Снимок чека изменился, нужна повторная сверка')
    company = db.query(Company).filter(Company.id == company_id).with_for_update().first()
    if not company or not company.is_active:
        raise HTTPException(403, 'Компания не найдена или отключена')
    view = account_view(db, document_id, company_id, user)
    if view['origin'] != 'unified_pool':
        raise HTTPException(409, 'Старый чек изменяется через действующий реестр')
    receipt = view['receipt']
    values = {}
    for field, value, limit in (('comment', comment, 2000), ('assignee', assignee, 200),
                                ('category', category, 100)):
        if value is not None:
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise HTTPException(422, 'Некорректное значение личного поля')
            value = value.strip()
            if value != receipt[field]:
                values[field] = value
    if personal_sum is not None:
        if isinstance(personal_sum, bool) or not isinstance(personal_sum, (int, float)):
            raise HTTPException(422, 'Некорректная личная сумма')
        amount = float(personal_sum)
        if not math.isfinite(amount) or amount < 0 or amount > float(receipt['total_sum']):
            raise HTTPException(422, 'Личная сумма должна быть от нуля до суммы чека')
        if amount != receipt['personal_sum']:
            values['personal_sum'] = amount
    if not values:
        return False
    # Имена полей берутся исключительно из фиксированного списка выше.
    assignments = ', '.join(f'{field}=:{field}' for field in values)
    db.execute(text(f'UPDATE unified_company_accounts SET {assignments} WHERE document_id=:document_id'),
               {**values, 'document_id': document_id})
    _record_event(db, document_id, user.id, 'updated', ','.join(sorted(values)))
    return True
