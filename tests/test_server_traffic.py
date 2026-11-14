"""Безопасный чтение статистики vnStat: неизвестный или неполный период не является нулём."""
from datetime import datetime

from app.services import server_traffic as traffic


def document(interface='ens3'):
    return {'jsonversion': '2', 'interfaces': [{'name': interface,
        'created': {'date': {'year': 2026, 'month': 10, 'day': 17}},
        'updated': {'date': {'year': 2026, 'month': 11, 'day': 8}, 'time': {'hour': 12, 'minute': 0}, 'timestamp': 1794139200},
        'traffic': {'month': [{'date': {'year': 2026, 'month': 11}, 'rx': 400, 'tx': 600},
                              {'date': {'year': 2026, 'month': 10}, 'rx': 10, 'tx': 20}],
                    'day': [{'date': {'year': 2026, 'month': 11, 'day': 8}, 'rx': 40, 'tx': 60}]}}]}


def test_parse_returns_observed_values_not_billing_or_completeness():
    parsed = traffic.parse(document(), 'ens3')
    assert parsed['interface'] == 'ens3'
    assert parsed['created_date'] == '2026-10-17'
    assert parsed['updated_at'] == '2026-11-08T12:00:00'
    assert parsed['months'] == [{'period': '2026-11', 'rx_bytes': 400, 'tx_bytes': 600},
                                {'period': '2026-10', 'rx_bytes': 10, 'tx_bytes': 20}]
    assert parsed['days'] == [{'date': '2026-11-08', 'rx_bytes': 40, 'tx_bytes': 60}]
    assert parsed['source'] == 'vnstat' and parsed['billing_verified'] is False
    assert 'remaining' not in parsed and 'forecast' not in parsed


def test_refuses_bad_schema_multiple_or_wrong_interfaces_and_corrupt_values():
    assert traffic.parse({'jsonversion': '1'}, 'ens3') is None
    assert traffic.parse(document('eth0'), 'ens3') is None
    doc = document(); doc['interfaces'] *= 2
    assert traffic.parse(doc, 'ens3') is None
    doc = document(); doc['interfaces'][0]['traffic']['month'][0]['rx'] = -1
    assert traffic.parse(doc, 'ens3') is None
    doc = document(); doc['interfaces'][0]['traffic']['day'][0]['date']['day'] = 40
    assert traffic.parse(doc, 'ens3') is None


def test_disabled_missing_source_timeout_invalid_interface_and_cache(monkeypatch):
    traffic.reset_cache()
    monkeypatch.delenv('YM_MONITOR_TRAFFIC_INTERFACE', raising=False)
    assert traffic.snapshot()['status'] == 'not_configured'
    monkeypatch.setenv('YM_MONITOR_TRAFFIC_INTERFACE', '--bad')
    assert traffic.snapshot()['status'] == 'invalid_interface'
    monkeypatch.setenv('YM_MONITOR_TRAFFIC_INTERFACE', 'ens3')
    monkeypatch.setattr(traffic.shutil, 'which', lambda _: None)
    assert traffic.snapshot()['status'] == 'not_installed'
    traffic.reset_cache()
    monkeypatch.setattr(traffic.shutil, 'which', lambda _: '/usr/bin/vnstat')
    calls = []
    def mock_read(*args):
        calls.append(args)
        return document()
    monkeypatch.setattr(traffic, '_read_json', mock_read)
    monkeypatch.setattr(traffic, '_now', lambda: datetime(2026, 11, 8, 12, 1))
    result = traffic.snapshot()
    assert result['status'] == 'freshness_unknown' and result['data']['interface'] == 'ens3'
    assert calls == [('/usr/bin/vnstat', 'm', '3', 'ens3'), ('/usr/bin/vnstat', 'd', '31', 'ens3')]
    assert traffic.snapshot() == result and len(calls) == 2
    traffic.reset_cache()
    monkeypatch.setattr(traffic, '_read_json', lambda *a: (_ for _ in ()).throw(TimeoutError()))
    assert traffic.snapshot()['status'] == 'unavailable'
    traffic.reset_cache()


def test_traffic_endpoint_only_admin_and_no_store(client, monkeypatch):
    from tests.conftest import login
    from tests.test_v17050 import _mk_user

    monkeypatch.setattr(traffic, 'snapshot', lambda: {'status': 'not_configured'})
    admin = login(client, 'admin', 'admin123')
    response = client.get('/api/v1/admin/server-monitor/traffic', headers=admin)
    assert response.status_code == 200 and response.json() == {'status': 'not_configured'}
    assert 'no-store' in response.headers['cache-control']
    user = _mk_user(client, 'user', 'traffic_endpoint_user')
    assert client.get('/api/v1/admin/server-monitor/traffic', headers=user).status_code == 403
    saved = list(client.cookies.jar)
    client.cookies.clear()
    try:
        assert client.get('/api/v1/admin/server-monitor/traffic').status_code == 401
    finally:
        for cookie in saved:
            client.cookies.jar.set_cookie(cookie)


def test_stale_and_changed_source_are_not_reported_fresh(monkeypatch):
    monkeypatch.setenv('YM_MONITOR_TRAFFIC_INTERFACE', 'ens3')
    monkeypatch.setattr(traffic.shutil, 'which', lambda _: '/usr/bin/vnstat')
    monkeypatch.setattr(traffic, '_now', lambda: datetime(2026, 11, 8, 13, 0))
    monkeypatch.setattr(traffic, '_read_json', lambda *a: document())
    traffic.reset_cache()
    assert traffic.snapshot()['status'] == 'freshness_unknown'
    traffic.reset_cache()
    docs = iter([document(), document('other')])
    monkeypatch.setattr(traffic, '_read_json', lambda *a: next(docs))
    assert traffic.snapshot()['status'] == 'unavailable'
    traffic.reset_cache()


def test_oversized_counter_is_rejected_before_javascript_rounding():
    doc = document()
    doc['interfaces'][0]['traffic']['month'][0]['rx'] = 2**53
    assert traffic.parse(doc, 'ens3') is None


def test_real_vnstat_212_synthetic_json_and_unknown_freshness(monkeypatch):
    import json
    from pathlib import Path

    folder = Path(__file__).parent / 'fixtures' / 'vnstat_2_12'
    docs = [json.loads((folder / name).read_text()) for name in ('month.json', 'day.json')]
    monkeypatch.setenv('YM_MONITOR_TRAFFIC_INTERFACE', 'eth0')
    monkeypatch.setattr(traffic.shutil, 'which', lambda _: '/usr/bin/vnstat')
    monkeypatch.setattr(traffic, '_read_json', lambda *args: docs.pop(0))
    traffic.reset_cache()
    result = traffic.snapshot()
    assert result['status'] == 'freshness_unknown'
    assert result['data']['updated_at'] == '2026-11-14T03:13:00'
    assert result['data']['months'][0]['rx_bytes'] == 400000000000
    assert result['data']['days'][0]['tx_bytes'] == 450000000000
    traffic.reset_cache()


def test_bounded_subprocess_output_and_timeout(tmp_path, monkeypatch):
    import subprocess

    import pytest

    path = tmp_path / 'vnstat'
    path.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.write("x" * 20000)\n')
    path.chmod(0o700)
    monkeypatch.setattr(traffic, '_MAX_OUTPUT', 1024)
    with pytest.raises(ValueError, match='большой'):
        traffic._read_json(str(path), 'm', '3', 'ens3')
    path.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        traffic._read_json(str(path), 'm', '3', 'ens3')


def test_vnstat_timestamp_depends_on_client_timezone():
    import json
    from pathlib import Path

    folder = Path(__file__).parent / 'fixtures' / 'vnstat_2_12'
    local = json.loads((folder / 'month.json').read_text())['interfaces'][0]['updated']
    utc = json.loads((folder / 'month_utc.json').read_text())['interfaces'][0]['updated']
    assert local['date'] == utc['date'] and local['time'] == utc['time']
    assert local['timestamp'] != utc['timestamp']


def scenario_data(day='2026-11-08', created='2026-10-01', rx=100, tx=100):
    from datetime import date, timedelta

    today = date.fromisoformat(day)
    return {'created_date': created, 'updated_at': f'{day}T12:00:00', 'billing_verified': False,
            'months': [{'period': today.strftime('%Y-%m'), 'rx_bytes': 1000, 'tx_bytes': 1000}],
            'days': [{'date': (today - timedelta(days=n)).isoformat(), 'rx_bytes': rx, 'tx_bytes': tx}
                     for n in range(1, 8)]}


def test_conditional_scenario_uses_seven_complete_days_and_source_clock():
    data = scenario_data()
    result = traffic.scenario(data, datetime(2026, 11, 8, 13, 0))
    # 2000 байт уже наблюдено + 200 байт/день × 22,5 дня, без округления в ГБ.
    assert result['projected_bytes'] == 6500
    assert result['sample_days'] == 7 and result['as_of'] == '2026-11-08T12:00:00'
    assert result['remaining_minutes'] == 22 * 1440 + 720
    assert result['billing_verified'] is False and result['completeness_verified'] is False


def test_conditional_scenario_fails_closed_for_missing_inconsistent_or_old_data():
    import copy

    now = datetime(2026, 11, 8, 13, 0)
    data = scenario_data()
    for bad in ([], data['days'][:-1], data['days'] + data['days'][:1]):
        copydata = copy.deepcopy(data); copydata['days'] = bad
        assert traffic.scenario(copydata, now) is None
    copydata = copy.deepcopy(data); copydata['created_date'] = '2026-11-01'
    assert traffic.scenario(copydata, now) is None
    copydata = copy.deepcopy(data); copydata['updated_at'] = '2026-11-07T12:00:00'
    assert traffic.scenario(copydata, now) is None
    copydata = copy.deepcopy(data); copydata['months'] = []
    assert traffic.scenario(copydata, now) is None
    copydata = copy.deepcopy(data); copydata['months'][0]['rx_bytes'] = 1; copydata['months'][0]['tx_bytes'] = 1
    assert traffic.scenario(copydata, now) is None
    assert traffic.scenario(scenario_data(rx=0, tx=0), now) is None


def test_conditional_scenario_crosses_year_and_leap_february_and_rejects_overflow():
    data = scenario_data('2028-02-29', created='2027-12-01')
    result = traffic.scenario(data, datetime(2028, 2, 29, 12))
    assert result['remaining_minutes'] == 720 and result['projected_bytes'] == 2100
    data = scenario_data('2027-01-01', created='2026-11-01')
    result = traffic.scenario(data, datetime(2027, 1, 1, 12))
    assert result['remaining_minutes'] == 30 * 1440 + 720
    data['days'][0]['rx_bytes'] = 2**53 - 1
    assert traffic.scenario(data, datetime(2027, 1, 1, 12)) is None


def test_scenario_cached_result_is_recomputed_across_midnight(monkeypatch):
    monkeypatch.setenv('YM_MONITOR_TRAFFIC_INTERFACE', 'ens3')
    monkeypatch.setattr(traffic.shutil, 'which', lambda _: '/usr/bin/vnstat')
    doc = document()
    from datetime import date, timedelta
    today = date(2026, 11, 8)
    doc['interfaces'][0]['created']['date'] = {'year': 2026, 'month': 10, 'day': 1}
    doc['interfaces'][0]['traffic']['month'][0].update(rx=1000, tx=1000)
    doc['interfaces'][0]['traffic']['day'] = [
        {'date': {'year': d.year, 'month': d.month, 'day': d.day}, 'rx': 100, 'tx': 100}
        for d in [today - timedelta(days=n) for n in range(1, 8)]
    ]
    monkeypatch.setattr(traffic, '_read_json', lambda *args: __import__('copy').deepcopy(doc))
    monkeypatch.setattr(traffic, '_now', lambda: datetime(2026, 11, 8, 13))
    traffic.reset_cache()
    try:
        assert traffic.snapshot()['scenario']['projected_bytes'] == 6500
        monkeypatch.setattr(traffic, '_now', lambda: datetime(2026, 11, 9, 0, 0))
        assert traffic.snapshot()['scenario'] is None
    finally:
        traffic.reset_cache()


def test_conditional_scenario_rejects_suspicious_clock_and_is_never_verified():
    import copy

    data = scenario_data()
    assert traffic.scenario(data, datetime(2026, 11, 8, 11, 54)) is None
    assert traffic.scenario(data, datetime(2026, 11, 8, 18, 1)) is None
    malformed = copy.deepcopy(data); malformed['updated_at'] = '2026-11-08T12:00:00+00:00'
    assert traffic.scenario(malformed, datetime(2026, 11, 8, 12)) is None
    malformed = copy.deepcopy(data); malformed['days'][0]['rx_bytes'] = True
    assert traffic.scenario(malformed, datetime(2026, 11, 8, 12)) is None
    malformed = copy.deepcopy(data); malformed['days'][0]['rx_bytes'] = -1
    assert traffic.scenario(malformed, datetime(2026, 11, 8, 12)) is None
