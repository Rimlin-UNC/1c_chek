"""v1.70.56 — сводка гипервизоров по документации openstacksdk.

Жалоба владельца (дословно, существенная часть): «Keystone пройден, но
Nova отказала: AttributeError: 'Proxy' object has no attribute
'get_hypervisor_statistics'. Код: AttributeError» при честных значках
(compute, placement, metric, network, image — «доступен», load_balancer —
«нет в каталоге») и требование: «Исправь как должно быть в документации».

Пара регион + интерфейс после v1.70.55 совпала с каталогом, сверка
пропустила запрос дальше — и отказала наша собственная программа: оба
места потребления сводки (проверка подключения и шаг 1 сборщика) звали
``conn.compute.get_hypervisor_statistics()``, которого в compute-прокси
openstacksdk 4.x НЕТ. Документированный API — ``hypervisors(details=True)``
(Nova ``GET /os-hypervisors/detail``), а сводка считается суммой полей
каждого гипервизора, как Nova считала собственный statistics: всё
суммируется, кроме ``disk_available_least`` — там минимум. Атрибуты
ресурса в 4.x переименованы относительно проводных имён Nova
(memory_mb → memory_size, local_gb → local_disk_size и т.д.).

Значения провайдера в этом файле заведомо подставные: настоящие адрес и
логин владельца в репозитории не хранятся (замок v1.70.52 это сторожит).

Что здесь проверяется:
 1. Корень жалобы зафиксирован на настоящем SDK: у Proxy нет старого
    метода, документированный hypervisors есть, карта полей соединяет
    канонические атрибуты 4.x с проводными именами.
 2. hypervisor_totals новым путём: точные суммы, минимум
    disk_available_least, счётчик count, запасной getattr для проводных
    имён, пропуск None-полей, пустой список.
 3. Старый путь: родной метод get_hypervisor_statistics зовётся и
    нормализуется прежним _as_plain, форма без count.
 4. test_connection: успех со сводкой, отказ Nova обёрнут прежним текстом,
    сверка каталога по-прежнему раньше Nova.
 5. collector.hypervisor_stats: прежняя форма (10 проводных ключей),
    причина отказа, подстановное облако v1.70.51 работает.
 6. Ритуал выпуска 1.70.56: шесть точек синхронизации, «Что нового»
    первым, CHANGELOG, журнал, цитата владельца, историзация
    TestRelease17055.
"""

import pathlib
import re

from keystoneauth1 import access as ksaccess
from openstack.compute.v2 import _proxy as compute_proxy

from app.openstack_monitor import collector, connection

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
APP_JS = STATIC / "js" / "app.js"
OSM_JS = STATIC / "js" / "osm.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_55_TESTS = ROOT / "tests" / "test_v17055.py"

OWNER_QUOTE_HEAD = "keystone пройден, но nova отказала: attributeerror"
OWNER_QUOTE_TAIL = "исправь как должно быть в документации"

FAKE_AUTH_URL = "https://os-api.example.ru/identity"


class HvCanonical:
    """Гипервизор с каноническими атрибутами openstacksdk 4.x."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Proxy4:
    """Compute-прокси новой ветки: родного метода статистики НЕТ."""

    def __init__(self, hypervisors=None, fail=None):
        self._hypervisors = hypervisors or []
        self._fail = fail

    def hypervisors(self, details=False):
        assert details is True, "документирован вызов с подробностями"
        if self._fail is not None:
            raise self._fail
        return iter(self._hypervisors)


class ProxyLegacy:
    """Compute-прокси старой ветки: родной метод статистики жив."""

    def get_hypervisor_statistics(self):
        return HvCanonical(vcpus=64, vcpus_used=8, memory_mb=131072,
                           memory_mb_used=16384, free_ram_mb=114688,
                           local_gb=2048, local_gb_used=200,
                           free_disk_gb=1848, running_vms=4,
                           current_workload=1, disk_available_least=1800)


class ConnBase:
    def __init__(self, compute):
        self.compute = compute

    def authorize(self):
        self.authorized = True


CATALOG_BODY = {"token": {
    "methods": ["password"],
    "expires_at": "2099-01-01T00:00:00.000000Z",
    "catalog": [
        {"type": "compute", "name": "nova", "endpoints": [
            {"interface": "public", "region": "ru-central",
             "region_id": "ru-central", "url": "https://nova.example.ru/v2.1"}]},
        {"type": "identity", "name": "keystone", "endpoints": [
            {"interface": "public", "region": "ru-central",
             "region_id": "ru-central", "url": FAKE_AUTH_URL}]},
    ],
    "user": {"id": "u1", "name": "fake",
             "domain": {"id": "default", "name": "Default"}},
    "project": {"id": "p1", "name": "demo",
                "domain": {"id": "default", "name": "Default"}},
    "audit_ids": ["x"],
}}


def conn_with_catalog(compute):
    """Подключение: каталог ru-central + public, авторизация без сети."""

    class Auth:
        def get_access(self, session):
            return ksaccess.AccessInfoV3(CATALOG_BODY)

    class Conn(ConnBase):
        session = type("S", (), {"auth": Auth()})()

    return Conn(compute)


CFG_OK = {
    "auth_url": FAKE_AUTH_URL, "auth_type": "password",
    "username": "monitor", "password": "sekrit", "project_name": "demo",
    "user_domain_name": "Default", "project_domain_name": "Default",
    "region_name": "ru-central", "interface": "public",
    "verify_tls": True, "timeout_sec": 30,
}

TWO_HV = [
    HvCanonical(vcpus=64, vcpus_used=10, memory_size=131072,
                memory_used=40960, memory_free=90112, local_disk_size=2048,
                local_disk_used=500, local_disk_free=1548, running_vms=5,
                current_workload=2, disk_available=1500),
    HvCanonical(vcpus=32, vcpus_used=4, memory_size=65536,
                memory_used=8192, memory_free=57344, local_disk_size=1024,
                local_disk_used=100, local_disk_free=924, running_vms=2,
                current_workload=1, disk_available=900),
]


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestSdkRootCause:
    """Корень жалобы, замеренный на настоящей установленной библиотеке."""

    def test_proxy_has_no_legacy_statistics_method(self):
        assert not hasattr(compute_proxy.Proxy,
                           "get_hypervisor_statistics"), \
            "в openstacksdk 4.x метода нет — ровно отказ владельца"

    def test_proxy_has_documented_hypervisors(self):
        assert callable(getattr(compute_proxy.Proxy, "hypervisors", None))

    def test_field_map_joins_canonical_and_wire_names(self):
        pairs = dict(connection._HYPERVISOR_FIELDS)
        assert pairs["memory_size"] == "memory_mb"
        assert pairs["memory_used"] == "memory_mb_used"
        assert pairs["memory_free"] == "free_ram_mb"
        assert pairs["local_disk_size"] == "local_gb"
        assert pairs["local_disk_used"] == "local_gb_used"
        assert pairs["local_disk_free"] == "free_disk_gb"
        assert pairs["disk_available"] == "disk_available_least"
        assert pairs["vcpus"] == "vcpus"
        assert pairs["running_vms"] == "running_vms"
        assert pairs["current_workload"] == "current_workload"
        assert len(connection._HYPERVISOR_FIELDS) == 11


class TestTotalsNewPath:
    """Документированный путь 4.x: сумма полей hypervisors(details=True)."""

    def test_canonical_attributes_are_summed(self):
        totals = connection.hypervisor_totals(ConnBase(Proxy4(TWO_HV)))
        assert totals == {
            "vcpus": 96, "vcpus_used": 14, "memory_mb": 196608,
            "memory_mb_used": 49152, "free_ram_mb": 147456,
            "local_gb": 3072, "local_gb_used": 600, "free_disk_gb": 2472,
            "running_vms": 7, "current_workload": 3,
            "disk_available_least": 900, "count": 2,
        }

    def test_disk_available_least_is_minimum_not_sum(self):
        totals = connection.hypervisor_totals(ConnBase(Proxy4(TWO_HV)))
        assert totals["disk_available_least"] == 900

    def test_wire_named_resources_are_read_by_fallback(self):
        wire = [HvCanonical(vcpus=8, vcpus_used=1, memory_mb=16384,
                            memory_mb_used=2048, local_gb=100,
                            local_gb_used=10, running_vms=1)]
        totals = connection.hypervisor_totals(ConnBase(Proxy4(wire)))
        assert totals["memory_mb"] == 16384
        assert totals["local_gb"] == 100
        assert totals["count"] == 1

    def test_none_and_bad_fields_are_skipped(self):
        partial = [HvCanonical(vcpus=None, memory_size="не число",
                               local_disk_size=500, running_vms=3)]
        totals = connection.hypervisor_totals(ConnBase(Proxy4(partial)))
        assert "vcpus" not in totals
        assert "memory_mb" not in totals
        assert totals["local_gb"] == 500
        assert totals["running_vms"] == 3

    def test_empty_cloud_gives_zero_count(self):
        totals = connection.hypervisor_totals(ConnBase(Proxy4([])))
        assert totals == {"count": 0}

    def test_proxy_failure_propagates_to_caller(self):
        conn = ConnBase(Proxy4(fail=RuntimeError("403 Forbidden")))
        try:
            connection.hypervisor_totals(conn)
            raise AssertionError("отказ прокси обязан долететь до вызывающего")
        except RuntimeError as exc:
            assert "403" in str(exc)


class TestTotalsLegacyPath:
    """Старый SDK: родной метод статистики зовётся и нормализуется."""

    def test_legacy_method_is_used_and_shape_kept(self):
        totals = connection.hypervisor_totals(ConnBase(ProxyLegacy()))
        assert totals["vcpus"] == 64
        assert totals["memory_mb"] == 131072
        assert totals["disk_available_least"] == 1800
        assert "count" not in totals, "старая форма не обрастает новым ключом"

    def test_legacy_values_are_normalized_to_int(self):
        class StrStats:
            vcpus = "64"
            memory_mb = "131072"

        class ProxyStr:
            def get_hypervisor_statistics(self):
                return StrStats()

        totals = connection.hypervisor_totals(ConnBase(ProxyStr()))
        assert totals["vcpus"] == 64 and totals["memory_mb"] == 131072


class TestConnectionUsesTotals:
    """Проверка подключения: успех, отказ Nova, приоритет сверки каталога."""

    def test_ok_with_summed_hypervisor(self, monkeypatch):
        conn = conn_with_catalog(Proxy4(TWO_HV))
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(CFG_OK)
        assert result["ok"] is True, result
        assert result["hypervisor"]["vcpus"] == 96
        assert result["hypervisor"]["count"] == 2
        assert result["services"]["compute"] == "доступен"
        assert "sekrit" not in str(result)

    def test_nova_failure_is_wrapped_as_before(self, monkeypatch):
        conn = conn_with_catalog(
            Proxy4(fail=RuntimeError("500 Internal Server Error")))
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(CFG_OK)
        assert result["ok"] is False
        assert result["error"].startswith("Keystone пройден, но Nova отказала")
        assert result["authorized"] is True
        assert result["hypervisor"] is None

    def test_catalog_check_still_runs_before_nova(self, monkeypatch):
        # Регион не совпал — до hypervisors дело не доходит (регрессия v1.70.54).
        conn = conn_with_catalog(
            Proxy4(fail=AssertionError("до Nova не должно дойти")))
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection({**CFG_OK, "region_name": "fi"})
        assert result["error_code"] == "catalog"
        assert result["hypervisor"] is None


class TestCollectorUsesTotals:
    """Сборщик метрик: форма прежняя, контракт «числа, причина» цел."""

    def test_stats_have_same_ten_wire_keys(self):
        nums, why = collector.hypervisor_stats(ConnBase(Proxy4(TWO_HV)))
        assert why == ""
        assert nums == {
            "vcpus": 96, "vcpus_used": 14, "memory_mb": 196608,
            "memory_mb_used": 49152, "free_ram_mb": 147456,
            "local_gb": 3072, "local_gb_used": 600, "free_disk_gb": 2472,
            "running_vms": 7, "current_workload": 3,
        }

    def test_failure_gives_reason_not_exception(self):
        nums, why = collector.hypervisor_stats(
            ConnBase(Proxy4(fail=RuntimeError("403 Forbidden"))))
        assert nums == {}
        assert why == "RuntimeError: 403 Forbidden"

    def test_legacy_fake_cloud_still_served(self):
        # Подстановное облако v1.70.51 определяет родной метод — сборщик
        # обязан читать его без правок старых замков.
        nums, why = collector.hypervisor_stats(ConnBase(ProxyLegacy()))
        assert why == "" and nums["vcpus"] == 64


class TestRelease17056:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.57).
    Здесь остаётся запись выпуска 1.70.56 в истории."""

    VERSION = "1.70.56"
    PREVIOUS = "1.70.55"

    def test_version_is_release(self):
        src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        # живое значение сверяет выпуск-владелец; здесь — форма релиза
        assert re.search(r"APP_VERSION: str = \"1\.70\.\d+\"", src)

    def test_changelog_has_release(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{self.VERSION}]" in text

    def test_changelog_quotes_the_owner(self):
        section = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split(
            f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        assert "get_hypervisor_statistics" in flat

    def test_journal_has_release(self):
        text = (ROOT / "docs" / "ui_journal.md").read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = (ROOT / "docs" / "ui_journal.md").read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS}")

    def test_whats_new_entry_exists_in_history(self):
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        assert f"'{self.VERSION}': [" in js

    def test_trackers_reference_current_release(self):
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert '"1.70.57"' in text

    def test_sw_cache_name_matches_history(self):
        sw = (ROOT / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        assert "ymaster-check-v1.70.57" in sw

    def test_no_placeholder_left(self):
        for path in (ROOT / "CHANGELOG.md", ROOT / "docs" / "ui_journal.md"):
            assert "TODO" not in path.read_text(encoding="utf-8")
