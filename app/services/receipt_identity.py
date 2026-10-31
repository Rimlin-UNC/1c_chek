"""Сериализация операций с одним фискальным чеком между пулом и реестром."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import ReceiptIdentityLock


def lock_identity(db, fn: str, fd: str, fp: str) -> None:
    """Блокировка до завершения транзакции; не содержит фискальных реквизитов."""
    key = hashlib.sha256(json.dumps([fn, fd, fp], ensure_ascii=False).encode()).hexdigest()
    dialect = db.get_bind().dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError("Неподдерживаемая СУБД для защиты от дублей чеков")
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    stmt = insert(ReceiptIdentityLock).values(key=key)
    db.execute(stmt.on_conflict_do_update(index_elements=[ReceiptIdentityLock.key],
                                          set_={"key": key}))
