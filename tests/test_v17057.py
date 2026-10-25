"""v1.70.57 — политика облака: честный код, примечание вместо приговора.

Жалоба владельца (дословно, существенная часть): «Подключение не прошло.
Keystone пройден, но Nova отказала: ForbiddenException:
ForbiddenException: 403: Client Error for url:
[https://os-api.…/compute/v2.1/…/os-hypervisors/detail], Policy doesn't
allow os_compute_api:os-hypervisors:list-detail to be performed. Код:
auth_denied» — и вдобавок чек-лист про пароль, хотя учётные данные уже
приняты. Требование владельца: «Проверь программу на баги».

Ревизия на настоящем openstacksdk 4.22.0 нашла четыре бага и один мёртвый
шаг:

* classify_error: ForbiddenException содержит «forbidden» в имени и уезжал
  в auth_denied, хотя отказ ПОЛИТИКИ при пройденном Keystone — не отказ
  учётных данных;
* карточка показывала чек-лист про секрет при authorized:true;
* текст задваивал имя исключения («ForbiddenException:
  ForbiddenException: …») — SDK встраивает имя в текст HTTP-отказа;
* test_connection не закрывал сессию: каждое нажатие «Проверить
  подключение» плодило висящее соединение;
* proxy.statistics(lb_id) у балансировщиков в SDK 4.x НЕ существует
  (замер: у Proxy нет атрибута) — шаг трафика вечно кончался
  AttributeError; документированная статистика Octavia — сквозной
  GET /v2/octavia/statistics;
* placement_inventories: сырой запрос SDK 4.x возвращает Response, а не
  словарь — ответ проваливался в json.dumps(default=str) и «разбирался»
  в строку "<Response [200]>".

Значения провайдера в этом файле заведомо подставные: настоящие адрес и
логин владельца в репозитории не хранятся (замок v1.70.52 это сторожит).

Что здесь проверяется:
 1. classify_error: «Policy doesn't allow…» → код «policy»; простой
    Forbidden и «(HTTP 403)» остаются auth_denied; старые ветки целы.
 2. exc_summary: задвоение имени устранено, обычные исключения с префиксом.
 3. Сценарий владельца: политика → ok:true + примечание на русском +
    предупреждение; сессия закрыта; неполитический отказ Nova по-прежнему
    фатален; catalog-отказ тоже закрывает сессию.
 4. Placement: Response.json() разбирается, dict-путь (мок v1.70.51) цел.
 5. Трафик: нет statistics → сквозной документированный запрос; старый
    путь со statistics жив; отказ даёт честную причину.
 6. osm.js: чек-лист про секрет защищён authorized, примечание
    рендерится и при успехе.
 7. Ритуал выпуска 1.70.57 и историзация TestRelease17056.
"""

import pathlib
import re

from keystoneauth1 import access as ksaccess
from openstack import exceptions as os_exc

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
RELEASE_56_TESTS = ROOT / "tests" / "test_v17056.py"

OWNER_QUOTE_HEAD = "keystone пройден, но nova отказала: forbiddenexception"
OWNER_QUOTE_TAIL = ("policy doesn't allow "
                    "os_compute_api:os-hypervisors:list-detail to be "
                    "performed")

FAKE_AUTH_URL = "https://os-api.example.ru/identity"

POLICY_MESSAGE = (
    "ForbiddenException: 403: Client Error for url: "
    "[https://os-api.example.ru/compute/v2.1/e6bc/os-hypervisors/detail], "
    "Policy doesn't allow os_compute_api:os-hypervisors:list-detail to be "
    "performed.")

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


def policy_exc():
    """Настоящий ForbiddenException с текстом отказа владельца."""
    return os_exc.ForbiddenException(POLICY_MESSAGE)


class RecSession:
    """Сессия-счётчик: каталог из тела токена, close() под учётом."""

    def __init__(self):
        outer = self

        class Auth:
            def get_access(self, session):
                return ksaccess.AccessInfoV3(CATALOG_BODY)

        self.auth = Auth()
        self.closed = 0
        self._outer = outer

    def close(self):
        self.closed += 1


class PolicyProxy:
    def hypervisors(self, details=False):
        raise policy_exc()


class BoomProxy:
    def hypervisors(self, details=False):
        raise RuntimeError("500 Server Error")


class FakeConn:
    def __init__(self, compute=None):
        self.session = RecSession()
        self.compute = compute
        self.authorized = False

    def authorize(self):
        self.authorized = True


CFG_OK = {
    "auth_url": FAKE_AUTH_URL, "auth_type": "password",
    "username": "monitor", "password": "sekrit", "project_name": "demo",
    "user_domain_name": "Default", "project_domain_name": "Default",
    "region_name": "ru-central", "interface": "public",
    "verify_tls": True, "timeout_sec": 30,
}


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestClassifyPolicy:
    """Отказ политики — не отказ учётных данных."""

    def test_policy_text_gives_code_policy(self):
        assert connection.classify_error(policy_exc()) == "policy"

    def test_plain_forbidden_stays_auth_denied(self):
        assert connection.classify_error(
            os_exc.ForbiddenException("Forbidden (HTTP 403)")) == "auth_denied"

    def test_sdk_http_codes_stay_auth_denied(self):
        class SDKException(Exception):
            pass

        assert connection.classify_error(
            SDKException("(HTTP 401)")) == "auth_denied"
        assert connection.classify_error(
            SDKException("(HTTP 403)")) == "auth_denied"

    def test_old_branches_are_intact(self):
        assert connection.classify_error(TimeoutError("timed out")) == \
            "timeout"
        assert connection.classify_error(ConnectionError("нет связи")) == \
            "connection"
        assert connection.classify_error(
            os_exc.ServiceDisabledException("x")) == "catalog"
        assert connection.classify_error(ValueError("прочее")) == "ValueError"


class TestExcSummary:
    """Одной строкой, без задвоения имени."""

    def test_sdk_embedded_name_is_not_doubled(self):
        text = connection.exc_summary(policy_exc())
        assert text.startswith("ForbiddenException: 403:")
        assert "ForbiddenException: ForbiddenException" not in text

    def test_plain_exception_keeps_prefix(self):
        assert connection.exc_summary(RuntimeError("403 Forbidden")) == \
            "RuntimeError: 403 Forbidden"

    def test_empty_text_gives_name(self):
        assert connection.exc_summary(ValueError("")) == "ValueError"

    def test_text_equal_to_name_is_not_doubled(self):
        class Boom(Exception):
            pass

        assert connection.exc_summary(Boom("Boom")) == "Boom"


class TestPolicyIsNoticeNotVerdict:
    """Сценарий владельца: подключение работает, статистика запрещена."""

    def test_policy_gives_ok_with_notice(self, monkeypatch):
        conn = FakeConn(PolicyProxy())
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(CFG_OK)
        assert result["ok"] is True, result
        assert result["authorized"] is True
        assert result["hypervisor"] is None
        assert result["error_code"] == ""
        assert "политика" in result["notice"]
        assert "Keystone" in result["notice"]
        assert "администратора" in result["notice"]
        assert any("запрещена политикой" in w for w in result["warnings"])

    def test_notice_hides_nothing_and_leaks_nothing(self, monkeypatch):
        conn = FakeConn(PolicyProxy())
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(CFG_OK)
        assert "403" in result["notice"], "ответ облака процитирован честно"
        assert "sekrit" not in str(result)
        assert "ForbiddenException: ForbiddenException" not in result["notice"]

    def test_session_is_closed_after_policy_notice(self, monkeypatch):
        conn = FakeConn(PolicyProxy())
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        connection.test_connection(CFG_OK)
        assert conn.session.closed == 1

    def test_non_policy_nova_failure_is_still_fatal(self, monkeypatch):
        conn = FakeConn(BoomProxy())
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(CFG_OK)
        assert result["ok"] is False
        assert result["error"].startswith(
            "Keystone пройден, но Nova отказала: RuntimeError")
        assert conn.session.closed == 1

    def test_catalog_refusal_also_closes_session(self, monkeypatch):
        conn = FakeConn(BoomProxy())
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection({**CFG_OK, "region_name": "fi"})
        assert result["error_code"] == "catalog"
        assert conn.session.closed == 1


class TestPlacementResponseParsing:
    """Сырой запрос SDK 4.x возвращает Response — его json() читается."""

    class Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if self._payload is None:
                raise ValueError("нет тела")
            return self._payload

    def conn_with(self, response):
        class Proxy:
            def get(self, url):
                assert "inventories" in url
                return response

        return type("C", (), {"placement": Proxy()})()

    def test_response_json_is_parsed(self):
        data, why = collector.placement_inventories(
            self.conn_with(self.Resp({"inventories": {"VCPU": {"total": 64,
                                                               "used": 12}}})),
            {"resource_provider_uuid": "abc-123"})
        assert why == ""
        assert data == {"inventories": {"VCPU": {"total": 64, "used": 12}}}

    def test_dict_path_is_preserved(self):
        # Мок v1.70.51 возвращает словарь — прежний путь не сломан.
        data, why = collector.placement_inventories(
            self.conn_with({"inventories": {"VCPU": {"total": 8,
                                                     "used": 1}}}),
            {"resource_provider_uuid": "abc"})
        assert why == "" and data["inventories"]["VCPU"]["total"] == 8

    def test_unreadable_body_gives_reason(self):
        data, why = collector.placement_inventories(
            self.conn_with(self.Resp(None)),
            {"resource_provider_uuid": "abc"})
        assert data is None and "не удалось разобрать" in why


class TestTrafficDocumentedFallback:
    """Шаг трафика: сквозной запрос вместо отсутствующего метода."""

    class Lb:
        id = "lb-1"

    class StatsResp:
        def json(self):
            return {"bytes_in": 1024, "bytes_out": 2048,
                    "active_connections": 3, "total_connections": 9}

    def conn_with(self, proxy):
        return type("C", (), {"load_balancer": proxy})()

    def test_whole_cloud_statistics_is_used(self):
        class Proxy:
            def load_balancers(self):
                return iter([TestTrafficDocumentedFallback.Lb()])

            def get(self, url):
                assert url == "octavia/statistics"
                return TestTrafficDocumentedFallback.StatsResp()

        tin, tout, note = collector.traffic_bytes(self.conn_with(Proxy()))
        assert (tin, tout) == (1024, 2048)
        assert "сквозная" in note

    def test_old_statistics_path_is_preserved(self):
        # Мок v1.70.51 определяет statistics(lb_id) — старый путь жив.
        class Proxy:
            def load_balancers(self):
                return iter([TestTrafficDocumentedFallback.Lb()])

            def statistics(self, lb_id):
                return {"bytes_in": 1073741824, "bytes_out": 2147483648}

        tin, tout, note = collector.traffic_bytes(self.conn_with(Proxy()))
        assert tin == 1073741824 and tout == 2147483648
        assert "собрано" in note

    def test_refusal_gives_honest_reason(self):
        class Proxy:
            def load_balancers(self):
                return iter([TestTrafficDocumentedFallback.Lb()])

            def get(self, url):
                raise os_exc.ForbiddenException("Forbidden")

        tin, tout, note = collector.traffic_bytes(self.conn_with(Proxy()))
        assert tin is None and tout is None
        assert "недоступна" in note and "ForbiddenException" in note

    def test_unreadable_statistics_gives_reason(self):
        class Proxy:
            def load_balancers(self):
                return iter([TestTrafficDocumentedFallback.Lb()])

            def get(self, url):
                return object()

        tin, tout, note = collector.traffic_bytes(self.conn_with(Proxy()))
        assert tin is None and "не разобран" in note


class TestUiPolicyFixes:
    """Карточка проверки: чек-лист по делу, примечание видно."""

    def test_secret_checklist_is_guarded_by_authorized(self):
        js = _read(OSM_JS)
        assert "result.authorized !== true" in js

    def test_notice_is_rendered(self):
        js = _read(OSM_JS)
        assert "result.notice" in js
        assert "esc(result.notice)" in js

    def test_previous_hints_are_intact(self):
        js = _read(OSM_JS)
        assert "auth_denied" in js
        assert "HTTP 40[13]" in js
        assert "const catalogIssue" in js
        assert "+ catalogHint" in js


class TestRelease17057:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.58).
    Здесь остаётся запись выпуска 1.70.57 в истории."""

    VERSION = "1.70.57"
    PREVIOUS = "1.70.56"

    def test_version_is_release(self):
        src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        # живое значение сверяет выпуск-владелец; здесь — форма релиза
        assert re.search(r"APP_VERSION: str = \"1\.70\.\d+\"", src)

    def test_changelog_has_release(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        assert f"## [{self.VERSION}]" in text

    def test_changelog_quotes_the_owner(self):
        section = CHANGELOG.read_text(encoding="utf-8").split(
            f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        assert "policy doesn't allow" in flat

    def test_journal_has_release(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_whats_new_entry_exists_in_history(self):
        js = APP_JS.read_text(encoding="utf-8")
        assert f"'{self.VERSION}': [" in js

    def test_trackers_reference_a_release(self):
        # история: трекеры существуют и сверяют версию выпуска формой
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert re.search(r'"1\.70\.\d+"', text)

    def test_sw_cache_name_is_versioned(self):
        sw = SW.read_text(encoding="utf-8")
        assert re.search(r"ymaster-check-v1\.70\.\d+", sw)

    def test_no_placeholder_left(self):
        for path in (CHANGELOG, JOURNAL):
            assert "TODO" not in path.read_text(encoding="utf-8")
