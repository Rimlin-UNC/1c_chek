"""v1.70.53 — облако ответило 401: домен по идентификатору и честный отказ.

Жалоба владельца (дословно, существенная часть): «Подключение не прошло

SDKException: The request you have made requires authentication. (HTTP 401)
(Request-ID: req-3dd2e09e-caee-4056-bca9-a9d8cd91aed9)

Код: sdk

Нужно исправить не работает.» К жалобе приложен файл openrc.sh из панели
провайдера — тот самый, вставку которого реализовал выпуск v1.70.52.

Request-ID в ошибке — штамп Keystone провайдера: запрос дошёл до облака,
сеть и адрес работают, облако не приняло учётные данные. Сверка файла
провайдера с тем, что программа отправляет в облако, нашла два расхождения
и один скрытый дефект:

* файл задаёт домен проекта идентификатором (OS_PROJECT_DOMAIN_ID="default"),
  а программа знала только домен по имени и отправляла «Default» — имя у
  провайдера может не совпадать буквально, и тогда Keystone отвечает ровно
  таким безликим 401;
* classify_error возвращал для такого отказа бесполезный код «sdk»;
* «Проверить подключение» со свежим, ещё не сохранённым секретом падал
  KeyError: форма отправляет секрет под ключом secret, а build_connection
  требовал cfg["password"].

Значения провайдера в этом файле заведомо подставные: настоящие адрес и
логин владельца в репозитории не хранятся (замок v1.70.52 это сторожит).

Что здесь проверяется:
 1. build_connection на моках: идентификатор домена в приоритете над именем,
    без идентификатора имена идут как прежде, свежий секрет из формы доходит
    до обеих веток аутентификации.
 2. validate_cfg принимает ось домена именем ИЛИ идентификатором.
 3. classify_error: «(HTTP 401)»/«(HTTP 403)» → auth_denied, старые ветки целы.
 4. Добавляющая миграция колонок на копии боевого сценария: данные и
    зашифрованный секрет переживают ALTER TABLE, повторный прогон безопасен.
 5. store и Pydantic-тела принимают новые поля; секрет наружу не торчит.
 6. Файл провайдера дословной структуры (значения подставные) прогоняется
    настоящим Node: unset/if/комментарии игнорируются, домен по
    идентификатору ложится в своё поле.
 7. Замки разметки карточки и чек-листа при отказе аутентификации.
 8. Выпуск v1.70.53: точки синхронизации, «Что нового», CHANGELOG, журнал,
    цитата владельца, прежний класс выпуска — только история.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

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
MONITOR = APP / "openstack_monitor"
DOCS = ROOT / "docs"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"
RELEASE_52_TESTS = ROOT / "tests" / "test_v17052.py"

OWNER_QUOTE_HEAD = "подключение не прошло"
OWNER_QUOTE_TAIL = "нужно исправить не работает"

# Заведомо подставные значения (замок v1.70.52 сторожит отсутствие настоящих).
FAKE_USERNAME = "cloud-user-00000000-0000-0000-0000-000000000000"
FAKE_AUTH_URL = "https://os-api.example.ru/identity"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def base_cfg(**over) -> dict:
    """Словарь подключения формы — как собирает readConnectionForm."""
    cfg = {"auth_url": FAKE_AUTH_URL, "auth_type": "password",
           "username": FAKE_USERNAME, "password": "stored-secret",
           "project_name": "demo-project", "user_domain_name": "Default",
           "project_domain_name": "Default", "region_name": "eu-west2",
           "interface": "public", "verify_tls": True, "timeout_sec": 30}
    cfg.update(over)
    return cfg


@pytest.fixture()
def captured(monkeypatch):
    """Подменить плагины keystoneauth и Connection захватом аргументов."""
    import keystoneauth1.identity.v3 as v3mod
    import keystoneauth1.session as sessmod
    import openstack.connection as connmod

    cap: dict = {}

    class FakePlugin:
        def __init__(self, **kwargs):
            cap.clear()
            cap.update(kwargs)

    class FakeSession:
        def __init__(self, **kwargs):
            pass

    class FakeConnection:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(v3mod, "Password", FakePlugin)
    monkeypatch.setattr(v3mod, "ApplicationCredential", FakePlugin)
    monkeypatch.setattr(sessmod, "Session", FakeSession)
    monkeypatch.setattr(connmod, "Connection", FakeConnection)
    return cap


# ==========================================================================
#  1. Идентификатор домена доходит до Keystone
# ==========================================================================
class TestDomainIdReachesKeystone:

    def test_project_domain_id_wins_over_name(self, captured):
        from app.openstack_monitor import connection
        connection.build_connection(
            base_cfg(project_domain_id="default", user_domain_id=""))
        assert captured["project_domain_id"] == "default"
        assert "project_domain_name" not in captured, \
            "имя не должно уходить вместе с идентификатором"
        assert captured["user_domain_name"] == "Default"
        assert "user_domain_id" not in captured

    def test_user_domain_id_wins_over_name(self, captured):
        from app.openstack_monitor import connection
        connection.build_connection(
            base_cfg(user_domain_id="uid-1", project_domain_id=""))
        assert captured["user_domain_id"] == "uid-1"
        assert "user_domain_name" not in captured
        assert captured["project_domain_name"] == "Default"

    def test_names_pass_when_ids_are_empty(self, captured):
        """Регрессия v1.70.51: без идентификаторов всё идёт по-старому."""
        from app.openstack_monitor import connection
        connection.build_connection(
            base_cfg(user_domain_id="", project_domain_id=""))
        assert captured["user_domain_name"] == "Default"
        assert captured["project_domain_name"] == "Default"
        assert "user_domain_id" not in captured
        assert "project_domain_id" not in captured

    def test_fresh_form_secret_reaches_password_auth(self, captured):
        """Дефект KeyError: /test присылает свежий секрет под ключом secret."""
        from app.openstack_monitor import connection
        form = base_cfg(secret="fresh-secret")
        form.pop("password")
        connection.build_connection(form)
        assert captured["password"] == "fresh-secret"

    def test_fresh_form_secret_reaches_app_cred_auth(self, captured):
        from app.openstack_monitor import connection
        form = base_cfg(auth_type="application_credential",
                        app_cred_id="ac-1", secret="ac-fresh")
        form.pop("password")
        connection.build_connection(form)
        assert captured["application_credential_id"] == "ac-1"
        assert captured["application_credential_secret"] == "ac-fresh"

    def test_stored_password_wins_over_form_secret(self, captured):
        """Сохранённый путь store.connection_cfg даёт оба ключа — главный
        по-прежнему password/app_cred_secret, secret лишь запасной."""
        from app.openstack_monitor import connection
        connection.build_connection(
            base_cfg(password="stored-secret", secret="stored-secret"))
        assert captured["password"] == "stored-secret"


# ==========================================================================
#  2. validate_cfg: домен именем ИЛИ идентификатором
# ==========================================================================
class TestValidateCfgDomainAxes:

    def test_domain_id_alone_is_enough(self):
        from app.openstack_monitor import connection
        problems = connection.validate_cfg(
            base_cfg(project_domain_name="", project_domain_id="default"))
        assert not [p for p in problems if "домен проекта" in p], problems

    def test_both_axes_report_in_russian(self):
        from app.openstack_monitor import connection
        problems = connection.validate_cfg(
            base_cfg(user_domain_name="", user_domain_id="",
                     project_domain_name="", project_domain_id=""))
        assert any("не задан домен пользователя" in p for p in problems)
        assert any("не задан домен проекта" in p for p in problems)
        for problem in problems:
            if "домен" in problem:
                assert "user_domain_id" in problem or "project_domain_id" in \
                    problem, "подсказка должна называть оба ключа оси"

    def test_region_is_still_required(self):
        from app.openstack_monitor import connection
        problems = connection.validate_cfg(base_cfg(region_name=""))
        assert any("region_name" in p for p in problems)


# ==========================================================================
#  3. classify_error: безликий 401 больше не «sdk»
# ==========================================================================
class TestClassifyError401:

    class SDKException(Exception):
        """Тот же тип, которым отвечает openstacksdk (имя решает)."""

    def test_owner_error_is_auth_denied(self):
        from app.openstack_monitor import connection
        exc = self.SDKException(
            "The request you have made requires authentication. "
            "(HTTP 401) (Request-ID: req-3dd2e09e-caee-4056-bca9-a9d8cd91aed9)")
        assert connection.classify_error(exc) == "auth_denied"

    def test_http_403_is_auth_denied(self):
        from app.openstack_monitor import connection
        assert connection.classify_error(
            self.SDKException("Forbidden (HTTP 403)")) == "auth_denied"

    def test_plain_sdk_exception_stays_sdk(self):
        from app.openstack_monitor import connection
        assert connection.classify_error(
            self.SDKException("прочий отказ облака")) == "sdk"

    def test_previous_branches_intact(self):
        from app.openstack_monitor import connection
        assert connection.classify_error(TimeoutError("timed out")) == "timeout"
        assert connection.classify_error(
            ConnectionError("нет связи")) == "connection"
        assert connection.classify_error(ValueError("прочее")) == "ValueError"


# ==========================================================================
#  4. Добавляющая миграция колонок на копии боевого сценария
# ==========================================================================
class TestColumnMigration:

    def _engine(self, tmp_path, name):
        from sqlalchemy import create_engine
        return create_engine(f"sqlite:///{tmp_path / name}")

    def test_fresh_database_has_columns(self, tmp_path):
        from sqlalchemy import inspect

        from app.openstack_monitor.models import ensure_openstack_schema
        engine = self._engine(tmp_path, "fresh.db")
        ensure_openstack_schema(engine)
        cols = {c["name"] for c in
                inspect(engine).get_columns("openstack_settings")}
        assert {"user_domain_id", "project_domain_id"} <= cols

    def test_columns_are_added_to_live_database(self, tmp_path):
        """Сценарий боевого сервера: база v1.70.51/52 с сохранённым
        подключением и запечатанным секретом переживает обновление."""
        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker

        from app.openstack_monitor import store
        from app.openstack_monitor.models import ensure_openstack_schema

        engine = self._engine(tmp_path, "live.db")
        ensure_openstack_schema(engine)
        session = sessionmaker(bind=engine)()
        store.apply_settings(session, {"auth_url": FAKE_AUTH_URL,
                                       "username": FAKE_USERNAME,
                                       "project_name": "demo-project",
                                       "region_name": "eu-west2",
                                       "secret": "sealed-secret"})
        with engine.begin() as conn:       # откат к виду v1.70.52
            conn.execute(text("ALTER TABLE openstack_settings "
                              "DROP COLUMN user_domain_id"))
            conn.execute(text("ALTER TABLE openstack_settings "
                              "DROP COLUMN project_domain_id"))
            before = conn.execute(text(
                "SELECT auth_url, secret_encrypted "
                "FROM openstack_settings")).fetchone()
        assert before[1].startswith("enc:"), "секрет должен быть запечатан"

        ensure_openstack_schema(engine)    # миграция на «боевой» базе
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT auth_url, secret_encrypted, user_domain_id, "
                "project_domain_id FROM openstack_settings")).fetchone()
        assert row[0] == before[0], "адрес подключения потерян"
        assert row[1] == before[1], "зашифрованный секрет потерян"
        assert row[2] == "" and row[3] == ""

        session2 = sessionmaker(bind=engine)()
        saved, _problems = store.apply_settings(
            session2, {"project_domain_id": "default"})
        assert saved.project_domain_id == "default"
        assert saved.auth_url == FAKE_AUTH_URL

    def test_migration_is_idempotent(self, tmp_path):
        from sqlalchemy import inspect

        from app.openstack_monitor.models import ensure_openstack_schema
        engine = self._engine(tmp_path, "idem.db")
        ensure_openstack_schema(engine)
        ensure_openstack_schema(engine)
        cols = {c["name"] for c in
                inspect(engine).get_columns("openstack_settings")}
        assert {"user_domain_id", "project_domain_id"} <= cols


# ==========================================================================
#  5. store и Pydantic-тела принимают новые поля
# ==========================================================================
class TestStoreAndBodies:

    def test_apply_settings_round_trip(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.openstack_monitor import store
        from app.openstack_monitor.models import ensure_openstack_schema
        engine = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
        ensure_openstack_schema(engine)
        session = sessionmaker(bind=engine)()
        row, _problems = store.apply_settings(
            session, {"user_domain_id": "uid-1",
                      "project_domain_id": "default"})
        assert row.user_domain_id == "uid-1"
        assert row.project_domain_id == "default"
        cfg = store.connection_cfg(row)
        assert cfg["user_domain_id"] == "uid-1"
        assert cfg["project_domain_id"] == "default"
        public = row.public_dict()
        assert public["user_domain_id"] == "uid-1"
        assert public["project_domain_id"] == "default"
        assert "secret" not in public and "password" not in public

    def test_pydantic_bodies_accept_new_fields(self):
        from app.openstack_monitor.router import SettingsBody, TestBody
        for model in (SettingsBody, TestBody):
            dumped = model(user_domain_id="uid-1",
                           project_domain_id="default").model_dump()
            assert dumped["user_domain_id"] == "uid-1", model.__name__
            assert dumped["project_domain_id"] == "default", model.__name__


# ==========================================================================
#  6. Файл провайдера дословной структуры — настоящий Node
# ==========================================================================
NODE_SHIM = """
import { readFileSync } from 'node:fs';
const elements = {};
const el = (id) => (elements[id] = elements[id] || { value: '', hidden: false,
  classList: { add() {}, remove() {}, toggle() {} }, style: {},
  appendChild() {}, addEventListener() {}, setAttribute() {}, remove() {},
  innerHTML: '', textContent: '' });
globalThis.document = { getElementById: (id) => el(id),
  querySelector: () => null, querySelectorAll: () => [],
  createElement: () => el('tmp'), addEventListener() {},
  body: el('body'), documentElement: el('html') };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.window = {};
globalThis.performance = { now: () => 0 };
globalThis.requestAnimationFrame = () => 0;
const inputs = JSON.parse(readFileSync('__INPUTS__', 'utf8'));
const { parseOpenrc, applyOpenrc } = await import('__OSM__');
const parsed = {};
for (const [name, text] of Object.entries(inputs)) {
  parsed[name] = parseOpenrc(text);
}
const filled = applyOpenrc(parseOpenrc(inputs.provider));
const fields = {};
for (const id of ['osm-auth-type', 'osm-auth-url', 'osm-username',
                  'osm-secret', 'osm-project', 'osm-udn', 'osm-pdn',
                  'osm-udn-id', 'osm-pdn-id', 'osm-region',
                  'osm-interface']) {
  fields[id] = elements[id] ? elements[id].value : null;
}
console.log(JSON.stringify({ parsed, filled, fields }));
"""


def provider_file() -> str:
    """Структура — дословно файл панели провайдера из жалобы владельца;
    значения заведомо подставные."""
    return "\n".join([
        "export OS_AUTH_URL=" + FAKE_AUTH_URL,
        "",
        "# export OS_PROJECT_ID=",
        "",
        f'export OS_PROJECT_NAME="{FAKE_USERNAME}"',
        "",
        'export OS_USER_DOMAIN_NAME="Default"',
        "",
        'if [ -z "$OS_USER_DOMAIN_NAME" ]; then unset OS_USER_DOMAIN_NAME; fi',
        "",
        'export OS_PROJECT_DOMAIN_ID="default"',
        "",
        'if [ -z "$OS_PROJECT_DOMAIN_ID" ]; then unset OS_PROJECT_DOMAIN_ID; fi',
        "",
        "# unset v2.0 items in case set",
        "unset OS_TENANT_ID",
        "unset OS_TENANT_NAME",
        "",
        "# In addition to the owning entity (tenant), OpenStack stores the",
        "# entity performing the action as the **user**.",
        f'export OS_USERNAME="{FAKE_USERNAME}"',
        "",
        'export OS_PASSWORD="fake-secret-value"',
        "",
        "# If your configuration has multiple regions, we set that here.",
        'export OS_REGION_NAME="eu-west2"',
        "",
        "# Don't leave a blank variable, unset it if it was empty",
        'if [ -z "$OS_REGION_NAME" ]; then unset OS_REGION_NAME; fi',
        "",
        "export OS_INTERFACE=public",
        "",
        "export OS_IDENTITY_API_VERSION=3",
    ])


def _run_node(tmp_path) -> dict:
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node недоступен")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"provider": provider_file()}),
                      encoding="utf-8")
    script = (NODE_SHIM
              .replace("__INPUTS__", inputs.as_posix())
              .replace("__OSM__", OSM_JS.as_posix()))
    f = tmp_path / "provider.mjs"
    f.write_text(script, encoding="utf-8")
    code = subprocess.run([node, str(f)], capture_output=True, text=True,
                          check=False)
    assert code.returncode == 0, (code.stdout + code.stderr)[:800]
    return json.loads(code.stdout)


class TestProviderFileShapeInNode:

    def test_service_lines_are_ignored(self, tmp_path):
        parsed = _run_node(tmp_path)["parsed"]["provider"]
        assert parsed == {
            "OS_AUTH_URL": FAKE_AUTH_URL,
            "OS_PROJECT_NAME": FAKE_USERNAME,
            "OS_USER_DOMAIN_NAME": "Default",
            "OS_PROJECT_DOMAIN_ID": "default",
            "OS_USERNAME": FAKE_USERNAME,
            "OS_PASSWORD": "fake-secret-value",
            "OS_REGION_NAME": "eu-west2",
            "OS_INTERFACE": "public",
            "OS_IDENTITY_API_VERSION": "3",
        }, "unset/if/комментарии не должны становиться переменными"

    def test_domain_id_lands_in_its_own_field(self, tmp_path):
        res = _run_node(tmp_path)
        fields = res["fields"]
        assert fields["osm-pdn-id"] == "default"
        assert fields["osm-pdn"] == "", \
            "имя домена проекта файл не задавал — поле не тронуто"
        assert fields["osm-udn"] == "Default"
        assert fields["osm-udn-id"] == ""
        assert "домен проекта по ID" in res["filled"]
        assert fields["osm-secret"] == "fake-secret-value"
        assert fields["osm-region"] == "eu-west2"
        assert fields["osm-interface"] == "public"
        assert fields["osm-auth-type"] == "password"


# ==========================================================================
#  7. Замки карточки и чек-листа
# ==========================================================================
class TestCardUiLocks:

    def test_domain_id_fields_on_the_card(self):
        """Поля создаются помощником field(), который подставляет
        идентификатор в шаблон при отрисовке: в исходнике идентификаторы
        живут аргументами вызова."""
        js = _read(OSM_JS)
        assert "field('osm-udn-id'" in js
        assert "field('osm-pdn-id'" in js
        assert "Домен проекта по ID (project_domain_id)" in js
        assert "Домен пользователя по ID (user_domain_id)" in js
        panel = js.split("function connPanel(", 1)[1].split("\nfunction ", 1)[0]
        assert "osm-pdn-id" in panel and "osm-udn-id" in panel

    def test_read_form_carries_domain_ids(self):
        js = _read(OSM_JS)
        body = js.split("function readConnectionForm() {", 1)[1].split(
            "\n}", 1)[0]
        assert "user_domain_id:" in body and "osm-udn-id" in body
        assert "project_domain_id:" in body and "osm-pdn-id" in body

    def test_applyopenrc_puts_domain_ids(self):
        js = _read(OSM_JS)
        body = js.split("export function applyOpenrc(", 1)[1].split(
            "\n}", 1)[0]
        assert "OS_USER_DOMAIN_ID" in body and "osm-udn-id" in body
        assert "OS_PROJECT_DOMAIN_ID" in body and "osm-pdn-id" in body

    def test_applyopenrc_is_exported_for_checks(self):
        assert "export function applyOpenrc(" in _read(OSM_JS)

    def test_denied_checklist_under_the_error(self):
        """Чек-лист по реальной жалобе: заглушка пароля в файле провайдера,
        пробелы при копировании, логин/проект, домен по идентификатору."""
        js = _read(OSM_JS)
        block = js.split("function renderTestResult(", 1)[1].split(
            "\nasync function", 1)[0]
        assert "auth_denied" in block
        assert "HTTP 40[13]" in block
        for needle in ("заглушк", "«Показать»", "важнее имени",
                       "пробел", "логин и проект"):
            assert needle in block, f"в чек-листе нет «{needle}»"
        assert "+ deniedHint" in block, "чек-лист не вставлен в разметку"

    def test_checklist_is_russian_only(self):
        js = _read(OSM_JS)
        hint = js.split("const deniedHint = denied", 1)[1].split(
            "box.innerHTML", 1)[0]
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", hint)) - {
            "div", "class", "form", "hint", "OS", "PROJECT", "DOMAIN", "ID"}
        assert not words, f"латинские слова в чек-листе: {sorted(words)}"


# ==========================================================================
#  8. Выпуск v1.70.53
# ==========================================================================
class TestRelease17053:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.52) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.53"
    PREVIOUS = "1.70.52"

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
        for path in (APP / "config.py", INDEX, SW, MANIFEST, MAX_HTML, CABINET):
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
        for needle in ("домен", "идентификатор", "чек-лист", "секрет",
                       "миграц", "заглушк"):
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
        for needle in ("домен", "идентификатор", "401", "openrc", "миграц",
                       "кэш-бастер", "Отклонено", "чек-лист", "KeyError"):
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
                       "Отклонённые варианты", "домен", "идентификатор",
                       "миграц", "кэш-бастер"):
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
        src = _read(RELEASE_52_TESTS)
        block = src.split("class TestRelease17052:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_52_TESTS)
        block = src.split("class TestRelease17052:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
