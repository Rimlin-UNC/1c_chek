"""Агрегированная диагностика SQLite перед экспериментальной миграцией копии.

Только чтение, без фискальных реквизитов, владельцев и идентификаторов строк в
результате. Это не миграция и не проверка всех бизнес-правил или PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


def inspect_source(source: str | Path) -> dict:
    """Прочитать один согласованный снимок, не меняя содержимое исходной БД."""
    path = Path(source).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Источник должен быть файлом SQLite')
    uri = 'file:' + quote(str(path), safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        check = db.execute('PRAGMA quick_check').fetchall()
        if check != [('ok',)]:
            # Не выводим строки SQLite: они могут содержать ключи и иные данные.
            raise RuntimeError('Проверка целостности исходной базы не пройдена')
        required = {'receipts': {'id', 'fn', 'fd', 'fp'},
                    'pool_receipts': {'id', 'fn', 'fd', 'fp'},
                    'receipt_items': {'id', 'receipt_id'},
                    'pool_items': {'id', 'receipt_id'}}
        for table, columns in required.items():
            actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not columns <= actual:
                raise RuntimeError(f'В исходной базе отсутствуют необходимые поля таблицы {table}')
        result = {'source_counts': {}, 'incomplete_identities': {}, 'orphan_items': {},
                  'foreign_key_violations': []}
        for kind, table in (('registry', 'receipts'), ('pool', 'pool_receipts')):
            result['source_counts'][kind] = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            result['incomplete_identities'][kind] = db.execute(
                f"SELECT count(*) FROM {table} WHERE fn IS NULL OR trim(fn)='' "
                "OR fd IS NULL OR trim(fd)='' OR fp IS NULL OR trim(fp)=''").fetchone()[0]
        for kind, items, receipts in (('registry', 'receipt_items', 'receipts'),
                                      ('pool', 'pool_items', 'pool_receipts')):
            result['orphan_items'][kind] = db.execute(
                f'SELECT count(*) FROM {items} i LEFT JOIN {receipts} r ON r.id=i.receipt_id '
                'WHERE i.receipt_id IS NULL OR r.id IS NULL').fetchone()[0]
        # PRAGMA возвращает rowid: агрегируем только имена таблиц и счётчик.
        counts = Counter((row[0], row[2]) for row in db.execute('PRAGMA foreign_key_check'))
        result['foreign_key_violations'] = [
            {'table': table, 'parent_table': parent, 'count': count}
            for (table, parent), count in sorted(counts.items())
        ]
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='существующая база SQLite (только чтение)')
    args = parser.parse_args()
    print(json.dumps(inspect_source(args.source), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
