"""v1.70.94: первичка — электронные УПД и счета-фактуры 5.01–5.03: загрузка, проверка, реестр (T-53).

Поведение проверяют tests/test_primary_upd.py (разбор 5.01 и 5.03, проверки, приём, дубли,
компании, архивы, API) и tests/js/primary.test.js (разметка и экран); сценарий экрана — в
DOM-стенде tests/ui_harness.js. Здесь — синхронизация выпуска, проводка, схема, документация и
база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.94"


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
        entry = md.split("\n## [1.70.93]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "шаг pip пропускается", "2 новые таблицы первички", "48 из 48",
                       "1.70.82–1.70.93", "(T-53)", "5.01–5.03", "«Определить по ИНН покупателя»",
                       "строка 1а", "Исходный XML не хранится", "(5 МБ)", "понятный отказ", "T-60", "T-58"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("«Первичка (УПД)»", "НДС 22 %", "формат 5.03", "Дубли не загружаются",
                       "ИНН указан покупателем"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Первичка (УПД)»: загрузка, проверка и реестр электронных УПД")
        entry = journal.split("\n## v1.70.93", 1)[0]
        for needle in ("«Определить по ИНН покупателя»", "«Удалить документ»", "по 50", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Первичка (электронные УПД)", "Роли и доступ"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 94) for v in reg.values())
        # новый блок отчёт обновления показывает как появившийся: прежней версии у него нет
        before = {name: ver for name, ver in reg.items() if name != "Первичка (электронные УПД)"}
        assert {"name": "Первичка (электронные УПД)", "from": "", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (04.11.2026)" in readme
        assert "Последний выпуск — v1.70.93" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.93.**" in readme


class TestSchema:
    def test_two_new_tables(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 52
        from app.primary.models import PrimaryDocument, PrimaryDocumentLine
        assert PrimaryDocument.__tablename__ == "primary_documents"
        assert PrimaryDocumentLine.__tablename__ == "primary_document_lines"
        cols = set(PrimaryDocument.__table__.columns.keys())
        # исходный XML не хранится — только отпечаток файла для поиска дублей
        assert "file_sha256" in cols and not {"xml", "content", "raw", "body"} & cols
        assert "exported_at" in cols            # под выгрузку в 1С (T-60)

    def test_tables_created_and_checked_on_upgrade(self):
        assert "from .primary import models as _primary_models" in read("app/database.py")
        assert 'PRIMARY_TABLES = ["primary_document_lines", "primary_documents"]' in read("qa/migration_e2e.py")
        assert 'up.get("primary_tables") == ["primary_document_lines", "primary_documents"]' in \
            read("qa/upgrade_rehearsal.py")
        assert "from app.primary import models as _primary" in read("tests/test_migration_compat_postgres.py")
        db = read("docs/DATABASE.md")
        assert "52 объявления `__tablename__`" in db and "| Первичка (v1.70.94) |" in db


class TestWiring:
    def test_router_and_roles(self):
        from app.main import app
        from app.services import rbac
        paths = set(app.openapi()["paths"])     # FastAPI 0.16x: подключённые роутеры — ленивые, без .path
        assert {"/api/v1/primary", "/api/v1/primary/upload", "/api/v1/primary/{doc_id}"} <= paths
        assert "primary" in rbac.space_views(rbac.ROLE_ADMIN)
        assert "primary" in rbac.space_views(rbac.ROLE_ACCOUNTANT)
        for role in (rbac.ROLE_MANAGER, rbac.ROLE_USER, rbac.ROLE_DONOR):
            assert "primary" not in rbac.space_views(role), role
        assert "| primary (первичка: электронные УПД, с v1.70.94) | ✓ | ✓ | — | — | — |" in read("docs/rbac_matrix.md")

    def test_screen_wired(self):
        js = read("app/static/js/app.js")
        assert "import { viewPrimary } from './primary.js';" in js
        assert "  primary: 'Первичка'," in js
        assert "primary: (c, alive) => viewPrimary(c, { alive, isAdmin: isAdmin()," in js
        assert js.count("'primary', 'settings']") == 2           # пространства admin и accountant
        html = read("app/static/index.html")
        assert '<a href="#/primary" data-view="primary" class="nav-item accountant-only">' in html
        screen = read("app/static/js/primary.js")
        assert "export async function viewPrimary(container, opts = {})" in screen
        assert "'/api/v1/primary/upload'" in screen and "slot.classList.add('prim-modal')" in screen
        assert '"primary.js"' in read("tests/test_v1121.py")
        # браузерный сценарий: загрузка, итог проверки, карточка и повторная загрузка как дубль
        spec = read("tests/e2e/app.spec.js")
        assert "test('первичка: администратор загружает УПД и видит итог проверки'" in spec
        assert "toContainText('уже был загружен')" in spec

    def test_manual_and_guide(self):
        from app.services.manual_content import SECTIONS
        ids = [s["id"] for s in SECTIONS]
        assert ids.index("ao") < ids.index("primary") < ids.index("companies")
        section = SECTIONS[ids.index("primary")]
        assert section["audience"] == "accountant" and "«Первичка (УПД)»" in section["html"]
        assert "### 7.3. Первичка: электронные УПД (с v1.70.94)" in read("docs/USER_GUIDE.md")


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_opens_the_screen(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_primary.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("бухгалтер открыл «Первичку» — раздел в его пространстве", "экран «Первичка» нарисован",
                     "реестр запрошен у сервера", "в реестре документ и итог проверки"):
            assert f"PASS v1.70.94: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_design_and_docs(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("**Сделано в 1.70.94:**", "| 1б |", "| T-60 |", "## Решения по УПД (1.70.94)", "SHA-256"):
            assert needle in design, needle

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t53 = tasks["T-53"]
        assert t53["Статус"] == "done" and t53["Задача"].endswith(f"выпущено в {VERSION}")
        t60 = tasks["T-60"]
        assert (t60["Статус"], t60["Приоритет"]) == ("todo", "P1")
        assert t60["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/45"
        assert t60["Источник"].endswith("docs/primary_docs_design.md")
        assert f"Ставить сразу v{VERSION}" in tasks["T-01"]["Блокер"]
        assert "[#45](https://github.com/Rimlin-UNC/1c_chek/issues/45) T-60" in read("docs/kb/02_roadmap.md")
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION
