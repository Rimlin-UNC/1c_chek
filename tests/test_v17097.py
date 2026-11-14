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
    """Выпуск 1.70.97 — исторические проверки (точные пины — в tests/test_v17098.py)."""

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

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-04"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("(T-58)", "3 новые таблицы (doc_requests, doc_request_events, doc_request_settings)",
                       "«🔎 Найти недостающее»", "закрывает запрос сам", "Автонапоминания включает бухгалтер"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2 and "Что нужно от клиента" in block and "«я всё отправил»" in block

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        assert "«🔎 Найти недостающее»" in entry and "«Журнал — письменный след»" in entry

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Роли и доступ", "Что нужно от клиента"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_three_new_tables_without_foreign_keys_to_old_ones(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 55                                                    # v1.70.98: +4 таблицы банка
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
        assert "| Что нужно от клиента (v1.70.97) |" in db


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
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.98: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 97)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 04.11.2026 (выпуск v1.70.97):** [#43](https://github.com/Rimlin-UNC/1c_chek/issues/43) T-58" \
            in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        rows = {r["Версия"]: r for r in ledger}
        assert rows[VERSION]["Опора репетиции"] == "да" and rows["1.70.96"]["Откат на неё"] == "можно"
