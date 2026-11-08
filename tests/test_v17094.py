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
    """Выпуск 1.70.94 — исторические проверки (точные пины — в tests/test_v17095.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 4
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-04"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("(T-53)", "5.01–5.03", "«Определить по ИНН покупателя»", "Исходный XML не хранится"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2 and "«Первичка (УПД)»" in block and "Дубли не загружаются" in block

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        assert "«Определить по ИНН покупателя»" in entry and "«Удалить документ»" in entry

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Первичка (электронные УПД)", "Роли и доступ"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        # блок, которого не было, отчёт обновления показывает как появившийся: прежней версии у него нет
        before = {name: ver for name, ver in reg.items() if name != "Первичка (электронные УПД)"}
        assert {"name": "Первичка (электронные УПД)", "from": "", "to": reg["Первичка (электронные УПД)"]} \
            in updater.diff_blocks(before, reg)

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_two_new_tables(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 52
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
        assert re.search(r"\d+ объявлени\w* `__tablename__`", db) and "| Первичка (v1.70.94) |" in db


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
        # v1.70.98: маршрут ждёт список компаний администратора (прямая ссылка) — затем экран
        assert "viewPrimary(c, { alive, isAdmin: isAdmin(), companies, companyFilter: state.companyFilter })" in js
        # пространства admin и accountant (v1.70.97: между ними — «что нужно от клиента»)
        # v1.70.98: и «банк: выписка» — между ними может стоять 'bank'
        assert len(re.findall(r"'primary', (?:'docreq', )?(?:'bank', )?(?:'clients', )?(?:'counterparties', )?'settings'\]", js)) == 2
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
        assert t60["Статус"] in ("todo", "done") and t60["Приоритет"] == "P1"     # v1.70.96: выпущено (T-60)
        assert t60["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/45"
        assert t60["Источник"].endswith("docs/primary_docs_design.md")
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.95: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 94)
        assert "[#45](https://github.com/Rimlin-UNC/1c_chek/issues/45) T-60" in read("docs/kb/02_roadmap.md")
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert any(r["Версия"] == VERSION for r in ledger)
