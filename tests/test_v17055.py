"""v1.70.55 — файл провайдера противоречит каталогу: предложение и кнопка.

Результат проверки владельца после v1.70.54 (дословно, существенная часть):
«Подключение не прошло. Keystone пройден, но в каталоге облака нет сервиса
compute для интерфейса «public» в регионе «eu-west2». Каталог предлагает
регионы: ru-central; интерфейсы: admin, internal, public. Регион и
интерфейс нужно брать из файла провайдера: строки OS_REGION_NAME и
OS_INTERFACE, поля «Регион» и «Интерфейс каталога» на вкладке
«Подключение». Код: catalog» и значки: compute, placement, metric,
network, image — «в каталоге, но не для выбранных региона и интерфейса»,
load_balancer — «нет в каталоге».

Сверка v1.70.54 сработала и дала точный диагноз: панель провайдера
противоречит собственному облаку — скачанный файл задаёт регион eu-west2,
а каталог, возвращённый Keystone на учётные данные владельца, предлагает
единственный регион ru-central. Каталог авторитетнее файла: его составило
само облако. Программа не меняет параметры администратора за его спиной
(отклонено в v1.70.54), но теперь предлагает однозначную пару и даёт
подставить её кнопкой — решение остаётся за человеком.

Значения провайдера в этом файле заведомо подставные: настоящие адрес и
логин владельца в репозитории не хранятся (замок v1.70.52 это сторожит).

Что здесь проверяется:
 1. catalog_suggestion на живом AccessInfoV3: сценарий владельца даёт пару
    (ru-central, public); неоднозначность предложения не даёт; регион из
    формы есть — интерфейс выбирается из его собственных с предпочтением
    public > internal > admin; регион единственный — пара однозначна.
 2. Отказ сверки несёт ключ «catalog» с регионами, интерфейсами и
    предложением; ключи v1.70.54 целы; секрет наружу не попадает; текст
    сведения советует верить каталогу.
 3. Значки повторяют картину владельца; после подстановки пары сверка
    молчит, compute и metric «доступен».
 4. Карточка: кнопка «Подставить из каталога», экранирование региона как
    недоверенных данных, интерфейс только из public/internal/admin,
    обратная связь «Подставлено» и блокировка от повторного нажатия.
 5. Ритуал выпуска 1.70.55: шесть точек синхронизации, «Что нового»
    первым, CHANGELOG, журнал, цитата владельца, историзация
    TestRelease17054.
"""

import pathlib
import re

from keystoneauth1 import access as ksaccess

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
RELEASE_54_TESTS = ROOT / "tests" / "test_v17054.py"

OWNER_QUOTE_HEAD = "keystone пройден, но в каталоге облака нет сервиса compute"
OWNER_QUOTE_TAIL = ("каталог предлагает регионы: ru-central; интерфейсы: "
                    "admin, internal, public")

FAKE_AUTH_URL = "https://os-api.example.ru/identity"
MISMATCH_STATUS = "в каталоге, но не для выбранных региона и интерфейса"


def _catalog_body(region: str, interfaces: tuple) -> dict:
    """Токен с каталогом: compute в одном регионе заданными интерфейсами.

    Картина владельца: load_balancer в каталоге НЕТ — значок «нет в
    каталоге» обязан остаться честным.
    """
    return {"token": {
        "methods": ["password"],
        "expires_at": "2099-01-01T00:00:00.000000Z",
        "catalog": [
            {"type": "compute", "name": "nova", "endpoints": [
                {"interface": iface, "region": region, "region_id": region,
                 "url": f"https://nova-{iface}.example.ru"}
                for iface in interfaces]},
            {"type": "identity", "name": "keystone", "endpoints": [
                {"interface": "public", "region": region,
                 "region_id": region, "url": FAKE_AUTH_URL}]},
            {"type": "placement", "name": "placement", "endpoints": [
                {"interface": "public", "region": region,
                 "region_id": region,
                 "url": "https://placement.example.ru"}]},
            {"type": "metric", "name": "gnocchi", "endpoints": [
                {"interface": "public", "region": region,
                 "region_id": region, "url": "https://metric.example.ru"}]},
            {"type": "network", "name": "neutron", "endpoints": [
                {"interface": "public", "region": region,
                 "region_id": region, "url": "https://neutron.example.ru"}]},
            {"type": "image", "name": "glance", "endpoints": [
                {"interface": "public", "region": region,
                 "region_id": region, "url": "https://glance.example.ru"}]},
        ],
        "user": {"id": "u1", "name": "fake",
                 "domain": {"id": "default", "name": "Default"}},
        "project": {"id": "p1", "name": "demo",
                    "domain": {"id": "default", "name": "Default"}},
        "audit_ids": ["x"],
    }}


OWNER_BODY = _catalog_body("ru-central", ("public", "admin", "internal"))


def conn_with(body: dict):
    """Подставное подключение: каталог из тела токена, сети нет.

    Атрибута compute НЕТ нарочно: добрался бы test_connection до Nova —
    тест упал бы AttributeError.
    """

    class Auth:
        def get_access(self, session):
            return ksaccess.AccessInfoV3(body)

    class Conn:
        session = type("S", (), {"auth": Auth()})()
        authorized = False

        def authorize(self):
            self.authorized = True

    return Conn()


def cfg_for(region: str, interface: str) -> dict:
    return {
        "auth_url": FAKE_AUTH_URL, "auth_type": "password",
        "username": "monitor", "password": "sekrit", "project_name": "demo",
        "user_domain_name": "Default", "project_domain_name": "Default",
        "region_name": region, "interface": interface,
        "verify_tls": True, "timeout_sec": 30,
    }


OWNER_CFG = cfg_for("eu-west2", "public")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestSuggestionOwnerScenario:
    """Картина владельца: каталог ru-central, форма eu-west2 + public."""

    def test_suggestion_is_the_catalog_pair(self):
        facts = connection.catalog_suggestion(conn_with(OWNER_BODY),
                                              OWNER_CFG)
        assert facts["suggestion"] == {"region": "ru-central",
                                       "interface": "public"}

    def test_facts_list_regions_and_interfaces(self):
        facts = connection.catalog_suggestion(conn_with(OWNER_BODY),
                                              OWNER_CFG)
        assert facts["regions"] == ["ru-central"]
        assert facts["interfaces"] == ["admin", "internal", "public"]

    def test_no_compute_in_catalog_gives_nothing(self):
        body = _catalog_body("ru-central", ("public",))
        body["token"]["catalog"] = body["token"]["catalog"][1:2]  # identity
        facts = connection.catalog_suggestion(conn_with(body), OWNER_CFG)
        assert facts == {"regions": [], "interfaces": [],
                         "suggestion": None}


class TestSuggestionAmbiguity:
    """Предложение даётся только когда выбор однозначен."""

    def test_two_regions_and_foreign_interface_give_no_suggestion(self):
        body = _catalog_body("r-a", ("public",))
        body["token"]["catalog"][0]["endpoints"].append(
            {"interface": "public", "region": "r-b", "region_id": "r-b",
             "url": "https://nova-b.example.ru"})
        facts = connection.catalog_suggestion(
            conn_with(body), cfg_for("zz", "internal"))
        assert facts["suggestion"] is None
        assert facts["regions"] == ["r-a", "r-b"]

    def test_own_region_keeps_and_picks_public(self):
        facts = connection.catalog_suggestion(
            conn_with(OWNER_BODY), cfg_for("ru-central", "vip"))
        assert facts["suggestion"] == {"region": "ru-central",
                                       "interface": "public"}

    def test_preference_internal_over_admin(self):
        body = _catalog_body("r-a", ("admin", "internal"))
        facts = connection.catalog_suggestion(conn_with(body),
                                              cfg_for("r-a", "vip"))
        assert facts["suggestion"] == {"region": "r-a",
                                       "interface": "internal"}

    def test_single_region_rule(self):
        body = _catalog_body("ru-central", ("public",))
        facts = connection.catalog_suggestion(conn_with(body),
                                              cfg_for("zz", "internal"))
        assert facts["suggestion"] == {"region": "ru-central",
                                       "interface": "public"}

    def test_interface_found_in_exactly_one_region_wins(self):
        body = _catalog_body("r-a", ("public", "internal"))
        body["token"]["catalog"][0]["endpoints"].append(
            {"interface": "internal", "region": "r-b", "region_id": "r-b",
             "url": "https://nova-b.example.ru"})
        facts = connection.catalog_suggestion(conn_with(body),
                                              cfg_for("zz", "public"))
        assert facts["suggestion"] == {"region": "r-a",
                                       "interface": "public"}


class TestMismatchResponseCarriesCatalog:
    """Отказ сверки: ключ «catalog», ключи v1.70.54 целы, секрета нет."""

    def test_test_connection_returns_catalog_key(self, monkeypatch):
        conn = conn_with(OWNER_BODY)
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(OWNER_CFG)
        assert result["ok"] is False
        assert result["error_code"] == "catalog"
        assert result["catalog"]["suggestion"] == {"region": "ru-central",
                                                   "interface": "public"}
        assert result["catalog"]["regions"] == ["ru-central"]

    def test_v54_keys_are_untouched(self, monkeypatch):
        conn = conn_with(OWNER_BODY)
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(OWNER_CFG)
        assert result["authorized"] is True
        assert result["hypervisor"] is None
        assert "warnings" in result and "services" in result
        assert "AttributeError" not in str(result)

    def test_message_tells_to_trust_catalog(self, monkeypatch):
        conn = conn_with(OWNER_BODY)
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(OWNER_CFG)
        assert "верьте каталогу" in result["error"]
        assert "OS_REGION_NAME" in result["error"]
        assert "OS_INTERFACE" in result["error"]

    def test_secret_never_leaks(self, monkeypatch):
        conn = conn_with(OWNER_BODY)
        monkeypatch.setattr(connection, "build_connection", lambda cfg: conn)
        result = connection.test_connection(OWNER_CFG)
        assert "sekrit" not in str(result)


class TestChipsMatchOwnerPicture:
    """Значки: картина владельца и честность после подстановки."""

    def test_owner_chips(self):
        services = connection._service_names(conn_with(OWNER_BODY),
                                             "eu-west2", "public")
        for name in ("compute", "placement", "metric", "network", "image"):
            assert services[name] == MISMATCH_STATUS, name
        assert services["load_balancer"] == "нет в каталоге"

    def test_after_substitution_chips_turn_green(self):
        services = connection._service_names(conn_with(OWNER_BODY),
                                             "ru-central", "public")
        assert services["compute"] == "доступен"
        assert services["metric"] == "доступен"
        assert services["network"] == "доступен"
        assert services["load_balancer"] == "нет в каталоге"

    def test_substituted_pair_passes_the_check(self):
        assert connection._catalog_mismatch(conn_with(OWNER_BODY),
                                            cfg_for("ru-central",
                                                    "public")) == ""


class TestUiSuggestButton:
    """Карточка проверки: кнопка подстановки и обратная связь."""

    def test_button_source_is_present(self):
        js = _read(OSM_JS)
        assert "osm-catalog-apply" in js
        assert "Подставить из каталога" in js
        assert 'class="btn btn-sm" id="osm-catalog-apply"' in js

    def test_region_is_escaped_as_untrusted(self):
        js = _read(OSM_JS)
        assert "esc(suggestion.region)" in js
        assert "esc(suggestion.interface)" in js

    def test_interface_is_guarded_by_whitelist(self):
        js = _read(OSM_JS)
        assert "['public', 'internal', 'admin'].includes(suggestion.interface)" in js

    def test_feedback_and_single_press(self):
        js = _read(OSM_JS)
        assert "catalogApply.textContent = 'Подставлено'" in js
        assert "catalogApply.disabled = true" in js
        assert "повторите" in js  # уведомление зовёт повторить проверку

    def test_hint_v54_is_intact(self):
        js = _read(OSM_JS)
        assert "const catalogIssue" in js
        assert "+ catalogHint" in js


class TestRelease17055:
    """v1.70.56: выпуск 1.70.55 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17056, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.54).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.
    """

    VERSION = "1.70.55"
    PREVIOUS = "1.70.54"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(APP / "config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 55)

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("каталог", "регион", "интерфейс", "кнопк", "Подстав",
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
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("каталог", "регион", "интерфейс", "ru-central",
                       "кнопк", "кэш-бастер", "Отклонено", "OS_REGION_NAME",
                       "catalog_suggestion", "верьте каталогу"):
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
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "каталог", "регион",
                       "интерфейс", "кнопк", "кэш-бастер"):
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
