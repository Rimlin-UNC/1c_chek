"""Предварительный разбор фискальных расхождений на отдельной SQLite-копии.

Не API и не миграция рабочей базы. Предложение не снимает флаг конфликта,
не меняет владельца и не доказывает достоверность или проверку ФНС.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import User, utcnow
from app.services.receipt_identity import lock_identity
from qa.migrate_receipt_copy import _normalized
from qa.unified_fiscal_proposal_fingerprint import fiscal_proposal_fingerprint
from qa.unified_integrity import (
    begin_read_snapshot, pool_seller_matches_snapshot, registry_seller_matches_snapshot, source_core_matches_snapshot, source_extra_matches_snapshot,
)
from qa.unified_item_resolution import _current_item_hashes


def _context(db: Session, document_id: str, company_id: str, actor_id: str):
    actor = db.execute(text('SELECT role, is_active FROM users WHERE id=:id'), {'id': actor_id}).first()
    if not actor or actor.role != 'admin' or not actor.is_active:
        raise HTTPException(403, 'Разбор доступен только администратору')
    doc = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                          'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    claim = db.execute(text('SELECT company_id, registry_receipt_id, pool_origin_id, assigned_at '
                            'FROM unified_company_claims WHERE document_id=:id'), {'id': document_id}).first()
    if not doc or not claim or not company_id or claim.company_id != company_id:
        raise HTTPException(404, 'Чек не найден')
    origins = db.execute(text('SELECT origin_kind, origin_id, fiscal_source_hash, extra_fiscal_hash, core_fiscal_hash '
                              'FROM unified_receipt_origins WHERE document_id=:id'), {'id': document_id}).all()
    company = db.execute(text('SELECT is_active FROM companies WHERE id=:id'), {'id': company_id}).first()
    if (not company or not company.is_active or not (doc.review_mask & 7) or doc.review_mask & 8
            or len(origins) != 2 or {row.origin_kind for row in origins} != {'registry', 'pool'}):
        raise HTTPException(409, 'Конфликт или владение требуют отдельного разбора')
    ids = {row.origin_kind: row.origin_id for row in origins}
    registry = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, company_id '
                               'FROM receipts WHERE id=:id'), {'id': ids['registry']}).first()
    pool = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, assigned_company_id, '
                           'status, assigned_at FROM pool_receipts WHERE id=:id'), {'id': ids['pool']}).first()
    if (not registry or not pool or claim.registry_receipt_id != ids['registry']
            or registry.company_id != company_id or pool.assigned_company_id not in (None, company_id)
            or (pool.assigned_company_id and claim.pool_origin_id != ids['pool'])
            or (not pool.assigned_company_id and claim.pool_origin_id is not None)
            or (pool.assigned_company_id and claim.assigned_at != pool.assigned_at)):
        raise HTTPException(409, 'Конфликт или владение требуют отдельного разбора')
    if tuple(registry[:3]) != tuple(doc[:3]) or tuple(pool[:3]) != tuple(doc[:3]):
        raise HTTPException(409, 'Фискальная идентичность источников изменилась')
    if (not registry_seller_matches_snapshot(db, document_id, ids['registry'])
            or not pool_seller_matches_snapshot(db, document_id, ids['pool'])
            or not source_core_matches_snapshot(db, 'registry', document_id, ids['registry'])
            or not source_core_matches_snapshot(db, 'pool', document_id, ids['pool'])
            or not source_extra_matches_snapshot(db, 'registry', document_id, ids['registry'])
            or not source_extra_matches_snapshot(db, 'pool', document_id, ids['pool'])
            or not _current_item_hashes(db, 'registry', ids['registry'])
            or not _current_item_hashes(db, 'pool', ids['pool'])):
        raise HTTPException(409, 'Источники изменились после создания копии')
    values = []
    for kind, row in (('registry', registry), ('pool', pool)):
        values.append((kind, ids[kind], *_normalized(row.receipt_date, row.total_sum, row.operation)))
    # Флаги исходного снимка не снимаются и должны точно отражать обе версии.
    mask = 0
    expected = []
    for idx, bit in ((2, 1), (3, 2), (4, 4)):
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
    revision = fiscal_proposal_fingerprint(document_id, actor_id, company_id, doc, values,
                                           origins, item_hashes,
                                           (pool.status, pool.assigned_company_id, pool.assigned_at))
    sources = {}
    for kind, _origin, date, amount, operation in values:
        sources[kind] = {'receipt_date': date, 'total_sum': amount, 'operation': operation,
                         'selectable': date is not None and amount is not None and amount > 0 and operation is not None}
    return {'revision': revision, 'company_id': company_id,
            'conflicts': {'receipt_date': bool(mask & 1), 'total_sum': bool(mask & 2),
                          'operation': bool(mask & 4)}, 'sources': sources,
            'warning': ('Это только предложение: конфликт не снят, данные ФНС не проверены, '
                        'оба источника сохраняются.')}, ids


def preview_fiscal_conflict(db: Session, document_id: str, company_id: str, actor: User) -> dict:
    """Показать администратору только даты, суммы и операции двух источников."""
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    begin_read_snapshot(db)
    view, _ = _context(db, document_id, company_id, actor.id)
    return view


def propose_fiscal_source(db: Session, document_id: str, company_id: str, actor: User, *,
                          origin_kind: str, revision: str, confirm: bool, acknowledge_unverified: bool) -> bool:
    """Сохранить только предложение; никаких записей источников и статусов ФНС не меняет."""
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    if (origin_kind not in ('registry', 'pool') or confirm is not True or acknowledge_unverified is not True
            or not isinstance(revision, str) or len(revision) != 64):
        raise HTTPException(422, 'Нужны источник, актуальный обзор и явные подтверждения')
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
    if not view['sources'][origin_kind]['selectable']:
        raise HTTPException(409, 'В выбранном источнике неполные фискальные реквизиты')
    old = db.execute(text('SELECT company_id, origin_kind, origin_id, actor_id, review_digest, '
                          'acknowledged_unverified FROM unified_fiscal_resolution_proposals WHERE document_id=:id'),
                     {'id': document_id}).first()
    if old:
        if old != (company_id, origin_kind, ids[origin_kind], actor.id, revision, 1):
            raise HTTPException(409, 'Для чека уже сохранено другое предложение')
        return False
    with db.begin_nested():
        db.execute(text('INSERT INTO unified_fiscal_resolution_proposals '
                        '(document_id, company_id, origin_kind, origin_id, actor_id, review_digest, '
                        'acknowledged_unverified, created_at) '
                        'VALUES (:doc, :company, :kind, :origin, :actor, :revision, 1, :created)'),
                   {'doc': document_id, 'company': company_id, 'kind': origin_kind, 'origin': ids[origin_kind],
                    'actor': actor.id, 'revision': revision, 'created': utcnow().isoformat(' ')})
    return True
