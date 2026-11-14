"""Отдельная необязательная история локальных метрик, без записи в основную БД.

Приложение только читает; отдельная команда администратора инициализирует новую
базу и записывает один снимок. Никакие службы автоматически не создаются.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

from ..config import _env
from . import local_monitor
from .server_traffic import _INTERFACE

_APP_ID = 0x594D4831
_RETENTION = 30 * 86400
_MAX_ROWS = 50000
_MAX_EXTRA_ROWS = 50000
_MAX_GAP = 600
_RANGES = {'1h': (3600, 60), '24h': (86400, 600), '7d': (7 * 86400, 3600), '30d': (_RETENTION, 21600)}


def _configured_path() -> Path | None:
    value = _env('YM_MONITOR_HISTORY_FILE', '').strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('путь истории должен быть абсолютным')
    return path


def _validate_directory(path: Path) -> None:
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('каталог истории должен принадлежать пользователю службы и иметь права 0700')


def _validate_file(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError('путь истории должен быть абсолютным')
    _validate_directory(path)
    info = path.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('небезопасные права файла истории')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('история должна быть отдельным обычным файлом без ссылок')


def _connection(path: Path, *, write: bool) -> sqlite3.Connection:
    _validate_file(path)
    mode = 'rw' if write else 'ro'
    con = sqlite3.connect(f'{path.as_uri()}?mode={mode}', uri=True, timeout=1)
    try:
        con.execute('PRAGMA trusted_schema=OFF')
        if not write:
            con.execute('PRAGMA query_only=ON')
        if con.execute('PRAGMA application_id').fetchone()[0] != _APP_ID:
            raise ValueError('это не отдельная база истории мониторинга')
        if con.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('неподдерживаемая версия истории')
        return con
    except Exception:
        con.close()
        raise


_EXTRA_CREATE = """CREATE TABLE extra_samples (
        timestamp INTEGER PRIMARY KEY, load_1 REAL, io_device TEXT, read_iops REAL, write_iops REAL,
        read_await_ms REAL, write_await_ms REAL, io_busy_pct REAL)"""
_EXTRA_COLUMNS = [('timestamp', 'INTEGER'), ('load_1', 'REAL'), ('io_device', 'TEXT'),
                  ('read_iops', 'REAL'), ('write_iops', 'REAL'), ('read_await_ms', 'REAL'),
                  ('write_await_ms', 'REAL'), ('io_busy_pct', 'REAL')]


def _extra_state(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT type, sql FROM sqlite_master WHERE name='extra_samples'").fetchone()
    if not row:
        return 'not_enabled'
    if row[0] != 'table' or row[1] != _EXTRA_CREATE:
        return 'unavailable'
    if con.execute("SELECT 1 FROM sqlite_master WHERE tbl_name='extra_samples' AND type IN ('trigger', 'index') LIMIT 1").fetchone():
        return 'unavailable'
    columns = con.execute('PRAGMA table_info(extra_samples)').fetchall()
    if [(row[1], row[2].upper()) for row in columns] != _EXTRA_COLUMNS or not columns[0][5]:
        return 'unavailable'
    return 'available'


def _create_extra(con: sqlite3.Connection) -> None:
    con.execute(_EXTRA_CREATE)


def enable_extra(path: Path) -> None:
    """Явное добавление таблицы в существующую историю; старую схему не меняем."""
    with closing(_connection(path, write=True)) as con, con:
        con.execute('BEGIN IMMEDIATE')
        state = _extra_state(con)
        if state == 'unavailable':
            raise ValueError('таблица дополнительных показателей имеет неизвестный формат')
        if state == 'not_enabled':
            _create_extra(con)


def initialize(path: Path) -> None:
    """Явно создать только новый файл, никогда не перезаписывать существующий."""
    if not path.is_absolute():
        raise ValueError('путь истории должен быть абсолютным')
    _validate_directory(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    os.close(fd)
    # При ошибке пустой файл остаётся для ручного расследования, не удаляем его.
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, timeout=1)) as con, con:
        con.execute(f'PRAGMA application_id={_APP_ID}')
        con.execute('PRAGMA user_version=1')
        con.execute('''CREATE TABLE samples (
            timestamp INTEGER PRIMARY KEY, cpu_pct REAL, memory_pct REAL, swap_pct REAL,
            interface TEXT, rx_bytes_per_sec REAL, tx_bytes_per_sec REAL)''')
        con.execute('CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), retention_paused INTEGER NOT NULL, resume_at INTEGER)')
        con.execute('INSERT INTO state VALUES (1, 0, NULL)')
        _create_extra(con)


def _number(value, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def _extra_values(timestamp: int, data: dict) -> tuple:
    loads = data.get('load_average')
    load = _number(loads[0], 2**53 - 1) if isinstance(loads, list) and loads else None
    io = data.get('disk_io')
    if not isinstance(io, dict):
        io = {}
    device = io.get('device')
    if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9_.!-]{1,64}', device):
        device = None
        io = {}
    return (timestamp, load, device, _number(io.get('read_iops'), 2**53 - 1),
            _number(io.get('write_iops'), 2**53 - 1), _number(io.get('read_await_ms'), 2**53 - 1),
            _number(io.get('write_await_ms'), 2**53 - 1), _number(io.get('busy_pct'), 100))


def record(path: Path, data: dict, interface: str, *, now: int | None = None) -> None:
    """Одна запись на минуту, только в заранее созданную базу с маркером."""
    timestamp = int(time.time() if now is None else now)
    if timestamp < 0:
        raise ValueError('неверное время')
    timestamp -= timestamp % 60
    if not _INTERFACE.fullmatch(interface or '') or interface == 'lo':
        interface = ''
    network = data.get('network') or {}
    net = (network.get(interface) or {}) if interface else {}
    values = (timestamp, _number(data.get('cpu_pct'), 100),
              _number((data.get('memory') or {}).get('pct'), 100),
              _number((data.get('swap') or {}).get('pct'), 100), interface,
              _number(net.get('rx_bytes_per_sec'), 2**53 - 1),
              _number(net.get('tx_bytes_per_sec'), 2**53 - 1))
    full = False
    with closing(_connection(path, write=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        latest = con.execute('SELECT max(timestamp) FROM samples').fetchone()[0]
        paused_value, resume_at = con.execute('SELECT retention_paused, resume_at FROM state WHERE id=1').fetchone()
        paused = bool(paused_value)
        if latest is not None and timestamp < latest:
            raise ValueError('время сервера раньше последнего сохранённого снимка')
        resumed_recently = resume_at is not None and 0 <= timestamp - resume_at <= _MAX_GAP
        if latest is not None and timestamp - latest > _MAX_GAP and not resumed_recently:
            paused = True
            con.execute('UPDATE state SET retention_paused=1 WHERE id=1')
        count = con.execute('SELECT count(*) FROM samples').fetchone()[0]
        if paused and count >= _MAX_ROWS and timestamp != latest:
            full = True
        else:
            inserted = con.execute('INSERT OR IGNORE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)', values).rowcount
            extra_state = _extra_state(con)
            if inserted and extra_state == 'available':
                extra_count = con.execute('SELECT count(*) FROM extra_samples').fetchone()[0]
                if extra_count < _MAX_EXTRA_ROWS:
                    con.execute('INSERT INTO extra_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)', _extra_values(timestamp, data))
            con.execute('UPDATE state SET resume_at=NULL WHERE id=1')
            if not paused:
                # Только отдельная база; после заметного перерыва автоочистка приостанавливается.
                con.execute('DELETE FROM samples WHERE timestamp < ?', (timestamp - _RETENTION,))
                if extra_state == 'available':
                    con.execute('DELETE FROM extra_samples WHERE timestamp < ?', (timestamp - _RETENTION,))
        con.commit()
    if full:
        raise ValueError('достигнут предел истории; очистка приостановлена после перерыва')


def resume_retention(path: Path, *, now: int | None = None) -> None:
    """Явное решение администратора после проверки времени и сохранения истории."""
    timestamp = int(time.time() if now is None else now)
    with closing(_connection(path, write=True)) as con:
        con.execute('BEGIN IMMEDIATE')
        latest = con.execute('SELECT max(timestamp) FROM samples').fetchone()[0]
        if latest is not None and latest > timestamp:
            raise ValueError('время сервера раньше последнего сохранённого снимка')
        con.execute('UPDATE state SET retention_paused=0, resume_at=? WHERE id=1', (timestamp - timestamp % 60,))
        con.commit()


def read(path: Path, period: str, *, interface: str, now: int | None = None) -> dict:
    if period not in _RANGES:
        return {'status': 'invalid_range'}
    length, width = _RANGES[period]
    timestamp = int(time.time() if now is None else now)
    timestamp -= timestamp % 60
    start = timestamp - length + 60
    with closing(_connection(path, write=False)) as con:
        rows = con.execute('''SELECT CAST(timestamp / ? AS INTEGER) * ? AS bucket,
            count(*), avg(cpu_pct), avg(memory_pct), avg(swap_pct),
            avg(CASE WHEN interface = ? THEN rx_bytes_per_sec END),
            avg(CASE WHEN interface = ? THEN tx_bytes_per_sec END),
            count(cpu_pct), count(memory_pct), count(swap_pct),
            count(CASE WHEN interface = ? THEN rx_bytes_per_sec END),
            count(CASE WHEN interface = ? THEN tx_bytes_per_sec END)
            FROM samples WHERE timestamp >= ? AND timestamp <= ? GROUP BY bucket ORDER BY bucket''',
            (width, width, interface, interface, interface, interface, start, timestamp)).fetchall()
        extra_state = _extra_state(con)
        extra_rows = []
        extra_full = False
        if extra_state == 'available':
            extra_full = con.execute('SELECT count(*) FROM extra_samples').fetchone()[0] >= _MAX_EXTRA_ROWS
            extra_rows = con.execute("""SELECT CAST(e.timestamp / ? AS INTEGER) * ? AS bucket,
                avg(e.load_1), count(e.load_1), count(e.io_device), count(DISTINCT e.io_device),
                min(e.io_device), avg(e.read_iops), count(e.read_iops), avg(e.write_iops), count(e.write_iops),
                avg(e.read_await_ms), count(e.read_await_ms), avg(e.write_await_ms), count(e.write_await_ms),
                avg(e.io_busy_pct), count(e.io_busy_pct)
                FROM extra_samples e JOIN samples s ON e.timestamp=s.timestamp
                WHERE e.timestamp >= ? AND e.timestamp <= ? GROUP BY bucket ORDER BY bucket""",
                (width, width, start, timestamp)).fetchall()
        last = con.execute('SELECT max(timestamp) FROM samples').fetchone()[0]
        paused = bool(con.execute('SELECT retention_paused FROM state WHERE id=1').fetchone()[0])
    by_time = {row[0]: row for row in rows}
    extras_by_time = {row[0]: row for row in extra_rows}
    first_bucket = start // width * width
    last_bucket = timestamp // width * width
    inferred_pause = last is not None and timestamp - last > _MAX_GAP
    clock_backwards = last is not None and last > timestamp + 60
    buckets = []
    for bucket in range(first_bucket, last_bucket + 1, width):
        row = by_time.get(bucket)
        expected = (min(bucket + width, timestamp + 60) - max(bucket, start)) // 60
        buckets.append({'timestamp': bucket, 'expected': expected, 'samples': row[1] if row else 0,
                        'cpu_pct': row[2] if row else None, 'memory_pct': row[3] if row else None,
                        'swap_pct': row[4] if row else None, 'rx_bytes_per_sec': row[5] if row else None,
                        'tx_bytes_per_sec': row[6] if row else None,
                        'cpu_samples': row[7] if row else 0, 'memory_samples': row[8] if row else 0,
                        'swap_samples': row[9] if row else 0, 'rx_samples': row[10] if row else 0,
                        'tx_samples': row[11] if row else 0})
        extra = extras_by_time.get(bucket)
        mixed = bool(extra and extra[4] > 1)
        valid_io = extra is not None and extra[4] == 1
        buckets[-1].update({'load_1': extra[1] if extra else None, 'load_samples': extra[2] if extra else 0,
                            'io_device': extra[5] if valid_io else None, 'io_mixed': mixed,
                            'io_samples': extra[3] if valid_io else 0,
                            'read_iops': extra[6] if valid_io else None, 'read_iops_samples': extra[7] if valid_io else 0,
                            'write_iops': extra[8] if valid_io else None, 'write_iops_samples': extra[9] if valid_io else 0,
                            'read_await_ms': extra[10] if valid_io else None, 'read_await_samples': extra[11] if valid_io else 0,
                            'write_await_ms': extra[12] if valid_io else None, 'write_await_samples': extra[13] if valid_io else 0,
                            'io_busy_pct': extra[14] if valid_io else None, 'io_busy_samples': extra[15] if valid_io else 0})
    return {'status': 'available', 'period': period, 'bucket_seconds': width, 'interface': interface or None,
            'last_sample_at': last, 'current_timestamp': timestamp, 'retention_paused': paused,
            'collection_gap': inferred_pause, 'clock_backwards': clock_backwards,
            'extra_status': extra_state, 'extra_full': extra_full, 'buckets': buckets}


def snapshot(period: str) -> dict:
    if period not in _RANGES:
        return {'status': 'invalid_range'}
    try:
        path = _configured_path()
        if path is None:
            return {'status': 'not_configured'}
        interface = _env('YM_MONITOR_TRAFFIC_INTERFACE', '').strip()
        if not _INTERFACE.fullmatch(interface) or interface == 'lo':
            interface = ''
        return read(path, period, interface=interface)
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return {'status': 'unavailable'}


def main() -> int:
    parser = argparse.ArgumentParser(description='Выделенная история локальных метрик; запускается только администратором')
    parser.add_argument('action', choices=('init', 'collect', 'resume', 'enable-extra'))
    args = parser.parse_args()
    try:
        path = _configured_path()
        if path is None:
            raise ValueError('YM_MONITOR_HISTORY_FILE не настроен')
        if args.action == 'init':
            initialize(path)
        elif args.action == 'resume':
            resume_retention(path)
        elif args.action == 'enable-extra':
            enable_extra(path)
        else:
            interface = _env('YM_MONITOR_TRAFFIC_INTERFACE', '').strip()
            record(path, local_monitor.snapshot(), interface)
        return 0
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
        print(f'История мониторинга: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
