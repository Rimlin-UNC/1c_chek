"""Проверки неизменности источников для экспериментального SQLite-снимка."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from qa.migrate_receipt_copy import _item_digest


def pool_items_match_snapshot(db: Session, pool_id: str) -> bool:
    """Проверить состав и содержимое позиций, не раскрывая их значения."""
    result = db.execute(text('SELECT * FROM pool_items WHERE receipt_id=:id'), {'id': pool_id})
    id_index = list(result.keys()).index('id')
    current = {row[id_index]: _item_digest(row) for row in result}
    linked = dict(db.execute(text(
        "SELECT item_id, content_hash FROM unified_receipt_item_origins "
        "WHERE origin_kind='pool' AND origin_id=:id"), {'id': pool_id}).all())
    return current == linked


def pool_seller_matches_snapshot(db: Session, document_id: str, pool_id: str) -> bool:
    """Сверить открываемые компании реквизиты продавца с источником снимка."""
    row = db.execute(text("SELECT p.merchant_name, p.merchant_inn, p.merchant_address, "
                          "o.fiscal_source_hash FROM unified_receipt_origins o "
                          "JOIN pool_receipts p ON p.id=o.origin_id "
                          "WHERE o.origin_kind='pool' AND o.origin_id=:pool AND o.document_id=:doc"),
                     {'pool': pool_id, 'doc': document_id}).first()
    return bool(row and row.fiscal_source_hash and row.fiscal_source_hash == _item_digest(row[:3]))
