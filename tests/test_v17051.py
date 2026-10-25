# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — выпуск v1.70.51: блок «Мониторинг OpenStack».
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: chek@ymaster.ru
#
# Требование владельца: «Выполни ТЗ docs/Monitor.md
# https://github.com/Rimlin-UNC/1c_chek/blob/arena/071f4163-1c-chek/docs/Monitor.md
# внедри аккуратно чтоб не сломать нашу программу».
#
# Файл проверяет пятнадцать задач ТЗ (раздел 13) и двенадцать критериев
# приёмки (раздел 12). Сквозные проверки идут на подставном облаке: ни
# одного настоящего сетевого запроса, поэтому тесты детерминированы и не
# зависят от доступности чужой инфраструктуры.
#
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
import pathlib
import re
from datetime import timedelta

import pytest

from tests.conftest import login

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
DOCS = ROOT / "docs"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"
TZ = DOCS / "Monitor.md"
REQUIREMENTS = ROOT / "requirements.txt"

INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
APP_JS = STATIC / "js" / "app.js"
OSM_JS = STATIC / "js" / "osm.js"
APP_CSS = STATIC / "css" / "app.css"
VENDOR = STATIC / "js" / "vendor"

MONITOR = APP / "openstack_monitor"
RELEASE_50_TESTS = ROOT / "tests" / "test_v17050.py"

PREFIX = "/api/v1/openstack"

OWNER_QUOTE_HEAD = "выполни тз docs/monitor.md"
OWNER_QUOTE_TAIL = "внедри аккуратно чтоб не сломать нашу программу"

# Секрет, который фигурирует в проверках: его не должно быть ни в одном
# ответе, ни в одной записи журнала.
SECRET = "ОченьСекретноеЗначение42"


def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
#  Подставное облако (критерий 15: e2e с mock-connector)
# --------------------------------------------------------------------------
class _Stats:
    vcpus, vcpus_used = 64, 12
    memory_mb, memory_mb_used = 262144, 65536
    free_ram_mb, local_gb, local_gb_used = 196608, 2000, 400
    free_disk_gb, running_vms, current_workload = 1600, 9, 3


class _Limits:
    absolute = [
        {"name": "maxTotalCores", "value": 100},
        {"name": "totalCoresUsed", "value": 12},
        {"name": "maxTotalRAMSize", "value": 262144},
        {"name": "totalRAMUsed", "value": 65536},
        {"name": "maxTotalInstances", "value": 50},
        {"name": "totalInstancesUsed", "value": 9},
    ]


class _Compute:
    def get_hypervisor_statistics(self):
        return _Stats()

    def get_limits(self):
        return _Limits()


class _Balancer:
    id = "lb-0001"


class _LoadBalancer:
    def load_balancers(self):
        return iter([_Balancer()])

    def statistics(self, lb_id):
        return {"bytes_in": 1073741824, "bytes_out": 2147483648}


class _Placement:
    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return {"inventories": {"VCPU": {"total": 64, "used": 12}}}


class FakeConnection:
    """Подставное облако: авторизация и сервисы без сети."""

    def __init__(self, with_balancer=True, fail=None):
        self.compute = _Compute()
        self.load_balancer = _LoadBalancer() if with_balancer else None
        self.placement = _Placement()
        self.session = None
        self.authorized = False
        self.fail = fail

    def authorize(self):
        if self.fail is not None:
            raise self.fail
        self.authorized = True


GOOD_CFG = {
    "auth_url": "https://keystone.example.ru/identity/v3",
    "auth_type": "password",
    "username": "monitor",
    "app_cred_id": "",
    "password": SECRET,
    "app_cred_secret": SECRET,
    "secret": SECRET,
    "project_name": "infra",
    "user_domain_name": "Default",
    "project_domain_name": "Default",
    "region_name": "RegionOne",
    "interface": "internal",
    "verify_tls": True,
    "timeout_sec": 25,
    "resource_provider_uuid": "",
}


@pytest.fixture
def fake_cloud(monkeypatch):
    """Подменяет построение подключения: возвращается подставное облако."""
    from app.openstack_monitor import connection

    built = {}

    def build(cfg):
        built["cfg"] = cfg
        return FakeConnection()

    monkeypatch.setattr(connection, "build_connection", build)
    monkeypatch.setattr(connection, "sdk_available", lambda: (True, ""))
    return built


@pytest.fixture(autouse=True)
def _worker_off_and_clean(monkeypatch):
    """Опрос облака запрещён на время модуля, настройки возвращаются в покой.

    Без YM_OSMON_OFF сохранение с enabled=True вызвало бы перезапуск потока,
    и тот попытался бы достучаться до настоящего облака по вымышленному
    адресу: тесты потеряли бы детерминизм. Приём тот же, что YM_FETCHQ_OFF
    в очереди чеков.
    """
    monkeypatch.setenv("YM_OSMON_OFF", "1")
    yield
    from app.database import SessionLocal
    from app.openstack_monitor import worker
    from app.openstack_monitor.models import OpenstackSettings

    worker.stop_worker()
    db = SessionLocal()
    try:
        for row in db.query(OpenstackSettings).all():
            row.enabled = False
        db.commit()
    finally:
        db.close()


def _admin_headers(client):
    return login(client, "admin", "admin123")


def _mk_user(client, role, username):
    """Регистрация пользователя по приглашению администратора → заголовки."""
    hdr = _admin_headers(client)
    inv = client.post("/api/v1/invites", json={"role": role},
                      headers=hdr).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _save(client, headers, **over):
    body = {
        "auth_url": GOOD_CFG["auth_url"],
        "auth_type": "password",
        "username": "monitor",
        "app_cred_id": "",
        "secret": SECRET,
        "project_name": "infra",
        "user_domain_name": "Default",
        "project_domain_name": "Default",
        "region_name": "RegionOne",
        "interface": "internal",
        "verify_tls": True,
        "timeout_sec": 25,
        "resource_provider_uuid": "",
        "interval_sec": 60,
        "history_days": 30,
        "thresholds": {"threshold_cpu_pct": 80, "threshold_ram_pct": 80,
                       "threshold_disk_pct": 80,
                       "threshold_traffic_gb_day": 0},
        "enabled": False,
    }
    body.update(over)
    return client.post(f"{PREFIX}/settings", headers=headers, json=body)


# ==========================================================================
#  1. Задача ТЗ 1: зависимости
# ==========================================================================
class TestDependencies:
    def test_sdk_is_in_requirements(self):
        """ТЗ, раздел 2: openstacksdk обязательно, keystoneauth1 — для
        аутентификации. Замок test_dependencies требует, чтобы каждая
        сторонняя библиотека была названа в requirements.txt."""
        text = _read(REQUIREMENTS)
        assert "openstacksdk" in text
        assert "keystoneauth1" in text

    def test_sdk_is_importable(self):
        import keystoneauth1  # noqa: F401
        import openstack  # noqa: F401
        from keystoneauth1.identity import v3
        assert hasattr(v3, "Password")
        assert hasattr(v3, "ApplicationCredential")

    def test_chart_js_is_vendored(self):
        """Решение владельца charts=chartjs: библиотека вендорена, как jsQR,
        поэтому страница не обращается к внешним сетям при загрузке."""
        assert (VENDOR / "chart.umd.js").exists()
        assert (VENDOR / "chart.js.LICENSE.txt").exists()

    def test_chart_js_is_included_before_the_module(self):
        html = _read(INDEX)
        assert '/js/vendor/chart.umd.js' in html
        assert html.index("chart.umd.js") < html.index("/js/app.js?v=")

    def test_vendor_is_not_scanned_by_the_debt_lock(self):
        """Вендор — чужой код: замок качества считает только свои модули."""
        assert (VENDOR / "jsQR.js").exists()

    def test_tz_is_in_the_repository(self):
        assert TZ.exists()
        assert "Мониторинг OpenStack" in _read(TZ)


# ==========================================================================
#  2. Задача ТЗ 2: модель данных (раздел 8)
# ==========================================================================
class TestSchema:
    def test_three_tables_are_declared(self):
        from app.openstack_monitor.models import (
            OpenstackMetric,
            OpenstackSettings,
            OpenstackSyncLog,
        )
        assert OpenstackSettings.__tablename__ == "openstack_settings"
        assert OpenstackMetric.__tablename__ == "openstack_metrics"
        assert OpenstackSyncLog.__tablename__ == "openstack_sync_log"

    def test_settings_columns_follow_the_tz(self):
        from app.openstack_monitor.models import OpenstackSettings
        need = {"id", "auth_url", "auth_type", "username", "secret_encrypted",
                "project_name", "user_domain_name", "project_domain_name",
                "region_name", "interface", "verify_tls", "timeout_sec",
                "resource_provider_uuid", "interval_sec", "history_days",
                "thresholds_json", "enabled", "created_at", "updated_at"}
        have = {c.name for c in OpenstackSettings.__table__.columns}
        assert need <= have, f"не хватает: {sorted(need - have)}"

    def test_metric_columns_follow_the_tz(self):
        from app.openstack_monitor.models import OpenstackMetric
        need = {"id", "collected_at", "vcpus_total", "vcpus_used",
                "ram_total_mb", "ram_used_mb", "disk_total_gb", "disk_used_gb",
                "traffic_in_bytes", "traffic_out_bytes", "raw_json"}
        have = {c.name for c in OpenstackMetric.__table__.columns}
        assert need <= have, f"не хватает: {sorted(need - have)}"

    def test_log_columns_follow_the_tz(self):
        from app.openstack_monitor.models import OpenstackSyncLog
        need = {"id", "ts", "level", "message", "error_code"}
        have = {c.name for c in OpenstackSyncLog.__table__.columns}
        assert need <= have, f"не хватает: {sorted(need - have)}"

    def test_indexes_follow_the_tz(self):
        """ТЗ, раздел 8: openstack_metrics(collected_at) и sync_log(ts DESC)."""
        from sqlalchemy import Index

        from app.openstack_monitor.models import OpenstackMetric, OpenstackSyncLog
        metric_ix = {i.name for i in OpenstackMetric.__table__.indexes}
        assert "ix_openstack_metrics_collected_at" in metric_ix
        log_ix = {i.name for i in OpenstackSyncLog.__table__.indexes}
        assert "ix_openstack_sync_log_ts_desc" in log_ix
        assert isinstance(Index, type)

    def test_log_index_is_descending(self, client):
        """Убывающий индекс должен быть убывающим и в самой базе."""
        import sqlalchemy

        from app.database import engine
        with engine.connect() as conn:
            row = conn.execute(sqlalchemy.text(
                "SELECT sql FROM sqlite_master "
                "WHERE name='ix_openstack_sync_log_ts_desc'")).fetchone()
        assert row is not None, "индекса нет в базе"
        assert "DESC" in row[0].upper(), row[0]

    def test_migration_is_additive_and_idempotent(self):
        from sqlalchemy import create_engine, inspect

        from app.openstack_monitor.models import ensure_openstack_schema
        engine = create_engine("sqlite://")
        ensure_openstack_schema(engine)
        ensure_openstack_schema(engine)          # повтор безопасен
        tables = set(inspect(engine).get_table_names())
        assert {"openstack_settings", "openstack_metrics",
                "openstack_sync_log"} <= tables

    def test_migration_does_not_touch_other_contours(self):
        """Добавляющая миграция создаёт только свои таблицы."""
        src = _read(MONITOR / "models.py")
        body = src.split("def ensure_openstack_schema", 1)[1]
        assert "create_all" in body
        assert "tables=[" in body
        for foreign in ("Receipt", "User", "B2bCompany", "PoolUser"):
            assert foreign not in body

    def test_database_registers_the_migration(self):
        src = _read(APP / "database.py")
        assert "ensure_openstack_schema" in src
        assert "openstack_monitor" in src


# ==========================================================================
#  3. Задача ТЗ 3: подключение (раздел 6.1)
# ==========================================================================
class TestConnection:
    def test_password_auth_uses_the_tz_keys(self):
        """Пример ТЗ 6.1 читает cfg["password"] и пять полей домена/проекта."""
        src = _read(MONITOR / "connection.py")
        body = src.split("def build_connection", 1)[1].split("\ndef ", 1)[0]
        assert "v3.Password(" in body
        for key in ("auth_url", "username", "password", "project_name",
                    "user_domain_name", "project_domain_name"):
            assert f'cfg["{key}"]' in body, key
        assert "session.Session(" in body
        assert "openstack.connection.Connection(" in body

    def test_application_credential_auth_uses_the_tz_keys(self):
        src = _read(MONITOR / "connection.py")
        body = src.split("def build_connection", 1)[1].split("\ndef ", 1)[0]
        assert "v3.ApplicationCredential(" in body
        assert 'cfg["app_cred_id"]' in body
        assert 'cfg["app_cred_secret"]' in body

    def test_verify_and_timeout_are_passed_to_the_session(self):
        src = _read(MONITOR / "connection.py")
        assert 'verify=bool(cfg.get("verify_tls"' in src
        assert 'timeout=int(cfg.get("timeout_sec")' in src

    def test_region_and_interface_reach_the_connection(self):
        src = _read(MONITOR / "connection.py")
        assert 'region_name=cfg.get("region_name")' in src
        assert 'interface=cfg.get("interface")' in src

    def test_sdk_import_is_lazy(self):
        """Импорт SDK внутри функции: без настроенного подключения программа
        библиотеку не загружает, поэтому её отсутствие не мешает запуску."""
        src = _read(MONITOR / "connection.py")
        head = src.split("def sdk_available", 1)[0]
        assert "import openstack" not in head
        assert "from keystoneauth1" not in head

    def test_sdk_availability_is_reported_not_fatal(self):
        from app.openstack_monitor import connection
        ok, why = connection.sdk_available()
        assert isinstance(ok, bool)
        assert isinstance(why, str)

    def test_missing_sdk_is_explained_in_russian(self, monkeypatch):
        import builtins

        from app.openstack_monitor import connection

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.split(".")[0] in ("openstack", "keystoneauth1"):
                raise ImportError("нет библиотеки")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        ok, why = connection.sdk_available()
        assert ok is False
        assert "openstacksdk" in why

    def test_validate_cfg_catches_an_empty_form(self):
        from app.openstack_monitor.connection import validate_cfg
        problems = validate_cfg({})
        assert len(problems) >= 5
        assert any("auth_url" in p for p in problems)

    def test_validate_cfg_accepts_a_full_form(self):
        from app.openstack_monitor.connection import validate_cfg
        assert validate_cfg(GOOD_CFG) == []

    def test_validate_cfg_rejects_a_bad_auth_type(self):
        from app.openstack_monitor.connection import validate_cfg
        problems = validate_cfg({**GOOD_CFG, "auth_type": "магия"})
        assert any("аутентификаци" in p for p in problems)

    def test_validate_cfg_warns_about_plain_http(self):
        from app.openstack_monitor.connection import validate_cfg
        problems = validate_cfg({**GOOD_CFG,
                                 "auth_url": "http://keystone.example.ru/v3"})
        assert any("TLS" in p for p in problems)

    def test_mask_secret_replaces_the_value(self):
        from app.openstack_monitor.connection import mask_secret
        text = f"отказ: пароль {SECRET} не подошёл"
        masked = mask_secret(text, SECRET)
        assert SECRET not in masked
        assert "***" in masked

    def test_mask_secret_ignores_short_values(self):
        from app.openstack_monitor.connection import mask_secret
        assert mask_secret("abc", "ab") == "abc"

    def test_error_codes_are_stable(self):
        from app.openstack_monitor.connection import classify_error
        assert classify_error(TimeoutError("timed out")) == "timeout"
        assert classify_error(ConnectionError("нет связи")) == "connection"
        assert classify_error(ValueError("прочее")) == "ValueError"


# ==========================================================================
#  4. Задача ТЗ 4: сборщик (раздел 6.2)
# ==========================================================================
class TestCollector:
    def test_snapshot_has_the_tz_fields(self, fake_cloud):
        from app.openstack_monitor import collector
        snap = collector.collect_metrics(FakeConnection(), GOOD_CFG)
        for key in ("collected_at", "vcpus_total", "vcpus_used",
                    "ram_total_mb", "ram_used_mb", "disk_total_gb",
                    "disk_used_gb", "traffic_in_bytes", "traffic_out_bytes",
                    "raw_json"):
            assert key in snap, key
        assert snap["vcpus_total"] == 64
        assert snap["ram_used_mb"] == 65536
        assert snap["disk_used_gb"] == 400

    def test_raw_json_is_serialisable(self, fake_cloud):
        from app.openstack_monitor import collector
        snap = collector.collect_metrics(FakeConnection(), GOOD_CFG)
        parsed = json.loads(snap["raw_json"])
        assert parsed["quotas"]["max_cores"] == 100
        assert parsed["quotas"]["used_ram_mb"] == 65536

    def test_placement_is_read_only_when_uuid_is_set(self, fake_cloud):
        from app.openstack_monitor import collector
        conn = FakeConnection()
        snap = collector.collect_metrics(conn, GOOD_CFG)
        assert snap["placement_inventories"] is None
        assert conn.placement.urls == []

        cfg = {**GOOD_CFG, "resource_provider_uuid": "rp-0001"}
        snap = collector.collect_metrics(conn, cfg)
        assert conn.placement.urls == ["/resource_providers/rp-0001/inventories"]
        assert snap["placement_inventories"] is not None

    def test_traffic_is_collected_from_octavia(self, fake_cloud):
        from app.openstack_monitor import collector
        snap = collector.collect_metrics(FakeConnection(), GOOD_CFG)
        assert snap["traffic_in_bytes"] == 1073741824
        assert snap["traffic_out_bytes"] == 2147483648

    def test_traffic_is_null_without_octavia(self, fake_cloud):
        """Критерий приёмки 7: traffic = null без падения."""
        from app.openstack_monitor import collector
        snap = collector.collect_metrics(FakeConnection(with_balancer=False),
                                         GOOD_CFG)
        assert snap["traffic_in_bytes"] is None
        assert snap["traffic_out_bytes"] is None
        assert "не подключён" in snap["notes"]["traffic_note"]

    def test_collector_survives_a_refusing_service(self):
        """Отказ одного сервиса не отменяет снимок."""
        from app.openstack_monitor import collector

        class Boom:
            def get_hypervisor_statistics(self):
                raise RuntimeError("Nova отказала")

            def get_limits(self):
                return _Limits()

        conn = FakeConnection()
        conn.compute = Boom()          # экземплярный атрибут важнее классового

        snap = collector.collect_metrics(conn, GOOD_CFG)
        assert snap["vcpus_total"] == 0
        assert "Nova отказала" in snap["notes"]["hypervisor_error"]
        assert snap["quotas"]["max_cores"] == 100

    def test_current_view_follows_the_tz_format(self, fake_cloud):
        """Раздел 7: cpu/ram/disk с total, used и pct, quotas, traffic,
        warnings."""
        from app.openstack_monitor import collector
        snap = collector.collect_metrics(FakeConnection(), GOOD_CFG)
        view = collector.current_view(snap)
        for block in ("cpu", "ram", "disk"):
            assert "pct" in view[block], block
        assert view["cpu"]["total"] == 64 and view["cpu"]["used"] == 12
        assert view["cpu"]["pct"] == 18.75
        assert view["ram"]["total_mb"] == 262144
        assert view["disk"]["total_gb"] == 2000
        assert view["quotas"]["max_cores"] == 100
        assert view["quotas"]["used_ram_mb"] == 65536
        assert view["traffic"] == {"in_bytes": 1073741824,
                                   "out_bytes": 2147483648}
        assert isinstance(view["warnings"], list)

    def test_pct_is_safe_against_zero_total(self):
        from app.openstack_monitor.collector import pct
        assert pct(0, 0) == 0.0
        assert pct(None, None) == 0.0
        assert pct(12, 64) == 18.75

    def test_limits_are_read_from_both_shapes(self):
        """Nova отдаёт absolute списком пар или словарём — берём и то и то."""
        from app.openstack_monitor.collector import quota_limits

        class DictLimits:
            absolute = {"maxTotalCores": 42, "totalCoresUsed": 7}

        class DictCompute:
            def get_limits(self):
                return DictLimits()

        conn = FakeConnection()
        conn.compute = DictCompute()

        quotas, err = quota_limits(conn)
        assert err == ""
        assert quotas["max_cores"] == 42


# ==========================================================================
#  5. Задача ТЗ 5: шифрование секретов (раздел 11)
# ==========================================================================
class TestSecretEncryption:
    def test_secret_is_sealed_at_rest(self, client):
        from app.database import SessionLocal
        from app.openstack_monitor import store
        headers = _admin_headers(client)
        r = _save(client, headers)
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            row = store.get_settings(db)
            assert row.secret_encrypted.startswith("enc:v1:")
            assert SECRET not in row.secret_encrypted
            assert store.secret_of(row) == SECRET
        finally:
            db.close()

    def test_secret_is_never_returned(self, client):
        headers = _admin_headers(client)
        _save(client, headers)
        for path in ("/settings", "/status", "/metrics/current",
                     "/metrics/history?period=1h", "/logs?limit=50"):
            r = client.get(f"{PREFIX}{path}", headers=headers)
            assert r.status_code == 200, path
            assert SECRET not in r.text, path

    def test_response_carries_a_flag_instead_of_the_value(self, client):
        headers = _admin_headers(client)
        _save(client, headers)
        body = client.get(f"{PREFIX}/settings", headers=headers).json()
        assert body["settings"]["secret_set"] is True
        assert "secret" not in body["settings"]
        assert "secret_encrypted" not in body["settings"]

    def test_empty_secret_does_not_erase_the_saved_one(self, client):
        """Пустое поле означает «не менять»: форма закрыта маской."""
        from app.database import SessionLocal
        from app.openstack_monitor import store
        headers = _admin_headers(client)
        _save(client, headers)
        r = _save(client, headers, secret="")
        assert r.status_code == 200
        db = SessionLocal()
        try:
            assert store.secret_of(store.get_settings(db)) == SECRET
        finally:
            db.close()

    def test_errors_are_masked_before_the_journal(self, client, fake_cloud,
                                                  monkeypatch):
        from app.database import SessionLocal
        from app.openstack_monitor import connection, store, worker

        class Boom(FakeConnection):
            def authorize(self):
                raise RuntimeError(f"отказ: секрет {SECRET} неверен")

        monkeypatch.setattr(connection, "build_connection",
                            lambda cfg: Boom())
        headers = _admin_headers(client)
        _save(client, headers, enabled=True)
        db = SessionLocal()
        try:
            res = worker.collect_once(db)
            assert res["ok"] is False
            assert SECRET not in res["error"]
            assert "***" in res["error"]
            row = store.get_settings(db)
            assert SECRET not in row.last_error
            logs = store.recent_logs(db, limit=20, level="ERROR")
            assert logs, "ошибка не попала в журнал"
            for entry in logs:
                assert SECRET not in entry["message"]
        finally:
            db.close()


# ==========================================================================
#  6. Задачи ТЗ 6 и 7: внутренние API (раздел 7)
# ==========================================================================
class TestApi:
    def test_eight_routes_are_registered(self, client):
        import app.main as main
        found = set()
        for entry in main.app.routes:
            router = getattr(entry, "original_router", entry)
            for route in getattr(router, "routes", []) or []:
                path = str(getattr(route, "path", ""))
                if path.startswith(PREFIX):
                    found.add(path)
        expected = {f"{PREFIX}/settings", f"{PREFIX}/test",
                    f"{PREFIX}/disable", f"{PREFIX}/metrics/current",
                    f"{PREFIX}/metrics/history", f"{PREFIX}/status",
                    f"{PREFIX}/logs"}
        assert expected <= found, f"не хватает: {sorted(expected - found)}"

    def test_spa_catch_all_is_registered_last(self, client):
        """Позиция строк в файле ничего не решает: важен порядок регистрации
        маршрутов. Всеобъемлющий SPA-перехват обязан стоять после API, иначе
        он перехватил бы обращения к контуру."""
        import app.main as main
        entries = list(main.app.routes)
        paths = [str(getattr(entry, "path", "")) for entry in entries]
        # Всеобъемлющий переходват обязан быть последним маршрутом.
        assert paths[-1] == "/{full_path:path}", paths[-1]
        # Роутер контура включён раньше: include_router оборачивается в
        # объект без path, поэтому сравниваем по префиксу вложенного роутера.
        idx = [i for i, e in enumerate(entries)
               if getattr(getattr(e, "original_router", e), "prefix", "")
               == PREFIX]
        assert idx, "роутер контура не найден среди маршрутов"
        assert idx[0] < len(paths) - 1

    def test_anonymous_is_refused(self, client):
        """Запрос без токена и без cookie.

        Вход в программе ставит HttpOnly-cookie ymaster_token, а
        get_current_user принимает её запасным вариантом (app/auth.py) —
        поэтому сессионный клиент после настоящего входа уже НЕ анонимен и
        ответил бы 200. Cookie сбрасывается на время проверки и возвращается,
        чтобы не задеть чужие тесты.
        """
        saved = dict(client.cookies)
        client.cookies.clear()
        try:
            for path in ("/settings", "/status", "/logs", "/metrics/current",
                         "/metrics/history"):
                assert client.get(f"{PREFIX}{path}").status_code == 401, path
            assert client.post(f"{PREFIX}/test", json={}).status_code == 401
            assert client.post(f"{PREFIX}/disable").status_code == 401
        finally:
            for name, value in saved.items():
                client.cookies.set(name, value)

    def test_cookie_path_is_protected_too(self, client):
        """Запасной путь аутентификации не ослабляет доступ к контуру:
        роль проверяется одинаково и для заголовка, и для cookie."""
        headers = _admin_headers(client)
        assert client.get(f"{PREFIX}/settings", headers=headers).status_code == 200
        # тот же токен, но полученный из cookie, установленной при входе
        assert client.get(f"{PREFIX}/settings").status_code == 200

    def test_settings_round_trip(self, client):
        headers = _admin_headers(client)
        r = _save(client, headers, interval_sec=120, history_days=14,
                  interface="public", region_name="RegionTwo")
        assert r.status_code == 200, r.text
        saved = r.json()["settings"]
        assert saved["interval_sec"] == 120
        assert saved["history_days"] == 14
        assert saved["interface"] == "public"
        assert saved["region_name"] == "RegionTwo"

        got = client.get(f"{PREFIX}/settings", headers=headers).json()
        assert got["settings"]["interval_sec"] == 120
        assert got["auth_types"] == ["password", "application_credential"]
        assert got["interfaces"] == ["public", "internal", "admin"]
        assert got["interval_min_sec"] == 30
        assert got["interval_max_sec"] == 3600

    def test_store_clamps_interval_defensively(self):
        """Раздел 5: интервал от 30 до 3600 секунд. Через API значение вне
        границ отклоняется ещё до записи (422), но store защищает и
        внутренних вызывающих — воркер не должен получить интервал 0."""
        from app.database import SessionLocal
        from app.openstack_monitor import store
        from app.openstack_monitor.models import (
            INTERVAL_MAX_SEC,
            INTERVAL_MIN_SEC,
        )
        db = SessionLocal()
        try:
            row, problems = store.apply_settings(db, {"interval_sec": 1})
            assert problems == []
            assert row.interval_sec == INTERVAL_MIN_SEC
            row, _problems = store.apply_settings(db, {"interval_sec": 99999})
            assert row.interval_sec == INTERVAL_MAX_SEC
        finally:
            db.close()

    def test_out_of_range_values_are_refused(self, client):
        headers = _admin_headers(client)
        r = _save(client, headers, interval_sec=5)
        assert r.status_code == 422, " pydantic должен отказать"

    def test_bad_auth_type_is_refused_with_a_reason(self, client):
        """Способ аутентификации проверяет pydantic: неизвестное значение
        отклоняется 422, а неполная форма — 400 с перечнем причин."""
        headers = _admin_headers(client)
        # Неизвестный способ аутентификации отсекается делом, а не схемой:
        # 400 с перечнем допустимых значений на русском.
        r = _save(client, headers, auth_type="магия")
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "недопустимый способ аутентификации" in detail
        assert "application_credential" in detail

        # Пустая форма — тот же отказ с перечнем причин.
        r = client.post(f"{PREFIX}/settings", headers=headers, json={
            "auth_url": "", "auth_type": "password", "username": "",
            "project_name": "", "interval_sec": 60, "history_days": 30,
            "enabled": True})
        assert r.status_code == 400, r.text
        assert "auth_url" in r.json()["detail"]

    def test_thresholds_are_saved_and_returned(self, client):
        headers = _admin_headers(client)
        r = _save(client, headers, thresholds={
            "threshold_cpu_pct": 55, "threshold_ram_pct": 66,
            "threshold_disk_pct": 77, "threshold_traffic_gb_day": 12})
        assert r.status_code == 200, r.text
        th = r.json()["settings"]["thresholds"]
        assert th == {"threshold_cpu_pct": 55, "threshold_ram_pct": 66,
                      "threshold_disk_pct": 77,
                      "threshold_traffic_gb_day": 12}

    def test_threshold_above_hundred_is_refused(self, client):
        headers = _admin_headers(client)
        r = _save(client, headers, thresholds={
            "threshold_cpu_pct": 140, "threshold_ram_pct": 80,
            "threshold_disk_pct": 80, "threshold_traffic_gb_day": 0})
        assert r.status_code == 422

    def test_test_endpoint_reports_services(self, client, fake_cloud):
        headers = _admin_headers(client)
        r = client.post(f"{PREFIX}/test", headers=headers, json={
            "auth_url": GOOD_CFG["auth_url"], "auth_type": "password",
            "username": "monitor", "secret": SECRET,
            "project_name": "infra", "user_domain_name": "Default",
            "project_domain_name": "Default", "region_name": "RegionOne",
            "interface": "internal", "verify_tls": True, "timeout_sec": 25})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["authorized"] is True
        assert body["services"]["compute"] == "доступен"
        assert body["hypervisor"]["vcpus"] == 64
        assert SECRET not in r.text

    def test_test_endpoint_reports_a_failure_with_text(self, client,
                                                       fake_cloud,
                                                       monkeypatch):
        """Критерий 3: ok/error с текстом ошибки."""
        from app.openstack_monitor import connection

        class Boom(FakeConnection):
            def authorize(self):
                raise RuntimeError("отказано в доступе")

        monkeypatch.setattr(connection, "build_connection",
                            lambda cfg: Boom())
        headers = _admin_headers(client)
        r = client.post(f"{PREFIX}/test", headers=headers, json={
            "auth_url": GOOD_CFG["auth_url"], "auth_type": "password",
            "username": "monitor", "secret": "ТайноеЗначение9",
            "project_name": "infra", "user_domain_name": "Default",
            "project_domain_name": "Default", "region_name": "RegionOne",
            "interface": "internal", "verify_tls": True, "timeout_sec": 25})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "отказано в доступе" in body["error"]
        assert body["error_code"]

    def test_test_endpoint_takes_the_saved_secret_when_empty(self, client,
                                                            fake_cloud):
        headers = _admin_headers(client)
        _save(client, headers)
        r = client.post(f"{PREFIX}/test", headers=headers, json={
            "auth_url": GOOD_CFG["auth_url"], "auth_type": "password",
            "username": "monitor", "project_name": "infra",
            "user_domain_name": "Default", "project_domain_name": "Default",
            "region_name": "RegionOne", "interface": "internal",
            "verify_tls": True, "timeout_sec": 25})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert fake_cloud["cfg"]["secret"] == SECRET

    def test_disable_turns_monitoring_off(self, client):
        headers = _admin_headers(client)
        _save(client, headers, enabled=True)
        r = client.post(f"{PREFIX}/disable", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["enabled"] is False
        assert body["worker_alive"] is False
        assert client.get(f"{PREFIX}/status",
                          headers=headers).json()["enabled"] is False

    def test_metrics_current_is_empty_but_valid_without_snapshots(self,
                                                                  client):
        headers = _admin_headers(client)
        from app.database import SessionLocal
        from app.openstack_monitor.models import OpenstackMetric
        db = SessionLocal()
        try:
            db.query(OpenstackMetric).delete()
            db.commit()
        finally:
            db.close()
        body = client.get(f"{PREFIX}/metrics/current", headers=headers).json()
        assert body["available"] is False
        assert body["collected_at"] is None
        assert body["cpu"]["pct"] == 0.0
        assert body["traffic"] is None
        assert isinstance(body["warnings"], list)

    def test_history_accepts_period(self, client, fake_cloud):
        from app.database import SessionLocal
        from app.openstack_monitor import collector, store
        db = SessionLocal()
        try:
            store.write_metric(db, collector.collect_metrics(FakeConnection(),
                                                             GOOD_CFG))
        finally:
            db.close()
        headers = _admin_headers(client)
        for period in ("1h", "24h", "7d", "30d"):
            r = client.get(f"{PREFIX}/metrics/history?period={period}",
                           headers=headers)
            assert r.status_code == 200, period
            body = r.json()
            assert body["ok"] is True
            assert body["period"] == period
            assert body["count"] >= 1, period
            assert body["thresholds"]["threshold_cpu_pct"] >= 0

    def test_history_accepts_explicit_from_to_step(self, client, fake_cloud):
        headers = _admin_headers(client)
        r = client.get(f"{PREFIX}/metrics/history",
                       headers=headers,
                       params={"from": "2020-01-01T00:00:00Z",
                               "to": "2030-01-01T00:00:00Z",
                               "step": "1h"})
        assert r.status_code == 200
        body = r.json()
        assert body["step_sec"] == 3600
        assert isinstance(body["points"], list)

    def test_history_step_shorthands_are_parsed(self):
        from app.openstack_monitor.router import _parse_step
        assert _parse_step("") == 0
        assert _parse_step("60") == 60
        assert _parse_step("5m") == 300
        assert _parse_step("1h") == 3600
        assert _parse_step("1d") == 86400
        assert _parse_step("мусор") == 0

    def test_moments_are_read_as_utc_not_server_local(self):
        """Момент с поясом приводится к UTC: база хранит наивный UTC, и
        сдвиг в пояс сервера исказил бы границу периода."""
        from datetime import datetime, timezone

        from app.openstack_monitor.router import _naive_utc, _parse_moment
        aware = datetime(2026, 10, 25, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        assert _naive_utc(aware) == datetime(2026, 10, 25, 12, 0, 0)
        assert _parse_moment("2026-10-25T15:00:00+03:00",
                             datetime(2000, 1, 1)) == datetime(2026, 10, 25, 12)
        assert _parse_moment("мусор", datetime(2000, 1, 1)) == datetime(2000, 1, 1)

    def test_status_reports_the_contour_state(self, client):
        headers = _admin_headers(client)
        body = client.get(f"{PREFIX}/status", headers=headers).json()
        for key in ("enabled", "configured", "interval_sec", "history_days",
                    "last_success_at", "last_error", "metrics_count",
                    "worker_alive", "sdk_available"):
            assert key in body, key

    def test_logs_are_newest_first_and_filterable(self, client, fake_cloud):
        from app.database import SessionLocal
        from app.openstack_monitor import worker
        headers = _admin_headers(client)
        _save(client, headers, enabled=True, thresholds={
            "threshold_cpu_pct": 5, "threshold_ram_pct": 5,
            "threshold_disk_pct": 5, "threshold_traffic_gb_day": 0})
        db = SessionLocal()
        try:
            worker.collect_once(db)
        finally:
            db.close()

        body = client.get(f"{PREFIX}/logs?limit=50", headers=headers).json()
        assert body["count"] >= 1
        stamps = [row["ts"] for row in body["logs"]]
        assert stamps == sorted(stamps, reverse=True), "журнал не по убыванию"
        for row in body["logs"]:
            assert row["level"] in ("INFO", "WARN", "ERROR")
            assert SECRET not in row["message"]

        warns = client.get(f"{PREFIX}/logs?level=WARN",
                           headers=headers).json()
        assert all(row["level"] == "WARN" for row in warns["logs"])
        assert warns["count"] >= 1, "превышение порога не записано в журнал"

    def test_logs_limit_is_bounded(self, client):
        headers = _admin_headers(client)
        body = client.get(f"{PREFIX}/logs?limit=99999", headers=headers).json()
        assert body["count"] <= 500


# ==========================================================================
#  7. Задача ТЗ 8: планировщик (раздел 5)
# ==========================================================================
class TestScheduler:
    def test_worker_is_a_daemon_thread_like_the_rest_of_the_app(self):
        """ТЗ допускает APScheduler, Celery beat или cron «по инфраструктуре».
        Ни одного из них в программе нет, поэтому повторён образец
        app/services/webhooks.py — демон-поток."""
        src = _read(MONITOR / "worker.py")
        assert "threading.Thread" in src
        assert "daemon=True" in src
        for name in ("start_worker", "stop_worker", "restart_worker",
                     "worker_is_alive", "_worker_loop"):
            assert f"def {name}" in src, name

    def test_kill_switch_prevents_the_thread(self, monkeypatch):
        from app.openstack_monitor import worker
        monkeypatch.setenv("YM_OSMON_OFF", "1")
        assert worker.off_by_env() is True
        assert worker.start_worker() is False
        assert worker.worker_is_alive() is False

    def test_kill_switch_is_documented_in_the_module(self):
        src = _read(MONITOR / "worker.py")
        assert "YM_OSMON_OFF" in src

    def test_disabled_monitoring_does_not_start_the_thread(self, monkeypatch):
        from app.database import SessionLocal
        from app.openstack_monitor import store, worker
        monkeypatch.delenv("YM_OSMON_OFF", raising=False)
        db = SessionLocal()
        try:
            row = store.get_settings(db)
            row.enabled = False
            db.commit()
            assert worker.start_worker() is False
        finally:
            worker.stop_worker()
            db.close()

    def test_is_due_follows_the_interval(self):
        from app.database import SessionLocal
        from app.openstack_monitor import store, worker
        from app.openstack_monitor.models import OpenstackMetric, _utcnow
        db = SessionLocal()
        try:
            row = store.get_settings(db)
            row.interval_sec = 60
            db.commit()
            db.query(OpenstackMetric).delete()
            db.commit()
            assert worker.is_due(db, row) is True, "без снимков пора собирать"

            db.add(OpenstackMetric(collected_at=_utcnow()))
            db.commit()
            assert worker.is_due(db, row) is False, "свежий снимок — рано"

            db.query(OpenstackMetric).delete()
            db.commit()
            db.add(OpenstackMetric(collected_at=_utcnow() - timedelta(seconds=61)))
            db.commit()
            assert worker.is_due(db, row) is True, "старый снимок — пора"
        finally:
            db.close()

    def test_main_starts_the_worker_in_lifespan(self):
        src = _read(APP / "main.py")
        assert "openstack_monitor import worker" in src
        assert "os_worker.start_worker()" in src

    def test_collect_once_writes_a_snapshot(self, client, fake_cloud):
        """Критерий 5: показатели собираются через SDK и пишутся в базу."""
        from app.database import SessionLocal
        from app.openstack_monitor import store, worker
        headers = _admin_headers(client)
        _save(client, headers, enabled=True)
        db = SessionLocal()
        try:
            before = store.metrics_count(db)
            res = worker.collect_once(db)
            assert res["ok"] is True
            assert store.metrics_count(db) == before + 1
            last = store.latest_metric(db)
            assert last.vcpus_total == 64
            assert last.ram_total_mb == 262144
            assert last.disk_total_gb == 2000
        finally:
            db.close()

    def test_failure_keeps_previous_metrics(self, client, fake_cloud,
                                            monkeypatch):
        """Критерий 10 и раздел 6.3: ошибка видна, прежние данные целы."""
        from app.database import SessionLocal
        from app.openstack_monitor import connection, store, worker
        headers = _admin_headers(client)
        _save(client, headers, enabled=True)
        db = SessionLocal()
        try:
            worker.collect_once(db)
            kept = store.metrics_count(db)
            before = store.latest_metric(db).collected_at

            class Boom(FakeConnection):
                def authorize(self):
                    raise TimeoutError("облако не ответило")

            monkeypatch.setattr(connection, "build_connection",
                                lambda cfg: Boom())
            res = worker.collect_once(db)
            assert res["ok"] is False
            assert res["error_code"] == "timeout"
            row = store.get_settings(db)
            assert "облако не ответило" in row.last_error
            assert row.last_error_at is not None
            assert store.metrics_count(db) == kept, "прежние снимки стёрты"
            assert store.latest_metric(db).collected_at == before
            assert row.last_success_at is not None
        finally:
            db.close()

    def test_collection_runs_off_the_request_thread(self, client, fake_cloud):
        """Критерий 12: сбор не блокирует остальную работу программы."""
        src = _read(MONITOR / "worker.py")
        assert "_worker_loop" in src and "threading" in src
        headers = _admin_headers(client)
        r = client.get(f"{PREFIX}/status", headers=headers)
        assert r.status_code == 200


# ==========================================================================
#  8. Задача ТЗ 9: пороги и предупреждения (разделы 6.4 и 9)
# ==========================================================================
class TestThresholds:
    def test_defaults_follow_the_tz(self):
        from app.openstack_monitor.models import DEFAULT_THRESHOLDS
        assert DEFAULT_THRESHOLDS == {
            "threshold_cpu_pct": 80, "threshold_ram_pct": 80,
            "threshold_disk_pct": 80, "threshold_traffic_gb_day": 0}

    def test_default_interval_and_history_follow_the_tz(self):
        from app.openstack_monitor.models import (
            DEFAULT_HISTORY_DAYS,
            DEFAULT_INTERVAL_SEC,
            DEFAULT_TIMEOUT_SEC,
        )
        assert DEFAULT_INTERVAL_SEC == 60
        assert DEFAULT_HISTORY_DAYS == 30
        assert DEFAULT_TIMEOUT_SEC == 30

    def test_no_warning_below_the_threshold(self):
        from app.openstack_monitor import collector
        view = {"cpu": {"pct": 10.0}, "ram": {"pct": 20.0},
                "disk": {"pct": 30.0}, "traffic": None}
        assert collector.warnings_of(view) == []

    def test_warning_appears_at_the_threshold(self):
        """Критерий 9: при превышении порога появляется предупреждение."""
        from app.openstack_monitor import collector
        view = {"cpu": {"pct": 10.0}, "ram": {"pct": 80.0},
                "disk": {"pct": 30.0}, "traffic": None}
        warns = collector.warnings_of(view)
        assert len(warns) == 1
        assert "Оперативная память" in warns[0]
        assert "80" in warns[0]

    def test_zero_threshold_disables_the_check(self):
        from app.openstack_monitor import collector
        view = {"cpu": {"pct": 99.0}, "ram": {"pct": 99.0},
                "disk": {"pct": 99.0}, "traffic": None}
        assert collector.warnings_of(view, {
            "threshold_cpu_pct": 0, "threshold_ram_pct": 0,
            "threshold_disk_pct": 0, "threshold_traffic_gb_day": 0}) == []

    def test_traffic_threshold_counts_gigabytes(self):
        from app.openstack_monitor import collector
        view = {"cpu": {"pct": 0}, "ram": {"pct": 0}, "disk": {"pct": 0},
                "traffic": {"in_bytes": 3 * 1073741824, "out_bytes": 0}}
        warns = collector.warnings_of(view, {
            "threshold_cpu_pct": 80, "threshold_ram_pct": 80,
            "threshold_disk_pct": 80, "threshold_traffic_gb_day": 2})
        assert len(warns) == 1 and "Трафик" in warns[0]

    def test_warning_is_written_to_the_journal_as_warn(self, client,
                                                       fake_cloud):
        from app.database import SessionLocal
        from app.openstack_monitor import store, worker
        headers = _admin_headers(client)
        _save(client, headers, enabled=True, thresholds={
            "threshold_cpu_pct": 1, "threshold_ram_pct": 80,
            "threshold_disk_pct": 80, "threshold_traffic_gb_day": 0})
        db = SessionLocal()
        try:
            res = worker.collect_once(db)
            assert res["warnings"], "порог в 1 % должен быть превышен"
            warns = store.recent_logs(db, limit=20, level="WARN")
            assert warns, "предупреждение не записано в журнал"
            assert any("превышен порог" in row["message"] for row in warns)
        finally:
            db.close()

    def test_corrupt_thresholds_json_falls_back_to_defaults(self):
        from app.openstack_monitor.models import (
            DEFAULT_THRESHOLDS,
            OpenstackSettings,
            thresholds_of,
        )
        row = OpenstackSettings(thresholds_json="{не json")
        assert thresholds_of(row) == DEFAULT_THRESHOLDS

    def test_thresholds_of_none_returns_defaults(self):
        from app.openstack_monitor.models import DEFAULT_THRESHOLDS, thresholds_of
        assert thresholds_of(None) == DEFAULT_THRESHOLDS


# ==========================================================================
#  9. Задача ТЗ 10: ретенция (раздел 8)
# ==========================================================================
class TestRetention:
    def test_old_metrics_are_deleted(self, client):
        """Критерий 11: ретенция удаляет метрики старше history_days."""
        from app.database import SessionLocal
        from app.openstack_monitor import store
        from app.openstack_monitor.models import OpenstackMetric, _utcnow
        headers = _admin_headers(client)
        _save(client, headers, history_days=7)
        db = SessionLocal()
        try:
            db.query(OpenstackMetric).delete()
            db.add(OpenstackMetric(collected_at=_utcnow() - timedelta(days=30)))
            db.add(OpenstackMetric(collected_at=_utcnow() - timedelta(days=1)))
            db.commit()
            assert store.metrics_count(db) == 2

            removed = store.prune_history(db)
            assert removed >= 1
            left = store.metrics_count(db)
            assert left == 1, "свежий снимок удалён вместе со старым"
        finally:
            db.close()

    def test_prune_runs_no_more_than_once_an_hour(self):
        from app.openstack_monitor.worker import PRUNE_EVERY
        assert PRUNE_EVERY == timedelta(hours=1)

    def test_prune_keeps_the_journal_bounded(self, client):
        """Журнал чистится по тому же окну, иначе рос бы без границы."""
        from app.database import SessionLocal
        from app.openstack_monitor import store
        from app.openstack_monitor.models import OpenstackSyncLog, _utcnow
        headers = _admin_headers(client)
        _save(client, headers, history_days=3)
        db = SessionLocal()
        try:
            db.query(OpenstackSyncLog).delete()
            db.add(OpenstackSyncLog(ts=_utcnow() - timedelta(days=40),
                                    level="INFO", message="старая запись"))
            db.add(OpenstackSyncLog(ts=_utcnow(), level="INFO",
                                    message="свежая запись"))
            db.commit()
            store.prune_history(db)
            left = store.recent_logs(db, limit=50)
            messages = [row["message"] for row in left]
            assert "старая запись" not in messages
            assert "свежая запись" in messages
        finally:
            db.close()

    def test_prune_result_is_described_for_the_journal(self):
        from app.openstack_monitor.store import describe_prune
        assert "нечего" in describe_prune(0)
        assert "5" in describe_prune(5)


# ==========================================================================
#  10. Задачи ТЗ 11–13: вкладки, графики, журнал в интерфейсе
# ==========================================================================
class TestTabs:
    def test_card_is_in_the_integrations_section(self):
        """Критерий 1: блок живёт в разделе настроек."""
        js = _read(OSM_JS)
        assert 'id="st-openstack"' in js
        assert 'data-sec="integr"' in js
        assert 'style="order:39"' in js

    def test_card_declares_four_tabs(self):
        js = _read(OSM_JS)
        assert 'role="tablist"' in js
        for name in ("Подключение", "Пороги и опрос", "Показатели", "Журнал"):
            assert f"'{name}'" in js, name

    def test_tabs_are_real_tabs(self):
        js = _read(OSM_JS)
        assert js.count('role="tab"') >= 1
        assert js.count('role="tabpanel"') == 4
        assert "aria-controls" in js
        assert "aria-labelledby" in js
        assert "aria-selected" in js

    def test_tab_panels_are_hidden_when_inactive(self):
        js = _read(OSM_JS)
        assert js.count("hidden") >= 3
        css = _read(APP_CSS)
        assert ".osm-panel[hidden] { display: none; }" in css

    def test_keyboard_navigation_is_wired(self):
        """Стрелки, Home и End — требование доступности для role=tablist."""
        js = _read(OSM_JS)
        for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
            assert f"'{key}'" in js, key

    def test_card_is_admin_only(self):
        js = _read(APP_JS)
        assert "isAdmin() && osSet ? osmCardHtml(" in js

    def test_app_js_only_inserts_the_card(self):
        """Разметка и поведение вынесены в osm.js: app.js вырос на минимум."""
        js = _read(APP_JS)
        assert "from './osm.js'" in js
        assert "osmWire(" in js
        assert "osmStop();" in js

    def test_connection_form_has_all_the_tz_fields(self):
        """Раздел 4: 14 полей формы подключения."""
        js = _read(OSM_JS)
        # Поля, объявленные в разметке литералом идентификатора.
        for field_id in ("osm-auth-url", "osm-username", "osm-app-cred-id",
                         "osm-secret", "osm-timeout", "osm-rp-uuid"):
            assert f'id="{field_id}"' in js, field_id
        # Остальные строят помощники field(), selectField() и checkbox(),
        # поэтому идентификатор приходит аргументом: 'id="${id}"'.
        for field_id in ("osm-project", "osm-udn", "osm-pdn", "osm-region",
                         "osm-auth-type", "osm-interface", "osm-verify-tls",
                         "osm-enabled"):
            assert f"'{field_id}'" in js, field_id
        assert 'id="${id}"' in js

    def test_three_buttons_from_the_tz(self):
        js = _read(OSM_JS)
        for button in ("osm-save", "osm-test", "osm-disable"):
            assert f'id="{button}"' in js, button
        assert "Проверить подключение" in js
        assert "Сохранить" in js
        assert "Отключить мониторинг" in js

    def test_secret_field_is_a_password_input(self):
        js = _read(OSM_JS)
        m = re.search(r'<input id="osm-secret"[^>]*>', js)
        assert m and 'type="password"' in m.group(0)

    def test_tls_warning_shows_only_when_unchecked(self):
        """Раздел 11: отключение проверки TLS — только с предупреждением."""
        js = _read(OSM_JS)
        assert 'id="osm-tls-warn"' in js
        assert "osm-tls-warn" in js
        assert "подмены" in js or "не защищено" in js
        assert "osm-verify-tls" in js

    def test_auth_type_switches_the_visible_fields(self):
        js = _read(OSM_JS)
        assert 'data-auth="password"' in js
        assert 'data-auth="app_cred"' in js
        assert "application_credential" in js
        # Авторский display:flex у label.field перебивает атрибут hidden,
        # поэтому в CSS есть точечное правило — приём из .b2badm-keycard.
        css = _read(APP_CSS)
        assert "label.field[hidden] { display: none; }" in css

    def test_thresholds_tab_has_the_tz_fields(self):
        js = _read(OSM_JS)
        for field_id in ("osm-interval", "osm-history", "osm-th-cpu",
                         "osm-th-ram", "osm-th-disk", "osm-th-traffic"):
            assert f'id="{field_id}"' in js, field_id
        assert 'min="30" max="3600"' in js


class TestChartsAndRefresh:
    def test_two_line_charts_are_declared(self):
        """Раздел 9: линейный график долей и линейный график трафика."""
        js = _read(OSM_JS)
        assert 'id="osm-chart-util"' in js
        assert 'id="osm-chart-net"' in js
        assert js.count("type: 'line'") == 2

    def test_chart_uses_the_vendored_global(self):
        js = _read(OSM_JS)
        assert "window.Chart" in js

    def test_missing_chart_library_does_not_break_the_card(self):
        js = _read(OSM_JS)
        assert "if (!window.Chart)" in js
        assert "osm-chart-empty" in js

    def test_charts_are_destroyed_before_redraw(self):
        """Повторное построение на том же холсте без destroy — ошибка Chart.js."""
        js = _read(OSM_JS)
        assert "function destroyCharts" in js
        assert js.index("destroyCharts();") < js.index("new window.Chart")

    def test_four_periods_are_offered(self):
        js = _read(OSM_JS)
        for label in ("1 ч", "24 ч", "7 д", "30 д"):
            assert f"'{label}'" in js, label
        assert "data-period" in js

    def test_autorefresh_is_thirty_seconds(self):
        """Раздел 9: автообновление каждые 30 секунд."""
        js = _read(OSM_JS)
        assert "const REFRESH_MS = 30000;" in js
        assert "setInterval" in js

    def test_refresh_stops_when_the_card_leaves_the_dom(self):
        js = _read(OSM_JS)
        assert "if (!document.getElementById('st-openstack')) { osmStop(); return; }" in js
        assert "export function osmStop()" in js

    def test_refresh_is_stopped_on_route_change(self):
        app = _read(APP_JS)
        assert "osmStop();   // v1.70.51" in app

    def test_chart_colours_are_the_locked_palette(self):
        """Цвета взяты из палитры графиков проекта: контраст закреплён."""
        js = _read(OSM_JS)
        charts = _read(STATIC / "js" / "charts.js")
        locked = set(re.findall(r"#(?:[0-9a-f]{6})", charts))
        block = js.split("const COLOR = {", 1)[1].split("};", 1)[0]
        used = set(re.findall(r"'(#[0-9a-f]{6})'", block))
        assert len(used) == 5, f"ожидалось пять цветов серий: {sorted(used)}"
        assert used <= locked, f"цвета вне палитры: {sorted(used - locked)}"
        # Подписи осей и сетка берутся из токенов темы, а не из литералов.
        assert "cssToken('--text-faint'" in js
        assert "cssToken('--border'" in js

    def test_threshold_colouring_has_three_levels(self):
        """Раздел 9: зелёный / жёлтый от порога / красный от порога + 10 %."""
        js = _read(OSM_JS)
        assert "function levelOf(" in js
        assert "osm-bad" in js and "osm-warn" in js and "osm-ok" in js
        assert "limitNum + 10" in js
        css = _read(APP_CSS)
        for cls in (".kpi.osm-ok", ".kpi.osm-warn", ".kpi.osm-bad"):
            assert cls in css, cls

    def test_six_widgets_of_the_tz_are_rendered(self):
        """Раздел 9: процессор, память, диск, квоты, сеть, статус."""
        js = _read(OSM_JS)
        for title in ("Процессор", "Оперативная память", "Диск",
                      "Квоты проекта", "Сеть", "Статус"):
            assert f"'{title}'" in js, title

    def test_chart_container_has_a_fixed_height(self):
        """Без заданной высоты родителя полотно Chart.js растёт бесконечно."""
        css = _read(APP_CSS)
        m = re.search(r"\.osm-chart \{([^}]*)\}", css)
        assert m and "position: relative" in m.group(1)
        assert "height:" in m.group(1)


class TestJournalTab:
    def test_journal_table_has_the_tz_columns(self):
        """Раздел 10: время, уровень, сообщение, код ошибки."""
        js = _read(OSM_JS)
        for head in ("Время", "Уровень", "Сообщение", "Код ошибки"):
            assert f"<th>{head}</th>" in js, head

    def test_journal_has_level_filters(self):
        js = _read(OSM_JS)
        assert "data-level" in js
        for level in ("INFO", "WARN", "ERROR"):
            assert f"'{level}'" in js, level

    def test_journal_has_a_refresh_button(self):
        js = _read(OSM_JS)
        assert 'id="osm-log-refresh"' in js

    def test_journal_values_are_escaped(self):
        js = _read(OSM_JS)
        assert "esc(row.message)" in js
        assert "esc(row.level)" in js
        assert "esc(row.error_code)" in js

    def test_journal_says_secrets_are_absent(self):
        js = _read(OSM_JS)
        assert "маской" in js

    def test_css_classes_of_the_module_are_defined(self):
        """Класс, которого нет в CSS, молча лишает элемент оформления."""
        js = _read(OSM_JS)
        css = re.sub(r"/\*.*?\*/", "", _read(APP_CSS), flags=re.S)
        defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))
        used = set()
        for m in re.finditer(r'class="([^"$]*)"', js):
            used.update(c for c in m.group(1).split() if c)
        hooks = {"hidden", "active", "full", "ok", "warn", "bad", "info",
                 "error", "spacer"}
        missing = sorted(used - defined - hooks)
        assert not missing, f"классы без правил в CSS: {missing}"

    def test_css_of_the_contour_is_valid(self):
        css = _read(APP_CSS)
        assert ".osm-tabs" in css and ".osm-tab" in css
        assert ".osm-chart" in css and ".osm-alert" in css
        assert ".osm-lvl" in css and ".osm-code" in css

    def test_only_one_focus_visible_rule_survives(self):
        """Своё кольцо фокуса для вкладок было бы вторым правилом
        :focus-visible, а значит одно из двух мертво (вывод v1.70.33)."""
        css = _read(APP_CSS)
        assert len(re.findall(r":focus-visible\s*\{", css)) == 1


# ==========================================================================
#  11. Задача ТЗ 14: доступ (разделы 3 и 11)
# ==========================================================================
class TestRbac:
    def test_every_route_requires_admin(self):
        src = _read(MONITOR / "router.py")
        assert src.count("Depends(require_admin)") == 8, \
            "не все восемь маршрутов закрыты администраторской зависимостью"
        assert "from ..auth import require_admin" in src

    def test_no_fine_grained_right_is_invented(self):
        """Отклонение от ТЗ согласовано: в программе нет таблицы точечных
        прав, а раздел 3 ТЗ сам помечает блок как администраторский."""
        src = _read(MONITOR / "router.py")
        assert "openstack_monitor.manage" in src, \
            "отклонение должно быть названо в коде"
        assert "require_role" not in src

    def test_non_admin_roles_are_refused(self, client):
        """Пользователь создаётся по приглашению, а не берётся из базы.

        Прежний вариант искал готового бухгалтера и угадывал его пароль:
        в изоляции тест пропускался, а в полной батарее чужой тест создавал
        бухгалтера со своим паролем, и проверка падала на самом входе.
        Регистрация по приглашению детерминирована — приём из test_v17050.
        """
        headers = _mk_user(client, "accountant", "os_acc51")
        for path in ("/settings", "/status", "/logs", "/metrics/current",
                     "/metrics/history"):
            assert client.get(f"{PREFIX}{path}",
                              headers=headers).status_code == 403, path
        assert client.post(f"{PREFIX}/settings", headers=headers,
                           json={}).status_code == 403
        assert client.post(f"{PREFIX}/disable",
                           headers=headers).status_code == 403

    def test_plain_user_is_refused(self, client):
        headers = _mk_user(client, "user", "os_usr51")
        assert client.get(f"{PREFIX}/settings", headers=headers).status_code == 403


# ==========================================================================
#  12. Критерии приёмки (раздел 12) — сводная проверка
# ==========================================================================
class TestAcceptanceCriteria:
    def test_criterion_1_four_tabs(self):
        js = _read(OSM_JS)
        assert 'role="tablist"' in js
        assert js.count('role="tabpanel"') == 4

    def test_criterion_2_settings_saved_secret_encrypted_not_returned(self,
                                                                     client):
        headers = _admin_headers(client)
        assert _save(client, headers).status_code == 200
        body = client.get(f"{PREFIX}/settings", headers=headers).json()
        assert body["settings"]["secret_set"] is True
        assert SECRET not in json.dumps(body, ensure_ascii=False)

    def test_criterion_3_test_returns_ok_or_error(self, client, fake_cloud):
        headers = _admin_headers(client)
        r = client.post(f"{PREFIX}/test", headers=headers, json={
            **{k: GOOD_CFG[k] for k in ("auth_url", "auth_type", "username",
                                        "project_name", "user_domain_name",
                                        "project_domain_name", "region_name",
                                        "interface", "verify_tls",
                                        "timeout_sec")},
            "secret": SECRET})
        body = r.json()
        assert body["ok"] is True and "error" in body

    def test_criterion_4_scheduler_follows_the_interval(self, client):
        headers = _admin_headers(client)
        _save(client, headers, enabled=True, interval_sec=300)
        body = client.get(f"{PREFIX}/status", headers=headers).json()
        assert body["enabled"] is True
        assert body["interval_sec"] == 300

    def test_criterion_5_metrics_reach_the_database(self, client, fake_cloud):
        from app.database import SessionLocal
        from app.openstack_monitor import store, worker
        headers = _admin_headers(client)
        _save(client, headers, enabled=True)
        db = SessionLocal()
        try:
            worker.collect_once(db)
            last = store.latest_metric(db)
            assert last is not None
            assert (last.vcpus_total, last.ram_used_mb, last.disk_total_gb) \
                == (64, 65536, 2000)
            assert json.loads(last.raw_json)["quotas"]["max_cores"] == 100
        finally:
            db.close()

    def test_criterion_6_placement_is_read_when_uuid_is_set(self, fake_cloud):
        from app.database import SessionLocal
        from app.openstack_monitor import store, worker
        from app.openstack_monitor.models import OpenstackMetric
        db = SessionLocal()
        try:
            row = store.get_settings(db)
            row.auth_url = GOOD_CFG["auth_url"]
            row.auth_type = "password"
            row.username = "monitor"
            row.project_name = "infra"
            row.user_domain_name = "Default"
            row.project_domain_name = "Default"
            row.region_name = "RegionOne"
            row.interface = "internal"
            row.resource_provider_uuid = "rp-0007"
            from app.services import secretbox
            row.secret_encrypted = secretbox.seal(SECRET)
            db.commit()
            db.query(OpenstackMetric).delete()
            db.commit()
            worker.collect_once(db)
            last = store.latest_metric(db)
            raw = json.loads(last.raw_json)
            assert raw["placement_inventories"] is not None
        finally:
            db.close()

    def test_criterion_7_traffic_null_without_octavia(self):
        from app.openstack_monitor import collector
        snap = collector.collect_metrics(FakeConnection(with_balancer=False),
                                         GOOD_CFG)
        view = collector.current_view(snap)
        assert view["traffic"] is None

    def test_criterion_8_cards_and_charts_for_four_periods(self):
        js = _read(OSM_JS)
        assert "osm-kpi" in js
        assert js.count("type: 'line'") == 2
        for label in ("1 ч", "24 ч", "7 д", "30 д"):
            assert f"'{label}'" in js

    def test_criterion_9_threshold_warning_is_visible(self):
        js = _read(OSM_JS)
        assert "osm-alert" in js
        assert "Превышены пороги" in js

    def test_criterion_10_errors_are_visible_in_status_and_journal(self,
                                                                   client):
        headers = _admin_headers(client)
        _save(client, headers)
        status = client.get(f"{PREFIX}/status", headers=headers).json()
        assert "last_error" in status and "last_error_at" in status
        logs = client.get(f"{PREFIX}/logs?level=ERROR", headers=headers).json()
        assert "logs" in logs

    def test_criterion_11_retention_deletes_old_metrics(self, client):
        from app.database import SessionLocal
        from app.openstack_monitor import store
        from app.openstack_monitor.models import OpenstackMetric, _utcnow
        headers = _admin_headers(client)
        _save(client, headers, history_days=2)
        db = SessionLocal()
        try:
            db.query(OpenstackMetric).delete()
            db.add(OpenstackMetric(collected_at=_utcnow() - timedelta(days=9)))
            db.commit()
            store.prune_history(db)
            assert store.metrics_count(db) == 0
        finally:
            db.close()

    def test_criterion_12_collection_is_off_the_request_path(self):
        src = _read(MONITOR / "worker.py")
        assert "daemon=True" in src
        main = _read(APP / "main.py")
        assert "os_worker.start_worker()" in main


# ==========================================================================
#  13. Программа не сломана: прежние поверхности на месте
# ==========================================================================
class TestNothingElseIsBroken:
    def test_previous_cards_survive(self):
        js = _read(APP_JS)
        for card in ("st-webhooks", "st-consent", "st-pool", "st-checko",
                     "st-onec", "st-clients", "st-transfer", "st-adguard"):
            assert f'id="{card}"' in js, card

    def test_own_charts_module_is_untouched(self):
        """Решение владельца charts=chartjs не отменяет собственные графики:
        charts.js остаётся для остальных экранов."""
        charts = _read(STATIC / "js" / "charts.js")
        assert "export function barChart" in charts
        assert "export function donutChart" in charts
        assert "без внешних зависимостей" in charts

    def test_spa_catch_all_prefix_is_unchanged(self):
        src = _read(APP / "main.py")
        assert '("api/", "onec/", "ws/", "maxapp/", "b2b/")' in src

    def test_vendor_jsqr_survives(self):
        assert (VENDOR / "jsQR.js").exists()
        assert "/js/vendor/jsQR.js" in _read(INDEX)

    def test_no_secret_value_or_column_is_leaked_into_the_ui(self):
        """Имена полей из ТЗ в подписях — не утечка: администратору полезно
        видеть, какое поле Keystone он заполняет. Утечкой было бы значение
        секрета или имя зашифрованной колонки в разметке."""
        js = _read(OSM_JS)
        assert "secret_encrypted" not in js
        assert SECRET not in js
        # Значение поля секрета в разметку не подставляется вовсе.
        m = re.search(r'<input id="osm-secret"[^>]*>', js)
        assert m and "value=" not in m.group(0), m.group(0) if m else ""
        # Имя зашифрованного поля не приходит и в ответе сервера.
        assert "app_cred_secret" in js, "подпись поля ТЗ исчезла"


# ==========================================================================
#  13б. Язык: токены со смесью письменностей
# ==========================================================================
class TestNoMixedScriptTokens:
    """Слепой участок аудита v1.70.48 закрыт.

    Правило аудита ловит латиницу внутри русских слов, но пропускает токен, в
    котором латинских знаков больше, чем кириллических: guard `cyr < lat`
    объявляет его «латинским словом, а не смесью». Через этот guard прошли два
    настоящих дефекта, оба найдены замером при подготовке этого выпуска:

    * `docs/dev_plan_checkpool.md` — «импersonate_pool_start», где «имп»
      набрано кириллицей (U+0438, U+043C, U+043F) перед латинским продолжением.
      Настоящее имя действия в коде — `impersonate_pool_start`
      (`app/pool/router_admin.py`, перевод в `app/services/audit_human.py`),
      поэтому поиск по документации не находил строку, а поиск по коду того,
      что в ней написано, не находил ничего. Исправлено.
    * `docs/ui_journal.md` этого выпуска — «с zamyslом» вместо «с задуманной».
      Исправлено.

    Пять токенов смешанного состава проверены руками и не являются дефектами;
    они перечислены ниже, чтобы находку не искали повторно.
    """

    EXEMPT = {
        # Имя класса в коде: кириллическое название и общепринятая
        # аббревиатура «QR». Прежнее, присутствует в HEAD.
        "СтрокаЧекаQR",
        # Артефакт разбора: токенизатор склеил «n» из escape-последовательности
        # \\n со следующим русским словом в тексте confirm() модуля osm.js.
        "nОпрос",
        # Цитата исправленного дефекта в записях прошлых выпусков: слово стоит
        # в кавычках именно как пример того, что было найдено и заменено на
        # «предоставлена».
        "udzielена",
    }

    def _findings(self):
        from tests.test_v17048 import FILE_EXT, _tokens, corpus

        out = []
        for rel, line, _kind, text in corpus():
            for part in _tokens(text):
                if FILE_EXT.search(part):
                    continue
                if part in self.EXEMPT:
                    continue
                has_cyr = bool(re.search(r"[а-яёА-ЯЁ]", part))
                has_lat = bool(re.search(r"[A-Za-z]", part))
                if has_cyr and has_lat:
                    out.append(f"{rel}:{line} {part!r}")
        return out

    def test_scan_is_clean(self):
        found = self._findings()
        assert not found, f"токены со смесью письменностей: {found[:8]}"

    def test_lock_is_not_vacuous(self):
        """Замок не вырожден: исключения действительно встречаются в корпусе,
        значит проверка выше не просто ничего не нашла."""
        from tests.test_v17048 import _tokens, corpus

        seen = set()
        for _rel, _line, _kind, text in corpus():
            seen.update(_tokens(text))
        present = self.EXEMPT & seen
        assert len(present) == len(self.EXEMPT), \
            f"исключения не найдены в корпусе: {sorted(self.EXEMPT - present)}"

    def test_documented_action_names_match_the_code(self):
        """Регрессия найденного дефекта: имена действий аудита в плане
        совпадают с именами в коде посимвольно."""
        plan = _read(DOCS / "dev_plan_checkpool.md")
        router = _read(APP / "pool" / "router_admin.py")
        for action in ("impersonate_pool_start", "impersonate_pool_stop"):
            assert action in router, f"{action}: нет в коде"
        assert "impersonate_pool_start/stop" in plan


# ==========================================================================
#  14. Выпуск v1.70.51
# ==========================================================================
class TestRelease17051:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.50) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.51"
    PREVIOUS = "1.70.50"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(APP / "config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read(SW)
        assert f'"version": "{self.VERSION}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        """Правка коснулась клиентских скриптов: без нового номера браузер и
        кэш приложения продолжили бы отдавать прежние файлы."""
        sw = _read(SW)
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'js/app.js?v={self.VERSION}'".replace("js/", "/js/") in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_new_module_is_served_with_the_release_version(self):
        """osm.js загружается как модуль из app.js, поэтому отдельного
        кэш-бастера не требует: его номер приходит вместе с app.js."""
        js = _read(APP_JS)
        assert "from './osm.js'" in js
        assert f"app.js?v={self.VERSION}" in _read(INDEX)

    def test_no_previous_version_left_in_sync_points(self):
        for path in (APP / "config.py", INDEX, SW, MANIFEST, MAX_HTML, CABINET):
            assert self.PREVIOUS not in _read(path), f"{path.name}: старая версия"

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self.VERSION, "запись выпуска не первая"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 5, "в записи выпуска не пять пунктов"
        for needle in ("облак", "вкладк", "порог", "секрет", "журнал"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"MAX", "Telegram", "service", "worker"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("вкладк", "порог", "журнал", "секрет", "облак",
                       "кэш-бастер", "Отклонено", "ретенц"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section, f"в CHANGELOG нет раздела «{head}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in (OWNER_QUOTE_HEAD, OWNER_QUOTE_TAIL):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "вкладк", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        """Цитата требования владельца присутствует дословно во всех трёх
        местах. Внутри цитаты допустим перевод строки — на состав слов он не
        влияет, поэтому сравнение идёт по тексту с нормализованными
        пробелами."""
        needle = re.sub(r"\s+", " ", OWNER_QUOTE_HEAD).lower()
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            assert needle in flat, f"{path.name}: нет цитаты владельца"

    def test_previous_release_class_is_history_only(self):
        """Класс прежнего выпуска больше не держит точки синхронизации: иначе
        каждый следующий релиз обязан был бы править чужой тест. Класс в своём
        файле последний, поэтому хвост берётся до конца файла."""
        src = _read(RELEASE_50_TESTS)
        block = src.split("class TestRelease17050:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_50_TESTS)
        block = src.split("class TestRelease17050:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
