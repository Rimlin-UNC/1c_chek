"""Экспериментальная миграция фискальных записей на отдельной копии SQLite.

Оригинальные таблицы сохраняются: приложение ещё не переключено на новую
модель. Скрипт не публикует чеки и не назначает компании. Запуск:
python -m qa.migrate_receipt_copy --source /path/current.db --output /path/copy.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4


_SCHEMA = """
CREATE TABLE unified_fiscal_documents (
    id TEXT PRIMARY KEY,
    fn TEXT NOT NULL,
    fd TEXT NOT NULL,
    fp TEXT NOT NULL,
    receipt_date TEXT,
    total_sum REAL,
    operation INTEGER,
    -- Биты: 1 дата, 2 сумма, 4 операция, 8 разные компании.
    -- Неоднозначные общие значения = NULL.
    review_mask INTEGER NOT NULL DEFAULT 0,
    UNIQUE(fn, fd, fp)
);
CREATE TABLE unified_receipt_origins (
    document_id TEXT NOT NULL REFERENCES unified_fiscal_documents(id),
    origin_kind TEXT NOT NULL CHECK(origin_kind IN ('registry', 'pool')),
    origin_id TEXT NOT NULL,
    fiscal_source_hash TEXT,
    PRIMARY KEY(origin_kind, origin_id)
);
CREATE INDEX ix_unified_origins_document ON unified_receipt_origins(document_id);
-- Позиции источников остаются раздельными, пока не доказано их соответствие.
CREATE TABLE unified_receipt_item_origins (
    origin_kind TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(origin_kind, item_id),
    FOREIGN KEY(origin_kind, origin_id) REFERENCES unified_receipt_origins(origin_kind, origin_id)
);
CREATE INDEX ix_unified_item_origin ON unified_receipt_item_origins(origin_kind, origin_id);
-- Единственное право компании на документ: одна строка на fiscal document.
CREATE TABLE unified_company_claims (
    document_id TEXT PRIMARY KEY REFERENCES unified_fiscal_documents(id),
    company_id TEXT NOT NULL CHECK (length(trim(company_id)) > 0),
    pool_origin_id TEXT REFERENCES pool_receipts(id),
    registry_receipt_id TEXT REFERENCES receipts(id),
    assigned_at TEXT,
    UNIQUE(document_id, company_id)
);
CREATE INDEX ix_unified_company_claims_company ON unified_company_claims(company_id);
-- Только личные учётные поля компании, фискальные данные не копируются.
CREATE TABLE unified_company_accounts (
    document_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    legacy_receipt_id TEXT UNIQUE REFERENCES receipts(id),
    pool_origin_id TEXT REFERENCES pool_receipts(id),
    created_by TEXT,
    created_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'verified')),
    fns_status TEXT NOT NULL CHECK(fns_status IN ('unknown', 'valid')),
    fns_checked_at TEXT,
    assignee TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    personal_sum REAL NOT NULL DEFAULT 0,
    exported INTEGER NOT NULL DEFAULT 0 CHECK(exported IN (0,1)),
    CHECK(legacy_receipt_id IS NOT NULL OR pool_origin_id IS NOT NULL),
    CHECK(fns_status != 'valid' OR (status = 'verified' AND fns_checked_at IS NOT NULL)),
    FOREIGN KEY(document_id, company_id) REFERENCES unified_company_claims(document_id, company_id)
);
CREATE INDEX ix_unified_company_accounts_company ON unified_company_accounts(company_id);
CREATE TABLE unified_company_account_events (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES unified_company_accounts(document_id),
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('created', 'updated')),
    fields TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX ix_unified_account_events_document ON unified_company_account_events(document_id);
"""


def _item_digest(row) -> str:
    """Контрольная сумма всех сохранённых колонок позиции, без публикации данных."""
    encoded = []
    for value in row:
        if value is None:
            encoded.append(['null', None])
        elif isinstance(value, bytes):
            encoded.append(['blob', value.hex()])
        elif isinstance(value, float):
            encoded.append(['float', value.hex()])
        elif isinstance(value, int):
            encoded.append(['integer', str(value)])
        else:
            encoded.append(['text', str(value)])
    raw = json.dumps(encoded, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _normalized(date, amount, operation):
    """Неполное или некорректное значение не подменяем догадкой."""
    try:
        date = datetime.fromisoformat(str(date)).isoformat(' ') if date else None
    except (TypeError, ValueError):
        date = None
    try:
        amount = float(amount) if amount is not None else None
        if amount is not None and not math.isfinite(amount):
            amount = None
    except (TypeError, ValueError):
        amount = None
    try:
        raw_operation = str(operation).strip() if operation is not None else ''
        operation = int(raw_operation) if raw_operation in ('1', '2', '3', '4') else None
    except (TypeError, ValueError):
        operation = None
    return date, amount, operation


def migrate_copy(source: str | Path, output: str | Path) -> dict:
    """Создать новую копию и построить единые идентичности, не меняя исходную БД.

    Это подготовительная миграция, а не переключение работающего приложения:
    таблицы источников, персональные поля, позиции и баллы остаются как были.
    Требует SQLite; output обязан не существовать. Права ОС на копию — 0600.
    """
    src, dst = Path(source).resolve(strict=True), Path(output).absolute()
    if not src.is_file():
        raise ValueError('Источник должен быть файлом SQLite')
    # O_EXCL не перезаписывает существующий файл и защищает от подмены ссылки.
    fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        source_uri = 'file:' + quote(str(src), safe='/') + '?mode=ro'
        with closing(sqlite3.connect(source_uri, uri=True)) as original:
            with closing(sqlite3.connect(dst)) as copied:
                original.backup(copied)
                _migrate(copied)
                _validate(copied)
                result = _summary(copied)
        return result
    except BaseException:
        # Удаляем только файл, созданный этой попыткой; исходник не трогаем.
        dst.unlink(missing_ok=True)
        raise


def _legacy_ownership(db: sqlite3.Connection, doc_id: str):
    """Установить владельца только если он однозначно следует из источников."""
    registry = db.execute("""
        SELECT r.id, r.company_id FROM unified_receipt_origins o
        JOIN receipts r ON r.id = o.origin_id
        WHERE o.document_id = ? AND o.origin_kind = 'registry'
    """, (doc_id,)).fetchall()
    pool = db.execute("""
        SELECT p.id, p.assigned_company_id, p.assigned_at FROM unified_receipt_origins o
        JOIN pool_receipts p ON p.id = o.origin_id
        WHERE o.document_id = ? AND o.origin_kind = 'pool'
    """, (doc_id,)).fetchall()
    # При нескольких источниках одного типа или неизвестном владельце реестра
    # не выбираем владельца догадкой, даже если пул уже кем-то занят.
    if len(registry) > 1 or len(pool) > 1:
        return None
    registry_id, registry_company = registry[0] if registry else (None, None)
    pool_id, pool_company, assigned_at = pool[0] if pool else (None, None, None)
    registry_company = registry_company if registry_company and registry_company.strip() else None
    pool_company = pool_company if pool_company and pool_company.strip() else None
    if registry and not registry_company:
        return None
    if registry_company and pool_company and registry_company != pool_company:
        return None
    owner = registry_company or pool_company
    if not owner:
        return None
    return (owner, pool_id if pool_company == owner else None, registry_id, assigned_at if pool_company == owner else None)


def _migrate(db: sqlite3.Connection) -> None:
    """Все новые таблицы и ссылки записываются в одной транзакции."""
    db.execute('PRAGMA foreign_keys = ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        # executescript фиксирует транзакцию. complete_statement учитывает
        # точки с запятой в комментариях и строках, в отличие от split(';').
        statement = ''
        for line in _SCHEMA.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ''
        if statement.strip():
            raise RuntimeError('Незавершённая команда схемы миграции')
        for kind, table in (('registry', 'receipts'), ('pool', 'pool_receipts')):
            cursor = db.execute(f'SELECT id, fn, fd, fp, receipt_date, total_sum, operation FROM {table}')
            while rows := cursor.fetchmany(500):
                for origin_id, fn, fd, fp, dt, amount, operation in rows:
                    if any(v is None or not str(v).strip() for v in (fn, fd, fp)):
                        continue
                    dt, amount, operation = _normalized(dt, amount, operation)
                    found = db.execute(
                        'SELECT id, receipt_date, total_sum, operation, review_mask '
                        'FROM unified_fiscal_documents WHERE fn=? AND fd=? AND fp=?',
                        (fn, fd, fp)).fetchone()
                    if not found:
                        doc_id = str(uuid4())
                        mask = sum(bit for value, bit in ((dt, 1), (amount, 2), (operation, 4)) if value is None)
                        db.execute('INSERT INTO unified_fiscal_documents '
                                   '(id, fn, fd, fp, receipt_date, total_sum, operation, review_mask) '
                                   'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                   (doc_id, fn, fd, fp, dt, amount, operation, mask))
                    else:
                        doc_id, old_dt, old_amount, old_op, mask = found
                        values = []
                        for old, new, bit in ((old_dt, dt, 1), (old_amount, amount, 2), (old_op, operation, 4)):
                            # Данные из разных источников нельзя молча округлять:
                            # даже небольшое расхождение требует разбора.
                            differs = old is None or new is None or old != new
                            if differs:
                                mask |= bit
                            values.append(None if mask & bit else old)
                        db.execute('UPDATE unified_fiscal_documents SET receipt_date=?, total_sum=?, '
                                   'operation=?, review_mask=? WHERE id=?', (*values, mask, doc_id))
                    db.execute('INSERT INTO unified_receipt_origins (document_id, origin_kind, origin_id) '
                               'VALUES (?, ?, ?)', (doc_id, kind, origin_id))
        # Хеш только открытых реквизитов продавца, которые отдаёт проекция
        # компании. Сырой ответ ФНС и персональные данные в него не включаются.
        cursor = db.execute("""SELECT p.id, p.merchant_name, p.merchant_inn, p.merchant_address
            FROM pool_receipts p JOIN unified_receipt_origins o
            ON o.origin_kind = 'pool' AND o.origin_id = p.id""")
        while batch := cursor.fetchmany(500):
            db.executemany("UPDATE unified_receipt_origins SET fiscal_source_hash=? "
                           "WHERE origin_kind='pool' AND origin_id=?",
                           [(_item_digest(row[1:]), row[0]) for row in batch])
        # Позиции не склеиваются по порядку: разные источники могут содержать
        # различный состав, детали или недостоверные суммы. Сохраняем все связи.
        for kind, table in (('registry', 'receipt_items'), ('pool', 'pool_items')):
            cursor = db.execute(f"""SELECT i.* FROM {table} i JOIN unified_receipt_origins o
                ON o.origin_kind = ? AND o.origin_id = i.receipt_id""", (kind,))
            names = [column[0] for column in cursor.description]
            id_index, receipt_index = names.index('id'), names.index('receipt_id')
            while batch := cursor.fetchmany(500):
                db.executemany('INSERT INTO unified_receipt_item_origins '
                               '(origin_kind, origin_id, item_id, content_hash) VALUES (?, ?, ?, ?)',
                               [(kind, row[receipt_index], row[id_index], _item_digest(row)) for row in batch])
        # Фискальные данные могут совпадать, а назначение пула — нет.
        # Только помечаем конфликт, никогда не меняем владельцев автоматически.
        db.execute("""UPDATE unified_fiscal_documents SET review_mask = review_mask | 8
            WHERE EXISTS (
                SELECT 1 FROM unified_receipt_origins a
                JOIN receipts r ON a.origin_kind = 'registry' AND r.id = a.origin_id
                JOIN unified_receipt_origins b ON b.document_id = a.document_id AND b.origin_kind = 'pool'
                JOIN pool_receipts p ON p.id = b.origin_id
                WHERE a.document_id = unified_fiscal_documents.id
                  AND r.company_id IS NOT NULL AND p.assigned_company_id IS NOT NULL
                  AND r.company_id != p.assigned_company_id
            )""")
        docs = db.execute('SELECT id FROM unified_fiscal_documents')
        while batch := docs.fetchmany(500):
            for (doc_id,) in batch:
                ownership = _legacy_ownership(db, doc_id)
                if ownership:
                    db.execute('INSERT INTO unified_company_claims '
                               '(document_id, company_id, pool_origin_id, registry_receipt_id, assigned_at) '
                               'VALUES (?, ?, ?, ?, ?)', (doc_id, *ownership))
                    company_id, pool_id, receipt_id, _ = ownership
                    if receipt_id:
                        # Приватные поля старого реестра остаются в исходной записи;
                        # здесь только ссылка, без копирования и без выдуманной ФНС.
                        db.execute('INSERT INTO unified_company_accounts '
                                   '(document_id, company_id, legacy_receipt_id, pool_origin_id, status, fns_status) '
                                   "VALUES (?, ?, ?, ?, 'pending', 'unknown')",
                                   (doc_id, company_id, receipt_id, pool_id))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _validate(db: sqlite3.Connection) -> None:
    """Проверить связи и целостность до передачи новой копии оператору."""
    if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise RuntimeError('Нарушена целостность копии')
    for table in ('unified_receipt_origins', 'unified_receipt_item_origins', 'unified_company_claims',
                  'unified_company_accounts', 'unified_company_account_events'):
        if db.execute(f'PRAGMA foreign_key_check({table})').fetchone():
            raise RuntimeError('Обнаружена повреждённая ссылка на единый чек')
    for kind, table in (('registry', 'receipts'), ('pool', 'pool_receipts')):
        # Вид таблицы выбирается только из констант выше, не из пользовательского ввода.
        bad = db.execute(f"""
            SELECT count(*) FROM unified_receipt_origins o
            JOIN unified_fiscal_documents d ON d.id = o.document_id
            LEFT JOIN {table} r ON r.id = o.origin_id
            WHERE o.origin_kind = ? AND (r.id IS NULL OR r.fn != d.fn OR r.fd != d.fd OR r.fp != d.fp
                  OR r.fn IS NULL OR r.fd IS NULL OR r.fp IS NULL)
        """, (kind,)).fetchone()[0]
        missing = db.execute(f"""
            SELECT count(*) FROM {table} r LEFT JOIN unified_receipt_origins o
            ON o.origin_kind = ? AND o.origin_id = r.id
            WHERE r.fn IS NOT NULL AND trim(r.fn) != '' AND r.fd IS NOT NULL AND trim(r.fd) != ''
              AND r.fp IS NOT NULL AND trim(r.fp) != '' AND o.origin_id IS NULL
        """, (kind,)).fetchone()[0]
        if bad or missing:
            raise RuntimeError('Связи единого чека не соответствуют исходным записям')
    cursor = db.execute("""SELECT p.merchant_name, p.merchant_inn, p.merchant_address,
            o.fiscal_source_hash FROM unified_receipt_origins o
        JOIN pool_receipts p ON o.origin_id = p.id WHERE o.origin_kind = 'pool'""")
    while batch := cursor.fetchmany(500):
        if any(not row[3] or row[3] != _item_digest(row[:3]) for row in batch):
            raise RuntimeError('Реквизиты продавца изменились после миграции')
    for kind, table in (('registry', 'receipt_items'), ('pool', 'pool_items')):
        bad = db.execute(f"""
            SELECT count(*) FROM unified_receipt_item_origins o
            LEFT JOIN {table} i ON i.id = o.item_id
            WHERE o.origin_kind = ? AND (i.id IS NULL OR i.receipt_id IS NULL OR i.receipt_id != o.origin_id)
        """, (kind,)).fetchone()[0]
        missing = db.execute(f"""
            SELECT count(*) FROM {table} i JOIN unified_receipt_origins r
            ON r.origin_kind = ? AND r.origin_id = i.receipt_id
            LEFT JOIN unified_receipt_item_origins o ON o.origin_kind = ? AND o.item_id = i.id
            WHERE o.item_id IS NULL
        """, (kind, kind)).fetchone()[0]
        if bad or missing:
            raise RuntimeError('Связи позиций не соответствуют исходным записям')
        cursor = db.execute(f"""SELECT i.* FROM {table} i JOIN unified_receipt_item_origins o
            ON o.origin_kind = ? AND o.item_id = i.id""", (kind,))
        names = [column[0] for column in cursor.description]
        id_index = names.index('id')
        while batch := cursor.fetchmany(500):
            for row in batch:
                saved = db.execute('SELECT content_hash FROM unified_receipt_item_origins '
                                   'WHERE origin_kind=? AND item_id=?', (kind, row[id_index])).fetchone()
                if not saved or saved[0] != _item_digest(row):
                    raise RuntimeError('Содержимое позиции изменилось после миграции')

    # Проведение сверки независимо от вычисления при построении таблиц:
    # ошибочная сумма или снятая пометка конфликта не проходят проверку.
    docs = db.execute('SELECT id, receipt_date, total_sum, operation, review_mask '
                      'FROM unified_fiscal_documents')
    while batch := docs.fetchmany(500):
        for doc_id, saved_date, saved_amount, saved_operation, saved_mask in batch:
            origins = db.execute("""
                SELECT r.receipt_date, r.total_sum, r.operation, r.company_id, NULL
                FROM unified_receipt_origins o JOIN receipts r ON r.id = o.origin_id
                WHERE o.origin_kind = 'registry' AND o.document_id = ?
                UNION ALL
                SELECT p.receipt_date, p.total_sum, p.operation, NULL, p.assigned_company_id
                FROM unified_receipt_origins o JOIN pool_receipts p ON p.id = o.origin_id
                WHERE o.origin_kind = 'pool' AND o.document_id = ?
            """, (doc_id, doc_id)).fetchall()
            if not origins:
                raise RuntimeError('Единый чек не связан с исходной записью')
            normalized = [_normalized(row[0], row[1], row[2]) for row in origins]
            mask = 0
            expected = []
            for index, bit in enumerate((1, 2, 4)):
                values = [row[index] for row in normalized]
                if any(value is None or value != values[0] for value in values):
                    mask |= bit
                    expected.append(None)
                else:
                    expected.append(values[0])
            registries = {row[3] for row in origins if row[3] is not None}
            pools = {row[4] for row in origins if row[4] is not None}
            if any(a != b for a in registries for b in pools):
                mask |= 8
            if (saved_date, saved_amount, saved_operation) != tuple(expected) or saved_mask != mask:
                raise RuntimeError('Общие реквизиты или признаки конфликта не соответствуют источникам')
            claim = db.execute('SELECT company_id, pool_origin_id, registry_receipt_id, assigned_at '
                               'FROM unified_company_claims WHERE document_id=?', (doc_id,)).fetchone()
            if claim != _legacy_ownership(db, doc_id):
                raise RuntimeError('Владелец единого чека не соответствует исходным назначениям')
            account = db.execute('SELECT company_id, legacy_receipt_id, pool_origin_id '
                                 'FROM unified_company_accounts WHERE document_id=?', (doc_id,)).fetchone()
            if claim and claim[2] and not account:
                raise RuntimeError('Не перенесена связь с учётной записью компании')
            if account and (not claim or account != (claim[0], claim[2], claim[1])):
                raise RuntimeError('Учётная запись не соответствует владельцу и источнику')


def _summary(db: sqlite3.Connection) -> dict:
    scalar = lambda sql: db.execute(sql).fetchone()[0]
    return {
        'documents': scalar('SELECT count(*) FROM unified_fiscal_documents'),
        'company_claims': scalar('SELECT count(*) FROM unified_company_claims'),
        'company_accounts': scalar('SELECT count(*) FROM unified_company_accounts'),
        'unresolved_ownership': scalar("""SELECT count(DISTINCT d.id) FROM unified_fiscal_documents d
            JOIN unified_receipt_origins o ON o.document_id = d.id
            LEFT JOIN receipts r ON o.origin_kind = 'registry' AND r.id = o.origin_id
            LEFT JOIN pool_receipts p ON o.origin_kind = 'pool' AND p.id = o.origin_id
            LEFT JOIN unified_company_claims c ON c.document_id = d.id
            WHERE c.document_id IS NULL AND (length(trim(coalesce(r.company_id, ''))) > 0
                OR length(trim(coalesce(p.assigned_company_id, ''))) > 0)"""),
        'registry_links': scalar("SELECT count(*) FROM unified_receipt_origins WHERE origin_kind='registry'"),
        'pool_links': scalar("SELECT count(*) FROM unified_receipt_origins WHERE origin_kind='pool'"),
        'needs_review': scalar('SELECT count(*) FROM unified_fiscal_documents WHERE review_mask != 0'),
        'registry_without_identity': scalar('SELECT count(*) FROM receipts') - scalar(
            "SELECT count(*) FROM unified_receipt_origins WHERE origin_kind='registry'"),
        'pool_without_identity': scalar('SELECT count(*) FROM pool_receipts') - scalar(
            "SELECT count(*) FROM unified_receipt_origins WHERE origin_kind='pool'"),
        'registry_items_linked': scalar("SELECT count(*) FROM unified_receipt_item_origins WHERE origin_kind='registry'"),
        'pool_items_linked': scalar("SELECT count(*) FROM unified_receipt_item_origins WHERE origin_kind='pool'"),
        'registry_items_unlinked': scalar('SELECT count(*) FROM receipt_items') - scalar(
            "SELECT count(*) FROM unified_receipt_item_origins WHERE origin_kind='registry'"),
        'pool_items_unlinked': scalar('SELECT count(*) FROM pool_items') - scalar(
            "SELECT count(*) FROM unified_receipt_item_origins WHERE origin_kind='pool'"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='существующая база SQLite (только чтение)')
    parser.add_argument('--output', required=True, help='новый файл копии; не должен существовать')
    args = parser.parse_args()
    print(json.dumps(migrate_copy(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
