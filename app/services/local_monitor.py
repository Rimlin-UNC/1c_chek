"""Локальные показатели среды процесса, без прав на OpenStack и без сети.

Это не доказательство идентичности виртуальной машины в облаке: приложение
может работать в контейнере или на отдельном хосте. Снимки не сохраняются.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import BASE_DIR

_SAMPLE_SEC = 0.25
_CACHE_SEC = 5.0
_lock = threading.Lock()
_cached = None
_cached_at = 0.0


def parse_cpu(text: str) -> tuple[int, int] | None:
    """Сумма и простой CPU из /proc/stat; guest уже включён в user/nice."""
    try:
        line = next(line for line in text.splitlines() if line.startswith('cpu '))
        values = [int(value) for value in line.split()[1:9]]
        if len(values) < 4 or any(value < 0 for value in values):
            return None
        values += [0] * (8 - len(values))
        return sum(values), values[3] + values[4]
    except (ValueError, StopIteration):
        return None


def cpu_percent(first: tuple[int, int] | None, last: tuple[int, int] | None) -> float | None:
    if first is None or last is None:
        return None
    elapsed, idle = last[0] - first[0], last[1] - first[1]
    if elapsed <= 0 or idle < 0 or idle > elapsed:
        return None
    return round(100 * (elapsed - idle) / elapsed, 2)


def parse_memory(text: str) -> dict | None:
    """Память Linux: доступная включает возвращаемый файловый кэш."""
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] in ('MemTotal:', 'MemAvailable:') and parts[2] == 'kB':
            try:
                values[parts[0]] = int(parts[1]) * 1024
            except ValueError:
                return None
    total, available = values.get('MemTotal:'), values.get('MemAvailable:')
    if total is None or available is None or total <= 0 or not 0 <= available <= total:
        return None
    used = total - available
    return {'total_bytes': total, 'used_bytes': used, 'available_bytes': available,
            'pct': round(100 * used / total, 2)}


def disk_view(stat) -> dict | None:
    try:
        size = stat.f_frsize or stat.f_bsize
        blocks, free, available = stat.f_blocks, stat.f_bfree, stat.f_bavail
        if size <= 0 or blocks <= 0 or not 0 <= available <= free <= blocks:
            return None
        total, used = blocks * size, (blocks - free) * size
        return {'total_bytes': total, 'used_bytes': used, 'available_bytes': available * size,
                'pct': round(100 * used / total, 2)}
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _cpu_ticks():
    try:
        return parse_cpu(Path('/proc/stat').read_text(encoding='ascii'))
    except (OSError, UnicodeError):
        return None


def _memory():
    try:
        return parse_memory(Path('/proc/meminfo').read_text(encoding='ascii'))
    except (OSError, UnicodeError):
        return None


def _disk():
    try:
        return disk_view(os.statvfs(BASE_DIR))
    except (OSError, AttributeError):
        return None


def _container_hint() -> bool:
    if Path('/.dockerenv').exists() or Path('/run/.containerenv').exists() or os.environ.get('container'):
        return True
    try:
        cgroup = Path('/proc/1/cgroup').read_text(encoding='utf-8').lower()
        return any(marker in cgroup for marker in ('docker', 'kubepods', 'containerd', 'libpod', 'lxc'))
    except (OSError, UnicodeError):
        return False


def snapshot() -> dict:
    """Короткий локальный замер, не чаще раза в 5 секунд на процесс."""
    global _cached, _cached_at
    with _lock:
        now = time.monotonic()
        if _cached is not None and 0 <= now - _cached_at < _CACHE_SEC:
            return dict(_cached)
        first = _cpu_ticks()
        if first is not None:
            time.sleep(_SAMPLE_SEC)
        last = _cpu_ticks() if first is not None else None
        _cached = {
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'cpu_pct': cpu_percent(first, last),
            'logical_cpus': os.cpu_count(),
            'memory': _memory(),
            'disk': _disk(),
            'container_hint': _container_hint(),
        }
        _cached_at = time.monotonic()
        return dict(_cached)


def reset_cache() -> None:
    """Очистка памяти измерителя в изолированных тестах."""
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0
