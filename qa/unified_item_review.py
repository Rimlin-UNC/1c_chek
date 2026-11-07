"""Явный разбор расхождений позиций на экспериментальной SQLite-копии.

Не API. Вызывающий аутентифицирует администратора и управляет транзакцией.
Исходные записи не удаляются, история и личные данные донора не объединяются.
"""
from __future__ import annotations

import hmac
import json

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import User, utcnow
from app.services.receipt_identity import lock_identity
from qa.migrate_receipt_copy import _normalized
from qa.unified_integrity import (
    begin_read_snapshot, pool_seller_matches_snapshot, registry_seller_matches_snapshot, source_core_matches_snapshot, source_extra_matches_snapshot,
)
from qa.unified_item_comparison import common_positions_match, sellers_match
from qa.unified_item_resolution import _current_item_hashes
from qa.unified_review_fingerprint import review_fingerprint


_FIELDS = ('name', 'quantity', 'price', 'total', 'vat_rate', 'vat_sum')


def _has_extra(raw) -> bool:
    """Показываем только факт наличия неизвестных данных, не их содержимое."""
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return True
    return parsed not in (None, {}, [], '')


def _registry_item(row) -> dict:
    """Ограниченная проекция дополнительных полей позиции реестра."""
    product_type, payment_type, unit, marking_present, extra_json = row[6:]
    if (any(value is not None and (type(value) is not int or not 0 <= value <= 9999)
            for value in (product_type, payment_type))
            or (unit is not None and (not isinstance(unit, str) or len(unit) > 20))
            or marking_present not in (None, 0, 1)):
        raise HTTPException(409, 'Дополнительные реквизиты позиции повреждены')
    result = dict(zip(_FIELDS, row[:6], strict=True))
    result['fiscal_metadata'] = {'product_type': product_type, 'payment_type': payment_type,
                                 'unit': unit or '',
                                 'marking_present': None if marking_present is None else bool(marking_present),
                                 'has_unreviewed_extra': _has_extra(extra_json)}
    return result


def _context(db: Session, document_id: str, company_id: str, actor_id: str):
    """Подготовить данные только после проверки роли и принадлежности."""
    actor = db.execute(text('SELECT role, is_active FROM users WHERE id=:id'), {'id': actor_id}).first()
    if not actor or actor.role != 'admin' or not actor.is_active:
        raise HTTPException(403, 'Сверка доступна только администратору')
    doc = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                          'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    claim = db.execute(text('SELECT company_id, registry_receipt_id, pool_origin_id '
                            'FROM unified_company_claims WHERE document_id=:id'), {'id': document_id}).first()
    if not doc or not claim or not company_id or claim.company_id != company_id:
        raise HTTPException(404, 'Чек не найден')
    company = db.execute(text('SELECT is_active FROM companies WHERE id=:id'), {'id': company_id}).first()
    origins = db.execute(text('SELECT origin_kind, origin_id, fiscal_source_hash, extra_fiscal_hash, core_fiscal_hash FROM unified_receipt_origins '
                              'WHERE document_id=:id'), {'id': document_id}).all()
    if (not company or not company.is_active or doc.review_mask or len(origins) != 2
            or {row.origin_kind for row in origins} != {'registry', 'pool'}):
        raise HTTPException(409, 'Источники или владение требуют отдельного разбора')
    ids = {row.origin_kind: row.origin_id for row in origins}
    registry = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, company_id, '
                               'merchant_name, merchant_inn, merchant_address, ext_json FROM receipts WHERE id=:id'),
                          {'id': ids['registry']}).first()
    pool = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, assigned_company_id, '
                           'merchant_name, merchant_inn, merchant_address, status, assigned_at, extra_json '
                           'FROM pool_receipts WHERE id=:id'), {'id': ids['pool']}).first()
    if (not registry or not pool or claim.registry_receipt_id != ids['registry']
            or registry.company_id != company_id or pool.assigned_company_id not in (None, company_id)
            or (pool.assigned_company_id and claim.pool_origin_id != ids['pool'])
            or (not pool.assigned_company_id and claim.pool_origin_id is not None)):
        raise HTTPException(409, 'Источники или владение требуют отдельного разбора')
    for source in (registry, pool):
        if (tuple(source[:3]) != tuple(doc[:3])
                or _normalized(source.receipt_date, source.total_sum, source.operation) != tuple(doc[3:6])):
            raise HTTPException(409, 'Реквизиты источников изменились, нужна сверка')
    if (not registry_seller_matches_snapshot(db, document_id, ids['registry'])
            or not pool_seller_matches_snapshot(db, document_id, ids['pool'])
            or not source_core_matches_snapshot(db, 'registry', document_id, ids['registry'])
            or not source_core_matches_snapshot(db, 'pool', document_id, ids['pool'])
            or not source_extra_matches_snapshot(db, 'registry', document_id, ids['registry'])
            or not source_extra_matches_snapshot(db, 'pool', document_id, ids['pool'])
            or not _current_item_hashes(db, 'registry', ids['registry'])
            or not _current_item_hashes(db, 'pool', ids['pool'])):
        raise HTTPException(409, 'Источники изменились после снимка')
    cols = ', '.join(_FIELDS)
    reg_rows = db.execute(text(f'SELECT {cols}, product_type, payment_type, unit, marking_present, extra_json '
                               'FROM receipt_items WHERE receipt_id=:id ORDER BY id'),
                          {'id': ids['registry']}).all()
    pool_rows = db.execute(text(f'SELECT {cols} FROM pool_items WHERE receipt_id=:id ORDER BY id'),
                           {'id': ids['pool']}).all()
    # Нельзя подтвердить пустые, нечисловые или некорректные позиции реестра.
    # Пустой пул допустим только как явно отображённое отсутствие данных.
    reg_common = [row[:6] for row in reg_rows]
    if not common_positions_match(reg_common, reg_common) or (pool_rows and not common_positions_match(pool_rows, pool_rows)):
        raise HTTPException(409, 'Позиции неполные или содержат некорректные значения')
    registry_items = [_registry_item(row) for row in reg_rows]
    item_hashes = db.execute(text('SELECT i.origin_kind, i.origin_id, i.item_id, i.content_hash '
                                  'FROM unified_receipt_item_origins i JOIN unified_receipt_origins o '
                                  'ON o.origin_kind=i.origin_kind AND o.origin_id=i.origin_id '
                                  'WHERE o.document_id=:id'), {'id': document_id}).all()
    digest = review_fingerprint(document_id, actor_id, company_id, doc, origins, item_hashes,
                                (pool.status, pool.assigned_company_id, pool.assigned_at))
    result = {'revision': digest, 'company_id': company_id,
              'review_scope': 'common_fields_and_registry_metadata',
              'warning': ('Показана только часть реквизитов; дополнительные JSON, сырой ответ и личные данные '
                          'не показаны. Оба исходника сохраняются; совпадение полных версий не подтверждено.'),
              'seller_equal': sellers_match(registry[7:10], pool[7:10]),
              'positions_equal': common_positions_match(reg_common, pool_rows),
              'registry': {'seller': dict(zip(('name', 'inn', 'address'), registry[7:10], strict=True)),
                           'has_unreviewed_header_extra': _has_extra(registry.ext_json), 'items': registry_items},
              'pool': {'seller': dict(zip(('name', 'inn', 'address'), pool[7:10], strict=True)),
                       'has_unreviewed_header_extra': _has_extra(pool.extra_json),
                       'items': [dict(zip(_FIELDS, row, strict=True)) for row in pool_rows]}}
    return result, ids


def preview_item_disagreement(db: Session, document_id: str, company_id: str, actor: User) -> dict:
    """Показать только общие фискальные поля обеих версий, без сырого ответа и частных полей."""
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    begin_read_snapshot(db)
    result, _ = _context(db, document_id, company_id, actor.id)
    state = db.execute(text('SELECT state FROM unified_fiscal_item_sets WHERE document_id=:id'),
                       {'id': document_id}).scalar_one_or_none()
    if state != 'needs_review':
        raise HTTPException(409, 'Состав уже выбран или требует отдельной проверки')
    return result


def resolve_reviewed_registry(db: Session, document_id: str, company_id: str, actor: User, *,
                              revision: str, confirm: bool, acknowledge_unreviewed: bool = False) -> bool:
    """Выбрать реестр после просмотра и подтверждения, не удаляя пул и его позиции."""
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    if (confirm is not True or acknowledge_unreviewed is not True
            or not isinstance(revision, str) or len(revision) != 64):
        raise HTTPException(422, 'Нужны просмотренная версия, подтверждение выбора и неполноты просмотра')
    actor_row = db.execute(text('SELECT role, is_active FROM users WHERE id=:id'), {'id': actor.id}).first()
    if not actor_row or actor_row.role != 'admin' or not actor_row.is_active:
        raise HTTPException(403, 'Сверка доступна только администратору')
    doc = db.execute(text('SELECT fn, fd, fp FROM unified_fiscal_documents WHERE id=:id'),
                     {'id': document_id}).first()
    if not doc:
        raise HTTPException(404, 'Чек не найден')
    db.flush()
    lock_identity(db, *doc)
    db.expire_all()
    view, ids = _context(db, document_id, company_id, actor.id)
    if not hmac.compare_digest(view['revision'], revision):
        raise HTTPException(409, 'Данные или права изменились после просмотра; повторите сверку')
    item_set = db.execute(text('SELECT state, origin_kind, origin_id FROM unified_fiscal_item_sets '
                               'WHERE document_id=:id'), {'id': document_id}).first()
    event = db.execute(text('SELECT registry_origin_id, pool_origin_id, method, review_digest, actor_id, acknowledged_incomplete '
                            'FROM unified_item_resolution_events WHERE document_id=:id'), {'id': document_id}).first()
    refs = set(db.execute(text('SELECT origin_kind, origin_id, item_id FROM unified_fiscal_item_refs '
                               'WHERE document_id=:id'), {'id': document_id}).all())
    expected_refs = {('registry', ids['registry'], item_id) for item_id in db.execute(text(
        "SELECT item_id FROM unified_receipt_item_origins WHERE origin_kind='registry' AND origin_id=:id"),
        {'id': ids['registry']}).scalars()}
    if item_set == ('resolved_registry', 'registry', ids['registry']):
        if event != (ids['registry'], ids['pool'], 'reviewed_registry', revision, actor.id, 1) or refs != expected_refs:
            raise HTTPException(409, 'Состав уже выбран или решение повреждено')
        return False
    if item_set != ('needs_review', None, None) or event or refs:
        raise HTTPException(409, 'Состав уже выбран или требует отдельной проверки')
    with db.begin_nested():
        db.execute(text("UPDATE unified_fiscal_item_sets SET state='resolved_registry', "
                        "origin_kind='registry', origin_id=:reg WHERE document_id=:doc"),
                   {'reg': ids['registry'], 'doc': document_id})
        db.execute(text('INSERT INTO unified_fiscal_item_refs (document_id, origin_kind, origin_id, item_id) '
                        "SELECT :doc, origin_kind, origin_id, item_id FROM unified_receipt_item_origins "
                        "WHERE origin_kind='registry' AND origin_id=:reg"),
                   {'doc': document_id, 'reg': ids['registry']})
        db.execute(text('INSERT INTO unified_item_resolution_events '
                        '(document_id, registry_origin_id, pool_origin_id, actor_id, method, review_digest, acknowledged_incomplete, created_at) '
                        "VALUES (:doc, :reg, :pool, :actor, 'reviewed_registry', :digest, 1, :created)"),
                   {'doc': document_id, 'reg': ids['registry'], 'pool': ids['pool'], 'actor': actor.id,
                    'digest': revision, 'created': utcnow().isoformat(' ')})
    return True
