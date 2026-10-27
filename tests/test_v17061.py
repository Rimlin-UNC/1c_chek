"""v1.70.61 — иерархия ролей: каждой роли своё пространство.

Задача владельца (дословно, существенная часть): «у нас в программе есть
разные роли основные администратор, бухгалтер, руководитель, сотрудник и
участник тот кто просто как донор сдает чеки… важно настроить каждому
пространство это определенный доступ и определенные ограниченные права,
а то я за ними что какой то участник видит полное пространство как
администратор это не правильно и меню у него расширенное это тоже не
правильно… Для начала напиши и сделай ирархичную ветку в которой каждая
роль получит свои видимость и пространство… упрасти участнику-донору
пространство и другим ролям».

Ревизия показала щели фронта: охранялись только экраны администратора,
бухгалтера и руководителя; дашборд, сканирование, реестр и настройки
открывались любой роли, а пул-страницы (пространство донора) мелькали в
меню компании при включённом пуле. Сервер при этом оставался строг:
токен пула ядром не принимается, админ-маршруты под require_admin.

Решение — иерархическая матрица пространств (docs/rbac_matrix.md):
  5 администратор — всё; 4 бухгалтер — чеки, выгрузка, мапинг, подбор;
  3 руководитель — только просмотр и свой отчёт; 2 сотрудник — рабочая
  зона; 1 участник-донор — три экрана пула и ничего из контура компании.
Источник истины — app/services/rbac.py; клиент зеркалит матрицу в
ROLE_SPACE (app.js); /api/v1/me отдаёт пространство роли ключом space.

Что здесь проверяется:
 1. Иерархия: администратор надмножен ролям компании, пространство
    донора не пересекается с контуром компании, неизвестная роль
    получает минимум прав.
 2. Паритет сервер/клиент: множества экранов и разделов настроек
    совпадают по ключам в rbac.py и ROLE_SPACE app.js.
 3. Применение: меню фильтруется по матрице, маршрут вне пространства
    закрыт даже ссылкой, настройки показывают только разделы роли,
    /api/v1/me несёт пространство.
 4. Ритуал выпуска 1.70.61 и историзация TestRelease17060.
"""

import pathlib
import re

from app.services import rbac

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
RBAC_PY = APP / "services" / "rbac.py"
AUTH_ROUTES = APP / "routers" / "auth_routes.py"
MATRIX_DOC = ROOT / "docs" / "rbac_matrix.md"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_60_TESTS = ROOT / "tests" / "test_v17060.py"

OWNER_QUOTE_A = ("участник видит полное пространство как администратор "
                 "это не правильно")
OWNER_QUOTE_B = "упрости участнику-донору пространство"

COMPANY_ROLES = ("admin", "accountant", "manager", "user")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _js_space() -> dict:
    """Разбор ROLE_SPACE из app.js: роль → (views, secs)."""
    src = _read(APP_JS)
    block = src.split("const ROLE_SPACE = {", 1)[1].split("\n};", 1)[0]
    out = {}
    for role, views, secs in re.findall(
            r"(\w+):\s*\{\s*views:\s*\[([^\]]*)\]\s*,\s*secs:\s*\[([^\]]*)\]",
            block, re.S):
        out[role] = (set(re.findall(r"'([^']+)'", views)),
                     set(re.findall(r"'([^']+)'", secs)))
    return out


class TestRoleHierarchy:
    """Ветка иерархии: уровень прав и вложенность пространств."""

    def test_levels_descend(self):
        levels = rbac.ROLE_LEVEL
        assert levels["admin"] > levels["accountant"] > \
            levels["manager"] > levels["user"] > levels["donor"]

    def test_admin_superset_of_company_roles(self):
        admin = rbac.space_views("admin")
        for role in COMPANY_ROLES[1:]:
            assert rbac.space_views(role) <= admin, role

    def test_donor_space_is_pool_only(self):
        assert rbac.space_views("donor") == {"public", "my", "partners"}
        assert rbac.space_settings("donor") == frozenset()

    def test_donor_does_not_intersect_company(self):
        company = set()
        for role in COMPANY_ROLES:
            company |= rbac.space_views(role)
        assert not (company & rbac.space_views("donor"))

    def test_unknown_role_gets_donor_minimum(self):
        assert rbac.space_views("intruder") == rbac.space_views("donor")
        assert rbac.space_settings("intruder") == frozenset()

    def test_hierarchy_selfcheck(self):
        assert rbac.hierarchy_is_intact()

    def test_manager_is_read_only_branch(self):
        # руководитель: сводки и отчёт, без сканирования и выгрузки
        views = rbac.space_views("manager")
        assert "scan" not in views and "export" not in views
        assert "boss" in views and "receipts" in views


class TestParityServerClient:
    """Матрица не разъезжается между сервером и клиентом."""

    def test_views_and_secs_match(self):
        js = _js_space()
        assert set(js) == {"admin", "accountant", "manager", "user",
                           "donor"}, set(js)
        for role, (views, secs) in js.items():
            assert views == set(rbac.space_views(role)), role
            assert secs == set(rbac.space_settings(role)), role

    def test_me_payload_carries_space(self):
        src = _read(AUTH_ROUTES)
        assert "space_payload(user.role)" in src
        assert 'd["space"]' in src

    def test_matrix_document_matches_code(self):
        doc = _read(MATRIX_DOC)
        for role in COMPANY_ROLES + ("donor",):
            assert role in doc, role
        for sec in ("data", "checks", "integr", "personal", "device"):
            assert sec in doc, sec


class TestSpaceEnforcement:
    """Пространство применяется в меню, маршрутах и настройках."""

    def test_menu_is_filtered_by_matrix(self):
        js = _read(APP_JS)
        assert ("if (a.dataset.view && !spaceHas('view:' + a.dataset.view)) "
                "a.classList.add('hidden');") in js

    def test_route_is_guarded_by_matrix(self):
        js = _read(APP_JS)
        assert "if (!spaceHas('view:' + view)) {" in js
        assert "location.hash = getToken() ? '#/dashboard' : '#/public';" \
            in js

    def test_settings_groups_and_cards_are_filtered(self):
        js = _read(APP_JS)
        assert "SETTINGS_GROUPS.filter((g) => spaceHas('sec:' + g.key))" in js
        assert ("container.querySelectorAll('.glass.card[data-sec]')"
                ".forEach((c) => {") in js
        assert "if (!spaceHas('sec:' + c.dataset.sec)) " \
            "c.classList.add('hidden');" in js

    def test_previous_role_classes_are_intact(self):
        # классовые фильтры прежних выпусков остались страховкой
        js = _read(APP_JS)
        assert "$$('.admin-only').forEach(el => el.classList.toggle(" \
            "'hidden', !isAdmin()));" in js
        assert "$$('.pool-only').forEach(el => el.classList.toggle(" \
            "'hidden', !state.poolEnabled));" in js

    def test_server_guards_are_intact(self):
        src = _read(ROOT / "app" / "auth.py")
        assert "def require_admin(" in src
        assert "def require_role(" in src


class TestRelease17061:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.60) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.61"
    PREVIOUS = "1.70.60"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(
            APP / "config.py")
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

    def test_changed_assets_are_served_with_the_release_version(self):
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index

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
        assert found and found.group(1) == self.VERSION, \
            "запись выпуска не первая"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("пространств", "меню", "настрой", "донор"):
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
        flat = section.lower()
        for needle in ("rbac", "space", "донор", "иерарх", "кэш-бастер",
                       "отклонено"):
            assert needle in flat, f"в CHANGELOG нет «{needle}»"
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section, f"в CHANGELOG нет «{head}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "пространств", "донор",
                       "меню", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
                assert needle in flat, f"{path.name}: нет цитаты владельца"

    def test_previous_release_class_is_history_only(self):
        src = _read(RELEASE_60_TESTS)
        block = src.split("class TestRelease17060:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_60_TESTS)
        block = src.split("class TestRelease17060:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
