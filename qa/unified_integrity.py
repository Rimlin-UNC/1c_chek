"""Проверки неизменности источников для экспериментального SQLite-снимка."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from qa.migrate_receipt_copy import _item_digest


def begin_read_snapshot(db: Session) -> None:
    """Начать физическую SQLite-транзакцию до первого чтения проекции.

    В режиме sqlite3 legacy один SELECT сам по себе не открывает транзакцию,
    и несколько проверок могли бы увидеть разные версии данных. Вызывающий
    должен завершить её обычным commit/rollback; PostgreSQL здесь не проверен.
    """
    connection = db.connection()
    if connection.dialect.name != 'sqlite':
        raise RuntimeError('Эксперимент поддерживает только SQLite')
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')


def pool_items_match_snapshot(db: Session, pool_id: str, document_id: str) -> bool:
    """Проверить состав и содержимое позиций, не раскрывая их значения."""
    result = db.execute(text('SELECT * FROM pool_items WHERE receipt_id=:id'), {'id': pool_id})
    id_index = list(result.keys()).index('id')
    current = {row[id_index]: _item_digest(row) for row in result}
    linked = dict(db.execute(text(
        "SELECT item_id, content_hash FROM unified_receipt_item_origins "
        "WHERE origin_kind='pool' AND origin_id=:id"), {'id': pool_id}).all())
    item_set = db.execute(text('SELECT state, origin_kind, origin_id FROM unified_fiscal_item_sets '
                               'WHERE document_id=:id'), {'id': document_id}).first()
    origins = db.execute(text('SELECT origin_kind, origin_id FROM unified_receipt_origins '
                              'WHERE document_id=:id'), {'id': document_id}).all()
    refs = db.execute(text('SELECT origin_kind, origin_id, item_id FROM unified_fiscal_item_refs '
                           'WHERE document_id=:id'), {'id': document_id}).all()
    return (current == linked and item_set == ('single_source', 'pool', pool_id)
            and origins == [('pool', pool_id)]
            and set(refs) == {('pool', pool_id, item_id) for item_id in linked})


def pool_seller_matches_snapshot(db: Session, document_id: str, pool_id: str) -> bool:
    """Сверить открываемые компании реквизиты продавца с источником снимка."""
    row = db.execute(text("SELECT p.merchant_name, p.merchant_inn, p.merchant_address, "
                          "o.fiscal_source_hash FROM unified_receipt_origins o "
                          "JOIN pool_receipts p ON p.id=o.origin_id "
                          "WHERE o.origin_kind='pool' AND o.origin_id=:pool AND o.document_id=:doc"),
                     {'pool': pool_id, 'doc': document_id}).first()
    return bool(row and row.fiscal_source_hash and row.fiscal_source_hash == _item_digest(row[:3]))


def registry_seller_matches_snapshot(db: Session, document_id: str, receipt_id: str) -> bool:
    """Проверить реквизиты продавца реестра, сохранённые при создании снимка."""
    row = db.execute(text("SELECT r.merchant_name, r.merchant_inn, r.merchant_address, "
                          "o.fiscal_source_hash FROM unified_receipt_origins o "
                          "JOIN receipts r ON r.id=o.origin_id "
                          "WHERE o.origin_kind='registry' AND o.origin_id=:receipt AND o.document_id=:doc"),
                     {'receipt': receipt_id, 'doc': document_id}).first()
    return bool(row and row.fiscal_source_hash and row.fiscal_source_hash == _item_digest(row[:3]))


def source_extra_matches_snapshot(db: Session, kind: str, document_id: str, origin_id: str) -> bool:
    """Проверить дополнительные данные источника, не раскрывая их содержимое."""
    table, column = {'registry': ('receipts', 'ext_json'), 'pool': ('pool_receipts', 'extra_json')}[kind]
    row = db.execute(text(f'SELECT p.{column}, o.extra_fiscal_hash FROM {table} p '
                          'JOIN unified_receipt_origins o ON o.origin_id=p.id '
                          'WHERE o.origin_kind=:kind AND o.origin_id=:id AND o.document_id=:doc'),
                     {'kind': kind, 'id': origin_id, 'doc': document_id}).first()
    return bool(row and row.extra_fiscal_hash and row.extra_fiscal_hash == _item_digest(row[:1]))
