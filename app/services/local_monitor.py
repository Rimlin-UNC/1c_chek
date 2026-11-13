"""Локальные показатели среды процесса без подключения к сети.

Это не доказательство идентичности виртуальной машины в облаке: приложение
может работать в контейнере или на отдельном хосте. Снимки не сохраняются.
"""
from __future__ import annotations

import math
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



def parse_swap(text: str) -> dict | None:
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] in ('SwapTotal:', 'SwapFree:') and fields[2] == 'kB':
            try:
                values[fields[0]] = int(fields[1]) * 1024
            except ValueError:
                return None
    total, free = values.get('SwapTotal:'), values.get('SwapFree:')
    if total is None or free is None or not 0 <= free <= total:
        return None
    used = total - free
    return {'total_bytes': total, 'used_bytes': used, 'free_bytes': free,
            'pct': round(100 * used / total, 2) if total else None}


def parse_cpu_details(text: str) -> tuple[int, ...] | None:
    try:
        line = next(line for line in text.splitlines() if line.startswith('cpu '))
        values = tuple(int(value) for value in line.split()[1:9])
        if len(values) < 4 or any(value < 0 for value in values):
            return None
        return values + (0,) * (8 - len(values))
    except (ValueError, StopIteration):
        return None


def cpu_breakdown(first, last) -> dict | None:
    if first is None or last is None:
        return None
    delta = [b - a for a, b in zip(first, last)]
    if len(delta) != 8 or any(value < 0 for value in delta) or sum(delta) <= 0:
        return None
    total = sum(delta)
    return {'user_pct': round(100 * (delta[0] + delta[1]) / total, 2),
            'system_pct': round(100 * (delta[2] + delta[5] + delta[6]) / total, 2),
            'iowait_pct': round(100 * delta[4] / total, 2),
            'steal_pct': round(100 * delta[7] / total, 2)}


def parse_network(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(':')
        name = name.strip()
        if not sep or not name or name == 'lo':
            continue
        try:
            fields = [int(value) for value in rest.split()]
            if len(fields) < 16 or any(value < 0 for value in fields[:16]):
                continue
            result[name] = tuple(fields[i] for i in (0, 8, 1, 9, 2, 10, 3, 11))
        except ValueError:
            continue
    return result


def network_rates(first, last, seconds: float) -> dict:
    result = {}
    for name, values in last.items():
        previous = first.get(name)
        if previous is None or not math.isfinite(seconds) or seconds <= 0:
            result[name] = None
            continue
        delta = [end - start for start, end in zip(previous, values)]
        if len(delta) != 8 or any(value < 0 for value in delta):
            result[name] = None
            continue
        result[name] = {
            'rx_bytes_per_sec': round(delta[0] / seconds, 2),
            'tx_bytes_per_sec': round(delta[1] / seconds, 2),
            'rx_packets_per_sec': round(delta[2] / seconds, 2),
            'tx_packets_per_sec': round(delta[3] / seconds, 2),
            'rx_errors_delta': delta[4], 'tx_errors_delta': delta[5],
            'rx_drops_delta': delta[6], 'tx_drops_delta': delta[7],
        }
    return result


def parse_uptime(text: str) -> float | None:
    try:
        value = float(text.split()[0])
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, IndexError):
        return None


def parse_load(text: str) -> list[float] | None:
    try:
        values = [float(value) for value in text.split()[:3]]
        return values if len(values) == 3 and all(math.isfinite(v) and v >= 0 for v in values) else None
    except ValueError:
        return None


def _read_proc(path: str) -> str:
    try:
        return Path(path).read_text(encoding='ascii')
    except (OSError, UnicodeError):
        return ''


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
        cpu_first = parse_cpu_details(_read_proc('/proc/stat'))
        net_first_text = _read_proc('/proc/net/dev')
        net_first = parse_network(net_first_text)
        net_time = time.monotonic()
        first = _cpu_ticks()
        if first is not None or net_first:
            time.sleep(_SAMPLE_SEC)
        last = _cpu_ticks() if first is not None else None
        net_last_text = _read_proc('/proc/net/dev')
        net_elapsed = time.monotonic() - net_time
        cpu_last = parse_cpu_details(_read_proc('/proc/stat'))
        _cached = {
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'cpu_pct': cpu_percent(first, last),
            'logical_cpus': os.cpu_count(),
            'cpu_detail': cpu_breakdown(cpu_first, cpu_last),
            'load_average': parse_load(_read_proc('/proc/loadavg')),
            'uptime_sec': parse_uptime(_read_proc('/proc/uptime')),
            'swap': parse_swap(_read_proc('/proc/meminfo')),
            'network': network_rates(net_first, parse_network(net_last_text), net_elapsed)
            if net_first_text and net_last_text else None,
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
