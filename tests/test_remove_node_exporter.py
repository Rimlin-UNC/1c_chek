"""Удаление интеграции не изменяет отдельные данные и штатный мониторинг."""
import importlib.util
import sqlite3

import pytest

from app.services import server_history
from tests.conftest import login


def test_integration_endpoint_and_module_removed_admin_only_history_unchanged(client, monkeypatch):
    from app.services import local_monitor
    from tests.test_v17050 import _mk_user

    assert importlib.util.find_spec('app.services.node_exporter_monitor') is None
    monkeypatch.setenv('YM_NODE_EXPORTER', '1')
    monkeypatch.setattr(local_monitor, 'snapshot', lambda: {'cpu_pct': 25, 'memory': {'pct': 60}})
    admin = login(client, 'admin', 'admin123')
    assert client.get('/api/v1/admin/server-monitor/node-exporter', headers=admin).status_code == 404
    snap = client.get('/api/v1/admin/server-monitor', headers=admin)
    assert snap.status_code == 200 and snap.json()['cpu_pct'] == 25
    assert 'no-store' in snap.headers['cache-control']
    user = _mk_user(client, 'user', 'remove_node_monitor_test')
    assert client.get('/api/v1/admin/server-monitor', headers=user).status_code == 403
    assert client.get('/api/v1/admin/server-monitor/history?source=node_exporter', headers=user).status_code == 403
    assert client.get('/api/v1/admin/server-monitor/history?source=node_exporter', headers=admin).json()['status'] == 'invalid_source'


def test_existing_history_and_old_exporter_file_are_separate_and_untouched(tmp_path, monkeypatch):
    local = tmp_path / 'local.sqlite3'
    old = tmp_path / 'other.sqlite3'
    server_history.initialize(local)
    with sqlite3.connect(old) as con:
        con.execute('PRAGMA application_id=1498236978')
        assert con.execute('PRAGMA application_id').fetchone()[0] == 0x594D4832
        con.execute('create table important_data(value text)')
        con.execute("insert into important_data values ('сохранить')")
    old.chmod(0o600)
    before = old.read_bytes()
    monkeypatch.setenv('YM_MONITOR_HISTORY_FILE', str(local))
    monkeypatch.setenv('YM_NODE_EXPORTER_HISTORY_FILE', str(old))
    server_history.record(local, {'cpu_pct': 42, 'memory': {'pct': 61}}, '', now=1800)
    with pytest.raises(ValueError):
        server_history.record(old, {'cpu_pct': 0}, '', now=1800)
    server_history.reset_cache()
    assert server_history.snapshot('1h', source='node_exporter')['status'] == 'invalid_source'
    with sqlite3.connect(local) as con:
        assert con.execute('select cpu_pct, memory_pct from samples').fetchone() == (42, 61)
    assert old.read_bytes() == before
