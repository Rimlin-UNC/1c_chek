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
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.62).
    Здесь остаётся запись выпуска 1.70.61 в истории."""

    VERSION = "1.70.61"
    PREVIOUS = "1.70.60"

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
        assert "участник видит полное пространство как администратор" in flat

    def test_journal_has_release(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_whats_new_entry_exists_in_history(self):
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        assert f"'{self.VERSION}': [" in js

    def test_trackers_reference_a_release(self):
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert re.search(r"\"1\.70\.\d+\"", text)

    def test_sw_cache_name_is_versioned(self):
        sw = SW.read_text(encoding="utf-8")
        assert re.search(r"ymaster-check-v1\.70\.\d+", sw)

    def test_no_placeholder_left(self):
        for path in (CHANGELOG, JOURNAL):
            assert "TODO" not in path.read_text(encoding="utf-8")
