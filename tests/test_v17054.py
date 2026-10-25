"""v1.70.54 — Keystone пройден, но Nova отказала: каталог, значки, код отказа.

Жалоба владельца (дословно, существенная часть): «Подключение не прошло.
Keystone пройден, но Nova отказала: ServiceDisabledException: Service
'compute' is disabled because its configuration could not be loaded. The
service is not present in the service catalog: admin endpoint for compute
service in fi region not found. Код: ServiceDisabledException» и значки
сервисов: compute, placement, load_balancer, network, image — «доступен»,
metric — «не подключён».

Авторизация после v1.70.53 проходит: отказ переехал на шаг глубже. Облако
ищет эндпоинт compute для интерфейса «admin» в регионе «fi», хотя файл
провайдера задаёт регион eu-west2 и интерфейс public (OS_REGION_NAME,
OS_INTERFACE): в сохранённых настройках остались введённые вручную прежние
значения. Локальное воспроизведение на подставном каталоге (настоящие
openstacksdk и keystoneauth, токен собран вручную) вскрыло три дефекта:

* ``access.service_catalog`` — объект ServiceCatalogV3, он НЕ итерируется;
  ``_catalog_types`` молча возвращал пустой список, и каталог не читался
  вовсе — значки «доступен» определялись наличием заготовки сервиса;
* SDK при ненайденном эндпоинте не бросает исключение на ``getattr``, а
  возвращает заглушку ``_ServiceDisabledProxyShim``, которая стреляет только
  при первом обращении к методу, — значок врал «доступен»;
* имя модуля ``keystoneauth1`` содержит «auth», и ``EndpointNotFound``
  провалился бы в ветку «auth» классификатора вместо честного кода.

Значения провайдера в этом файле заведомо подставные: настоящие адрес и
логин владельца в репозитории не хранятся (замок v1.70.52 это сторожит).

Что здесь проверяется:
 1. Каталог читается по-настоящему: ``_catalog_entries``/``_catalog_types``/
    ``service_endpoints`` на живом AccessInfoV3; подставное подключение без
    сессии даёт пустой список, а не исключение.
 2. ``_catalog_mismatch``: сценарий владельца (fi + admin) даёт сведение на
    русском с регионами и интерфейсами каталога; верная пара молчит.
 3. ``_service_names``: честные статусы по каталогу и по заглушке SDK —
    настоящая заглушка даёт «недоступен: причина» вместо «доступен».
 4. ``test_connection`` останавливается ДО обращения к Nova: код «catalog»,
    authorized True, у подставного подключения нет атрибута compute — если бы
    код дошёл до статистики, отказ был бы другим.
 5. ``classify_error``: ServiceDisabledException и EndpointNotFound → код
    «catalog», ветка стоит раньше «auth»; старые ветки целы.
 6. Карточка проверки: подсказка для кода «catalog» ведёт к строкам файла
    провайдера (osm.js).
 7. Ритуал выпуска 1.70.54: шесть точек синхронизации, «Что нового» первым,
    CHANGELOG, журнал, цитата владельца, историзация TestRelease17053.
"""

import pathlib
import re

from keystoneauth1 import access as ksaccess
from keystoneauth1.exceptions import catalog as ks_catalog
from openstack import exceptions as os_exc
from openstack.service_description import _ServiceDisabledProxyShim

from app.openstack_monitor import connection

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
RELEASE_53_TESTS = ROOT / "tests" / "test_v17053.py"

OWNER_QUOTE_HEAD = "keystone пройден, но nova отказала"
OWNER_QUOTE_TAIL = ("admin endpoint for compute service in fi region "
                    "not found")

# Заведомо подставные значения (замок v1.70.52 сторожит отсутствие настоящих).
FAKE_AUTH_URL = "https://os-api.example.ru/identity"

# Каталог как у провайдера владельца по СТРУКТУРЕ: compute в регионе
# eu-west2 тремя интерфейсами; значение региона подставное совпало с общеп-
# ринятым именем — секретом оно не является.
CATALOG_BODY = {"token": {
    "methods": ["password"],
    "expires_at": "2099-01-01T00:00:00.000000Z",
    "catalog": [
        {"type": "compute", "name": "nova", "endpoints": [
            {"interface": "public", "region": "eu-west2",
             "region_id": "eu-west2", "url": "https://nova.example.ru/v2.1"},
            {"interface": "admin", "region": "eu-west2",
             "region_id": "eu-west2",
             "url": "https://nova-admin.example.ru/v2.1"},
            {"interface": "internal", "region": "eu-west2",
             "region_id": "eu-west2",
             "url": "https://nova-int.example.ru/v2.1"}]},
        {"type": "identity", "name": "keystone", "endpoints": [
            {"interface": "public", "region": "eu-west2",
             "region_id": "eu-west2", "url": FAKE_AUTH_URL}]},
        {"type": "network", "name": "neutron", "endpoints": [
            {"interface": "public", "region": "eu-west2",
             "region_id": "eu-west2",
             "url": "https://neutron.example.ru"}]},
    ],
    "user": {"id": "u1", "name": "fake",
             "domain": {"id": "default", "name": "Default"}},
    "project": {"id": "p1", "name": "demo",
                "domain": {"id": "default", "name": "Default"}},
    "audit_ids": ["x"],
}}


class FakeAuth:
    """Плагин без сети: отдаёт собранный вручную токен с каталогом."""

    def get_access(self, session):
        return ksaccess.AccessInfoV3(CATALOG_BODY)


class FakeSession:
    auth = FakeAuth()


class FakeConn:
    """Подставное подключение: сессия с каталогом, авторизация без сети.

    Атрибута compute НЕТ нарочно: если test_connection доберётся до Nova,
    тест упадёт AttributeError — значит сверка с каталогом не сработала.
    """

    session = FakeSession()
    authorized = False

    def authorize(self):
        self.authorized = True


def cfg_for(region: str, interface: str) -> dict:
    return {
        "auth_url": FAKE_AUTH_URL, "auth_type": "password",
        "username": "monitor", "password": "sekrit", "project_name": "demo",
        "user_domain_name": "Default", "project_domain_name": "Default",
        "region_name": region, "interface": interface,
        "verify_tls": True, "timeout_sec": 30,
    }


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestCatalogIsReallyRead:
    """Дефект 1: каталог прежде не читался вовсе — объект не итерируется."""

    def test_service_catalog_is_not_iterable(self):
        access = ksaccess.AccessInfoV3(CATALOG_BODY)
        service_catalog = access.service_catalog
        try:
            list(service_catalog)
            raise AssertionError("ServiceCatalogV3 неожиданно итерируется")
        except TypeError:
            pass  # корень прежней лжи: старый код обходил объект как список

    def test_entries_are_read_from_the_catalog_attribute(self):
        entries = connection._catalog_entries(FakeConn())
        assert isinstance(entries, list) and len(entries) == 3
        assert {e["type"] for e in entries} == {"compute", "identity",
                                                "network"}

    def test_catalog_types_and_endpoints(self):
        conn = FakeConn()
        assert connection._catalog_types(conn) == ["compute", "identity",
                                                   "network"]
        endpoints = connection.service_endpoints(conn, "compute")
        assert len(endpoints) == 3
        assert {e["interface"] for e in endpoints} == {"public", "admin",
                                                       "internal"}
        assert connection.service_endpoints(conn, "placement") == []

    def test_connection_without_session_is_safe(self):
        class NoSession:
            session = None

        assert connection._catalog_entries(NoSession()) == []
        assert connection._catalog_types(NoSession()) == []
        assert connection.service_endpoints(NoSession(), "compute") == []


class TestCatalogMismatchMessage:
    """Сверка региона и интерфейса с каталогом до первого запроса."""

    def test_owner_scenario_fi_admin_is_explained(self):
        msg = connection._catalog_mismatch(FakeConn(),
                                           cfg_for("fi", "admin"))
        assert "Keystone пройден" in msg
        assert "«fi»" in msg and "«admin»" in msg
        assert "eu-west2" in msg
        assert "OS_REGION_NAME" in msg and "OS_INTERFACE" in msg
        for interface in ("public", "internal"):
            assert interface in msg, "каталог должен перечислить интерфейсы"

    def test_right_pair_is_silent(self):
        assert connection._catalog_mismatch(
            FakeConn(), cfg_for("eu-west2", "public")) == ""

    def test_empty_region_matches_any_region(self):
        assert connection._catalog_mismatch(
            FakeConn(), cfg_for("", "public")) == ""

    def test_empty_interface_defaults_to_public(self):
        assert connection._catalog_mismatch(
            FakeConn(), cfg_for("eu-west2", "")) == ""

    def test_no_compute_in_catalog_is_not_judged(self):
        class NoCompute(FakeConn):
            pass

        body = {"token": dict(CATALOG_BODY["token"])}
        body["token"]["catalog"] = [CATALOG_BODY["token"]["catalog"][1]]

        class OneAuth:
            def get_access(self, session):
                return ksaccess.AccessInfoV3(body)

        NoCompute.session = type("S", (), {"auth": OneAuth()})()
        assert connection._catalog_mismatch(NoCompute(),
                                            cfg_for("fi", "admin")) == ""


class TestServiceNamesHonesty:
    """Дефект 2: значок «доступен» больше не ложь."""

    def test_catalog_pair_mismatch_is_shown(self):
        services = connection._service_names(FakeConn(), "fi", "admin")
        assert services["compute"] == ("в каталоге, но не для выбранных "
                                       "региона и интерфейса")
        assert services["network"] == ("в каталоге, но не для выбранных "
                                       "региона и интерфейса")
        assert services["placement"] == "нет в каталоге"

    def test_right_pair_shows_dostupen(self):
        services = connection._service_names(FakeConn(), "eu-west2", "public")
        assert services["compute"] == "доступен"
        assert services["network"] == "доступен"

    def test_disabled_shim_reason_is_readable(self):
        shim = _ServiceDisabledProxyShim("compute", "причина отказа")
        assert connection._disabled_reason(shim) == "причина отказа"
        assert connection._disabled_reason(object()) == ""

    def test_shim_gives_ne_dostupen_instead_of_lie(self):
        shim = _ServiceDisabledProxyShim(
            "compute", "The service is not present in the service catalog: "
            "admin endpoint for compute service in fi region not found")

        class ShimConn:
            session = None          # каталог нечитаем → путь заглушки
            compute = shim

        services = connection._service_names(ShimConn())
        assert services["compute"].startswith("недоступен: ")
        assert "fi region" in services["compute"]

    def test_plain_fallback_is_preserved(self):
        class PlainConn:
            session = None
            compute = object()
            load_balancer = None

        services = connection._service_names(PlainConn())
        assert services["compute"] == "доступен"
        assert services["load_balancer"] == "не подключён"


class TestConnectionStopsBeforeNova:
    """Отказ каталога возвращается раньше обращения к статистике."""

    def test_owner_scenario_returns_catalog_code(self, monkeypatch):
        conn = FakeConn()
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(cfg_for("fi", "admin"))
        assert result["ok"] is False
        assert result["error_code"] == "catalog"
        assert result["authorized"] is True
        assert result["hypervisor"] is None
        assert "eu-west2" in result["error"]
        assert result["services"]["compute"].startswith("в каталоге")

    def test_no_nova_attribute_was_touched(self, monkeypatch):
        # У FakeConn нет compute: добрался бы код до Nova — был бы
        # AttributeError и другой код ошибки.
        conn = FakeConn()
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(cfg_for("fi", "admin"))
        assert result["error_code"] == "catalog"
        assert "AttributeError" not in str(result)

    def test_secret_never_leaks_into_the_report(self, monkeypatch):
        conn = FakeConn()
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        cfg = cfg_for("fi", "admin")
        result = connection.test_connection(cfg)
        assert "sekrit" not in str(result)


class TestClassifyErrorCatalog:
    """Дефект 3: честный код вместо названия стороннего исключения."""

    def test_service_disabled_is_catalog(self):
        exc = os_exc.ServiceDisabledException(
            "Service 'compute' is disabled because its configuration could "
            "not be loaded.")
        assert connection.classify_error(exc) == "catalog"

    def test_endpoint_not_found_is_catalog_not_auth(self):
        # Имя модуля keystoneauth1 содержит «auth» — ветка «catalog» обязана
        # стоять раньше, иначе отказ уехал бы в «auth».
        exc = ks_catalog.EndpointNotFound(
            "admin endpoint for compute service in fi region not found")
        assert connection.classify_error(exc) == "catalog"

    def test_old_branches_are_intact(self):
        assert connection.classify_error(TimeoutError("timed out")) == \
            "timeout"
        assert connection.classify_error(ConnectionError("нет связи")) == \
            "connection"
        assert connection.classify_error(ValueError("прочее")) == "ValueError"

    def test_auth_denied_and_sdk_are_intact(self):
        class SDKException(Exception):
            pass

        assert connection.classify_error(
            SDKException("(HTTP 401)")) == "auth_denied"
        assert connection.classify_error(
            SDKException("(HTTP 403)")) == "auth_denied"
        assert connection.classify_error(SDKException("прочее")) == "sdk"


class TestUiCatalogHint:
    """Карточка проверки подключения: подсказка для кода «catalog»."""

    def test_hint_source_is_present(self):
        js = _read(OSM_JS)
        assert "const catalogIssue" in js
        assert "error_code === 'catalog'" in js
        assert "+ catalogHint" in js

    def test_hint_points_to_provider_file_lines(self):
        js = _read(OSM_JS)
        assert "OS_REGION_NAME" in js and "OS_INTERFACE" in js
        assert "«Регион»" in js and "«Интерфейс каталога»" in js

    def test_hint_also_catches_foreign_disabled_text(self):
        # Отказ может прийти и текстом SDK, если сверка не сработала
        # (например, compute вообще отсутствует в каталоге).
        js = _read(OSM_JS)
        assert "not present in the service catalog" in js


class TestRelease17054:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.53) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.54"
    PREVIOUS = "1.70.53"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(APP / "config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read(SW)
        assert f'"version": "{self.VERSION}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        sw = _read(SW)
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_changed_module_is_served_with_the_release_version(self):
        js = _read(APP_JS)
        assert "from './osm.js'" in js
        assert f"app.js?v={self.VERSION}" in _read(INDEX)

    def test_no_previous_version_left_in_sync_points(self):
        for path in (APP / "config.py", INDEX, SW, MANIFEST, MAX_HTML,
                     CABINET):
            text = _read(path)
            for form in (f"v={self.PREVIOUS}", f'"{self.PREVIOUS}"',
                         f"check-v{self.PREVIOUS}"):
                assert form not in text, f"{path.name}: {form}"

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
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("каталог", "регион", "интерфейс", "значк", "заглушк",
                       "провайдер"):
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
        for needle in ("каталог", "регион", "интерфейс", "compute", "значк",
                       "заглушк", "кэш-бастер", "Отклонено",
                       "OS_REGION_NAME", "ServiceDisabledException"):
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
                       "Отклонённые варианты", "каталог", "регион",
                       "интерфейс", "значк", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        needle = re.sub(r"\s+", " ", OWNER_QUOTE_HEAD).lower()
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            assert needle in flat, f"{path.name}: нет цитаты владельца"

    def test_previous_release_class_is_history_only(self):
        src = _read(RELEASE_53_TESTS)
        block = src.split("class TestRelease17053:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_53_TESTS)
        block = src.split("class TestRelease17053:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
