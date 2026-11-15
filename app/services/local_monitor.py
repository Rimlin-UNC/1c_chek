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
_FS_TIMEOUT = 0.5
_fs_lock = threading.Lock()
_fs_inflight = None


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


def _nonnegative_fields(text: str, names: tuple[str, ...]) -> dict | None:
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in names:
            if len(fields) != 2 or fields[0] in values:
                return None
            try:
                value = int(fields[1])
            except ValueError:
                return None
            if value < 0:
                return None
            values[fields[0]] = value
    return values if len(values) == len(names) else None


def parse_context_switches(text: str) -> int | None:
    values = _nonnegative_fields(text, ('ctxt',))
    return values['ctxt'] if values else None


def parse_swap_io(text: str) -> tuple[int, int] | None:
    values = _nonnegative_fields(text, ('pswpin', 'pswpout'))
    return (values['pswpin'], values['pswpout']) if values else None


def counter_rate(first: int | None, last: int | None, seconds: float) -> float | None:
    if first is None or last is None or last < first or not math.isfinite(seconds) or seconds <= 0:
        return None
    return round((last - first) / seconds, 2)


def swap_io_rates(first, last, seconds: float) -> dict | None:
    if first is None or last is None:
        return None
    ins, outs = (counter_rate(a, b, seconds) for a, b in zip(first, last))
    if ins is None or outs is None:
        return None
    return {'in_pages_per_sec': ins, 'out_pages_per_sec': outs}


def parse_tasks(text: str) -> dict | None:
    """Четвёртое поле loadavg — задачи планировщика, включая потоки, не число процессов."""
    try:
        runnable, total = (int(value) for value in text.split()[3].split('/'))
        if not 0 <= runnable <= total or total <= 0:
            return None
        return {'runnable': runnable, 'total': total}
    except (IndexError, ValueError):
        return None


def parse_tcp_sockets(text: str) -> int | None:
    """TCP inuse включает и слушающие сокеты, это не только активные соединения."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == 'TCP:':
            try:
                index = fields.index('inuse')
                value = int(fields[index + 1])
                return value if value >= 0 else None
            except (ValueError, IndexError):
                return None
    return None


def parse_tcp_established(text: str) -> int | None:
    """CurrEstab — ESTABLISHED и CLOSE_WAIT сетевого пространства имён, без адресов."""
    lines = [line.split()[1:] for line in text.splitlines() if line.split() and line.split()[0] == 'Tcp:']
    if len(lines) != 2:
        return None
    names, values = lines
    if len(names) != len(values) or names.count('CurrEstab') != 1:
        return None
    value = values[names.index('CurrEstab')]
    if not value.isascii() or not value.isdigit():
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if count <= 2**53 - 1 else None


def inode_view(stat) -> dict | None:
    try:
        total, free, available = stat.f_files, stat.f_ffree, stat.f_favail
        if total <= 0 or not 0 <= available <= free <= total:
            return None
        used = total - free
        return {'total': total, 'used': used, 'available': available, 'pct': round(100 * used / total, 2)}
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def parse_diskstats(text: str, device: tuple[int, int] | None) -> dict | None:
    """Только точное совпадение major/minor, без догадок о томах и виртуальных дисках."""
    if device is None:
        return None
    for line in text.splitlines():
        fields = line.split()
        try:
            if len(fields) < 14 or (int(fields[0]), int(fields[1])) != device:
                continue
            counters = tuple(int(fields[i]) for i in (3, 5, 6, 7, 9, 10, 12))
            if any(value < 0 for value in counters):
                return None
            return {'name': fields[2], 'device': device, 'counters': counters}
        except (ValueError, IndexError):
            continue
    return None


def disk_rates(first: dict | None, last: dict | None, seconds: float) -> dict | None:
    if not first or not last or first['device'] != last['device'] or first['name'] != last['name']:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    delta = [b - a for a, b in zip(first['counters'], last['counters'])]
    if len(delta) != 7 or any(v < 0 for v in delta):
        return None
    reads, r_sectors, r_ms, writes, w_sectors, w_ms, busy_ms = delta
    busy_pct = busy_ms / (seconds * 10)
    # При сбросах и несовместимых счётчиках не показываем невозможную загрузку.
    if busy_pct > 100:
        return None
    return {'device': last['name'], 'read_iops': round(reads / seconds, 2),
            'write_iops': round(writes / seconds, 2),
            'read_bytes_per_sec': round(r_sectors * 512 / seconds, 2),
            'write_bytes_per_sec': round(w_sectors * 512 / seconds, 2),
            'read_await_ms': round(r_ms / reads, 2) if reads else None,
            'write_await_ms': round(w_ms / writes, 2) if writes else None,
            'busy_pct': round(busy_pct, 2)}


def _stat_path():
    return os.stat(BASE_DIR)


def _statvfs_path():
    return os.statvfs(BASE_DIR)


def _filesystem_call(key: str, function):
    """Не более одной незавершённой операции для каталога приложения на процесс.

    Системный вызов нельзя принудительно прервать; поток daemon остаётся до его
    завершения, а новые запросы при занятости возвращают отсутствие данных.
    """
    global _fs_inflight
    with _fs_lock:
        entry = _fs_inflight
        if entry is not None and entry['key'] != key:
            return None
        if entry is None:
            entry = {'key': key, 'done': threading.Event(), 'result': None}
            _fs_inflight = entry

            def worker():
                global _fs_inflight
                try:
                    entry['result'] = function()
                except Exception:
                    entry['result'] = None
                finally:
                    with _fs_lock:
                        if _fs_inflight is entry:
                            _fs_inflight = None
                        entry['done'].set()

            try:
                thread = threading.Thread(target=worker, daemon=True, name='app-filesystem-probe')
                entry['thread'] = thread
                thread.start()
            except (OSError, RuntimeError):
                _fs_inflight = None
                return None
    if not entry['done'].wait(_FS_TIMEOUT):
        return None
    return entry['result']


def _block_device() -> tuple[int, int] | None:
    try:
        info = _filesystem_call('stat', _stat_path)
        if info is None:
            return None
        dev = info.st_dev
        return os.major(dev), os.minor(dev)
    except (OSError, AttributeError, ValueError):
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
        stat = _filesystem_call('statvfs', _statvfs_path)
        if stat is None:
            return None
        disk = disk_view(stat)
        return {**disk, 'inodes': inode_view(stat)} if disk else None
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
        system_time = time.monotonic()
        stat_first = _read_proc('/proc/stat')
        cpu_first = parse_cpu_details(stat_first)
        swap_io_first = parse_swap_io(_read_proc('/proc/vmstat'))
        net_first_text = _read_proc('/proc/net/dev')
        net_first = parse_network(net_first_text)
        device = _block_device()
        io_first = parse_diskstats(_read_proc('/proc/diskstats'), device)
        io_time = time.monotonic()
        net_time = time.monotonic()
        first = _cpu_ticks()
        if first is not None or net_first or io_first or swap_io_first or parse_context_switches(stat_first) is not None:
            time.sleep(_SAMPLE_SEC)
        last = _cpu_ticks() if first is not None else None
        net_last_text = _read_proc('/proc/net/dev')
        net_elapsed = time.monotonic() - net_time
        stat_last = _read_proc('/proc/stat')
        cpu_last = parse_cpu_details(stat_last)
        swap_io_last = parse_swap_io(_read_proc('/proc/vmstat'))
        system_elapsed = time.monotonic() - system_time
        io_last = parse_diskstats(_read_proc('/proc/diskstats'), device)
        io_elapsed = time.monotonic() - io_time
        loadavg = _read_proc('/proc/loadavg')
        _cached = {
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'cpu_pct': cpu_percent(first, last),
            'logical_cpus': os.cpu_count(),
            'cpu_detail': cpu_breakdown(cpu_first, cpu_last),
            'context_switches_per_sec': counter_rate(parse_context_switches(stat_first),
                                                      parse_context_switches(stat_last), system_elapsed),
            'swap_io': swap_io_rates(swap_io_first, swap_io_last, system_elapsed),
            'load_average': parse_load(loadavg),
            'tasks': parse_tasks(loadavg),
            'tcp_sockets': parse_tcp_sockets(_read_proc('/proc/net/sockstat')),
            'tcp_established': parse_tcp_established(_read_proc('/proc/net/snmp')),
            'disk_io': disk_rates(io_first, io_last, io_elapsed),
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
