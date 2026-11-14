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


def extra_sample(device='sda1', load=1.25, read=5, write=10, await_read=2, await_write=4, busy=8):
    return {**sample(), 'load_average': [load, 1, 1],
            'disk_io': {'device': device, 'read_iops': read, 'write_iops': write,
                        'read_await_ms': await_read, 'write_await_ms': await_write, 'busy_pct': busy}}


def test_new_history_adds_load_and_io_without_changing_old_table_schema(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, extra_sample(), 'ens3', now=3600)
    with sqlite3.connect(db) as con:
        columns = [row[1] for row in con.execute('pragma table_info(samples)')]
        assert columns == ['timestamp', 'cpu_pct', 'memory_pct', 'swap_pct', 'interface', 'rx_bytes_per_sec', 'tx_bytes_per_sec']
        # Буквально такой INSERT использовала версия 1.72.25; доп. таблица его не меняет.
        con.execute('INSERT OR IGNORE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (3660, 33, 44, None, 'ens3', 1, 2))
    result = history.read(db, '1h', interface='ens3', now=3660)
    assert result['extra_status'] == 'available'
    rows = {r['timestamp']: r for r in result['buckets']}
    assert rows[3600]['load_1'] == 1.25 and rows[3600]['load_samples'] == 1
    assert rows[3600]['read_iops'] == 5 and rows[3600]['write_await_ms'] == 4
    assert rows[3600]['io_device'] == 'sda1' and rows[3600]['io_samples'] == 1
    assert rows[3660]['cpu_pct'] == 33 and rows[3660]['load_1'] is None
    assert rows[3660]['read_iops'] is None and rows[3660]['io_samples'] == 0


def test_enable_extra_is_explicit_idempotent_and_preserves_preexisting_history(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    with sqlite3.connect(db) as con:
        con.execute('drop table extra_samples')
    history.record(db, extra_sample(), 'ens3', now=3600)
    before = history.read(db, '1h', interface='ens3', now=3600)
    assert before['extra_status'] == 'not_enabled'
    history.enable_extra(db)
    history.enable_extra(db)
    after = history.read(db, '1h', interface='ens3', now=3600)
    assert after['extra_status'] == 'available'
    assert next(r for r in after['buckets'] if r['timestamp'] == 3600)['cpu_pct'] == 20
    assert next(r for r in after['buckets'] if r['timestamp'] == 3600)['load_1'] is None
    history.record(db, extra_sample(), 'ens3', now=3660)
    assert next(r for r in history.read(db, '1h', interface='ens3', now=3660)['buckets']
                if r['timestamp'] == 3660)['load_1'] == 1.25


def test_device_changes_within_bucket_are_not_averaged_and_no_false_zero(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    history.record(db, extra_sample(device='sda1'), 'ens3', now=86400)
    history.record(db, extra_sample(device='sdb1'), 'ens3', now=86460)
    bucket = next(r for r in history.read(db, '24h', interface='ens3', now=86460)['buckets']
                  if r['timestamp'] == 86400)
    assert bucket['load_1'] == 1.25 and bucket['load_samples'] == 2
    assert bucket['io_device'] is None and bucket['read_iops'] is None
    assert bucket['io_mixed'] is True


def test_extra_prunes_together_and_gap_does_not_delete(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    now = 50 * 86400
    history.record(db, extra_sample(), 'ens3', now=now - 31 * 86400)
    history.record(db, extra_sample(), 'ens3', now=now)
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from extra_samples').fetchone()[0] == 2
    history.resume_retention(db, now=now + 60)
    history.record(db, extra_sample(), 'ens3', now=now + 60)
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from extra_samples').fetchone()[0] == 2


def test_invalid_extra_values_are_unknown_and_do_not_block_base_history(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    data = {**sample(), 'load_average': [-1, 0, 0], 'disk_io': ['bad']}
    history.record(db, data, 'ens3', now=3600)
    row = next(r for r in history.read(db, '1h', interface='ens3', now=3600)['buckets'] if r['timestamp'] == 3600)
    assert row['cpu_pct'] == 20 and row['load_1'] is None and row['read_iops'] is None


def test_extra_collision_does_not_change_old_row_or_stop_base_collection(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    with sqlite3.connect(db) as con:
        con.execute('drop table extra_samples')
        con.execute('create table extra_samples (important text)')
        con.execute("insert into extra_samples values ('keep')")
    with pytest.raises(ValueError):
        history.enable_extra(db)
    history.record(db, extra_sample(), 'ens3', now=3600)
    result = history.read(db, '1h', interface='ens3', now=3600)
    assert result['extra_status'] == 'unavailable'
    assert next(row for row in result['buckets'] if row['timestamp'] == 3600)['cpu_pct'] == 20
    with sqlite3.connect(db) as con:
        assert con.execute('select * from extra_samples').fetchall() == [('keep',)]


def test_duplicate_minute_does_not_create_extra_for_old_base_sample(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    with sqlite3.connect(db) as con:
        con.execute('INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)', (3600, 50, 60, None, 'ens3', 1, 2))
    history.record(db, extra_sample(), 'ens3', now=3600)
    row = next(r for r in history.read(db, '1h', interface='ens3', now=3600)['buckets'] if r['timestamp'] == 3600)
    assert row['cpu_pct'] == 50 and row['load_1'] is None


def test_extra_history_has_independent_row_limit_without_blocking_base(tmp_path, monkeypatch):
    db = tmp_path / 'history.db'
    history.initialize(db)
    monkeypatch.setattr(history, '_MAX_EXTRA_ROWS', 2)
    history.record(db, extra_sample(), 'ens3', now=3600)
    history.record(db, extra_sample(), 'ens3', now=3660)
    history.record(db, extra_sample(), 'ens3', now=3720)
    result = history.read(db, '1h', interface='ens3', now=3720)
    assert result['extra_full'] is True
    row = next(r for r in result['buckets'] if r['timestamp'] == 3720)
    assert row['cpu_pct'] == 20 and row['load_1'] is None
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from extra_samples').fetchone()[0] == 2


def test_concurrent_explicit_enable_preserves_history_and_succeeds(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / 'history.db'
    history.initialize(db)
    with sqlite3.connect(db) as con:
        con.execute('drop table extra_samples')
    history.record(db, sample(), 'ens3', now=3600)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(history.enable_extra, db) for _ in range(2)]
        for future in futures:
            future.result()
    assert history.read(db, '1h', interface='ens3', now=3600)['extra_status'] == 'available'


def test_extra_foreign_constraints_and_triggers_never_modify_existing_rows(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    with sqlite3.connect(db) as con:
        con.execute('drop table extra_samples')
        con.execute('''CREATE TABLE extra_samples (timestamp INTEGER PRIMARY KEY, load_1 REAL NOT NULL,
            io_device TEXT, read_iops REAL, write_iops REAL, read_await_ms REAL,
            write_await_ms REAL, io_busy_pct REAL)''')
    history.record(db, sample(), 'ens3', now=3600)
    assert history.read(db, '1h', interface='ens3', now=3600)['extra_status'] == 'unavailable'
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from extra_samples').fetchone()[0] == 0
        assert con.execute('select cpu_pct from samples').fetchall() == [(20,)]


def test_extra_unexpected_trigger_is_not_executed_and_base_continues(tmp_path):
    db = tmp_path / 'history.db'
    history.initialize(db)
    with sqlite3.connect(db) as con:
        con.execute('create table untouched(value integer)')
        con.execute('''create trigger unexpected after insert on extra_samples begin
            insert into untouched values (1); end''')
    history.record(db, extra_sample(), 'ens3', now=3600)
    result = history.read(db, '1h', interface='ens3', now=3600)
    assert result['extra_status'] == 'unavailable'
    with sqlite3.connect(db) as con:
        assert con.execute('select count(*) from untouched').fetchone()[0] == 0
        assert con.execute('select count(*) from samples').fetchone()[0] == 1
