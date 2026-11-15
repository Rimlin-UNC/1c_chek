"""Вывод облачного контура не затрагивает локальный мониторинг и старые данные."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_cloud_routes_or_runtime_dependencies():
    from app.main import app

    paths = app.openapi()['paths']
    assert not any(path.startswith('/api/v1/openstack') for path in paths)
    assert '/api/v1/admin/server-monitor' in paths
    assert not (ROOT / 'app/openstack_monitor').exists()
    requirements = (ROOT / 'requirements.txt').read_text(encoding='utf-8').lower()
    assert 'openstacksdk' not in requirements
    assert 'keystoneauth1' not in requirements
    app_js = (ROOT / 'app/static/js/app.js').read_text(encoding='utf-8')
    assert '/api/v1/openstack' not in app_js
    assert 'osmCardHtml' not in app_js
    assert 'localServerCardHtml' in app_js
    # Пустой совместимый модуль нужен старым закэшированным вкладкам.
    shim = (ROOT / 'app/static/js/osm.js').read_text(encoding='utf-8')
    assert '/api/v1/openstack' not in shim
    assert 'export function osmCardHtml' in shim
    assert 'export function osmStop' in shim
    assert 'server_monitor.js?v=' in shim


def test_upgrade_keeps_legacy_cloud_tables_unchanged(tmp_path):
    db = tmp_path / 'upgrade.sqlite'
    names = ('openstack_settings', 'openstack_metrics', 'openstack_sync_log')
    with sqlite3.connect(db) as con:
        for name in names:
            con.execute(f'CREATE TABLE {name} (id TEXT PRIMARY KEY, payload TEXT)')
            con.execute(f'INSERT INTO {name} VALUES (?, ?)', ('synthetic-id', 'сохранить'))
        before = {name: con.execute(f'SELECT * FROM {name}').fetchall() for name in names}
        schemas = {name: con.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0] for name in names}
    env = dict(os.environ, DATABASE_URL=f'sqlite:///{db}', YM_FETCHQ_OFF='1')
    result = subprocess.run([sys.executable, '-c', 'from app.database import init_db; init_db()'],
                            cwd=ROOT, env=env, text=True, capture_output=True, timeout=90, check=False)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as con:
        for name in names:
            assert con.execute(f'SELECT * FROM {name}').fetchall() == before[name]
            assert con.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0] == schemas[name]
