"""Необязательный источник node_exporter: только loopback и ограниченный ответ."""
from __future__ import annotations

import math
import re
import sqlite3
import threading
import time

import httpx
from prometheus_client.parser import text_string_to_metric_families

from ..config import _env

_URL = 'http://127.0.0.1:9100/metrics'
_MAX_BYTES = 1024 * 1024
_TIMEOUT = 2.0
HISTORY_APP_ID = 0x594D4832
_SAMPLE_SECONDS = 1.0
_WAIT_SECONDS = 3.0
_inflight_lock = threading.Lock()
_inflight = None
_lock = threading.Lock()
_cached = None
_cached_at = 0.0
_NAMES = {'node_memory_MemTotal_bytes', 'node_memory_MemAvailable_bytes',
          'node_load1', 'node_load5', 'node_load15', 'node_time_seconds', 'node_boot_time_seconds',
          'node_memory_SwapTotal_bytes', 'node_memory_SwapFree_bytes'}


def _fetch() -> str:
    deadline = time.monotonic() + _TIMEOUT
    result = bytearray()
    with httpx.Client(trust_env=False, timeout=_TIMEOUT, follow_redirects=False) as client:
        with client.stream('GET', _URL, headers={'Accept-Encoding': 'identity'}) as response:
            response.raise_for_status()
            if response.headers.get('content-encoding', 'identity').strip().lower() != 'identity':
                raise ValueError('сжатый ответ node_exporter не поддерживается')
            for chunk in response.iter_raw():
                if time.monotonic() > deadline or len(result) + len(chunk) > _MAX_BYTES:
                    raise ValueError('ответ node_exporter слишком долгий или большой')
                result.extend(chunk)
    return result.decode('utf-8', errors='strict')


def _fetch_limited() -> str:
    """Не дублирует зависший вызов; поток нельзя гарантированно отменить."""
    global _inflight
    with _inflight_lock:
        if _inflight is not None:
            raise ValueError('опрос node_exporter ещё выполняется')
        entry = {'done': threading.Event(), 'result': None, 'error': None}
        _inflight = entry

        def worker():
            global _inflight
            try:
                entry['result'] = _fetch()
            except Exception as exc:
                entry['error'] = exc
            finally:
                with _inflight_lock:
                    if _inflight is entry:
                        _inflight = None
                    entry['done'].set()

        try:
            threading.Thread(target=worker, daemon=True, name='node-exporter-read').start()
        except (RuntimeError, OSError):
            _inflight = None
            raise ValueError('не удалось запустить опрос') from None
    if not entry['done'].wait(_WAIT_SECONDS):
        raise ValueError('превышено время ожидания node_exporter')
    if entry['error'] is not None:
        raise ValueError('не удалось прочитать node_exporter') from entry['error']
    return entry['result']


def parse_metrics(text: str) -> dict:
    values = {}
    cpu = {}
    network = {}
    disk = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            value = sample.value
            if sample.name == 'node_cpu_seconds_total' and set(sample.labels) == {'cpu', 'mode'}:
                key = (sample.labels['cpu'], sample.labels['mode'])
                if re.fullmatch(r'[0-9]{1,6}', key[0]) and key[1] in {'user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal'}:
                    cpu[key] = value if key not in cpu and math.isfinite(value) and 0 <= value <= 2**53 - 1 else None
                continue
            if sample.name in {'node_network_receive_bytes_total', 'node_network_transmit_bytes_total'} and set(sample.labels) == {'device'}:
                device = sample.labels['device']
                if re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', device) and device != 'lo':
                    key = (device, 'rx' if sample.name == 'node_network_receive_bytes_total' else 'tx')
                    network[key] = value if key not in network and math.isfinite(value) and 0 <= value <= 2**53 - 1 else None
                continue
            if sample.name in {'node_disk_reads_completed_total', 'node_disk_writes_completed_total',
                                'node_disk_read_time_seconds_total', 'node_disk_write_time_seconds_total',
                                'node_disk_io_time_seconds_total'} and set(sample.labels) == {'device'}:
                device = sample.labels['device']
                if re.fullmatch(r'[A-Za-z0-9_.!-]{1,64}', device):
                    key = (device, sample.name)
                    disk[key] = value if key not in disk and math.isfinite(value) and 0 <= value <= 2**53 - 1 else None
                continue
            if sample.name not in _NAMES or sample.labels:
                continue
            valid = math.isfinite(value) and 0 <= value <= 9007199254740991
            values[sample.name] = value if sample.name not in values and valid else None
    total, avail = values.get('node_memory_MemTotal_bytes'), values.get('node_memory_MemAvailable_bytes')
    memory = None
    if total and avail is not None and avail <= total:
        memory = {'total_bytes': total, 'available_bytes': avail,
                  'used_bytes': total - avail, 'pct': round(100 * (total - avail) / total, 2)}
    now, boot = values.get('node_time_seconds'), values.get('node_boot_time_seconds')
    swap_total, swap_free = values.get('node_memory_SwapTotal_bytes'), values.get('node_memory_SwapFree_bytes')
    swap = None
    if swap_total and swap_free is not None and swap_free <= swap_total:
        swap = {'pct': round(100 * (swap_total - swap_free) / swap_total, 2)}
    return {'memory': memory, 'swap': swap, '_cpu': cpu, '_network': network, '_disk': disk, 'load': [values.get(f'node_load{x}') for x in (1, 5, 15)],
            'uptime_seconds': now - boot if now is not None and boot is not None and now >= boot else None}


def calculate_rates(first: dict, last: dict, seconds: float, *, disk_device: str = '') -> dict:
    first_cpu, last_cpu = first['_cpu'], last['_cpu']
    pct = None
    cores = {cpu for cpu, _mode in first_cpu}
    if (seconds > 0 and first_cpu and first_cpu.keys() == last_cpu.keys()
            and all((core, mode) in first_cpu for core in cores for mode in ('idle', 'user', 'system'))
            and all(a is not None and last_cpu[key] is not None and last_cpu[key] >= a for key, a in first_cpu.items())):
        diffs = {key: last_cpu[key] - val for key, val in first_cpu.items()}
        total = sum(diffs.values())
        idle = sum(val for (_core, mode), val in diffs.items() if mode in {'idle', 'iowait'})
        if total > 0:
            pct = round(100 * (total - idle) / total, 2)
    net = {}
    for device, _direction in last['_network']:
        if device in net:
            continue
        rates = {}
        for direction in ('rx', 'tx'):
            key = (device, direction)
            a, b = first['_network'].get(key), last['_network'].get(key)
            rates[f'{direction}_bytes_per_sec'] = round((b - a) / seconds, 2) if seconds > 0 and a is not None and b is not None and b >= a else None
        net[device] = rates
    disk_io = None
    names = ('node_disk_reads_completed_total', 'node_disk_writes_completed_total',
             'node_disk_read_time_seconds_total', 'node_disk_write_time_seconds_total', 'node_disk_io_time_seconds_total')
    if seconds > 0 and re.fullmatch(r'[A-Za-z0-9_.!-]{1,64}', disk_device):
        keys = [(disk_device, name) for name in names]
        a, b = [first['_disk'].get(key) for key in keys], [last['_disk'].get(key) for key in keys]
        if all(x is not None and y is not None and y >= x for x, y in zip(a, b)):
            reads, writes, read_time, write_time, busy = [y - x for x, y in zip(a, b)]
            busy_pct = 100 * busy / seconds
            if busy_pct <= 100:
                disk_io = {'device': disk_device, 'read_iops': round(reads / seconds, 2),
                           'write_iops': round(writes / seconds, 2),
                           'read_await_ms': round(1000 * read_time / reads, 2) if reads else None,
                           'write_await_ms': round(1000 * write_time / writes, 2) if writes else None,
                           'busy_pct': round(busy_pct, 2)}
    return {'cpu_pct': pct, 'memory': last['memory'], 'swap': last['swap'], 'network': net,
            'disk_io': disk_io, 'load_average': last['load'], 'uptime_seconds': last['uptime_seconds']}


def snapshot() -> dict:
    global _cached, _cached_at
    if _env('YM_NODE_EXPORTER', '').strip() != '1':
        return {'status': 'not_configured'}
    if not _lock.acquire(blocking=False):
        return {'status': 'busy'}
    try:
        now = time.monotonic()
        if _cached is not None and 0 <= now - _cached_at < 10:
            return _cached
        try:
            first = parse_metrics(_fetch_limited())
            started = time.monotonic()
            time.sleep(_SAMPLE_SECONDS)
            last = parse_metrics(_fetch_limited())
            metrics = calculate_rates(first, last, time.monotonic() - started,
                                      disk_device=_env('YM_NODE_EXPORTER_DISK_DEVICE', '').strip())
            if not metrics['memory'] and all(value is None for value in metrics['load_average']) and metrics['uptime_seconds'] is None:
                return {'status': 'unavailable'}
            _cached = {'status': 'available', **metrics, 'load': metrics['load_average']}
            _cached_at = time.monotonic()
            return _cached
        except (httpx.HTTPError, OSError, ValueError, UnicodeError, OverflowError):
            return {'status': 'unavailable'}
    finally:
        _lock.release()


def reset_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0


def history_command(action: str) -> int:
    """Отдельная база с отдельным маркером; отсутствие exporter не создаёт строку."""
    from pathlib import Path

    from . import server_history

    try:
        value = _env('YM_NODE_EXPORTER_HISTORY_FILE', '').strip()
        if not value or not Path(value).is_absolute():
            raise ValueError('YM_NODE_EXPORTER_HISTORY_FILE должен быть абсолютным путём')
        path = Path(value)
        if action == 'init':
            server_history.initialize(path, app_id=HISTORY_APP_ID)
        elif action == 'collect':
            result = snapshot()
            if result.get('status') != 'available':
                raise ValueError('node_exporter недоступен; запись не сделана')
            interface = _env('YM_MONITOR_TRAFFIC_INTERFACE', '').strip()
            server_history.record(path, result, interface, app_id=HISTORY_APP_ID)
        elif action == 'resume':
            server_history.resume_retention(path, app_id=HISTORY_APP_ID)
        elif action == 'enable-extra':
            server_history.enable_extra(path, app_id=HISTORY_APP_ID)
        else:
            raise ValueError('неизвестная команда')
        return 0
    except (OSError, ValueError, TypeError, OverflowError, sqlite3.Error) as exc:
        print(f'История node_exporter: {exc}')
        return 1


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Отдельная история node_exporter')
    parser.add_argument('action', choices=('init', 'collect', 'resume', 'enable-extra'))
    return history_command(parser.parse_args().action)


if __name__ == '__main__':
    raise SystemExit(main())
