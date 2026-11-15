"""Ограниченный локальный снимок процессов по запросу администратора.

Не читает командные строки, не требует root, не собирает и не хранит историю.
Видимость зависит от настроек procfs и пространств имён.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .local_monitor import _read_proc, parse_memory

_LIMIT = 256
_SAMPLE_SEC = 0.25
_CACHE_SEC = 10.0
_lock = threading.Lock()
_cached = None
_cached_at = 0.0


def parse_stat(text: str, expected_pid: int) -> dict | None:
    try:
        opening, closing = text.index('('), text.rindex(')')
        pid = int(text[:opening].strip())
        fields = text[closing + 1:].split()
        if pid != expected_pid or pid <= 0 or closing <= opening or len(fields) < 22:
            return None
        state = fields[0]
        utime, stime, start, rss = (int(fields[i]) for i in (11, 12, 19, 21))
        if len(state) != 1 or min(utime, stime, start, rss) < 0:
            return None
        # comm может содержать скобки и управляющие символы. Аргументы процесса не читаем.
        name = ''.join(ch if ch.isprintable() else ' ' for ch in text[opening + 1:closing]).strip()
        return {'pid': pid, 'name': name[:64], 'state': state, 'ticks': utime + stime,
                'start': start, 'rss_pages': rss}
    except (ValueError, IndexError, TypeError):
        return None


def scan(root: Path = Path('/proc'), *, limit: int = _LIMIT) -> tuple[dict | None, bool]:
    result = {}
    partial = False
    count = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.isascii() or not entry.name.isdigit():
                    continue
                if count >= limit:
                    partial = True
                    break
                count += 1
                pid = int(entry.name)
                try:
                    text = (root / entry.name / 'stat').read_text(encoding='utf-8', errors='replace')
                    record = parse_stat(text, pid)
                    if not record:
                        partial = True
                        continue
                    record['sampled'] = time.monotonic()
                    result[pid] = record
                except OSError:
                    # Процесс мог завершиться или его состояние могло быть скрыто.
                    partial = True
    except OSError:
        return None, True
    return result, partial


def summarize(first: dict, last: dict, *, partial: bool, hz: int, page_size: int, mem_total: int | None) -> dict:
    partial = partial or first.keys() != last.keys()
    by_cpu, by_memory = [], []
    for pid, record in last.items():
        rss = record['rss_pages'] * page_size
        if rss > 2**53 - 1:
            partial = True
            continue
        row = {'pid': pid, 'name': record['name'], 'state': record['state'], 'rss_bytes': rss,
               'memory_pct': round(rss * 100 / mem_total, 2) if mem_total and mem_total > 0 else None}
        by_memory.append(row)
        prev = first.get(pid)
        if not prev or prev['start'] != record['start']:
            partial = True
            continue
        elapsed = record['sampled'] - prev['sampled']
        ticks = record['ticks'] - prev['ticks']
        if not math.isfinite(elapsed) or elapsed <= 0 or ticks < 0:
            partial = True
            continue
        # 100% соответствует одному логическому CPU; многопоточный процесс может превысить 100%.
        by_cpu.append({**row, 'cpu_pct': round(100 * ticks / hz / elapsed, 2)})
    return {'visible': len(last), 'zombies': sum(r['state'] == 'Z' for r in last.values()),
            'partial': partial, 'top_cpu': sorted(by_cpu, key=lambda x: (-x['cpu_pct'], x['pid']))[:5],
            'top_memory': sorted(by_memory, key=lambda x: (-x['rss_bytes'], x['pid']))[:5]}


def snapshot() -> dict:
    """Только по запросу; не более двух проходов и 256 видимых PID за проход."""
    global _cached, _cached_at
    with _lock:
        now = time.monotonic()
        if _cached is not None and 0 <= now - _cached_at < _CACHE_SEC:
            return _cached
        result = {'status': 'unavailable'}
        try:
            hz, page_size = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
            if hz <= 0 or page_size <= 0:
                raise ValueError('системные единицы недоступны')
            first, partial_first = scan()
            if first is None:
                raise OSError('procfs недоступен')
            time.sleep(_SAMPLE_SEC)
            last, partial_last = scan()
            if last is None:
                raise OSError('procfs недоступен')
            memory = parse_memory(_read_proc('/proc/meminfo'))
            result = {'status': 'available', 'collected_at': datetime.now(timezone.utc).isoformat(),
                      **summarize(first, last, partial=partial_first or partial_last, hz=hz,
                                  page_size=page_size, mem_total=memory['total_bytes'] if memory else None)}
        except (OSError, ValueError, OverflowError):
            pass
        _cached, _cached_at = result, time.monotonic()
        return result


def reset_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0
