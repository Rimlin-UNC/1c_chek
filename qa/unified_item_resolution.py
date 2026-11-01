"""Экспериментальная сверка позиций двух источников на SQLite-копии.

Доступ — только администратору, аутентификация и commit/rollback остаются
за вызывающим. Функция не подключена к API и не удаляет старые данные.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Company, Receipt, User, utcnow
from app.pool.models import PoolReceipt
from app.services.receipt_identity import lock_identity
from qa.migrate_receipt_copy import _item_digest, _normalized
from qa.unified_integrity import pool_seller_matches_snapshot
from qa.unified_item_comparison import common_positions_match, sellers_match


def _current_item_hashes(db: Session, kind: str, origin_id: str) -> bool:
    # Имя таблицы выбирается только из фиксированного списка, не из ввода.
    table = {'registry': 'receipt_items', 'pool': 'pool_items'}[kind]
    rows = db.execute(text(f'SELECT * FROM {table} WHERE receipt_id=:id'), {'id': origin_id})
    index = list(rows.keys()).index('id')
    actual = {row[index]: _item_digest(row) for row in rows}
    expected = dict(db.execute(text('SELECT item_id, content_hash FROM unified_receipt_item_origins '
                                    'WHERE origin_kind=:kind AND origin_id=:id'),
                               {'kind': kind, 'id': origin_id}).all())
    return actual == expected


def resolve_matching_items(db: Session, document_id: str, actor: User) -> bool:
    """После явного действия администратора выбрать позиции реестра, если общие поля совпадают.

    Требуются два источника, доказуемо совпадающие продавец и непустые позиции,
    единый владелец и неизменность снимка. Дополнительные поля реестра и все
    исходные записи пула сохраняются; совпадение не подтверждает полноту ФНС.
    True — новое решение, False — проверенный идемпотентный повтор.
    """
    if db.get_bind().dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    if not actor.is_active or actor.role != 'admin':
        raise HTTPException(403, 'Сверка доступна только администратору')
    actor_id = actor.id
    doc = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                          'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    if not doc:
        raise HTTPException(404, 'Чек не найден')
    db.flush()
    lock_identity(db, doc.fn, doc.fd, doc.fp)
    db.expire_all()
    actor = db.get(User, actor_id)
    if not actor or not actor.is_active or actor.role != 'admin':
        raise HTTPException(403, 'Сверка доступна только администратору')
    fresh = db.execute(text('SELECT fn, fd, fp, receipt_date, total_sum, operation, review_mask '
                            'FROM unified_fiscal_documents WHERE id=:id'), {'id': document_id}).first()
    origins = db.execute(text('SELECT origin_kind, origin_id FROM unified_receipt_origins '
                              'WHERE document_id=:id'), {'id': document_id}).all()
    if fresh != doc or doc.review_mask or len(origins) != 2 or {r[0] for r in origins} != {'registry', 'pool'}:
        raise HTTPException(409, 'Источники или реквизиты требуют отдельной сверки')
    sources = dict(origins)
    registry, pool = db.get(Receipt, sources['registry']), db.get(PoolReceipt, sources['pool'])
    claim = db.execute(text('SELECT company_id, registry_receipt_id, pool_origin_id '
                            'FROM unified_company_claims WHERE document_id=:id'), {'id': document_id}).first()
    if not registry or not pool or not claim or claim.registry_receipt_id != registry.id:
        raise HTTPException(409, 'Владелец или источники требуют отдельной сверки')
    company = db.get(Company, claim.company_id)
    if (not company or not company.is_active or registry.company_id != claim.company_id
            or pool.assigned_company_id not in (None, claim.company_id)
            or (pool.assigned_company_id and claim.pool_origin_id != pool.id)
            or (not pool.assigned_company_id and claim.pool_origin_id is not None)):
        raise HTTPException(409, 'Владелец или источники требуют отдельной сверки')
    expected = (doc.receipt_date, doc.total_sum, doc.operation)
    for source in (registry, pool):
        if ((source.fn, source.fd, source.fp) != (doc.fn, doc.fd, doc.fp)
                or _normalized(source.receipt_date, source.total_sum, source.operation) != expected):
            raise HTTPException(409, 'Реквизиты источников изменились, нужна сверка')
    if (not pool_seller_matches_snapshot(db, document_id, pool.id)
            or not _current_item_hashes(db, 'registry', registry.id)
            or not _current_item_hashes(db, 'pool', pool.id)):
        raise HTTPException(409, 'Источники изменились после снимка')
    reg_seller = (registry.merchant_name, registry.merchant_inn, registry.merchant_address)
    pool_seller = (pool.merchant_name, pool.merchant_inn, pool.merchant_address)
    fields = 'name, quantity, price, total, vat_rate, vat_sum'
    registry_items = db.execute(text(f'SELECT {fields} FROM receipt_items WHERE receipt_id=:id'),
                                {'id': registry.id}).all()
    pool_items = db.execute(text(f'SELECT {fields} FROM pool_items WHERE receipt_id=:id'),
                            {'id': pool.id}).all()
    if not sellers_match(reg_seller, pool_seller) or not common_positions_match(registry_items, pool_items):
        raise HTTPException(409, 'Данные продавца или позиций не совпадают либо неполны')
    item_set = db.execute(text('SELECT state, origin_kind, origin_id FROM unified_fiscal_item_sets '
                               'WHERE document_id=:id'), {'id': document_id}).first()
    event = db.execute(text('SELECT registry_origin_id, pool_origin_id, method '
                            'FROM unified_item_resolution_events WHERE document_id=:id'), {'id': document_id}).first()
    refs = set(db.execute(text('SELECT origin_kind, origin_id, item_id FROM unified_fiscal_item_refs '
                               'WHERE document_id=:id'), {'id': document_id}).all())
    expected_refs = {('registry', registry.id, item_id) for item_id in db.execute(text(
        "SELECT item_id FROM unified_receipt_item_origins WHERE origin_kind='registry' AND origin_id=:id"),
        {'id': registry.id}).scalars()}
    if item_set == ('resolved_registry', 'registry', registry.id):
        if event != (registry.id, pool.id, 'equal_common_fields') or refs != expected_refs:
            raise HTTPException(409, 'Решение по позициям повреждено')
        return False
    if item_set != ('needs_review', None, None) or event or refs:
        raise HTTPException(409, 'Решение по позициям требует отдельной сверки')
    with db.begin_nested():
        db.execute(text("UPDATE unified_fiscal_item_sets SET state='resolved_registry', "
                        "origin_kind='registry', origin_id=:reg WHERE document_id=:doc"),
                   {'reg': registry.id, 'doc': document_id})
        db.execute(text('INSERT INTO unified_fiscal_item_refs (document_id, origin_kind, origin_id, item_id) '
                        "SELECT :doc, origin_kind, origin_id, item_id FROM unified_receipt_item_origins "
                        "WHERE origin_kind='registry' AND origin_id=:reg"),
                   {'doc': document_id, 'reg': registry.id})
        db.execute(text('INSERT INTO unified_item_resolution_events '
                        '(document_id, registry_origin_id, pool_origin_id, actor_id, method, created_at) '
                        "VALUES (:doc, :reg, :pool, :actor, 'equal_common_fields', :created)"),
                   {'doc': document_id, 'reg': registry.id, 'pool': pool.id,
                    'actor': actor.id, 'created': utcnow().isoformat(' ')})
    return True
