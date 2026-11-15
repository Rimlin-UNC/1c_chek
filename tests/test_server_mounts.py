"""Ограниченный ручной обзор монтирований без обращения к сетевым ФС."""
import threading
import time
from types import SimpleNamespace

from app.services import server_mounts as mounts


def line(mount_id, path, fs='ext4', device='8:1', source='/dev/sda1'):
    return f'{mount_id} 1 {device} / {path} rw,relatime - {fs} {source} rw\n'


def test_parse_mountinfo_escapes_and_does_not_expose_source():
    text = line(10, '/srv/with\\040space') + line(11, '/mnt/network', 'nfs4', '0:41', 'user:secret@host:/private')
    items, partial = mounts.parse_mountinfo(text)
    assert partial is False
    assert items[0]['path'] == '/srv/with space' and items[0]['type'] == 'ext4'
    assert items[1]['path'] == '/mnt/network' and items[1]['type'] == 'nfs4'
    assert all('source' not in item for item in items)
    assert 'secret' not in str(items)


def test_skips_remote_pseudo_overmount_automount_and_nested_automount(monkeypatch):
    text = (line(10, '/', 'ext4') + line(11, '/srv', 'ext4') + line(12, '/srv', 'nfs4')
            + line(13, '/net', 'autofs') + line(14, '/net/hidden', 'ext4')
            + line(15, '/sys', 'sysfs') + line(16, '/media', 'fuse.sshfs'))
    items, _ = mounts.parse_mountinfo(text)
    called = []
    monkeypatch.setattr(mounts.os, 'statvfs', lambda p: called.append(p) or SimpleNamespace(
        f_frsize=4096, f_bsize=4096, f_blocks=100, f_bfree=20, f_bavail=10,
        f_files=100, f_ffree=20, f_favail=20))
    result = mounts.probe(items, deadline_seconds=1)
    assert called == ['/']
    assert next(row for row in result if row['path'] == '/')['disk']['pct'] == 80
    for row in result:
        if row['path'] != '/':
            assert row['status'] == 'not_probed' and row['disk'] is None


def test_bad_or_oversized_mount_table_is_partial():
    rows, partial = mounts.parse_mountinfo(line(1, '/') + 'bad line\n')
    assert partial and len(rows) == 1
    rows, partial = mounts.parse_mountinfo(''.join(line(i, f'/mnt/{i}') for i in range(100)))
    assert partial and len(rows) == mounts.MAX_MOUNTS
    rows, partial = mounts.parse_mountinfo('')
    assert partial and rows == []


def test_hung_calls_are_bounded_process_wide_and_not_duplicated(monkeypatch):
    gate = threading.Event()
    entered = []
    def hung(path):
        entered.append(path)
        gate.wait(3)
        raise OSError('недоступно')
    monkeypatch.setattr(mounts.os, 'statvfs', hung)
    items, _ = mounts.parse_mountinfo(''.join(line(i, f'/mnt/{i}') for i in range(12)))
    try:
        start = time.monotonic()
        first = mounts.probe(items, deadline_seconds=0.05)
        assert time.monotonic() - start < 0.5
        assert len(entered) <= mounts.MAX_INFLIGHT
        second = mounts.probe(items, deadline_seconds=0.05)
        assert len(entered) <= mounts.MAX_INFLIGHT
        assert all(row['disk'] is None for row in first + second)
        assert any(row['status'] == 'timeout' for row in first)
    finally:
        with mounts._lock:
            workers = [entry['thread'] for entry in mounts._inflight.values()]
        gate.set()
        for worker in workers:
            worker.join(2)


def test_endpoint_admin_only_and_no_store(client, monkeypatch):
    from tests.conftest import login
    from tests.test_v17050 import _mk_user

    monkeypatch.setattr(mounts, 'snapshot', lambda: {'status': 'available', 'mounts': [], 'partial': True})
    admin = login(client, 'admin', 'admin123')
    response = client.get('/api/v1/admin/server-monitor/mounts', headers=admin)
    assert response.status_code == 200 and response.json()['partial'] is True
    assert 'no-store' in response.headers['cache-control']
    user = _mk_user(client, 'user', 'mount_test_user')
    assert client.get('/api/v1/admin/server-monitor/mounts', headers=user).status_code == 403
    client.cookies.clear()
    assert client.get('/api/v1/admin/server-monitor/mounts').status_code == 401


def test_all_responsive_local_mounts_within_budget_can_be_read(monkeypatch):
    items, _ = mounts.parse_mountinfo(''.join(line(i, f'/volume/{i}') for i in range(8)))
    called = []
    def fast(path):
        time.sleep(.01)
        called.append(path)
        return SimpleNamespace(f_frsize=1, f_bsize=1, f_blocks=100, f_bfree=10, f_bavail=5,
                               f_files=10, f_ffree=1, f_favail=1)
    monkeypatch.setattr(mounts.os, 'statvfs', fast)
    result = mounts.probe(items, deadline_seconds=1)
    assert len(called) == 8 and all(row['status'] == 'available' for row in result)


def test_descendants_of_remote_and_overmounted_ancestors_are_not_probed(monkeypatch):
    text = (line(1, '/data', 'nfs4') + line(2, '/data/disk', 'ext4')
            + line(3, '/srv', 'ext4') + line(4, '/srv', 'ext4') + line(5, '/srv/disk', 'ext4'))
    items, _ = mounts.parse_mountinfo(text)
    called = []
    monkeypatch.setattr(mounts.os, 'statvfs', lambda path: called.append(path))
    mounts.probe(items, deadline_seconds=.1)
    assert not called


def test_malformed_or_truncated_input_disables_probes_entirely(monkeypatch):
    calls = []
    monkeypatch.setattr(mounts.os, 'statvfs', lambda path: calls.append(path))
    for text in (line(1, '/') + 'broken\n', line(1, '/') + '\n'.join('bad' for _ in range(2050))):
        items, partial = mounts.parse_mountinfo(text)
        assert partial
        result = mounts.probe(items, deadline_seconds=.1)
        assert all(row['status'] == 'not_probed' for row in result)
    assert calls == []


def test_inaccessible_file_system_and_unknown_inodes_are_not_zero(monkeypatch):
    items, _ = mounts.parse_mountinfo(line(1, '/a') + line(2, '/b'))
    def stat(path):
        if path == '/a':
            raise PermissionError('denied')
        return SimpleNamespace(f_frsize=1, f_bsize=1, f_blocks=100, f_bfree=20, f_bavail=10,
                               f_files=0, f_ffree=0, f_favail=0)
    monkeypatch.setattr(mounts.os, 'statvfs', stat)
    rows = mounts.probe(items)
    assert rows[0]['status'] == 'unavailable' and rows[0]['disk'] is None
    assert rows[1]['status'] == 'available' and rows[1]['inodes'] is None


def test_snapshot_has_time_and_caches_the_result(monkeypatch):
    mounts._cached = None
    monkeypatch.setattr(mounts, 'parse_mountinfo', lambda _: ([], True))
    try:
        first = mounts.snapshot()
        second = mounts.snapshot()
        assert first is second and first['collected_at'].endswith('+00:00')
    finally:
        mounts._cached = None
