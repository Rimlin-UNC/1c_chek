"""v1.70.111: время чеков по QR — отчёт и возврат кнопкой администратора (T-78, #62, ADR-41).

Поведение — tests/test_receipt_time_repair.py (SQLite и PostgreSQL) и вкладка в
tests/js/unified_review.test.js. Здесь — точки выпуска, маршруты, документы и база знаний.
Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.111"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/62"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    """Выпуск 1.70.111 — исторические проверки (точные пины — в tests/test_v170112.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 3
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.110", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-06"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109–1.70.110 не меняются — шаг pip пропускается",
                       "откатится само", "Схема базы не меняется", "Сама установка время чеков не трогает",
                       "48 из 48", "1.70.82–1.70.110", "T-78, #62, ADR-41", "«Время чеков»",
                       "«Вернуть время из QR»", "без неё время не меняется", "старое и новое время",
                       "Ручной ввод", "без пересчёта в часовой пояс браузера", "SQLite и PostgreSQL", "T-46"):
            assert needle in entry, needle

    def test_whats_new_kept(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Время чеков — по QR", "«Разбор конфликтов» → «Время чеков»", "«Вернуть время из QR»",
                       "Ничего не меняется без вас", "Старое время каждого чека остаётся в журнале"):
            assert needle in block, needle
        assert "'1.70.110': [" in js                         # история выпусков на месте

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Разбор конфликтов»: вкладка «Время чеков» и возврат по QR"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("после «Готовность» — вкладка «Время чеков»", "«Вернуть время из QR (K)»", "«−5 ч»",
                       "без пересчёта в часовой пояс браузера", "значком 🕒", "ADR-41", "две\n  карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Единый реестр чеков"]) >= self._ver(VERSION)
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)

    def test_manual(self):
        from app.services.manual_content import build_manual
        manual = build_manual()
        assert manual["updated"] >= "2026-11-06"
        html = "".join(s.get("html", "") for s in manual["sections"])
        assert "<b>С версии 1.70.111 — вкладка «Время чеков».</b>" in html
        assert "<b>«Вернуть время из QR»</b>" in html


# ====================================================================== код
class TestCode:
    def test_routes_and_audit(self):
        from app.main import app
        from app.services.audit_human import humanize_audit
        paths = app.openapi()["paths"]
        assert "get" in paths["/api/v1/unified/receipt-time"]
        assert "post" in paths["/api/v1/unified/receipt-time/restore"]
        router = read("app/unified/router.py")
        assert 'backups.create_backup("manual")' in router and 'dialect.name == "sqlite"' in router
        title, details = humanize_audit("receipt_time_restored",
                                        {"from": "2026-09-22T10:56:00", "to": "2026-09-22T15:56:00", "hours": -5})
        assert title == "время чека возвращено по QR" and "сдвиг: -5 ч" in details
        assert "receipt_time_restored: ['warn', '🕒']" in read("app/static/js/app.js")

    def test_rules(self):
        from app.unified import time_repair
        assert time_repair.MAX_HOURS == 72 and time_repair.TOLERANCE.total_seconds() == 60
        src = read("app/unified/time_repair.py")
        assert 'row.source == "manual"' in src and 'row.details_source != "manual_edit"' in src
        assert "unified.sync_registry(db, receipt, user" in src and '"receipt_time_restored"' in src

    def test_tab(self):
        js = read("app/static/js/unified_review.js")
        assert 'id="ur-tab-time"' in js and 'id="ur-pane-time"' in js
        assert "const PANES = ['conflicts', 'claims', 'visibility', 'readiness', 'time'];" in js
        assert "api.post('/api/v1/unified/receipt-time/restore', {})" in js
        helper = js.split("export function wallTime(iso) {", 1)[1].split("\n}", 1)[0]
        assert "new Date" not in helper                     # время — как есть, без пояса браузера
        assert '"tests/test_receipt_time_repair.py"' in read("qa/pg_local.py")


# ====================================================================== документы и база знаний
class TestDocsAndKb:
    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-41](#adr-41) |" in arch and "## ADR-41" in arch
        adr = arch.split("## ADR-41", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.70.111, 2026-11-06", "`app/unified/time_repair.py`",
                       "`details_source = manual_edit`", "`sync_registry`", "`receipt_time_restored`",
                       "`POST /api/v1/unified/receipt-time/restore`", "[ADR-40](#adr-40)", "[ADR-14](#adr-14)",
                       f"[#62]({ISSUE})", "T-78"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        assert "- С 1.70.111 «Разбор конфликтов» → вкладка «Время чеков» (администратор): чеки, у" in guide
        assert "вкладка «Время чеков» (1.70.111)." in guide

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t78 = tasks["T-78"]
        assert t78["Статус"] == "done" and t78["Задача"].endswith(f"выпущено в {VERSION}")
        assert t78["Issue/PR"] == ISSUE and t78["Связанные ADR"] == "ADR-14, ADR-40, ADR-41"
        t01 = tasks["T-01"]["Блокер"]
        m = re.search(r"Ставить сразу v([\d.]+)", t01)                      # v1.70.112: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert "без замороженного «Моего дела»" in t01
        assert "(уже сдвинутые возвращает вкладка «Время чеков» в «Разборе конфликтов» — 1.70.111)" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 06.11.2026 (выпуск v{VERSION}):** [#62]({ISSUE}) T-78" in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.112: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert "T-78" not in agent and "T-41" not in agent
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", row["Коммит"])
        assert ledger["1.70.110"]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        m = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb)          # v1.70.112: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
