"""Сериализация операций с одним фискальным чеком между пулом и реестром."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import ReceiptIdentityLock


# Сколько ключей блокировать одной вставкой: строка параметров остаётся
# короткой и для SQLite, и для PostgreSQL.
LOCK_CHUNK = 500


def _lock_key(fn: str, fd: str, fp: str) -> str:
    return hashlib.sha256(json.dumps([fn, fd, fp], ensure_ascii=False).encode()).hexdigest()


def _insert_for(db):
    dialect = db.get_bind().dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError("Неподдерживаемая СУБД для защиты от дублей чеков")
    return sqlite_insert if dialect == "sqlite" else pg_insert


def lock_identity(db, fn: str, fd: str, fp: str) -> None:
    """Блокировка до завершения транзакции; не содержит фискальных реквизитов."""
    key = _lock_key(fn, fd, fp)
    stmt = _insert_for(db)(ReceiptIdentityLock).values(key=key)
    db.execute(stmt.on_conflict_do_update(index_elements=[ReceiptIdentityLock.key],
                                          set_={"key": key}))


def lock_identities(db, triples) -> int:
    """Пакетная блокировка: то же, что lock_identity для каждой тройки.

    Ключи берутся без повторов и в отсортированном порядке — любые две
    пакетные операции захватывают общие ключи в одном порядке и не ждут
    друг друга по кругу (PostgreSQL блокирует строки по мере вставки).
    Возвращает число заблокированных ключей.
    """
    keys = sorted({_lock_key(fn, fd, fp) for fn, fd, fp in triples})
    if not keys:
        return 0
    insert = _insert_for(db)
    for start in range(0, len(keys), LOCK_CHUNK):
        stmt = insert(ReceiptIdentityLock).values([{"key": k} for k in keys[start:start + LOCK_CHUNK]])
        db.execute(stmt.on_conflict_do_update(index_elements=[ReceiptIdentityLock.key],
                                              set_={"key": stmt.excluded.key}))
    return len(keys)
