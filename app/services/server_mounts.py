"""Ограниченный обзор монтирований Linux: только по требованию администратора.

statvfs может зависнуть даже на локальном устройстве. Дочерние потоки daemon
ограничены общим числом и не запускаются повторно для зависшего пути. Сетевые,
FUSE, pseudo и автоматические монтирования никогда намеренно не опрашиваются.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .local_monitor import disk_view, inode_view

MAX_MOUNTS = 64
MAX_INFLIGHT = 4
MAX_PROBES = 16
_MAX_INPUT = 256 * 1024
_MAX_LINES = 2048
_LOCAL_TYPES = frozenset({'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'vfat', 'exfat', 'f2fs'})
_ESCAPE = re.compile(r'\\(040|011|012|134)')
_lock = threading.Lock()
_condition = threading.Condition(_lock)
_snapshot_lock = threading.Lock()
_inflight: dict[str, dict] = {}
_cached = None
_cached_at = 0.0


def _path(value: str) -> str | None:
    decoded = _ESCAPE.sub(lambda m: {'040': ' ', '011': '\t', '012': '\n', '134': '\\'}[m.group(1)], value)
    if not decoded.startswith('/') or '\x00' in decoded or any(part in ('.', '..') for part in decoded.split('/')):
        return None
    # Неоднозначный неизвестный escape не должен менять реальный путь.
    if re.search(r'\\[0-7]{3}', value.replace('\\040', '').replace('\\011', '').replace('\\012', '').replace('\\134', '')):
        return None
    return decoded


def parse_mountinfo(text: str) -> tuple[list[dict], bool]:
    """Сначала проверяем весь список, затем ограничиваем выдачу и опрос."""
    if not text or len(text.encode('utf-8', errors='replace')) > _MAX_INPUT:
        return [], True
    lines = text.splitlines()
    unsafe = len(lines) > _MAX_LINES
    items = []
    for line in lines[:_MAX_LINES]:
        try:
            before, after = line.split(' - ', 1)
            fields, tail = before.split(), after.split()
            path = _path(fields[4])
            fs = tail[0]
            device = fields[2]
            if (path is None or len(fields) < 6 or len(tail) < 3 or not fields[0].isdigit()
                    or not re.fullmatch(r'\d+:\d+', device) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', fs)):
                raise ValueError('неверная строка монтирования')
            items.append({'path': path, 'type': fs, 'device': device})
        except (ValueError, IndexError):
            unsafe = True
    counts = {}
    for item in items:
        counts[item['path']] = counts.get(item['path'], 0) + 1
    blocked_parents = [item['path'] for item in items
                       if item['type'] not in _LOCAL_TYPES or counts[item['path']] > 1]
    for item in items:
        path = item['path']
        under_blocked = any(path == parent or path.startswith(parent.rstrip('/') + '/') for parent in blocked_parents)
        item['_probe'] = (not unsafe and item['type'] in _LOCAL_TYPES and not under_blocked)
    partial = unsafe or len(items) > MAX_MOUNTS
    return items[:MAX_MOUNTS], partial


def _run(path: str, entry: dict) -> None:
    try:
        info = os.statvfs(path)
        entry['disk'] = disk_view(info)
        entry['inodes'] = inode_view(info)
        entry['status'] = 'available' if entry['disk'] is not None else 'unavailable'
    except (OSError, ValueError, TypeError, OverflowError):
        entry['status'] = 'unavailable'
    finally:
        with _lock:
            if _inflight.get(path) is entry:
                del _inflight[path]
            entry['done'].set()
            _condition.notify_all()


def _start(path: str) -> dict | None | bool:
    with _lock:
        existing = _inflight.get(path)
        if existing is not None:
            return existing
        if len(_inflight) >= MAX_INFLIGHT:
            return None
        entry = {'done': threading.Event(), 'status': 'timeout', 'disk': None, 'inodes': None}
        _inflight[path] = entry
        try:
            thread = threading.Thread(target=_run, args=(path, entry), daemon=True, name='server-mount-probe')
            entry['thread'] = thread
            thread.start()
        except (RuntimeError, OSError):
            del _inflight[path]
            return False
        return entry


def probe(items: list[dict], *, deadline_seconds: float = 1.0) -> list[dict]:
    """Один общий срок; при зависании не создавать бесконечное число потоков."""
    deadline = time.monotonic() + max(0, deadline_seconds)
    jobs = []
    attempted = 0
    for item in items:
        entry = None
        if item.get('_probe') and attempted < MAX_PROBES:
            attempted += 1
            while time.monotonic() < deadline:
                candidate = _start(item['path'])
                if candidate is False:  # Не удалось создать поток.
                    break
                if candidate is not None:
                    entry = candidate
                    break
                with _condition:
                    if len(_inflight) >= MAX_INFLIGHT:
                        _condition.wait(max(0, deadline - time.monotonic()))
        jobs.append(entry)
    result = []
    for item, entry in zip(items, jobs, strict=True):
        if entry is not None:
            entry['done'].wait(max(0, deadline - time.monotonic()))
        complete = entry is not None and entry['done'].is_set()
        status = entry['status'] if complete else 'timeout' if entry else 'busy' if item.get('_probe') else 'not_probed'
        result.append({'path': item['path'], 'type': item['type'], 'device': item['device'], 'status': status,
                       'disk': entry['disk'] if complete else None, 'inodes': entry['inodes'] if complete else None})
    return result


def snapshot() -> dict:
    """В памяти одного процесса, без фонового сбора и раскрытия источника монтирования."""
    global _cached, _cached_at
    with _snapshot_lock:
        now = time.monotonic()
        if _cached is not None and 0 <= now - _cached_at < 10:
            return _cached
        try:
            with Path('/proc/self/mountinfo').open('r', encoding='utf-8', errors='replace') as stream:
                text = stream.read(_MAX_INPUT + 1)
            items, partial = parse_mountinfo(text)
            result = {'status': 'available' if items else 'unavailable', 'partial': partial,
                      'mounts': probe(items)}
        except (OSError, ValueError):
            result = {'status': 'unavailable', 'partial': True, 'mounts': []}
        result['collected_at'] = datetime.now(timezone.utc).isoformat()
        _cached, _cached_at = result, time.monotonic()
        return result
