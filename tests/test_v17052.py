"""v1.70.52 — подключение к облаку провайдера по файлу openrc.sh.

Задача владельца (дословно): «Провайдер сервера предоставляет на скриншоте,
заработай чтоб можно было видеть статистику сервера.»

На скриншоте — панель управления провайдера: вкладка «API», блок панели
OpenStack (адрес, логин, пароль под маской с кнопкой «Показать») и рядом
блок «OpenStack CLI клиент», который отдаёт готовый файл openrc.sh со
всеми параметрами Keystone. Вручную переносить из панели в карточку
мониторинга семь полей долго, а ошибка в домене или регионе превращается
в непонятный отказ подключения.

Решение выпуска: на вкладке «Подключение» карточки мониторинга облака
появились поле для содержимого openrc.sh и кнопка «Заполнить из openrc.sh».
Файл разбирается СТРОГО в браузере: его текст не уходит на сервер ни одним
запросом, поэтому секрет не попадает ни в журнал запросов, ни в ответы
программы. Реальные адрес и логин провайдера владельца в репозитории не
хранятся — в проверках используются заведомо подставные значения.

Что здесь проверяется:
 1. Настоящий парсер parseOpenrc прогоняется в Node на пяти входах: полный
    файл, прикладные учётные данные, пустой текст, мусор, значения с путём
    и пробелами.
 2. Поле и кнопка на карточке существуют, подсказка объясняет, что разбор
    идёт в браузере; отсутствующие переменные не стирают ручной ввод.
 3. Содержимое поля не достигает сервера: в маршрутах контура нет openrc,
    в полезной нагрузке «Сохранить» нет этого поля, текст не читается нигде,
    кроме обработчика заполнения.
 4. Данные провайдера владельца отсутствуют во всех текстовых файлах
    репозитория.
 5. Выпуск v1.70.52: точки синхронизации, «Что нового», CHANGELOG, журнал
    вёрстки, цитата владельца, прежний класс выпуска — только история.
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
DOCS = ROOT / "docs"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"
ROUTER = APP / "openstack_monitor" / "router.py"
RELEASE_51_TESTS = ROOT / "tests" / "test_v17051.py"

OWNER_QUOTE_HEAD = "провайдер сервера предоставляет на скриншоте"
OWNER_QUOTE_TAIL = "чтоб можно было видеть статистику сервера"

# Заведомо подставные значения: настоящие адрес и логин провайдера владельца
# не должны попадать ни в код, ни в тесты (см. TestProviderDataIsNotInTheRepo).
FAKE_USERNAME = "cloud-user-00000000-0000-0000-0000-000000000000"
FAKE_AUTH_URL = "https://horizon.example.ru:5000/v3"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ==========================================================================
#  1. Настоящий парсер openrc.sh в Node
# ==========================================================================
NODE_SHIM = """
import { readFileSync } from 'node:fs';
const el = () => ({ classList: { add() {}, remove() {}, toggle() {} }, style: {},
  appendChild() {}, querySelector: () => null, addEventListener() {},
  setAttribute() {}, remove() {}, innerHTML: '', textContent: '', value: '' });
globalThis.document = { getElementById: () => el(), querySelector: () => el(),
  querySelectorAll: () => [], createElement: () => el(),
  addEventListener() {}, body: el(), documentElement: el() };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.window = {};
globalThis.performance = { now: () => 0 };
globalThis.requestAnimationFrame = () => 0;
const inputs = JSON.parse(readFileSync('__INPUTS__', 'utf8'));
const { parseOpenrc } = await import('__OSM__');
const out = {};
for (const [name, text] of Object.entries(inputs)) {
  out[name] = parseOpenrc(text);
}
console.log(JSON.stringify(out));
"""


def _inputs() -> dict:
    full = "\n".join([
        "# Console openrc file — скачан из панели провайдера",
        "export OS_AUTH_URL=" + FAKE_AUTH_URL,
        "export OS_PROJECT_NAME=demo",
        "export OS_USERNAME=" + FAKE_USERNAME,
        'export OS_USER_DOMAIN_NAME="Default"',
        "export OS_PROJECT_DOMAIN_NAME='Default'",
        "export OS_REGION_NAME=RegionOne",
        "export OS_INTERFACE=public",
        'export OS_PASSWORD="s3cr3t-value"',
        "export OS_TENANT_NAME=fallback-project",
        "  OS_IDENTITY_API_VERSION=3",
    ])
    appcred = "\n".join([
        "export OS_AUTH_URL=" + FAKE_AUTH_URL,
        "export OS_APPLICATION_CREDENTIAL_ID=ac-id-1",
        "export OS_APPLICATION_CREDENTIAL_SECRET=ac-secret-1",
    ])
    garbage = "\n".join([
        "просто текст",
        "без переменных",
        "FOO=bar",
        "# export OS_REGION_NAME=закомментировано",
    ])
    return {
        "full": full,
        "appcred": appcred,
        "empty": "",
        "garbage": garbage,
        "urlpath": "export OS_AUTH_URL=" + FAKE_AUTH_URL + "/auth",
        "spaces": 'export OS_PASSWORD="value with spaces"',
    }


def _run_parser(tmp_path):
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node недоступен")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(_inputs()), encoding="utf-8")
    script = (NODE_SHIM
              .replace("__INPUTS__", inputs.as_posix())
              .replace("__OSM__", OSM_JS.as_posix()))
    f = tmp_path / "openrc.mjs"
    f.write_text(script, encoding="utf-8")
    code = subprocess.run([node, str(f)], capture_output=True, text=True,
                          check=False)
    assert code.returncode == 0, (code.stdout + code.stderr)[:800]
    return json.loads(code.stdout)


class TestOpenrcParserInNode:
    """Проверяется НАСТОЯЩАЯ функция из osm.js, а не её пересказ."""

    def test_full_file_is_parsed(self, tmp_path):
        res = _run_parser(tmp_path)["full"]
        assert res["OS_AUTH_URL"] == FAKE_AUTH_URL
        assert res["OS_USERNAME"] == FAKE_USERNAME
        assert res["OS_PROJECT_NAME"] == "demo"
        assert res["OS_PASSWORD"] == "s3cr3t-value", "кавычки не сняты"
        assert res["OS_USER_DOMAIN_NAME"] == "Default"
        assert res["OS_PROJECT_DOMAIN_NAME"] == "Default"
        assert res["OS_REGION_NAME"] == "RegionOne"
        assert res["OS_INTERFACE"] == "public"
        assert res["OS_TENANT_NAME"] == "fallback-project"
        # строка без export и с ведущими пробелами тоже разобрана
        assert res["OS_IDENTITY_API_VERSION"] == "3"

    def test_comments_are_skipped(self, tmp_path):
        res = _run_parser(tmp_path)
        for case in ("full", "garbage"):
            assert not any(k.lstrip().startswith("#") for k in res[case]), case
        # закомментированный export не становится значением
        assert "OS_REGION_NAME" not in res["garbage"]

    def test_application_credential_file(self, tmp_path):
        res = _run_parser(tmp_path)["appcred"]
        assert res == {
            "OS_AUTH_URL": FAKE_AUTH_URL,
            "OS_APPLICATION_CREDENTIAL_ID": "ac-id-1",
            "OS_APPLICATION_CREDENTIAL_SECRET": "ac-secret-1",
        }

    def test_empty_and_garbage_yield_nothing(self, tmp_path):
        res = _run_parser(tmp_path)
        assert res["empty"] == {}
        assert res["garbage"] == {}, "нерелевантные строки не должны париться"

    def test_value_keeps_path_and_spaces(self, tmp_path):
        res = _run_parser(tmp_path)
        assert res["urlpath"]["OS_AUTH_URL"] == FAKE_AUTH_URL + "/auth", \
            "путь после порта потерян"
        assert res["spaces"]["OS_PASSWORD"] == "value with spaces"

    def test_parser_is_exported_for_reuse(self):
        js = _read(OSM_JS)
        assert "export function parseOpenrc(" in js


# ==========================================================================
#  2. Поле и кнопка на карточке
# ==========================================================================
class TestOpenrcFieldOnTheCard:

    def test_textarea_and_button_exist(self):
        js = _read(OSM_JS)
        assert 'id="osm-openrc"' in js
        assert 'id="osm-openrc-apply"' in js
        assert "Заполнить из openrc.sh" in js

    def test_textarea_sits_on_the_connection_tab(self):
        """Поле живёт в шаблоне вкладки «Подключение», до полей формы:
        администратор видит его первым, а не ищет после четырнадцати полей.
        Обёртку `osm-panel-conn` искать нельзя: в исходнике она стоит ПОСЛЕ
        шаблона connPanel, поэтому тело вырезается по границам функции."""
        js = _read(OSM_JS)
        body = js.split("function connPanel(", 1)[1].split("\nfunction ", 1)[0]
        assert 'id="osm-openrc"' in body
        assert body.index('id="osm-openrc"') < body.index('id="osm-auth-url"')

    def test_hint_says_parsing_happens_in_the_browser(self):
        js = _read(OSM_JS)
        assert "Разбирается в браузере" in js
        assert "не отправляется" in js
        assert "на вашем устройстве" in js

    def test_missing_variables_do_not_wipe_manual_input(self):
        js = _read(OSM_JS)
        assert "if (!el || !value) return;" in js, \
            "пустая переменная openrc.sh не должна стирать введённое руками"

    def test_filled_list_is_reported_to_the_admin(self):
        js = _read(OSM_JS)
        assert "filled.push(" in js
        assert "filled.join(" in js
        # пустой результат — честная ошибка, а не тихая вставка
        assert "не найдено ни одной переменной OS_" in js

    def test_auth_type_switch_is_shared_with_the_handler(self):
        """Переключение полей по способу аутентификации — одна функция на
        select и на заполнение из файла: иначе раскладка разошлась бы."""
        js = _read(OSM_JS)
        assert "function syncAuthTypeVisibility()" in js
        assert js.count("syncAuthTypeVisibility()") >= 3, \
            "объявление + onchange + заполнение из openrc"

    def test_placeholder_shows_the_expected_shape(self):
        """В подсказке поля — образец строк файла: администратор сразу видит,
        что вставлять нужно файл целиком, а не одно значение."""
        js = _read(OSM_JS)
        tag = js.split("<textarea", 1)[1].split(">", 1)[0]
        assert 'id="osm-openrc"' in tag
        for var in ("OS_AUTH_URL", "OS_USERNAME", "OS_PROJECT_NAME"):
            assert var in tag, f"в образце нет {var}"


# ==========================================================================
#  3. Содержимое файла не достигает сервера
# ==========================================================================
class TestOpenrcNeverReachesTheServer:

    def test_backend_has_no_openrc_route(self):
        router = _read(ROUTER).lower()
        assert "openrc" not in router, \
            "приём файла на сервере — лишний путь для секрета"

    def test_settings_payload_has_no_openrc_field(self):
        js = _read(OSM_JS)
        body = js.split("function readConnectionForm() {", 1)[1].split(
            "\n}", 1)[0]
        assert "osm-openrc" not in body, \
            "полезная нагрузка «Сохранить» не должна содержать текст файла"

    def test_textarea_value_is_read_exactly_once(self):
        js = _read(OSM_JS)
        assert js.count("getElementById('osm-openrc')") == 1, \
            "значение поля читает только обработчик заполнения"

    def test_no_request_carries_openrc(self):
        js = _read(OSM_JS)
        low = js.lower()
        calls = list(re.finditer(r"api\.(?:post|get|put|delete)\(|fetch\(", low))
        assert calls, "запросы в osm.js не найдены — проверка выродилась"
        for m in calls:
            chunk = low[m.start():m.start() + 160]
            assert "openrc" not in chunk, f"запрос с текстом файла: {chunk}"

    def test_apply_only_touches_dom_values(self):
        """applyOpenrc кладёт значения в поля и ничего не отправляет: внутри
        функции нет ни api, ни fetch."""
        js = _read(OSM_JS)
        body = js.split("function applyOpenrc(", 1)[1].split("\n}", 1)[0]
        assert "api." not in body and "fetch(" not in body
        assert "el.value = value" in body


# ==========================================================================
#  4. Данные провайдера владельца не хранятся в репозитории
# ==========================================================================
class TestProviderDataIsNotInTheRepo:
    """Скриншот владельца содержит настоящие адрес панели и логин. Им не
    место в коде, тестах и документации: репозиторий переживает смену
    провайдера, а персональные данные — нет. В проверках выпуска поэтому
    используются заведомо подставные значения (FAKE_*)."""

    # Иглы собраны из соседних литералов: Python склеивает их при разборе, а
    # исходник этого файла полных строк не содержит — иначе замок ловил бы сам
    # себя (проверено: без этого приём падал на tests/test_v17052.py).
    NEEDLES = ("rocket" "cloud", "rcl" "oud-", "cloud" "core")
    SKIP_DIRS = {".git", ".venv", "node_modules", "data", "__pycache__",
                 ".mypy_cache", ".pytest_cache", ".ruff_cache"}

    def test_repo_carries_no_provider_credentials(self):
        hits = []
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            if self.SKIP_DIRS & set(p.parts):
                continue
            try:
                low = p.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            for needle in self.NEEDLES:
                if needle in low:
                    hits.append(f"{p.relative_to(ROOT)}: {needle}")
        assert not hits, f"данные провайдера в репозитории: {hits[:8]}"

    def test_fake_values_are_actually_fake(self):
        """Замок не выродился: подставные значения в этом файле присутствуют,
        значит тест выше отличает их от настоящих."""
        text = _read(pathlib.Path(__file__))
        assert FAKE_USERNAME in text and FAKE_AUTH_URL in text
        assert "example.ru" in FAKE_AUTH_URL
        assert len(self.NEEDLES) == 3
        assert all(len(n) > 6 for n in self.NEEDLES), "иглы вырождены"


# ==========================================================================
#  5. Выпуск v1.70.52
# ==========================================================================
class TestRelease17052:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.51) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.52"
    PREVIOUS = "1.70.51"

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
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_changed_module_is_served_with_the_release_version(self):
        """osm.js загружается как модуль из app.js, поэтому отдельного
        кэш-бастера не требует: его номер приходит вместе с app.js."""
        js = _read(APP_JS)
        assert "from './osm.js'" in js
        assert f"app.js?v={self.VERSION}" in _read(INDEX)

    def test_no_previous_version_left_in_sync_points(self):
        """Исторические комментарии-метки («v1.70.51: Chart.js …») в index.html
        легитимны — они отмечают рождение функции, а не текущую версию.
        Ловятся только рабочие формы: кэш-бастер, имя кэша и поле версии."""
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
        for needle in ("openrc", "браузер", "секрет", "Проверить подключение",
                       "вкладк", "статистику"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        # «openrc» и «sh» — имя файла провайдера ровно так, как его пишет
        # панель управления (openrc.sh); без него администратор не сопоставит
        # запись «Что нового» с полем на карточке. Приём тот же, что для
        # MAX/Telegram/service/worker в прошлых выпусках.
        allowed = {"MAX", "Telegram", "service", "worker", "openrc", "sh"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("openrc", "браузер", "секрет", "кэш-бастер",
                       "Отклонено", "подключен", "скриншот"):
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
                       "Отклонённые варианты", "openrc", "браузер",
                       "кэш-бастер"):
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
        src = _read(RELEASE_51_TESTS)
        block = src.split("class TestRelease17051:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_51_TESTS)
        block = src.split("class TestRelease17051:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
