"""v1.70.100: рабочее место аутсорсера («Клиенты», переключение клиента), пакет для других учётных систем,
длительность заставки, картинки инструкции.

Поведение проверяют tests/test_clients.py (назначение клиентов, заголовок X-Ym-Company, подмена компании без записи
в базу, изоляция, сводка), tests/test_export_package.py (состав и целостность архива, CSV, файлы УПД, изоляция),
tests/test_splash_settings.py (0–60 с, только администратор), tests/js/clients.test.js (экран) и
tests/js/ym_splash.test.js (контроллер заставки). Сценарии — в DOM-стенде tests/ui_harness.js и в Playwright
(tests/e2e/app.spec.js). Здесь — синхронизация выпуска, схема, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.100"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.100 — исторические проверки (точные пины — в tests/test_v170101.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 5
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_sees_three_digit_patch_as_newer(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.70.99") is True              # 100 > 99 — числами, не строкой
        assert updater._is_newer("1.70.99", VERSION) is False
        assert updater._is_newer(VERSION, "1.70.80") is True

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Рабочее место аутсорсера", "резервная копия", "поверх 1.70.95–1.70.99 не меняются — шаг pip "
                       "пропускается", "откатится само", "новая таблица accountant_clients", "48 из 48", "1.70.82–1.70.99",
                       "«🏢 Клиенты»", "без перевхода", "«📦 Пакет за месяц»", "файлы УПД 5.03 по схеме ФНС",
                       "«Моё дело»", "не помечаются выгруженными", "от 0 до 60 секунд, по умолчанию 5 (было около 3)",
                       "0 — без заставки", "T-69", "T-70", "T-63", "T-64"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Все клиенты в одном окне", "«Клиенты»", "без '\n        + 'перевхода",
                       "Пакет за месяц и своя длительность заставки", "от 0 до 60 секунд, по умолчанию 5"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Клиенты», пакет для других учётных систем и длительность заставки"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«🏢 Все клиенты в\n  одном окне»", "«Компания · своя»", "«🏢 Клиенты»", "«Выбрано клиентов: N»",
                       "«📦 Пакет для других учётных систем»", "«⬇ Скачать пакет»", "«🎬 Заставка загрузки»",
                       "«▶ Показать»", "USER_GUIDE §7.7 и §7.8", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Заставка загрузки", "Инструкция", "Настройки", "Печать и выгрузка в 1С", "Рабочее место аутсорсера",
                     "Роли и доступ"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != "Рабочее место аутсорсера"}
        assert {"name": "Рабочее место аутсорсера", "from": "", "to": reg["Рабочее место аутсорсера"]} in \
            updater.diff_blocks(before, reg)

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_clients_table_without_foreign_keys(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 61                                                    # v1.70.101: +goods_memory
        from app.clients.models import AccountantClient
        table = AccountantClient.__table__
        assert table.name == "accountant_clients" and not table.foreign_keys                     # ADR-09
        assert {c.name for c in table.columns} == {"id", "user_id", "company_id", "granted_by", "created_at"}
        assert any(getattr(c, "name", "") == "uq_accountant_clients_user_company" for c in table.constraints)
        assert table.columns["user_id"].index and table.columns["company_id"].index
        assert "from .clients import models as _clients_models" in read("app/database.py")
        assert "from app.clients import models as _clients" in read("tests/test_migration_compat_postgres.py")
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"clients_tables": sorted(t for t in inspect(engine).get_table_names() if t == "accountant_clients"),' \
            in rehearsal and 'and up.get("clients_tables") == ["accountant_clients"]' in rehearsal
        db = read("docs/DATABASE.md")
        m = re.search(r"(\d+) объявлени\w* `__tablename__`", db)                # v1.70.101: число растёт
        assert m and int(m.group(1)) >= 61 and "| Клиенты бухгалтера (v1.70.100) |" in db
        m = re.search(r"все (\d+) таблиц\w* приложения", db)
        assert m and int(m.group(1)) >= 61 and "с 1.70.100 — `accountant_clients`" in db


class TestWiring:
    def test_routes_and_single_access_point(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/clients", "/api/v1/clients/mine", "/api/v1/clients/access/{user_id}",
                     "/api/v1/export/package", "/api/v1/settings/splash"):
            assert path in paths, path
        auth = read("app/auth.py")
        assert 'requested = request.headers.get("x-ym-company")' in auth
        assert "clients.activate(db, user, requested)" in auth and "if requested and user.role == ROLE_ACCOUNTANT:" in auth
        from app.clients import service
        assert service.HEADER == "X-Ym-Company" and service.ATTENTION_DAYS == 3 and service.MAX_CLIENTS == 500
        src = read("app/clients/service.py")
        assert 'set_committed_value(user, "company_id", cid)' in src                    # подмена без записи в базу
        assert "def registry_visible_filter():" in read("app/services/scoping.py")
        main = read("app/main.py")
        assert "app.include_router(clients_router.router)" in main and "app.include_router(exportpkg_router.router)" in main
        assert '"splash_seconds": splash_seconds,' in main

    def test_role_matrices_and_services(self):
        from app.services import rbac
        assert "clients" in rbac.space_views("admin") and "clients" in rbac.space_views("accountant")
        assert "clients" not in rbac.space_views("manager") and "clients" not in rbac.space_views("user")
        assert "| clients (рабочее место аутсорсера" in read("docs/rbac_matrix.md")
        from app.exportpkg import service as pkg
        assert pkg.FORMAT == "ymaster-chek-package" and pkg.FORMAT_VERSION == "1.0" and pkg.MAX_RECEIPTS == 50000
        from app.services import splash
        assert (splash.DEFAULT, splash.MIN_SECONDS, splash.MAX_SECONDS, splash.KEY) == (5, 0, 60, "splash_seconds")

    def test_audit_phrases(self):
        from app.services.audit_human import humanize_audit as describe
        assert describe("clients_access_set", {"username": "b", "total": 1, "added": 1, "removed": 0})[0] == \
            "назначены клиенты бухгалтера"
        assert describe("export_package", {})[0] == "выгружен пакет для других учётных систем"
        assert describe("splash_settings_updated", {"seconds": 7, "before": 5})[0] == \
            "изменена длительность заставки загрузки"


class TestScreen:
    def test_clients_screen_switcher_and_api(self):
        screen = read("app/static/js/clients.js")
        for needle in ("export async function viewClients(container, opts = {})", "export function clientsCardHtml(row)",
                       "export function clientsSwitcher(sel, me, onSwitch)", "export async function clientsAccessDialog(user, companies)",
                       "export async function clientsDownloadPackage(period, companyId, btn)",
                       "export function clientsPackageHtml(opts = {})", "'/api/v1/export/package?'", "data-open=",
                       "data-pack="):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("clients", "CLIENTS", "viewClients")) for t in tops), tops     # общая область сборки
        app_js = read("app/static/js/app.js")
        assert ("import { viewClients, clientsSwitcher, clientsAccessDialog, clientsPackageHtml, clientsPackageBind } "
                "from './clients.js';") in app_js
        assert "'primary', 'docreq', 'bank', 'clients', 'counterparties', 'settings'],\n                secs: ['data'" in app_js
        # v1.71.1: экран сообщает наверх о созданном клиенте — шапка перерисовывает переключатель
        assert "clients: (c, alive) => viewClients(c, { alive, open: openClientCompany," in app_js
        assert "onCreated: () => { if (state.me && state.me.role === 'accountant') initClientSwitch(); }" in app_js
        assert "if (state.me && state.me.role === 'accountant') initClientSwitch();" in app_js
        assert "window.addEventListener('ymaster:client-reset', (e) => {" in app_js
        assert "class=\"btn btn-sm u-clients\"" in app_js
        assert "${clientsPackageHtml({ isAdmin: isAdmin(), companyId: companyIdParam() })}" in app_js
        index = read("app/static/index.html")
        assert '<a href="#/clients" data-view="clients" class="nav-item accountant-only">' in index
        assert '<select id="client-switch" class="company-select hidden"' in index
        api_js = read("app/static/js/api.js")
        for needle in ("if (client) headers['X-Ym-Company'] = client;", "const CLIENT_DENIED = /компани[ия] клиента/;",
                       "if (e.clientReset && !options.clientRetried) {", "setClient(''); }"):
            assert needle in api_js, needle
        css = read("app/static/css/app.css")
        for cls in (".cl-card {", ".cl-tile {", ".cl-kpis {", ".cl-access-list {", ".splash-set {", ".cl-pack-card {"):
            assert cls in css, cls
        assert '"clients.js"' in read("tests/test_v1121.py")
        spec = read("tests/e2e/app.spec.js")
        assert "test('рабочее место аутсорсера: назначение клиента, «Клиенты» и переключение без перевхода'" in spec

    def test_splash_duration_controller_and_card(self):
        js = read("app/static/js/splash.js")
        for needle in ("var KEY = 'ym-splash-sec', DEFAULT_SEC = 5, MAX_SEC = 60, QUICK_MS = 1300;",
                       "setDuration: function (v) {", "preview: function (v) {", "root.setAttribute('data-length',",
                       "if (!sec) { done = true; root.classList.add('is-gone'); return; }"):
            assert needle in js, needle
        assert 'id="ym-splash-tip"' in read("app/static/index.html")
        app_js = read("app/static/js/app.js")
        assert '<div class="glass card" id="st-splash" data-sec="device" style="order:48">' in app_js
        assert app_js.index('id="st-splash"') > app_js.index("${isAdmin() ? `\n      <div class=\"glass card\" id=\"st-splash\"")
        assert "window.ymSplash.setDuration(about.splash_seconds);" in app_js
        css = read("app/static/css/splash.css")
        assert ".ym-splash[data-length=\"long\"] .ym-splash__tile { animation-duration: 9s; }" in css


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_clients(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_clients100.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("бухгалтер открыл «Клиенты» — раздел в его пространстве", "сначала компания, где нужны действия",
                     "запросы уходят с заголовком выбранного клиента", "открыт «Что нужно от клиента» этой компании",
                     "на экране выгрузки — пакет JSON, CSV и УПД за месяц"):
            assert f"PASS v1.70.100: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_adr_guide_manual(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-30](#adr-30) |" in arch and "## ADR-30" in arch and "`accountant_clients`" in arch
        assert "**с версии / даты:** 1.70.100, 2026-11-05" in arch and "`X-Ym-Company`" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.7. Клиенты: все компании в одном окне (с v1.70.100) — бухгалтер/админ" in guide
        assert "### 7.8. Пакет для других учётных систем: JSON, CSV и УПД (с v1.70.100) — бухгалтер/админ" in guide
        assert "### Настройки → Устройство и служебное → Заставка загрузки (админ, v1.70.100)" in guide
        from app.services.manual_content import SECTIONS, build_manual
        by_id = {s["id"]: s for s in SECTIONS}
        assert "С версии 1.70.100." in by_id["clients"]["html"] and "«🏢 Клиенты»" in by_id["clients"]["html"]
        assert "«📦 Пакет за месяц»" in by_id["package"]["html"]
        assert "Заставка загрузки" in by_id["settings_admin"]["html"] and "🏢 Клиенты" in by_id["users"]["html"]
        for sid in ("install", "my", "look", "verify", "merchant-rules", "policy", "ao", "primary"):
            image = by_id[sid]["image"]
            assert image == f"/img/manual/{sid}.jpg", sid
            assert (ROOT / "app/static" / image.lstrip("/")).read_bytes()[:2] == b"\xff\xd8", sid    # JPEG
        assert build_manual()["updated"] >= "2026-11-05"                  # v1.70.108: не раньше

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        for tid, issue in (("T-50", 35), ("T-68", 53)):
            row = tasks[tid]
            assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}"), tid
            assert row["Issue/PR"] == f"https://github.com/Rimlin-UNC/1c_chek/issues/{issue}", tid
        assert "ADR-30" in tasks["T-50"]["Связанные ADR"] and "ADR-30" in tasks["T-68"]["Связанные ADR"]
        for tid, issue in (("T-69", 54), ("T-70", 55), ("T-71", 56)):                # v1.70.101: T-69, T-70 — done
            assert tasks[tid]["Статус"] in ("todo", "done", "blocked") and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        assert tasks["T-72"]["Статус"] == "done" and tasks["T-72"]["Задача"].endswith(f"выпущено в {VERSION}")
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.101: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        for tid in ("T-41", "T-42", "T-43", "T-44", "T-45", "T-46", "T-47", "T-48", "T-49"):
            assert tasks[tid]["Источник"].endswith("docs/kb/10_research.md"), tid
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.100):** [#35](https://github.com/Rimlin-UNC/1c_chek/issues/35) T-50" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.101: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.99")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
