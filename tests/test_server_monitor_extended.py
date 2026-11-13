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
