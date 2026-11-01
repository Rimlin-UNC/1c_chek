"""Экспериментальное закрепление единого чека на мигрированной SQLite-копии.

Не подключено к production API. Вызывающий отвечает за аутентификацию и за
commit/rollback сессии; все записи выполняются в одной транзакции.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Company, Receipt, User, utcnow
from app.pool import ingest
from app.pool.models import PoolReceipt
from app.pool.router_company import month_used, pick_limit
from app.services.receipt_identity import lock_identity
from qa.migrate_receipt_copy import _normalized
from qa.unified_integrity import pool_items_match_snapshot, pool_seller_matches_snapshot


def claim_pool_document(db: Session, document_id: str, company_id: str, user: User) -> bool:
    """Закрепить документ за компанией без создания копии чека и запроса ФНС.

    True — новое закрепление; False — повтор того же закрепления. Владелец
    сохраняется единственной строкой по document_id; баллы и донор не меняются.
    """
    if not user.is_active or user.role not in ('admin', 'accountant'):
        raise HTTPException(403, 'Для подбора нужны права бухгалтера')
    if not company_id or (user.role != 'admin' and user.company_id != company_id):
        raise HTTPException(403, 'Подбор доступен только для своей компании')
    document = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                               'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    pool_ids = list(db.execute(text("SELECT origin_id FROM unified_receipt_origins "
                                    "WHERE document_id=:id AND origin_kind='pool'"),
                               {'id': document_id}).scalars())
    if not document or len(pool_ids) != 1:
        raise HTTPException(404, 'Чек пула не найден')
    fn, fd, fp, saved_date, saved_amount, saved_operation, review_mask = document
    # Тот же замок используется действующим путём сканирования и подбора.
    lock_identity(db, fn, fd, fp)
    db.expire_all()
    fresh_document = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                                     'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    fresh_ids = list(db.execute(text("SELECT origin_id FROM unified_receipt_origins "
                                     "WHERE document_id=:id AND origin_kind='pool'"),
                                {'id': document_id}).scalars())
    if fresh_document != document or fresh_ids != pool_ids:
        raise HTTPException(409, 'Снимок чека изменился, нужна повторная сверка')
    pool = db.get(PoolReceipt, pool_ids[0])
    company = db.query(Company).filter(Company.id == company_id).with_for_update().first()
    if not company or not company.is_active:
        raise HTTPException(403, 'Компания не найдена или отключена')
    if not ingest.pool_enabled(db):
        raise HTTPException(403, 'Подбор из пула выключен')
    if not pool or (pool.fn, pool.fd, pool.fp) != (fn, fd, fp):
        raise HTTPException(409, 'Снимок чека устарел, нужна повторная сверка')
    if review_mask or _normalized(pool.receipt_date, pool.total_sum, pool.operation) != (
            saved_date, saved_amount, saved_operation):
        raise HTTPException(409, 'В сохранённых данных есть расхождения, нужна проверка')
    if not pool_seller_matches_snapshot(db, document_id, pool.id):
        raise HTTPException(409, 'Реквизиты продавца изменились, нужна повторная сверка')
    if not pool_items_match_snapshot(db, pool.id, document_id):
        raise HTTPException(409, 'Позиции чека изменились, нужна повторная сверка')
    if pool.status != 'verified':
        raise HTTPException(409, 'Чек пула не доступен для подбора')
    if db.query(Receipt.id).filter_by(fn=fn, fd=fd, fp=fp).first():
        raise HTTPException(409, 'Чек уже зарегистрирован в реестре')
    claim = db.execute(text('SELECT company_id, pool_origin_id FROM unified_company_claims '
                            'WHERE document_id=:id'), {'id': document_id}).first()
    if claim:
        if claim.company_id == company_id and claim.pool_origin_id == pool.id and pool.assigned_company_id == company_id:
            return False
        raise HTTPException(409, 'Чек уже зарегистрирован за другой организацией или требует сверки')
    if pool.assigned_company_id:
        raise HTTPException(409, 'Чек уже закреплён или снимок устарел')
    limit = pick_limit(db)
    if not limit or month_used(db, company_id) >= limit:
        raise HTTPException(409, 'Квота подбора исчерпана или выключена')
    pool.assigned_company_id = company_id
    pool.assigned_at = utcnow()
    pool.assigned_by = user.id
    db.flush()
    db.execute(text('INSERT INTO unified_company_claims '
                    '(document_id, company_id, pool_origin_id, registry_receipt_id, assigned_at) '
                    'SELECT :id, :company, id, NULL, assigned_at FROM pool_receipts WHERE id=:pool_id'),
               {'id': document_id, 'company': company_id, 'pool_id': pool.id})
    return True
