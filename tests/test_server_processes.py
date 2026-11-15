"""Процессы — ограниченный снимок только для администратора, без командных строк."""
from app.services import server_processes as procs


def stat_line(pid=42, name='рабочий ) процесс', state='S', utime=10, stime=5, start=100, rss=20):
    fields = [state] + ['0'] * 21
    fields[11] = str(utime)
    fields[12] = str(stime)
    fields[19] = str(start)
    fields[21] = str(rss)
    return f'{pid} ({name}) ' + ' '.join(fields)


def test_parse_stat_handles_parentheses_and_invalid_data():
    entry = procs.parse_stat(stat_line(), 42)
    assert entry == {'pid': 42, 'name': 'рабочий ) процесс', 'state': 'S', 'ticks': 15, 'start': 100, 'rss_pages': 20}
    assert procs.parse_stat(stat_line(pid=43), 42) is None
    assert procs.parse_stat(stat_line(rss=-1), 42) is None
    assert procs.parse_stat('42 (bad) S', 42) is None
    assert '\n' not in procs.parse_stat(stat_line(name='a\nb'), 42)['name']


def test_summary_uses_starttime_and_measured_time_and_marks_partial():
    first = {42: {**procs.parse_stat(stat_line(), 42), 'sampled': 10}}
    second = {42: {**procs.parse_stat(stat_line(utime=30, stime=15, rss=30), 42), 'sampled': 10.5}}
    result = procs.summarize(first, second, partial=True, hz=100, page_size=4096, mem_total=409600)
    assert result['partial'] is True and result['visible'] == 1 and result['zombies'] == 0
    assert result['top_cpu'][0]['cpu_pct'] == 60.0
    assert result['top_memory'][0]['rss_bytes'] == 122880 and result['top_memory'][0]['memory_pct'] == 30.0
    second[42]['start'] = 101
    assert procs.summarize(first, second, partial=True, hz=100, page_size=4096, mem_total=409600)['top_cpu'] == []
    second[42]['start'] = 100
    second[42]['ticks'] = 0
    assert procs.summarize(first, second, partial=True, hz=100, page_size=4096, mem_total=None)['top_cpu'] == []
    assert procs.summarize(first, second, partial=True, hz=100, page_size=4096, mem_total=None)['top_memory'][0]['memory_pct'] is None


def test_scan_bounds_and_partial(tmp_path):
    for pid in (1, 2, 3):
        folder = tmp_path / str(pid)
        folder.mkdir()
        (folder / 'stat').write_text(stat_line(pid=pid, state='Z' if pid == 2 else 'S'))
    data, partial = procs.scan(tmp_path, limit=2)
    assert len(data) == 2 and partial is True
    data, partial = procs.scan(tmp_path, limit=4)
    assert len(data) == 3 and partial is False
    (tmp_path / '2' / 'stat').write_text('malformed')
    data, partial = procs.scan(tmp_path, limit=4)
    assert len(data) == 2 and partial is True


def test_endpoint_admin_only_and_not_cached_in_browser(client, monkeypatch):
    from tests.conftest import login
    from tests.test_v17050 import _mk_user

    monkeypatch.setattr(procs, 'snapshot', lambda: {'status': 'unavailable'})
    admin = login(client, 'admin', 'admin123')
    response = client.get('/api/v1/admin/server-monitor/processes', headers=admin)
    assert response.status_code == 200 and response.json() == {'status': 'unavailable'}
    assert 'no-store' in response.headers['cache-control']
    user = _mk_user(client, 'user', 'process_monitor_test_user')
    assert client.get('/api/v1/admin/server-monitor/processes', headers=user).status_code == 403
    saved = list(client.cookies.jar)
    client.cookies.clear()
    try:
        assert client.get('/api/v1/admin/server-monitor/processes').status_code == 401
    finally:
        for cookie in saved:
            client.cookies.jar.set_cookie(cookie)


def test_cache_and_missing_proc_are_not_mistaken_for_empty(monkeypatch):
    procs.reset_cache()
    calls = []
    monkeypatch.setattr(procs, 'scan', lambda: (calls.append(1), (None, True))[1])
    assert procs.snapshot()['status'] == 'unavailable'
    assert procs.snapshot()['status'] == 'unavailable' and len(calls) == 1
    procs.reset_cache()
    scans = iter([({42: {**procs.parse_stat(stat_line(), 42), 'sampled': 10}}, False),
                  ({42: {**procs.parse_stat(stat_line(utime=30), 42), 'sampled': 10.5}}, False)])
    monkeypatch.setattr(procs, 'scan', lambda: next(scans))
    monkeypatch.setattr(procs.time, 'sleep', lambda _: None)
    snapshot = procs.snapshot()
    assert snapshot['status'] == 'available' and snapshot['visible'] == 1
    assert procs.snapshot() == snapshot
    procs.reset_cache()


def test_process_created_or_exited_between_samples_marks_partial():
    row = {**procs.parse_stat(stat_line(), 42), 'sampled': 10}
    result = procs.summarize({}, {42: row}, partial=False, hz=100, page_size=4096, mem_total=1000000)
    assert result['partial'] is True and result['top_cpu'] == []
