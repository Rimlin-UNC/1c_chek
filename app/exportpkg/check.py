"""Проверка месячного пакета без записи в базу и распаковки на диск.

Контрольные суммы в манифесте не подписаны и не подтверждают автора, полноту
данных или успешное восстановление; это не проверка резервной копии.
"""
from __future__ import annotations

import hashlib
import io
import json
import lzma
import re
import zipfile
import zlib

from .service import FORMAT, FORMAT_VERSION

MAX_UPLOAD = 20 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_FILES = 10000
DATASETS = ('receipts', 'receipt_items', 'primary_documents', 'primary_lines', 'bank_operations', 'reimbursements')
REQUIRED = {'FORMAT.md'} | {f'{name}.{ext}' for name in DATASETS for ext in ('json', 'csv')}


class CheckError(ValueError):
    """Пакет повреждён или не соответствует ограничениям проверки."""


def _bad(message: str):
    raise CheckError(message)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _bad('В JSON повторяется имя поля')
        result[key] = value
    return result


def _json(data: bytes):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_object_pairs,
                          parse_constant=lambda _: _bad('Недопустимое число в JSON'))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise CheckError('Некорректный JSON в архиве') from exc


def _relationships(contents: dict, names: list[str], skipped: list) -> None:
    """Проверить ссылки внутри пакета, не сравнивая его с исходной базой."""
    def identities(rows: list) -> set[str]:
        ids = [row.get('id') for row in rows]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            _bad('Отсутствуют или повторяются идентификаторы записей')
        return set(ids)

    receipts = identities(contents['receipts.json'])
    documents = identities(contents['primary_documents.json'])
    for filename, field, parents in (('receipt_items.json', 'receipt_id', receipts),
                                      ('primary_lines.json', 'document_id', documents)):
        for row in contents[filename]:
            value = row.get(field)
            if not isinstance(value, str) or value not in parents:
                _bad('Строка ссылается на отсутствующую запись')

    skipped_ids = []
    for row in skipped:
        if not isinstance(row, dict) or not isinstance(row.get('document_id'), str):
            _bad('Некорректный список пропущенных УПД')
        skipped_ids.append(row['document_id'])
    if len(set(skipped_ids)) != len(skipped_ids) or not set(skipped_ids) <= documents:
        _bad('Пропущенный УПД ссылается на отсутствующий или повторный документ')
    skipped_set = set(skipped_ids)
    links = []
    for row in contents['primary_documents.json']:
        link = row.get('upd_file')
        if not isinstance(link, str):
            _bad('Некорректная ссылка на УПД')
        if bool(link) == (row['id'] in skipped_set):
            _bad('Документ должен иметь файл УПД или причину пропуска')
        if link:
            links.append(link)
    if len(set(links)) != len(links) or set(links) != {name for name in names if name.startswith('upd/')}:
        _bad('Ссылки на файлы УПД не совпадают с составом архива')


def inspect_package(data: bytes) -> dict:
    """Проверить ZIP, манифест и структуру основных JSON; данные наружу не возвращать."""
    if len(data) > MAX_UPLOAD:
        _bad('Архив больше 20 МБ')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_FILES:
                _bad('Недопустимое число файлов в архиве')
            names = [entry.filename for entry in entries]
            if len(set(names)) != len(names):
                _bad('Повторяющиеся имена файлов в архиве')
            if any(entry.is_dir() or entry.flag_bits & 1 for entry in entries):
                _bad('Каталоги и зашифрованные файлы не поддерживаются')
            if sum(entry.file_size for entry in entries) > MAX_UNPACKED:
                _bad('Архив слишком велик после распаковки')
            allowed = lambda name: name == 'manifest.json' or name in REQUIRED or (  # noqa: E731
                re.fullmatch(r'upd/[A-Za-z0-9_.-]+\.xml', name) is not None and '..' not in name)
            if any(not allowed(name) for name in names):
                _bad('Неизвестное или небезопасное имя файла')
            if not REQUIRED | {'manifest.json'} <= set(names):
                _bad('В архиве не хватает обязательных файлов')
            manifest = _json(archive.read('manifest.json'))
            if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('format_version') != FORMAT_VERSION:
                _bad('Неизвестный формат или версия пакета')
            listed = manifest.get('files')
            if not isinstance(listed, list) or any(not isinstance(row, dict) for row in listed):
                _bad('Некорректный список файлов')
            listed_names = [row.get('name') for row in listed]
            if any(not isinstance(n, str) for n in listed_names) or len(set(listed_names)) != len(listed_names):
                _bad('Повторяющиеся или некорректные записи манифеста')
            if set(listed_names) != set(names) - {'manifest.json'}:
                _bad('Состав архива отличается от манифеста')
            contents = {}
            for row in listed:
                name = row['name']
                content = archive.read(name)
                if type(row.get('bytes')) is not int or row['bytes'] != len(content):
                    _bad('Размер файла не соответствует манифесту')
                if row.get('sha256') != hashlib.sha256(content).hexdigest():
                    _bad('Контрольная сумма файла не совпадает')
                if name.endswith('.json'):
                    contents[name] = _json(content)
            counts = manifest.get('counts')
            if not isinstance(counts, dict):
                _bad('В манифесте нет счётчиков')
            for name in DATASETS:
                rows = contents[name + '.json']
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    _bad('Некорректный состав записей')
                if type(counts.get(name)) is not int or counts[name] != len(rows):
                    _bad('Количество записей отличается от манифеста')
            upd_count = sum(name.startswith('upd/') for name in names)
            if type(counts.get('upd_files')) is not int or counts['upd_files'] != upd_count:
                _bad('Количество файлов УПД отличается от манифеста')
            skipped = manifest.get('skipped_upd')
            if not isinstance(skipped, list):
                _bad('Некорректный список пропущенных УПД')
            _relationships(contents, names, skipped)
            return {
                'ok': True,
                'relationships_checked': True,
                'counts': {key: counts[key] for key in (*DATASETS, 'upd_files')},
                'skipped_upd': len(skipped),
                'restore_tested': False,
                'notice': 'Проверены состав, суммы файлов, числа записей и отдельные связи JSON. Содержимое CSV и УПД со строками JSON не сверялось. Пакет за месяц не является резервной копией; восстановление не проверено, авторство и полнота не подтверждены.',
            }
    except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, OSError, KeyError, zlib.error, lzma.LZMAError) as exc:
        raise CheckError('Повреждённый или неподдерживаемый ZIP-архив') from exc
