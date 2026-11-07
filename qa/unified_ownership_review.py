"""Предварительный разбор конфликтов владения на экспериментальной SQLite-копии.

Это не API и не решение о передаче чека. Администратор может зафиксировать
предложение проверить существующего владельца реестра; права второй компании,
назначения, учётные записи и история при этом не меняются.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import User, utcnow
from app.services.receipt_identity import lock_identity
from qa.migrate_receipt_copy import _normalized
from qa.unified_integrity import (
    begin_read_snapshot, pool_seller_matches_snapshot, registry_seller_matches_snapshot,
    source_core_matches_snapshot, source_extra_matches_snapshot,
)
from qa.unified_item_resolution import _current_item_hashes
from qa.unified_review_fingerprint import review_fingerprint


def _context(db: Session, document_id: str, company_id: str, actor_id: str):
    actor = db.execute(text('SELECT role, is_active FROM users WHERE id=:id'), {'id': actor_id}).first()
    if not actor or actor.role != 'admin' or not actor.is_active:
        raise HTTPException(403, 'Разбор доступен только администратору')
    doc = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                          'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    if not doc or not company_id:
        raise HTTPException(404, 'Чек не найден')
    origins = db.execute(text('SELECT origin_kind, origin_id, fiscal_source_hash, extra_fiscal_hash, core_fiscal_hash '
                              'FROM unified_receipt_origins WHERE document_id=:id'), {'id': document_id}).all()
    if len(origins) != 2 or {row.origin_kind for row in origins} != {'registry', 'pool'}:
        raise HTTPException(409, 'Источники требуют отдельного разбора')
    ids = {row.origin_kind: row.origin_id for row in origins}
    registry = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, company_id '
                               'FROM receipts WHERE id=:id'), {'id': ids['registry']}).first()
    pool = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, assigned_company_id, '
                           'status, assigned_at FROM pool_receipts WHERE id=:id'), {'id': ids['pool']}).first()
    # Не подтверждаем принадлежность или существование чека другой компании.
    if not registry or registry.company_id != company_id:
        raise HTTPException(404, 'Чек не найден')
    company = db.execute(text('SELECT is_active FROM companies WHERE id=:id'), {'id': company_id}).first()
    if (not company or not company.is_active or not pool or not pool.assigned_company_id
            or not pool.assigned_company_id.strip() or pool.assigned_company_id == company_id or not (doc.review_mask & 8)):
        raise HTTPException(409, 'Состояние требует отдельного разбора')
    claim = db.execute(text('SELECT 1 FROM unified_company_claims WHERE document_id=:id'),
                       {'id': document_id}).first()
    account = db.execute(text('SELECT 1 FROM unified_company_accounts WHERE document_id=:id'),
                         {'id': document_id}).first()
    item_set = db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                          {'id': document_id}).scalar_one_or_none()
    if claim or account or item_set != 'needs_review':
        raise HTTPException(409, 'Состояние требует отдельного разбора')
    if tuple(registry[:3]) != tuple(doc[:3]) or tuple(pool[:3]) != tuple(doc[:3]):
        raise HTTPException(409, 'Идентичность источников изменилась')
    if (not registry_seller_matches_snapshot(db, document_id, ids['registry'])
            or not pool_seller_matches_snapshot(db, document_id, ids['pool'])
            or not source_core_matches_snapshot(db, 'registry', document_id, ids['registry'])
            or not source_core_matches_snapshot(db, 'pool', document_id, ids['pool'])
            or not source_extra_matches_snapshot(db, 'registry', document_id, ids['registry'])
            or not source_extra_matches_snapshot(db, 'pool', document_id, ids['pool'])
            or not _current_item_hashes(db, 'registry', ids['registry'])
            or not _current_item_hashes(db, 'pool', ids['pool'])):
        raise HTTPException(409, 'Источники изменились после создания копии')
    values = [_normalized(row.receipt_date, row.total_sum, row.operation) for row in (registry, pool)]
    mask, expected = 8, []
    for idx, bit in enumerate((1, 2, 4)):
        parts = [row[idx] for row in values]
        conflict = any(value is None or value != parts[0] for value in parts)
        mask |= bit if conflict else 0
        expected.append(None if conflict else parts[0])
    if mask != doc.review_mask or tuple(expected) != tuple(doc[3:6]):
        raise HTTPException(409, 'Признаки конфликта не соответствуют источникам')
    item_hashes = db.execute(text('SELECT i.origin_kind, i.origin_id, i.item_id, i.content_hash '
                                  'FROM unified_receipt_item_origins i JOIN unified_receipt_origins o '
                                  'ON o.origin_kind=i.origin_kind AND o.origin_id=i.origin_id '
                                  'WHERE o.document_id=:id'), {'id': document_id}).all()
    revision = review_fingerprint(document_id, actor_id, company_id, doc, origins, item_hashes,
                                  (registry.company_id, pool.assigned_company_id, pool.status, pool.assigned_at))
    return {'revision': revision, 'company_id': company_id, 'ownership_conflict': True,
            'other_assignment_exists': True, 'additional_fiscal_conflicts': bool(mask & 7),
            'warning': ('Только предварительное предложение: владение не определяется и не передаётся, '
                        'вторая привязка не создаётся, обе исходные записи сохраняются.')}, ids


def preview_ownership_conflict(db: Session, document_id: str, company_id: str, actor: User) -> dict:
    """Показать факт конфликта без названия или приватных данных другой компании."""
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    begin_read_snapshot(db)
    view, _ = _context(db, document_id, company_id, actor.id)
    return view


def propose_registry_owner_review(db: Session, document_id: str, company_id: str, actor: User, *,
                                  revision: str, confirm: bool, acknowledge_no_transfer: bool) -> bool:
    """Предложить дальнейшую проверку текущего владельца реестра, ничего не передавая."""
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    if (confirm is not True or acknowledge_no_transfer is not True
            or not isinstance(revision, str) or len(revision) != 64):
        raise HTTPException(422, 'Нужны актуальный обзор и явные подтверждения')
    actor_row = db.execute(text('SELECT role, is_active FROM users WHERE id=:id'), {'id': actor.id}).first()
    if not actor_row or actor_row.role != 'admin' or not actor_row.is_active:
        raise HTTPException(403, 'Разбор доступен только администратору')
    doc = db.execute(text('SELECT fn, fd, fp FROM unified_fiscal_documents WHERE id=:id'),
                     {'id': document_id}).first()
    if not doc:
        raise HTTPException(404, 'Чек не найден')
    db.flush()
    lock_identity(db, *doc)
    db.expire_all()
    view, ids = _context(db, document_id, company_id, actor.id)
    if not hmac.compare_digest(view['revision'], revision):
        raise HTTPException(409, 'Снимок или права изменились после просмотра')
    old = db.execute(text('SELECT registry_company_id, registry_origin_id, pool_origin_id, actor_id, '
                          'review_digest, acknowledged_no_transfer FROM unified_ownership_review_proposals '
                          'WHERE document_id=:id'), {'id': document_id}).first()
    if old:
        if old != (company_id, ids['registry'], ids['pool'], actor.id, revision, 1):
            raise HTTPException(409, 'Для чека уже сохранено другое предложение')
        return False
    with db.begin_nested():
        db.execute(text('INSERT INTO unified_ownership_review_proposals '
                        '(document_id, registry_company_id, registry_origin_id, pool_origin_id, actor_id, '
                        'review_digest, acknowledged_no_transfer, created_at) '
                        'VALUES (:doc, :company, :registry, :pool, :actor, :revision, 1, :created)'),
                   {'doc': document_id, 'company': company_id, 'registry': ids['registry'], 'pool': ids['pool'],
                    'actor': actor.id, 'revision': revision, 'created': utcnow().isoformat(' ')})
    return True
