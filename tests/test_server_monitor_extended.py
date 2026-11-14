"""Расширенные показатели среды Linux: ошибки источников не становятся нулями."""
from app.services import local_monitor as monitor


def test_swap_used_and_absence_are_distinct():
    assert monitor.parse_swap('SwapTotal: 8192 kB\nSwapFree: 2048 kB\n') == {
        'total_bytes': 8388608, 'used_bytes': 6291456, 'free_bytes': 2097152, 'pct': 75.0}
    assert monitor.parse_swap('SwapTotal: 0 kB\nSwapFree: 0 kB\n') == {
        'total_bytes': 0, 'used_bytes': 0, 'free_bytes': 0, 'pct': None}
    assert monitor.parse_swap('SwapTotal: 4 kB\nSwapFree: 5 kB\n') is None
    assert monitor.parse_swap('MemTotal: 4 kB') is None


def test_cpu_breakdown_and_reset():
    a = monitor.parse_cpu_details('cpu 100 0 100 500 10 0 0 10 30 0\n')
    b = monitor.parse_cpu_details('cpu 120 0 110 550 15 0 0 15 40 0\n')
    assert monitor.cpu_breakdown(a, b) == {'user_pct': 22.22, 'system_pct': 11.11,
        'iowait_pct': 5.56, 'steal_pct': 5.56}
    assert monitor.cpu_breakdown(b, a) is None
    assert monitor.cpu_breakdown(a, a) is None
    assert monitor.cpu_breakdown(a, monitor.parse_cpu_details('cpu 120 0 110 550 9 0 0 15\n')) is None
    assert monitor.parse_cpu_details('cpu 1 bad 3 4') is None


def test_network_delta_filters_loopback_rejects_resets_and_preserves_partial():
    before = monitor.parse_network('''Inter-| Receive | Transmit
 lo: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0
 ens3: 1000 10 1 2 0 0 0 0 2000 20 3 4 0 0 0 0
 broken: x
''')
    after = monitor.parse_network('''ens3: 1200 14 2 3 0 0 0 0 2300 26 4 4 0 0 0 0
 new0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
''')
    assert 'lo' not in before and 'broken' not in before
    rates = monitor.network_rates(before, after, 2)
    assert rates['ens3'] == {'rx_bytes_per_sec': 100.0, 'tx_bytes_per_sec': 150.0,
        'rx_packets_per_sec': 2.0, 'tx_packets_per_sec': 3.0,
        'rx_errors_delta': 1, 'tx_errors_delta': 1, 'rx_drops_delta': 1, 'tx_drops_delta': 0}
    assert rates['new0'] is None
    assert monitor.network_rates(after, before, 2)['ens3'] is None
    assert monitor.network_rates(before, after, 0)['ens3'] is None


def test_uptime_and_load_reject_bad_values():
    assert monitor.parse_uptime('1234.56 2000.12\n') == 1234.56
    assert monitor.parse_uptime('-2 1') is None
    assert monitor.parse_uptime('nan 1') is None
    assert monitor.parse_load('0.10 0.20 0.30 1/123 800') == [0.1, 0.2, 0.3]
    assert monitor.parse_load('0.10 bad 0.3') is None


def test_snapshot_handles_unavailable_proc_without_fabrication(monkeypatch):
    monitor.reset_cache()
    monkeypatch.setattr(monitor, '_read_proc', lambda _: '')
    monkeypatch.setattr(monitor, '_cpu_ticks', lambda: None)
    monkeypatch.setattr(monitor, '_memory', lambda: None)
    monkeypatch.setattr(monitor, '_disk', lambda: None)
    result = monitor.snapshot()
    assert result['cpu_pct'] is None and result['cpu_detail'] is None
    assert result['network'] is None and result['swap'] is None
    assert result['load_average'] is None and result['uptime_sec'] is None
    monitor.reset_cache()


def test_inode_usage_does_not_assume_unsupported_or_invalid_is_zero():
    from types import SimpleNamespace

    assert monitor.inode_view(SimpleNamespace(f_files=1000, f_ffree=150, f_favail=100)) == {
        'total': 1000, 'used': 850, 'available': 100, 'pct': 85.0}
    assert monitor.inode_view(SimpleNamespace(f_files=0, f_ffree=0, f_favail=0)) is None
    assert monitor.inode_view(SimpleNamespace(f_files=100, f_ffree=101, f_favail=1)) is None
    assert monitor.inode_view(SimpleNamespace()) is None


def test_loadavg_tasks_and_tcp_sockets_are_not_process_counts_or_established_connections():
    assert monitor.parse_tasks('0.10 0.20 0.30 2/254 122') == {'runnable': 2, 'total': 254}
    assert monitor.parse_tasks('0 0 0 7/6 1') is None
    assert monitor.parse_tasks('0 0 0 1/-2 1') is None
    assert monitor.parse_tasks('') is None
    assert monitor.parse_tcp_sockets('sockets: used 10\nTCP: inuse 8 orphan 0 tw 2 alloc 12 mem 1\n') == 8
    assert monitor.parse_tcp_sockets('TCP: inuse -1 orphan 0\n') is None
    assert monitor.parse_tcp_sockets('TCP: alloc 2\n') is None


def test_block_io_only_exact_device_and_reset_or_unknown_are_unavailable():
    text = ('8 0 sda 100 4 200 30 90 1 150 45 2 60 80 0 0 0 0\n'
            '8 1 sda1 5 0 20 10 8 0 30 15 0 12 12\n')
    first = monitor.parse_diskstats(text, (8, 1))
    assert first['name'] == 'sda1'
    assert monitor.parse_diskstats(text, (9, 9)) is None
    assert monitor.parse_diskstats('8 1 sda1 malformed', (8, 1)) is None
    updated = monitor.parse_diskstats('8 1 sda1 15 0 60 30 28 0 80 55 0 112 112', (8, 1))
    assert monitor.disk_rates(first, updated, 2) == {'device': 'sda1', 'read_iops': 5.0,
        'write_iops': 10.0, 'read_bytes_per_sec': 10240.0, 'write_bytes_per_sec': 12800.0,
        'read_await_ms': 2.0, 'write_await_ms': 2.0, 'busy_pct': 5.0}
    assert monitor.disk_rates(updated, first, 2) is None
    assert monitor.disk_rates(first, updated, 0) is None
    assert monitor.disk_rates(first, {**updated, 'name': 'other'}, 2) is None


def test_disk_io_no_operations_has_unknown_await_and_excess_busy_is_rejected():
    stat = monitor.parse_diskstats('8 1 sda1 10 0 20 30 40 0 50 60 0 100 100', (8, 1))
    no_change = monitor.disk_rates(stat, stat, 1)
    assert no_change['read_iops'] == no_change['write_iops'] == 0
    assert no_change['read_await_ms'] is None and no_change['write_await_ms'] is None
    later = monitor.parse_diskstats('8 1 sda1 10 0 20 30 40 0 50 60 0 2200 2200', (8, 1))
    assert monitor.disk_rates(stat, later, 1) is None


def test_inode_data_and_exact_device_sample_are_optional_and_fail_closed(monkeypatch):
    from types import SimpleNamespace

    stat = SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=100, f_bfree=40,
                           f_bavail=30, f_files=1000, f_ffree=150, f_favail=100)
    monkeypatch.setattr(monitor.os, 'statvfs', lambda _: stat)
    disk = monitor._disk()
    assert disk['pct'] == 60 and disk['inodes']['pct'] == 85
    monkeypatch.setattr(monitor.os, 'stat', lambda _: (_ for _ in ()).throw(PermissionError()))
    assert monitor._block_device() is None


def test_context_switch_and_swap_io_rates_reject_bad_or_reset_counters():
    stat_a = monitor.parse_context_switches('cpu 1 2 3 4\nctxt 1000\n')
    stat_b = monitor.parse_context_switches('cpu 2 3 4 5\nctxt 1100\n')
    assert monitor.counter_rate(stat_a, stat_b, 2) == 50.0
    assert monitor.counter_rate(stat_b, stat_a, 2) is None
    assert monitor.parse_context_switches('ctxt -1') is None
    assert monitor.parse_context_switches('') is None
    a = monitor.parse_swap_io('pgpgin 1\npswpin 12\npswpout 30\n')
    b = monitor.parse_swap_io('pswpin 16\npswpout 34\n')
    assert monitor.swap_io_rates(a, b, 2) == {'in_pages_per_sec': 2.0, 'out_pages_per_sec': 2.0}
    assert monitor.swap_io_rates(b, a, 2) is None
    assert monitor.parse_swap_io('pswpin -1\npswpout 4') is None
    assert monitor.parse_swap_io('pswpin 1') is None


def test_application_filesystem_timeout_is_bounded_and_never_duplicates(monkeypatch):
    import threading
    import time

    gate = threading.Event()
    entered = []
    def stuck():
        entered.append('stat')
        gate.wait(3)
        raise OSError('недоступно')
    monkeypatch.setattr(monitor, '_stat_path', stuck)
    monkeypatch.setattr(monitor, '_FS_TIMEOUT', .05)
    try:
        start = time.monotonic()
        assert monitor._block_device() is None
        assert monitor._disk() is None
        assert monitor._block_device() is None
        assert time.monotonic() - start < .5
        assert entered == ['stat']
    finally:
        with monitor._fs_lock:
            worker = monitor._fs_inflight['thread'] if monitor._fs_inflight else None
        gate.set()
        if worker:
            worker.join(2)


def test_application_filesystem_recovers_after_timeout(monkeypatch):
    import threading
    from types import SimpleNamespace

    gate = threading.Event()
    def stuck():
        gate.wait(3)
        return SimpleNamespace(st_dev=0)
    monkeypatch.setattr(monitor, '_stat_path', stuck)
    monkeypatch.setattr(monitor, '_FS_TIMEOUT', .05)
    assert monitor._block_device() is None
    with monitor._fs_lock:
        worker = monitor._fs_inflight['thread']
    gate.set(); worker.join(2)
    monkeypatch.setattr(monitor, '_stat_path', lambda: SimpleNamespace(st_dev=0))
    assert monitor._block_device() == (0, 0)


def test_snapshot_remains_usable_when_application_statvfs_hangs(monkeypatch):
    import threading
    import time

    gate = threading.Event()
    entered = []
    def stuck():
        entered.append('statvfs')
        gate.wait(3)
        raise OSError('недоступно')
    monkeypatch.setattr(monitor, '_statvfs_path', stuck)
    monkeypatch.setattr(monitor, '_FS_TIMEOUT', .05)
    monitor.reset_cache()
    try:
        start = time.monotonic()
        data = monitor.snapshot()
        assert time.monotonic() - start < 2
        assert data['disk'] is None and data['collected_at']
        monitor.reset_cache()
        assert monitor.snapshot()['disk'] is None
        assert entered == ['statvfs']
    finally:
        with monitor._fs_lock:
            worker = monitor._fs_inflight['thread'] if monitor._fs_inflight else None
        gate.set()
        if worker:
            worker.join(2)
        monitor.reset_cache()
