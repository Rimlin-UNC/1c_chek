"""Необязательное чтение локальной истории vnStat без установки или изменения системы.

История представляет счётчики выбранного интерфейса, не биллинг провайдера.
Процесс и сервис vnstatd должны быть установлены и настроены администратором.
"""
from __future__ import annotations

import calendar
import json
import re
import selectors
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

from ..config import _env

_INTERFACE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}\Z')
_CACHE_SEC = 60.0
_MAX_OUTPUT = 1024 * 1024
_lock = threading.Lock()
_cached = None
_cached_key = None
_cached_at = 0.0


def _day(value) -> date:
    return date(value['year'], value['month'], value['day'])


def _entries(items, *, monthly: bool, limit: int) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError('неверный список')
    result, seen = [], set()
    for row in items:
        d = row['date']
        day = date(d['year'], d['month'], 1 if monthly else d['day'])
        key = day.strftime('%Y-%m' if monthly else '%Y-%m-%d')
        rx, tx = row['rx'], row['tx']
        if key in seen or type(rx) is not int or type(tx) is not int or rx < 0 or tx < 0 or rx + tx > 2**53 - 1:
            raise ValueError('неверные счётчики')
        seen.add(key)
        result.append({'period' if monthly else 'date': key, 'rx_bytes': rx, 'tx_bytes': tx})
    return sorted(result, key=lambda row: row['period' if monthly else 'date'], reverse=True)[:limit]


def parse(document, interface: str) -> dict | None:
    """vnStat JSON v2: строго один выбранный интерфейс и только наблюдённые значения."""
    try:
        if not isinstance(document, dict) or str(document['jsonversion']) != '2':
            return None
        interfaces = document['interfaces']
        if not isinstance(interfaces, list) or len(interfaces) != 1 or interfaces[0]['name'] != interface:
            return None
        info = interfaces[0]
        created = _day(info['created']['date'])
        updated = info['updated']
        day = _day(updated['date'])
        clock = updated['time']
        updated_at = datetime(day.year, day.month, day.day, clock['hour'], clock['minute'])
        if created > day:
            return None
        values = info['traffic']
        return {'source': 'vnstat', 'interface': interface, 'created_date': created.isoformat(),
                'updated_at': updated_at.isoformat(), 'billing_verified': False,
                'months': _entries(values['month'], monthly=True, limit=3),
                'days': _entries(values['day'], monthly=False, limit=31)}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _now() -> datetime:
    return datetime.now().astimezone()


def scenario(data: dict, now: datetime) -> dict | None:
    """Условная экстраполяция, не проверка свежести, полноты или тарификации."""
    try:
        updated = datetime.fromisoformat(data['updated_at'])
        local_now = now.replace(tzinfo=None)
        if updated.tzinfo is not None or updated.date() != local_now.date():
            return None
        age = (local_now - updated).total_seconds()
        # Лишь грубое сито: часовой пояс vnStat неизвестен и свежесть не доказана.
        if age < -300 or age > 6 * 3600:
            return None
        today = updated.date()
        oldest = today - timedelta(days=7)
        created = date.fromisoformat(data['created_date'])
        if created >= min(oldest, today.replace(day=1)):
            return None
        months = [row for row in data['months'] if row['period'] == today.strftime('%Y-%m')]
        if len(months) != 1:
            return None
        daily = data['days']
        by_day = {date.fromisoformat(row['date']): row for row in daily}
        if len(by_day) != len(daily):
            return None
        last_week = [by_day[today - timedelta(days=n)] for n in range(1, 8)]

        def total(row):
            rx, tx = row['rx_bytes'], row['tx_bytes']
            if type(rx) is not int or type(tx) is not int or rx < 0 or tx < 0 or rx + tx > 2**53 - 1:
                raise ValueError('неверные счётчики')
            return rx + tx

        current = total(months[0])
        values = [total(row) for row in last_week]
        if not sum(values):
            return None
        # Сравниваем только полностью прошедшие дни этого календарного месяца.
        if sum(total(row) for d, row in by_day.items() if d.year == today.year and d.month == today.month and d < today) > current:
            return None
        remaining = (calendar.monthrange(today.year, today.month)[1] - today.day) * 1440 + (24 * 60 - updated.hour * 60 - updated.minute)
        # integer arithmetic: средние за 7 суток × минуты до конца месяца.
        projected = current + (sum(values) * remaining + 5040) // (7 * 1440)
        if projected > 2**53 - 1:
            return None
        return {'projected_bytes': projected, 'sample_days': 7, 'as_of': updated.isoformat(),
                'remaining_minutes': remaining, 'billing_verified': False, 'completeness_verified': False}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _view(result: dict) -> dict:
    if 'data' not in result:
        return result
    now = _now()
    return {**result, 'current_period': now.strftime('%Y-%m'), 'scenario': scenario(result['data'], now)}


def _read_json(executable: str, mode: str, count: str, interface: str):
    # Читаем ограниченное число байтов, а не capture_output без лимита памяти.
    with subprocess.Popen([executable, '--json', mode, count, '-i', interface],
                          shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + 3
            data = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(process.args, 3)
                    chunk = process.stdout.read1(min(65536, _MAX_OUTPUT + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > _MAX_OUTPUT:
                        raise ValueError('слишком большой ответ vnStat')
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                raise ValueError('не удалось прочитать vnStat')
            return json.loads(data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def snapshot() -> dict:
    """Только по запросу администратора; процессы без оболочки, не чаще раза в минуту."""
    global _cached, _cached_key, _cached_at
    try:
        interface = _env('YM_MONITOR_TRAFFIC_INTERFACE', '').strip()
    except OSError:
        return {'status': 'unavailable'}
    if not interface:
        return {'status': 'not_configured'}
    if not _INTERFACE.fullmatch(interface) or interface == 'lo':
        return {'status': 'invalid_interface'}
    with _lock:
        now = time.monotonic()
        if _cached is not None and _cached_key == interface and 0 <= now - _cached_at < _CACHE_SEC:
            # Граница месяца может наступить в течение срока кеширования.
            return _view(_cached)
        executable = shutil.which('vnstat')
        result = {'status': 'not_installed'}
        if executable:
            result = {'status': 'unavailable'}
            try:
                documents = []
                for mode, count in (('m', '3'), ('d', '31')):
                    documents.append(_read_json(executable, mode, count, interface))
                monthly, daily = documents
                # Оба ответа должны относиться к одному источнику. Возраст оцениваем
                # по более старой отметке, чтобы не выдать устаревший ряд за свежий.
                if (not isinstance(monthly, dict) or not isinstance(daily, dict)
                        or monthly.get('jsonversion') != daily.get('jsonversion')
                        or len(monthly['interfaces']) != 1 or len(daily['interfaces']) != 1):
                    raise ValueError('несогласованные данные')
                mi, di = monthly['interfaces'][0], daily['interfaces'][0]
                if mi['name'] != di['name'] or mi['created'] != di['created']:
                    raise ValueError('сменился источник')
                mi['traffic']['day'] = di['traffic']['day']
                parsed = parse(monthly, interface)
                if parsed:
                    later = parse({**daily, 'interfaces': [{**di, 'traffic': {
                        'month': mi['traffic']['month'], 'day': di['traffic']['day']}}]}, interface)
                    if not later:
                        raise ValueError('неверный суточный ряд')
                    parsed['updated_at'] = min(parsed['updated_at'], later['updated_at'])
                    # JSON v2 не указывает зону демона: timestamp клиента пересчитывается
                    # из даты БД в его TZ, поэтому свежесть надёжно не определить.
                    result = {'status': 'freshness_unknown',
                              'current_period': _now().strftime('%Y-%m'), 'data': parsed}
            except (OSError, UnicodeError, TimeoutError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, IndexError, RecursionError):
                pass
        _cached, _cached_key, _cached_at = result, interface, time.monotonic()
        return _view(result)


def reset_cache() -> None:
    global _cached, _cached_key, _cached_at
    with _lock:
        _cached, _cached_key, _cached_at = None, None, 0.0
