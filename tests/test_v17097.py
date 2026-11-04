"""v1.70.97: «Что нужно от клиента» — недостающие документы, срок сдачи, «час икс», напоминания, журнал (T-58).

Поведение проверяют tests/test_docreq.py (расписание на фиксированных датах, поиск недостающего и самозакрытие,
адресность и отсутствие повторов, журнал, «некому отправить», изоляция компаний в API) и tests/js/docreq.test.js
(экран). Сценарии — в DOM-стенде tests/ui_harness.js и в Playwright (tests/e2e/app.spec.js). Здесь — синхронизация
выпуска, схема, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.97"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    def test_sync_points(self):
        from app.config import settings
        assert settings.APP_VERSION == VERSION
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert VERSION in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{VERSION}" in sw and sw.count(f"?v={VERSION}") == 2
        assert read("app/static/index.html").count(f"?v={VERSION}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-04"
        entry = md.split("\n## [1.70.96]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("(T-58)", "резервная копия", "поверх 1.70.95 и 1.70.96 не меняются — шаг pip пропускается",
                       "откатится само", "3 новые таблицы (doc_requests, doc_request_events, doc_request_settings)",
                       "48 из 48", "1.70.82–1.70.96", "«🔎 Найти недостающее»", "два из трёх прошлых месяцев",
                       "закрывает запрос сам", "«час икс» (18:00 МСК)", "Автонапоминания включает бухгалтер",
                       "некому отправить", "Записи не удаляются", "дисциплина клиента", "T-45"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Что нужно от клиента", "чеки без подтверждения ФНС", "закрывает запрос сам", "«час икс»",
                       "только когда вы их включили", "«я всё отправил»"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Что нужно от клиента»: недостающие документы, срок сдачи, "
                                  "напоминания, журнал")
        entry = journal.split("\n## v1.70.96", 1)[0]
        for needle in ("«🔎 Найти недостающее»", "«⚙ Срок и напоминания»", "«📣 Напомнить сейчас»", "«Закрыт сам: …»",
                       "кнопки столбиком", "«Журнал — письменный след»", "USER_GUIDE §7.4", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Роли и доступ", "Что нужно от клиента"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 97) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != "Что нужно от клиента"}
        assert {"name": "Что нужно от клиента", "from": "", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (04.11.2026)" in readme
        assert "Последний выпуск — v1.70.96" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.96.**" in readme


class TestSchema:
    def test_three_new_tables_without_foreign_keys_to_old_ones(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 55
        from app.docreq.models import DocRequest, DocRequestEvent, DocRequestSettings
        assert (DocRequest.__tablename__, DocRequestEvent.__tablename__, DocRequestSettings.__tablename__) == \
            ("doc_requests", "doc_request_events", "doc_request_settings")
        for model in (DocRequest, DocRequestEvent, DocRequestSettings):
            assert not model.__table__.foreign_keys, model.__tablename__        # ADR-09: откат их не замечает
        settings = DocRequestSettings.__table__.columns
        assert settings["enabled"].server_default is not None and settings["auto_detect"].server_default is not None
        assert settings["deadline_day"].server_default.arg == "5" and settings["x_hour"].server_default.arg == "18:00"
        assert "from .docreq import models as _docreq_models" in read("app/database.py")
        assert "from app.docreq import models as _docreq" in read("tests/test_migration_compat_postgres.py")
        assert 'up.get("docreq_tables") == ["doc_request_events", "doc_request_settings", "doc_requests"]' in \
            read("qa/upgrade_rehearsal.py")
        db = read("docs/DATABASE.md")
        assert "55 объявлений `__tablename__`" in db and "| Что нужно от клиента (v1.70.97) |" in db


class TestWiring:
    def test_routes_roles_and_scheduler(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/doc-requests", "/api/v1/doc-requests/detect", "/api/v1/doc-requests/remind",
                     "/api/v1/doc-requests/preview", "/api/v1/doc-requests/journal", "/api/v1/doc-requests/settings",
                     "/api/v1/doc-requests/{req_id}/status"):
            assert path in paths, path
        from app.services import rbac
        views = {role: set(rbac._ROLE_VIEWS[role]) for role in rbac._ROLE_VIEWS}
        assert "docreq" in views["admin"] and "docreq" in views["accountant"]
        assert "docreq" not in views["manager"] and "docreq" not in views["user"]
        main = read("app/main.py")
        assert 'if _os.environ.get("YM_DOCREQ_OFF") != "1":' in main and "await _aio.to_thread(_doc_requests_once)" in main
        assert 'os.environ["YM_DOCREQ_OFF"] = "1"' in read("tests/conftest.py")
        service = read("app/docreq/service.py")
        assert 'REMIND_AT = time(10, 0)' in service and "WINDOW = timedelta(hours=20)" in service
        assert 'DEFAULTS = {"enabled": False, "auto_detect": True, "deadline_day": 5, "x_hour": "18:00"' in service
        assert "| docreq (что нужно от клиента" in read("docs/rbac_matrix.md")

    def test_screen_wired(self):
        screen = read("app/static/js/docreq.js")
        for needle in ("'/api/v1/doc-requests/detect'", "'/api/v1/doc-requests/remind'", "'/api/v1/doc-requests/settings'",
                       "/api/v1/doc-requests/preview?", "/api/v1/doc-requests/journal?", "id=\"dreq-detect\"",
                       "id=\"dreq-remind\"", "id=\"dreq-settings\"", "export async function viewDocRequests(container, opts = {})",
                       "pre.warning"):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("dreq", "DREQ", "viewDocRequests")) for t in tops), tops   # общая область сборки
        app_js = read("app/static/js/app.js")
        assert "import { viewDocRequests } from './docreq.js';" in app_js
        assert "docreq: 'Что нужно от клиента'," in app_js and "docreq_reminded: ['ok', '📣']" in app_js
        assert '<a href="#/docreq" data-view="docreq" class="nav-item accountant-only">' in read("app/static/index.html")
        assert '"primary.js", "docreq.js", "unified_fill.js"' in read("tests/test_v1121.py")
        css = read("app/static/css/app.css")
        assert ".dreq-actions { display: flex; flex-direction: column;" in css and ".dreq-error:empty" in css
        spec = read("tests/e2e/app.spec.js")
        assert "test('что нужно от клиента: запрос документа, отметка «получено» и журнал'" in spec

    def test_manual_and_guide(self):
        from app.services.manual_content import SECTIONS
        section = next(s for s in SECTIONS if s["id"] == "docreq")
        assert section["audience"] == "accountant"
        for needle in ("С версии 1.70.97", "«Найти недостающее»", "закрывается сам", "Автонапоминания включаете вы",
                       "Записи не удаляются"):
            assert needle in section["html"], needle
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.4. Что нужно от клиента (с v1.70.97) — бухгалтер/админ" in guide


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_doc_requests(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_docreq97.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("бухгалтер открыл «Что нужно от клиента»", "запросы месяца запрошены у сервера",
                     "виден срок сдачи и что нужно от клиента", "журнал запрошен",
                     "«Найти недостающее» ушло на сервер за выбранный месяц"):
            assert f"PASS v1.70.97: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_design_and_adr(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("**Сделано в 1.70.97:**", "## «Что нужно от клиента» (1.70.97): решения", "| Что считать недостающим |",
                       "| Расписание |", "| Включение |", "| Дисциплина клиента |"):
            assert needle in design, needle
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-27](#adr-27) |" in arch and "## ADR-27" in arch and "`YM_DOCREQ_OFF`" in arch

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t58 = tasks["T-58"]
        assert t58["Статус"] == "done" and t58["Задача"].endswith(f"выпущено в {VERSION}")
        assert t58["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/43" and "ADR-27" in t58["Связанные ADR"]
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.96" in tasks["T-01"]["Блокер"]
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 04.11.2026 (выпуск v1.70.97):** [#43](https://github.com/Rimlin-UNC/1c_chek/issues/43) T-58" \
            in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert ledger[-2]["Версия"] == "1.70.96" and ledger[-2]["Откат на неё"] == "можно"
