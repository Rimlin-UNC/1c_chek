"""Необязательная отдельная история: без вмешательства в рабочую базу и без ложного заполнения пропусков."""
import sqlite3

import pytest

from app.services import server_history as history


def sample(cpu=20, ram=30, swap=None, rx=100, tx=200):
    return {'cpu_pct': cpu, 'memory': {'pct': ram}, 'swap': {'pct': swap},
            'network': {'ens3': {'rx_bytes_per_sec': rx, 'tx_bytes_per_sec': tx}}}


def test_initialization_never_overwrites_existing_file_or_primary_database(tmp_path):
    db = tmp_path / 'history.db'
    db.write_bytes(b'important user data')
    with pytest.raises(FileExistsError):
        history.initialize(db)
    assert db.read_bytes() == b'important user data'
    original = tmp_path / 'app.db'
    with sqlite3.connect(original) as connection:
        connection.execute('create table receipts(id integer primary key)')
        connection.execute('insert into receipts values (42)')
    before = original.read_bytes()
    with pytest.raises(ValueError):
        history.record(original, sample(), 'ens3', now=1800)
    assert original.read_bytes() == before
    link = tmp_path / 'link.db'
    link.symlink_to(original)
    with pytest.raises((ValueError, OSError)):
        history.record(link, sample(), 'ens3', now=1800)
    assert original.read_bytes() == before


def test_slots_gaps_unknown_values_interface_and_duplicate_are_explicit(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, sample(), 'ens3', now=3600)
    history.record(db, sample(cpu=99), 'ens3', now=3601)
    history.record(db, sample(cpu=None, ram=40), 'other', now=3720)
    result = history.read(db, '1h', interface='ens3', now=3780)
    assert result['status'] == 'available'
    slots = {row['timestamp']: row for row in result['buckets']}
    assert slots[3600]['cpu_pct'] == 20 and slots[3600]['rx_bytes_per_sec'] == 100
    assert slots[3660]['samples'] == 0 and slots[3660]['cpu_pct'] is None
    assert slots[3720]['cpu_pct'] is None and slots[3720]['memory_pct'] == 40
    assert slots[3720]['rx_bytes_per_sec'] is None
    assert slots[3600]['samples'] == 1


def test_retention_is_scoped_to_separate_history_and_invalid_metrics_are_null(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    now = 50 * 86400
    history.record(db, sample(), 'ens3', now=now - 31 * 86400)
    with sqlite3.connect(db) as connection:
        connection.execute('insert into samples (timestamp) values (?)', (now - 60,))
    history.record(db, sample(cpu=-1, ram=101, rx=-5), 'ens3', now=now)
    with sqlite3.connect(db) as connection:
        rows = connection.execute('select timestamp,cpu_pct,memory_pct,rx_bytes_per_sec from samples order by timestamp').fetchall()
    assert rows == [(now - 60, None, None, None), (now, None, None, None)]


def test_read_only_handles_disabled_missing_unknown_range_and_wrong_database(tmp_path, monkeypatch):
    monkeypatch.setattr(history, '_configured_path', lambda: None)
    assert history.snapshot('1h')['status'] == 'not_configured'
    missing = tmp_path / 'absent.db'
    monkeypatch.setattr(history, '_configured_path', lambda: missing)
    assert history.snapshot('1h')['status'] == 'unavailable'
    assert not missing.exists()
    assert history.snapshot('2h')['status'] == 'invalid_range'
    with sqlite3.connect(missing) as connection:
        connection.execute('create table customer_data(id int)')
    before = missing.read_bytes()
    assert history.snapshot('1h')['status'] == 'unavailable'
    assert missing.read_bytes() == before


def test_endpoint_is_admin_only_and_no_store(client, monkeypatch):
    from tests.conftest import login
    from tests.test_v17050 import _mk_user

    monkeypatch.setattr(history, 'snapshot', lambda period: {'status': 'not_configured'})
    admin = login(client, 'admin', 'admin123')
    result = client.get('/api/v1/admin/server-monitor/history?period=1h', headers=admin)
    assert result.status_code == 200 and result.json()['status'] == 'not_configured'
    assert 'no-store' in result.headers['cache-control']
    user = _mk_user(client, 'user', 'history_monitor_test_user')
    assert client.get('/api/v1/admin/server-monitor/history', headers=user).status_code == 403
    saved = list(client.cookies.jar)
    client.cookies.clear()
    try:
        assert client.get('/api/v1/admin/server-monitor/history').status_code == 401
    finally:
        for cookie in saved:
            client.cookies.jar.set_cookie(cookie)


def test_large_gap_and_backward_clock_do_not_delete_history(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, sample(), 'ens3', now=3600)
    history.record(db, sample(), 'ens3', now=40 * 86400)
    result = history.read(db, '30d', interface='ens3', now=40 * 86400)
    assert result['retention_paused'] is True
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 2
    with pytest.raises(ValueError, match='время'):
        history.record(db, sample(), 'ens3', now=3600)
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 2


def test_paused_history_stops_at_bound_and_never_deletes(tmp_path, monkeypatch):
    db = tmp_path / 'history.db'
    history.initialize(db)
    monkeypatch.setattr(history, '_MAX_ROWS', 2)
    history.record(db, sample(), 'ens3', now=3600)
    history.record(db, sample(), 'ens3', now=5000)
    with pytest.raises(ValueError, match='предел'):
        history.record(db, sample(), 'ens3', now=5100)
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 2


def test_explicit_resume_after_gap_waits_for_next_collection_before_cleanup(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, sample(), 'ens3', now=3600)
    history.record(db, sample(), 'ens3', now=40 * 86400)
    with pytest.raises(ValueError):
        history.resume_retention(db, now=3600)
    history.resume_retention(db, now=40 * 86400 + 7200)
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 2
    history.record(db, sample(), 'ens3', now=40 * 86400 + 7200)
    result = history.read(db, '1h', interface='ens3', now=40 * 86400 + 7200)
    assert result['retention_paused'] is False
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from samples').fetchone()[0] == 2


def test_metric_coverage_and_partial_bucket_do_not_hide_missing_values(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, sample(cpu=None, ram=40), 'ens3', now=86400)
    history.record(db, sample(cpu=20, ram=40), 'ens3', now=86460)
    result = history.read(db, '24h', interface='ens3', now=86460)
    bucket = next(row for row in result['buckets'] if row['timestamp'] == 86400)
    assert bucket['samples'] == 2 and bucket['expected'] == 2
    assert bucket['cpu_samples'] == 1 and bucket['memory_samples'] == 2
    assert bucket['cpu_pct'] == 20 and bucket['memory_pct'] == 40


def test_file_security_and_single_collector_sample_per_minute(tmp_path):
    import os

    db = tmp_path / 'history.db'
    history.initialize(db)
    assert db.stat().st_mode & 0o777 == 0o600
    linked = tmp_path / 'linked.db'
    os.link(db, linked)
    with pytest.raises(ValueError):
        history.record(db, sample(), 'ens3', now=1800)
    linked.unlink()
    history.record(db, sample(), 'ens3', now=1800)
    history.record(db, sample(cpu=80), 'ens3', now=1830)
    with sqlite3.connect(db) as con:
        assert con.execute('select cpu_pct from samples').fetchall() == [(20,)]
    with pytest.raises(ValueError):
        history.initialize(history.Path('relative.db'))


def test_history_rejects_shared_directory_and_insecure_file_permissions(tmp_path):
    folder = tmp_path / 'shared'
    folder.mkdir(mode=0o755)
    db = folder / 'history.db'
    with pytest.raises(ValueError, match='каталог'):
        history.initialize(db)
    assert not db.exists()
    folder.chmod(0o700)
    history.initialize(db)
    db.chmod(0o644)
    with pytest.raises(ValueError, match='права'):
        history.record(db, sample(), 'ens3', now=1800)


def test_read_warns_about_gap_before_next_collector_and_backwards_clock(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, sample(), 'ens3', now=3600)
    data = history.read(db, '1h', interface='ens3', now=6000)
    assert data['collection_gap'] is True and data['retention_paused'] is False
    data = history.read(db, '1h', interface='ens3', now=1800)
    assert data['clock_backwards'] is True


def test_static_systemd_example_parses_and_launches_only_opt_in_collector():
    from pathlib import Path

    service = Path('deploy/systemd/ymaster-monitor-history.service').read_text()
    timer = Path('deploy/systemd/ymaster-monitor-history.timer').read_text()
    assert 'User=ymaster' in service and '-m app.services.server_history collect' in service
    assert 'Persistent=false' in timer and 'OnCalendar=*-*-* *:*:00' in timer
