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
