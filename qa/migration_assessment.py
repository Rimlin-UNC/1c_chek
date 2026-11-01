"""Агрегированная диагностика источника для миграции, без изменения БД.

SQLite: python -m qa.migration_assessment --sqlite /path/to/source.db
PostgreSQL: задать YM_ASSESS_DATABASE_URL в окружении и использовать --postgres.
Не выводит строки чеков и не переносит данные. Используйте копию базы.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, text

# Ровно набор пробельных символов Python str.strip(), используемый миграцией.
WHITESPACE = ''.join(map(chr, (9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760,
                               *range(8192, 8203), 8232, 8233, 8239, 8287, 12288)))
_REQUIRED = {
    'receipts': {'id', 'fn', 'fd', 'fp', 'receipt_date', 'total_sum', 'operation', 'company_id'},
    'pool_receipts': {'id', 'fn', 'fd', 'fp', 'receipt_date', 'total_sum', 'operation', 'assigned_company_id', 'pool_user_id'},
    'receipt_items': {'id', 'receipt_id'},
    'pool_items': {'id', 'receipt_id'},
    'companies': {'id'},
    'pool_users': {'id', 'points'},
    'pool_points': {'id', 'user_id', 'delta'},
    'pool_consents': {'id', 'user_id'},
}


def _complete(alias: str) -> str:
    return ' AND '.join(f"{alias}.{col} IS NOT NULL AND trim({alias}.{col}, :ws) <> ''"
                        for col in ('fn', 'fd', 'fp'))


def assess_connection(conn) -> dict:
    """Проверить одну транзакцию, которую вызывающий открыл только для чтения."""
    from sqlalchemy import inspect

    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    for table, columns in _REQUIRED.items():
        if table not in tables:
            raise ValueError(f'Отсутствует необходимая таблица: {table}')
        actual = {col['name'] for col in inspector.get_columns(table)}
        if not columns <= actual:
            raise ValueError(f'В таблице {table} отсутствуют необходимые поля')

    def count(query: str) -> int:
        return int(conn.execute(text(query), {'ws': WHITESPACE}).scalar_one())

    # При активной RLS часть строк может быть скрыта даже от SELECT: такой
    # результат нельзя использовать как полную оценку миграции.
    if conn.dialect.name == 'postgresql':
        for table in _REQUIRED:
            if conn.execute(text('SELECT row_security_active(to_regclass(:table_name))'),
                            {'table_name': table}).scalar_one():
                raise ValueError('RLS скрывает данные от текущей роли')

    result = {'source_counts': {}, 'incomplete_identities': {}, 'duplicate_identity_groups': {},
              'orphan_items': {}, 'ownership': {}, 'overlap': {}, 'participants': {}}
    for kind, table in (('registry', 'receipts'), ('pool', 'pool_receipts')):
        complete = _complete('r')
        result['source_counts'][kind] = count(f'SELECT count(*) FROM {table} r')
        result['incomplete_identities'][kind] = count(f'SELECT count(*) FROM {table} r WHERE NOT ({complete})')
        result['duplicate_identity_groups'][kind] = count(
            f'SELECT count(*) FROM (SELECT r.fn, r.fd, r.fp FROM {table} r WHERE {complete} '
            'GROUP BY r.fn, r.fd, r.fp HAVING count(*) > 1) groups_found')
    for kind, item, parent in (('registry', 'receipt_items', 'receipts'),
                               ('pool', 'pool_items', 'pool_receipts')):
        result['orphan_items'][kind] = count(
            f'SELECT count(*) FROM {item} i LEFT JOIN {parent} r ON r.id=i.receipt_id '
            'WHERE i.receipt_id IS NULL OR r.id IS NULL')

    for kind, table, field in (('registry', 'receipts', 'company_id'),
                               ('pool', 'pool_receipts', 'assigned_company_id')):
        nonempty = f"r.{field} IS NOT NULL AND trim(r.{field}, :ws) <> ''"
        result['ownership'][kind + '_without_company'] = count(
            f'SELECT count(*) FROM {table} r WHERE NOT ({nonempty})')
        result['ownership'][kind + '_unknown_company'] = count(
            f'SELECT count(*) FROM {table} r LEFT JOIN companies c ON c.id=r.{field} '
            f'WHERE {nonempty} AND c.id IS NULL')

    for key, table in (('users', 'pool_users'), ('point_events', 'pool_points'),
                       ('consents', 'pool_consents')):
        result['participants'][key] = count(f'SELECT count(*) FROM {table}')
    for key, table, user_field in (('receipts_unknown_user', 'pool_receipts', 'pool_user_id'),
                                   ('points_unknown_user', 'pool_points', 'user_id'),
                                   ('consents_unknown_user', 'pool_consents', 'user_id')):
        result['participants'][key] = count(
            f'SELECT count(*) FROM {table} t LEFT JOIN pool_users u ON u.id=t.{user_field} '
            f'WHERE t.{user_field} IS NOT NULL AND u.id IS NULL')
    result['participants']['points_missing_user'] = count(
        'SELECT count(*) FROM pool_points WHERE user_id IS NULL')
    result['participants']['balance_mismatch_users'] = count(
        'SELECT count(*) FROM pool_users u LEFT JOIN '
        '(SELECT user_id, sum(delta) AS total FROM pool_points GROUP BY user_id) p ON p.user_id=u.id '
        'WHERE coalesce(u.points, 0) <> coalesce(p.total, 0)')

    join = (' FROM receipts r JOIN pool_receipts p ON r.fn=p.fn AND r.fd=p.fd AND r.fp=p.fp '
            f'WHERE {_complete("r")} AND {_complete("p")}')
    result['overlap']['matched_pairs'] = count('SELECT count(*)' + join)
    for name, field in (('different_datetime', 'receipt_date'), ('different_amount', 'total_sum'),
                        ('different_operation', 'operation')):
        result['overlap'][name] = count('SELECT count(*)' + join +
            f' AND r.{field} IS NOT NULL AND p.{field} IS NOT NULL AND r.{field} <> p.{field}')
    result['overlap']['missing_fiscal_data'] = count('SELECT count(*)' + join +
        ' AND (r.receipt_date IS NULL OR p.receipt_date IS NULL OR r.total_sum IS NULL '
        'OR p.total_sum IS NULL OR r.operation IS NULL OR p.operation IS NULL)')
    result['overlap']['different_companies'] = count('SELECT count(*)' + join +
        " AND r.company_id IS NOT NULL AND trim(r.company_id, :ws) <> ''"
        " AND p.assigned_company_id IS NOT NULL AND trim(p.assigned_company_id, :ws) <> ''"
        ' AND r.company_id <> p.assigned_company_id')
    return result


def assess_sqlite(path: str | Path) -> dict:
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Источник должен быть файлом SQLite')
    uri = 'file:' + quote(str(path), safe='/') + '?mode=ro'
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(uri, uri=True))
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql('PRAGMA query_only=ON')
            conn.exec_driver_sql('BEGIN')
            return assess_connection(conn)
    finally:
        engine.dispose()


def assess_postgres(url: str) -> dict:
    if not url:
        raise ValueError('Переменная YM_ASSESS_DATABASE_URL не задана')
    # Не подключаемся по URL другой СУБД и не выводим адрес и пароль в отчёт.
    from sqlalchemy.engine import make_url
    if make_url(url).get_backend_name() != 'postgresql':
        raise ValueError('Требуется URL PostgreSQL')
    engine = create_engine(url, isolation_level='REPEATABLE READ')
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql('SET TRANSACTION READ ONLY')
            return assess_connection(conn)
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--sqlite', metavar='PATH')
    group.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    try:
        result = assess_sqlite(args.sqlite) if args.sqlite else assess_postgres(os.getenv('YM_ASSESS_DATABASE_URL', ''))
    except (Exception, KeyboardInterrupt):  # не выводить SQL, параметры, URL или другие частные данные
        print('Не удалось выполнить диагностику; проверьте доступ и совместимость схемы.', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
