"""Ограниченные права OpenStack: реальные типы SDK и честные частичные снимки."""
import json
from datetime import timedelta

from openstack.compute.v2.limits import Limits
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.openstack_monitor import collector, connection, router, store, worker
from app.openstack_monitor.models import _utcnow
from tests.test_v17057 import CFG_OK, FakeConn, PolicyProxy


class LimitedCompute:
    def hypervisors(self, details=False):
        raise RuntimeError("Policy doesn't allow os_compute_api:os-hypervisors:list-detail")

    def get_limits(self):
        return Limits(absolute={"maxTotalCores": 20, "totalCoresUsed": 3,
                                "maxTotalRAMSize": 8192, "totalRAMUsed": 2048,
                                "maxTotalInstances": 10, "totalInstancesUsed": 2})


class Cloud:
    compute = LimitedCompute()
    load_balancer = None


def test_policy_test_does_not_claim_unchecked_services_work(monkeypatch):
    conn = FakeConn(PolicyProxy())
    monkeypatch.setattr(connection, "build_connection", lambda _cfg: conn)
    result = connection.test_connection(CFG_OK)
    assert result["ok"] and result["authorized"]
    assert "проверка не включает" in result["notice"].lower()
    assert "не сохраняет" in result["notice"].lower()
    assert "выдать роль администратора" not in result["notice"].lower()
    assert conn.session.closed == 1


def test_worker_partial_is_recorded_without_credentials(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    conn = Cloud()
    conn.authorize = lambda: None
    conn.close = lambda: None
    monkeypatch.setattr(connection, "build_connection", lambda _cfg: conn)
    monkeypatch.setattr(store, "connection_cfg", lambda _row: {"secret": "example-secret"})
    try:
        row = store.get_settings(session)
        result = worker.collect_once(session, row)
        assert result["ok"]
        assert result["view"]["cpu"]["pct"] is None
        logs = store.recent_logs(session, level="WARN")
        assert any("гипервизор" in line["message"].lower() for line in logs)
        assert all("example-secret" not in str(line) for line in logs)
    finally:
        session.close()
        engine.dispose()


def test_nova_limits_real_sdk():
    quotas, error = collector.quota_limits(Cloud())
    assert not error
    assert quotas == {"max_cores": 20, "used_cores": 3,
                      "max_ram_mb": 8192, "used_ram_mb": 2048,
                      "max_instances": 10, "used_instances": 2}


def test_partial_snapshot_and_view_do_not_turn_denied_into_zero():
    snapshot = collector.collect_metrics(Cloud(), {})
    assert snapshot["vcpus_total"] is None
    assert snapshot["ram_used_mb"] is None
    assert snapshot["disk_total_gb"] is None
    assert snapshot["quotas"]["used_cores"] == 3
    view = collector.current_view(snapshot)
    assert view["cpu"]["pct"] is None
    assert view["ram"]["free_mb"] is None
    assert view["disk"]["pct"] is None
    assert not view["warnings"]
    assert view["notes"]["hypervisor_error"]


def test_real_zero_and_unlimited_quota_are_not_missing():
    class Compute(LimitedCompute):
        def get_limits(self):
            return Limits(absolute={"maxTotalCores": -1, "totalCoresUsed": 0})
    conn = Cloud()
    conn.compute = Compute()
    q, _ = collector.quota_limits(conn)
    assert q["max_cores"] == -1 and q["used_cores"] == 0
    assert "max_ram_mb" not in q


def test_empty_and_missing_quota_are_distinct_from_zero():
    class Compute(LimitedCompute):
        def get_limits(self):
            return Limits(absolute={})
    conn = Cloud()
    conn.compute = Compute()
    quotas, error = collector.quota_limits(conn)
    assert quotas == {} and error


def test_raw_note_masks_secret_and_never_persists_arbitrary_sdk_error():
    class Compute(LimitedCompute):
        def hypervisors(self, details=False):
            raise RuntimeError('example-secret opaque-token')
    conn = Cloud()
    conn.compute = Compute()
    snapshot = collector.collect_metrics(conn, {'secret': 'example-secret'})
    assert 'example-secret' not in snapshot['notes']['hypervisor_error']
    assert 'example-secret' not in snapshot['raw_json']
    assert 'opaque-token' not in snapshot['raw_json']


def test_portable_unbucketed_partial_history(store):
    session = store.session()
    try:
        now = _utcnow()
        snapshot = collector.collect_metrics(Cloud(), {})
        snapshot['collected_at'] = now
        from app.openstack_monitor import store as store_module
        store_module.write_metric(session, snapshot)
        points = store_module.metric_history(session, now - timedelta(seconds=1), now + timedelta(seconds=1))
        assert any(p['collected_at'] and p['vcpus_total'] is None for p in points)
    finally:
        session.close()


def test_stored_and_bucketed_history_excludes_denied_values():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        now = _utcnow().replace(second=0, microsecond=0)
        bad = collector.collect_metrics(Cloud(), {})
        bad["collected_at"] = now
        store.write_metric(session, bad)
        current = router.metrics_current(user=None, db=session)
        assert current["cpu"]["pct"] is None
        assert current["quotas"]["used_cores"] == 3
        assert current["notes"]["hypervisor_error"]
        points = store.metric_history(session, now - timedelta(seconds=1), now + timedelta(seconds=50), 60)
        assert points[0]["vcpus_total"] is None
        # Старые снимки с причиной ошибки в raw_json тоже нельзя выдавать за нулевые.
        raw = json.loads(bad["raw_json"])
        raw["vcpus_total"] = 0
        bad["raw_json"] = json.dumps(raw)
        bad["collected_at"] = now + timedelta(seconds=1)
        store.write_metric(session, bad)
        assert store.latest_metric(session).public_dict()["vcpus_total"] is None
        unbucketed = store.metric_history(session, now - timedelta(seconds=1), now + timedelta(seconds=50), 0)
        assert all(p["vcpus_total"] is None for p in unbucketed)
        good = {"collected_at": now + timedelta(seconds=2), "vcpus_total": 40,
                "vcpus_used": 10, "ram_total_mb": 100, "ram_used_mb": 40,
                "disk_total_gb": 100, "disk_used_gb": 20,
                "raw_json": json.dumps({"notes": {}})}
        store.write_metric(session, good)
        points = store.metric_history(session, now - timedelta(seconds=1), now + timedelta(seconds=50), 60)
        assert points[0]["vcpus_total"] == 40 and points[0]["vcpus_used"] == 10
    finally:
        session.close()
        engine.dispose()
