"""v1.71.3: сводка клиентов договаривает то, что сервер уже знал (этапы 3 и 4 плана «Клиенты»).

T-86 (ADR-51): согласования авансовых отчётов в сводке. T-87 (ADR-52): банки в карточке клиента.

Поведение — tests/test_v1713.py (SQLite и PostgreSQL), окно — tests/js/clients.test.js и сценарий
в tests/e2e/app.spec.js. Здесь — точки выпуска, документы, руководство и база знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.71.3"
PREV = "1.71.2"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        assert settings.APP_VERSION == VERSION
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert VERSION in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{VERSION}" in sw and sw.count(f"?v={VERSION}") == 4
        assert read("app/static/index.html").count(f"?v={VERSION}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_updater_order(self):
        from app.services import updater
        for older in (PREV, "1.71.1", "1.71.0", "1.70.114"):
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-08"
        entry = md.split(f"\n## [{PREV}]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "Зависимости и схема базы не меняются", "T-86, ADR-51", "T-87, ADR-32",
                       "«ждут меня»", "просрочено", "очереди внимания", "выключено", "своя ошибка",
                       "1.70.82–1.71.2", "SQLite и PostgreSQL", "только синтетика", "T-86 и T-87 закрыты"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{\s*'([\d.]+)'", js)
        assert m and m.group(1) == VERSION, "запись выпуска не первая"
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Согласования видны в сводке", "Банки в карточке", "ждут меня", "своей ошибкой"):
            assert needle in block, needle
        assert f"'{PREV}': [" in js

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Клиенты»")
        entry = journal.split("\n## v", 1)[0]
        for needle in ("Согласования", "data-open=\"approvals\"", "clientsApprovalsTile",
                       "clientsBankLines", "cl-bank-error", "Две карточки", "KPI"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert reg["Рабочее место аутсорсера"] == VERSION
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (08.11.2026)" in readme
        assert f"**Ставьте {VERSION} вместо {PREV}.**" in readme
        assert f"Последний выпуск — v{PREV}" not in readme
        section = readme.split(f"## 🆕 Последний выпуск — v{VERSION}", 1)[1].split("\n## ", 1)[0]
        for needle in ("**Согласования видны в сводке:**", "**Банки по отдельности:**", "Согласования",
                       "Из 1.71.2:"):
            assert needle in section, needle

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-08"
        by_id = {s["id"]: s for s in SECTIONS}
        html = by_id["clients"]["html"]
        assert "<b>С версии 1.71.3 — согласования рядом.</b>" in html
        assert "«Согласования»" in html and "ждут вашего хода" in html
        assert "<b>С версии 1.71.3 — банки по отдельности.</b>" in html
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_one_turn_rule(self):
        """Правило «чей ход» — одна функция: экран «Согласования» и сводка клиентов не расходятся."""
        from app.approvals import service as approvals
        assert callable(approvals.turn_filter)
        router = read("app/approvals/router.py")
        assert "service.turn_filter(" in router
        assert 'AdvanceApproval.status == "draft", AdvanceApproval.employee_id == user.id' not in router, \
            "правило «ждут меня» продублировано в роутере"
        clients = read("app/clients/service.py")
        assert "approvals.turn_filter(" in clients

    def test_overview_fields(self):
        """Поля сводки: блок approvals в компании и итоги по согласованиям."""
        src = read("app/clients/service.py")
        for needle in ('"enabled": False, "todo": 0, "overdue": 0, "work": 0, "month": 0',
                       '"approvals_todo": 0, "approvals_overdue": 0', '"approvals_overdue": 3'):
            assert needle in src, needle

    def test_banks_list(self):
        """Банки в карточке: список считается на сервере, худший — первым, у каждого своя ошибка."""
        src = read("app/clients/service.py")
        for needle in ('"banks": []', '"last_error": f"{title}: {conn.last_error}"', 'info["banks"].sort(',
                       "def _accounts_count("):
            assert needle in src, needle
        js = read("app/static/js/clients.js")
        assert "export function clientsBankLines(" in js and "cl-bank-error" in js

    def test_schema_unchanged(self):
        """Схема не менялась: по-прежнему 73 таблицы, миграций нет."""
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 73
        assert "73 объявлений `__tablename__`" in read("docs/DATABASE.md")

    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-51](#adr-51) |" in arch and "## ADR-51" in arch
        adr = arch.split("## ADR-51", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.71.3, 2026-11-08", "turn_filter", "approval_settings.enabled",
                       "Схема базы не меняется", "[ADR-30](#adr-30)", "[ADR-14](#adr-14)", "T-86"):
            assert needle in adr, needle
        assert "| [ADR-52](#adr-52) |" in arch and "## ADR-52" in arch
        adr2 = arch.split("## ADR-52", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.71.3, 2026-11-08", "`bank.banks`", "bank_connections",
                       "худшего к лучшему", "Схема базы не меняется", "[ADR-32](#adr-32)", "T-87"):
            assert needle in adr2, needle
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.7.2. Согласования в сводке клиентов (с v1.71.3)" in guide
        assert "### 7.7.3. Банки в карточке клиента (с v1.71.3)" in guide
        assert "ждут меня" in guide and "просрочено" in guide and "«Банк»" in guide

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t86 = tasks["T-86"]
        assert t86["Статус"] == "done" and "ADR-51" in t86["Связанные ADR"]
        assert "1.71.3" in t86["Задача"] and t86["Обновлено"] == "2026-11-08"
        t87 = tasks["T-87"]
        assert t87["Статус"] == "done" and "ADR-52" in t87["Связанные ADR"]
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации"
        assert ledger[PREV]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        assert f"| Последний выпуск | **v{VERSION}**" in kb

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return TestRelease._ver(value)
