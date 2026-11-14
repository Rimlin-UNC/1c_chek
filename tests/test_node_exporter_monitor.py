"""Дополнительный источник данных готового node_exporter, без внешних запросов."""
from app.services import node_exporter_monitor as monitor

METRICS = '''# HELP node_memory_MemTotal_bytes Total memory
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes 1000
node_memory_MemAvailable_bytes 400
node_load1 1.25
node_load5 0.75
node_load15 0.5
node_time_seconds 10000
node_boot_time_seconds 8000
'''


def test_prometheus_metrics_are_parsed_and_bad_values_are_not_zero():
    result = monitor.parse_metrics(METRICS)
    assert result['memory']['pct'] == 60
    assert result['load'] == [1.25, .75, .5]
    assert result['uptime_seconds'] == 2000
    assert monitor.parse_metrics('node_memory_MemTotal_bytes 10\nnode_memory_MemAvailable_bytes 99\nnode_load1 NaN\n')['memory'] is None
    assert monitor.parse_metrics('node_load1 NaN\n')['load'][0] is None
    assert monitor.parse_metrics('node_memory_MemTotal_bytes 10\nnode_memory_MemTotal_bytes 20\n')['memory'] is None


def test_opt_in_and_admin_endpoint(client, monkeypatch):
    from tests.conftest import login
    from tests.test_v17050 import _mk_user

    monkeypatch.delenv('YM_NODE_EXPORTER', raising=False)
    monkeypatch.setattr(monitor, '_fetch', lambda: (_ for _ in ()).throw(AssertionError('disabled fetch')))
    assert monitor.snapshot()['status'] == 'not_configured'
    monkeypatch.setenv('YM_NODE_EXPORTER', '1')
    monkeypatch.setattr(monitor, '_fetch', lambda: METRICS)
    monitor.reset_cache()
    admin = login(client, 'admin', 'admin123')
    res = client.get('/api/v1/admin/server-monitor/node-exporter', headers=admin)
    assert res.status_code == 200 and 'no-store' in res.headers['cache-control']
    assert res.json()['memory']['pct'] == 60
    user = _mk_user(client, 'user', 'node_exporter_test_user')
    assert client.get('/api/v1/admin/server-monitor/node-exporter', headers=user).status_code == 403
    monitor.reset_cache()


def test_unavailable_is_not_faked_or_cached(monkeypatch):
    monkeypatch.setenv('YM_NODE_EXPORTER', '1')
    def fail():
        raise OSError('not reachable')
    monkeypatch.setattr(monitor, '_fetch', fail)
    monitor.reset_cache()
    assert monitor.snapshot()['status'] == 'unavailable'
    monkeypatch.setattr(monitor, '_fetch', lambda: METRICS)
    assert monitor.snapshot()['status'] == 'available'
    monitor.reset_cache()


def test_real_loopback_http_and_rejects_oversized_redirects(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/metrics')
                self.end_headers()
            elif self.path == '/compressed':
                self.send_response(200)
                self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                self.wfile.write(b'not-gzip')
            else:
                body = b'x' * (monitor._MAX_BYTES + 1) if self.path == '/large' else METRICS.encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    import httpx
    import pytest
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        monkeypatch.setattr(monitor, '_URL', base + '/metrics')
        assert monitor.parse_metrics(monitor._fetch())['memory']['pct'] == 60
        monkeypatch.setattr(monitor, '_URL', base + '/redirect')
        with pytest.raises(httpx.HTTPStatusError):
            monitor._fetch()
        monkeypatch.setattr(monitor, '_URL', base + '/compressed')
        with pytest.raises(ValueError):
            monitor._fetch()
        monkeypatch.setattr(monitor, '_URL', base + '/large')
        with pytest.raises(ValueError):
            monitor._fetch()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_upstream_node_exporter_v182_fixture():
    from pathlib import Path
    fixture = Path('tests/fixtures/node_exporter_1_8_2/e2e-output.txt').read_text(encoding='utf-8')
    parsed = monitor.parse_metrics(fixture)
    assert parsed['load'] == [0.21, 0.37, 0.39]
    # В фикстуре MemAvailable отсутствует: нельзя подставлять MemFree.
    assert parsed['memory'] is None


def test_calculated_rates_use_per_core_deltas_and_do_not_bridge_resets():
    def metrics(idle, user, rx, tx):
        return METRICS + f'''
node_cpu_seconds_total{{cpu="0",mode="idle"}} {idle}
node_cpu_seconds_total{{cpu="0",mode="iowait"}} 10
node_cpu_seconds_total{{cpu="0",mode="user"}} {user}
node_cpu_seconds_total{{cpu="0",mode="system"}} 5
node_network_receive_bytes_total{{device="ens3"}} {rx}
node_network_transmit_bytes_total{{device="ens3"}} {tx}
node_memory_SwapTotal_bytes 1000
node_memory_SwapFree_bytes 250
'''
    first, last = (monitor.parse_metrics(metrics(*x)) for x in [(100, 100, 1000, 500), (160, 140, 1200, 800)])
    sample = monitor.calculate_rates(first, last, 2)
    assert sample['cpu_pct'] == 40
    assert sample['memory']['pct'] == 60 and sample['swap']['pct'] == 75
    assert sample['network']['ens3']['rx_bytes_per_sec'] == 100
    assert sample['network']['ens3']['tx_bytes_per_sec'] == 150
    assert monitor.calculate_rates(last, first, 2)['cpu_pct'] is None
    assert monitor.calculate_rates(last, first, 2)['network']['ens3']['rx_bytes_per_sec'] is None
    assert monitor.calculate_rates(first, last, 0)['cpu_pct'] is None
    changed = monitor.parse_metrics(metrics(160, 140, 1200, 800).replace('cpu="0"', 'cpu="1"'))
    assert monitor.calculate_rates(first, changed, 2)['cpu_pct'] is None
    missing = monitor.parse_metrics(metrics(160, 140, 1200, 800).replace('node_cpu_seconds_total{cpu="0",mode="system"} 5\n', ''))
    assert monitor.calculate_rates(missing, missing, 2)['cpu_pct'] is None


def test_history_source_is_separately_identified_and_never_overwrites_local(tmp_path, monkeypatch):
    import sqlite3

    import pytest

    from app.services import server_history as history

    local, exporter = tmp_path / 'local.db', tmp_path / 'exporter.db'
    history.initialize(local)
    history.initialize(exporter, app_id=monitor.HISTORY_APP_ID)
    with pytest.raises(ValueError):
        history.record(local, {}, '', now=1800, app_id=monitor.HISTORY_APP_ID)
    with pytest.raises(ValueError):
        history.record(exporter, {}, '', now=1800)
    history.record(exporter, {'cpu_pct': 40, 'memory': {'pct': 60}, 'load_average': [1.25]}, '',
                   now=1800, app_id=monitor.HISTORY_APP_ID)
    with sqlite3.connect(local) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 0
    monkeypatch.setenv('YM_NODE_EXPORTER_HISTORY_FILE', str(exporter))
    monkeypatch.setenv('YM_NODE_EXPORTER', '1')
    result = history.snapshot('1h', source='node_exporter')
    assert result['status'] == 'available' and result['source'] == 'node_exporter'
    assert sum(row['samples'] for row in result['buckets']) == 0  # старые данные не выдаются как текущие
    monkeypatch.setenv('YM_NODE_EXPORTER_HISTORY_FILE', str(local))
    assert history.snapshot('1h', source='node_exporter')['status'] == 'unavailable'
    assert history.snapshot('1h', source='unknown')['status'] == 'invalid_source'


def test_history_cli_no_fallback_to_local_and_source_api(client, tmp_path, monkeypatch):
    import sqlite3

    from app.services import server_history as history
    from tests.conftest import login

    path = tmp_path / 'exporter.db'
    monkeypatch.setenv('YM_NODE_EXPORTER_HISTORY_FILE', str(path))
    monkeypatch.setenv('YM_NODE_EXPORTER', '1')
    monkeypatch.setattr(monitor, 'snapshot', lambda: {'status': 'unavailable'})
    assert monitor.history_command('init') == 0
    assert monitor.history_command('collect') != 0
    with sqlite3.connect(path) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 0
    monkeypatch.setattr(monitor, 'snapshot', lambda: {'status': 'available', 'cpu_pct': 40,
        'memory': {'pct': 60}, 'swap': None, 'load_average': [1.25], 'network': {}})
    assert monitor.history_command('collect') == 0
    admin = login(client, 'admin', 'admin123')
    res = client.get('/api/v1/admin/server-monitor/history?period=1h&source=node_exporter', headers=admin)
    assert res.status_code == 200 and res.json()['source'] == 'node_exporter'
    assert sum(row['samples'] for row in res.json()['buckets']) == 1
    assert 'no-store' in res.headers['cache-control']
    history.reset_cache()


def test_stuck_fetch_is_bounded_and_not_duplicated(monkeypatch):
    import threading
    import time
    entered, release = threading.Event(), threading.Event()
    calls = []
    def stuck():
        calls.append(1)
        entered.set()
        release.wait(5)
        return METRICS
    monkeypatch.setenv('YM_NODE_EXPORTER', '1')
    monkeypatch.setattr(monitor, '_fetch', stuck)
    monkeypatch.setattr(monitor, '_WAIT_SECONDS', .05)
    monitor.reset_cache()
    try:
        start = time.monotonic()
        assert monitor.snapshot()['status'] == 'unavailable'
        assert time.monotonic() - start < 1
        assert entered.is_set()
        assert monitor.snapshot()['status'] == 'unavailable'
        assert len(calls) == 1
    finally:
        with monitor._inflight_lock:
            done = monitor._inflight['done'] if monitor._inflight else None
        release.set()
        if done is not None:
            assert done.wait(3)
        monitor.reset_cache()


def test_exporter_disk_rates_only_for_explicit_device_and_no_resets():
    def metrics(read, write, read_sec, write_sec, busy):
        return METRICS + f'''
node_disk_reads_completed_total{{device="sda"}} {read}
node_disk_writes_completed_total{{device="sda"}} {write}
node_disk_read_time_seconds_total{{device="sda"}} {read_sec}
node_disk_write_time_seconds_total{{device="sda"}} {write_sec}
node_disk_io_time_seconds_total{{device="sda"}} {busy}
'''
    a = monitor.parse_metrics(metrics(10, 20, 1, 2, 5))
    b = monitor.parse_metrics(metrics(14, 22, 1.4, 2.4, 6))
    assert monitor.calculate_rates(a, b, 2)['disk_io'] is None
    disk = monitor.calculate_rates(a, b, 2, disk_device='sda')['disk_io']
    assert disk == {'device': 'sda', 'read_iops': 2, 'write_iops': 1,
                    'read_await_ms': 100, 'write_await_ms': 200, 'busy_pct': 50}
    assert monitor.calculate_rates(b, a, 2, disk_device='sda')['disk_io'] is None
    assert monitor.calculate_rates(a, b, 2, disk_device='sdb')['disk_io'] is None
    assert monitor.calculate_rates(a, a, 2, disk_device='sda')['disk_io']['read_await_ms'] is None


def test_separate_process_collector_reads_http_and_history_without_touching_local(tmp_path, monkeypatch, client):
    import os
    import sqlite3
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from app.services import server_history
    from tests.conftest import login

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = METRICS.encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db, local = tmp_path / 'exporter.db', tmp_path / 'local.db'
    server_history.initialize(local)
    before = local.read_bytes()
    env = os.environ.copy()
    env.update({'YM_NODE_EXPORTER': '1', 'YM_NODE_EXPORTER_HISTORY_FILE': str(db),
                'YM_MONITOR_HISTORY_FILE': str(local), 'YM_MONITOR_TRAFFIC_INTERFACE': 'lo'})
    code = ('import sys; from app.services import node_exporter_monitor as m; '
            'm._URL=sys.argv[1]; m._SAMPLE_SECONDS=.02; raise SystemExit(m.history_command(sys.argv[2]))')
    try:
        for action in ('init', 'collect', 'collect'):
            proc = subprocess.run([sys.executable, '-c', code, f'http://127.0.0.1:{server.server_port}/metrics', action],
                                  cwd=Path(__file__).resolve().parents[1], env=env, text=True,
                                  capture_output=True, timeout=20, check=False)
            assert proc.returncode == 0, proc.stdout + proc.stderr
        with sqlite3.connect(db) as con:
            assert con.execute('select count(*) from samples').fetchone()[0] in (1, 2)
            assert con.execute('select memory_pct from samples').fetchone()[0] == 60
        assert local.read_bytes() == before
        monkeypatch.setenv('YM_NODE_EXPORTER_HISTORY_FILE', str(db))
        monkeypatch.setenv('YM_NODE_EXPORTER', '1')
        admin = login(client, 'admin', 'admin123')
        res = client.get('/api/v1/admin/server-monitor/history?source=node_exporter', headers=admin)
        assert res.status_code == 200 and res.json()['source'] == 'node_exporter'
        assert sum(x['samples'] for x in res.json()['buckets']) in (1, 2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        server_history.reset_cache()
