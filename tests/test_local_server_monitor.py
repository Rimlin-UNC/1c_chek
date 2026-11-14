"""Показатели среды запуска: только чтение, отсутствие данных не равно нулю."""
from types import SimpleNamespace

from app.services import local_monitor as local_server


def test_cpu_delta_excludes_guest_counters_and_handles_resets():
    first = local_server.parse_cpu('cpu 100 10 40 500 20 5 5 0 20 4\ncpu0 1 2 3 4')
    last = local_server.parse_cpu('cpu 140 10 50 550 20 5 5 0 30 4')
    assert first == (680, 520)
    assert last == (780, 570)
    assert local_server.cpu_percent(first, last) == 50.0
    assert local_server.cpu_percent(last, first) is None
    assert local_server.cpu_percent(first, first) is None
    assert local_server.parse_cpu('invalid') is None


def test_memory_available_and_disk_use_are_not_fabricated():
    memory = local_server.parse_memory('MemTotal: 8192 kB\nMemAvailable: 3072 kB\n')
    assert memory == {'total_bytes': 8388608, 'used_bytes': 5242880,
                      'available_bytes': 3145728, 'pct': 62.5}
    assert local_server.parse_memory('MemTotal: 8192 kB\nMemFree: 1024 kB') is None
    assert local_server.parse_memory('MemTotal: 0 kB\nMemAvailable: 0 kB') is None
    disk = local_server.disk_view(SimpleNamespace(f_frsize=4096, f_bsize=4096,
                                                  f_blocks=100, f_bfree=40, f_bavail=30))
    assert disk == {'total_bytes': 409600, 'used_bytes': 245760,
                    'available_bytes': 122880, 'pct': 60.0}
    assert local_server.disk_view(SimpleNamespace(f_frsize=4096, f_bsize=4096,
                                                   f_blocks=0, f_bfree=0, f_bavail=0)) is None


def test_local_sample_survives_missing_sources_and_rate_limits(monkeypatch):
    local_server.reset_cache()
    clock = [100.0]
    calls = []
    cpus = iter([(100, 60), (200, 110), (300, 160), (400, 210)])
    monkeypatch.setattr(local_server.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(local_server.time, 'sleep', lambda _: None)
    monkeypatch.setattr(local_server, '_cpu_ticks', lambda: (calls.append('cpu'), next(cpus))[1])
    monkeypatch.setattr(local_server, '_memory', lambda: None)
    monkeypatch.setattr(local_server, '_disk', lambda: None)
    monkeypatch.setattr(local_server, '_container_hint', lambda: True)
    first = local_server.snapshot()
    second = local_server.snapshot()
    assert first['cpu_pct'] == 50.0 and len(calls) == 2
    assert first['memory'] is None and first['disk'] is None
    assert first['container_hint'] is True
    assert first == second
    clock[0] += 5
    assert local_server.snapshot()['cpu_pct'] == 50.0 and len(calls) == 4
    local_server.reset_cache()


def test_separate_endpoint_is_admin_only_and_does_not_call_openstack(client, monkeypatch):
    from tests.conftest import login
    from tests.test_v17050 import _mk_user

    monkeypatch.setattr(local_server, 'snapshot', lambda: {'cpu_pct': 12.5, 'logical_cpus': 4})
    admin = login(client, 'admin', 'admin123')
    result = client.get('/api/v1/admin/server-monitor', headers=admin)
    assert result.status_code == 200
    assert result.json()['cpu_pct'] == 12.5 and result.json()['logical_cpus'] == 4
    assert 'no-store' in result.headers.get('cache-control', '')
    user = _mk_user(client, 'user', 'local_monitor_test_user')
    assert client.get('/api/v1/admin/server-monitor', headers=user).status_code == 403
    saved = list(client.cookies.jar)
    client.cookies.clear()
    try:
        assert client.get('/api/v1/admin/server-monitor').status_code == 401
    finally:
        for cookie in saved:
            client.cookies.jar.set_cookie(cookie)


def test_settings_exposes_local_card_even_when_cloud_settings_unavailable():
    from pathlib import Path
    source = Path('app/static/js/app.js').read_text(encoding='utf-8')
    assert "${isAdmin() ? localServerCardHtml() : ''}" in source
    assert "if ($('#st-server-monitor')) localServerWire()" in source
    assert 'localServerStop()' in source
