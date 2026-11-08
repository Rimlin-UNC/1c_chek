"""v1.70.113: папка с УПД и закрытие выписки документами (T-79, #63, ADR-43).

Поведение — tests/test_bank_closing.py (SQLite и PostgreSQL), браузер — tests/js/primary_folder.test.js и
tests/js/bank_closing.test.js, путь пользователя — сценарий в tests/e2e/app.spec.js. Здесь — точки выпуска,
маршруты, регистрация таблиц, документы и база знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.113"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/63"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    """Выпуск 1.70.113 — исторические проверки (точные пины — в tests/test_v170114.py)."""

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

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.112", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-07"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109–1.70.112 не меняются — шаг pip пропускается", "откатится само",
                       "добавляются 2 новые таблицы (bank_incomes — поступления, bank_links — ручные привязки); старые "
                       "таблицы не меняются", "прежняя версия новых таблиц просто не видит", "48 из 48",
                       "1.70.82–1.70.112", "T-79, #63, ADR-43", "«📁 Папка с УПД»", "по SHA-256", "покупатель — расход, "
                       "продавец — продажа", "Документы продажи в 1С как поступление не выгружаются",
                       "«Закрытие документами»", "остаток по очереди дат", "«закрыт частично» с остатком",
                       "загрузите тот же файл ещё раз", "SQLite и PostgreSQL", "T-62", "#47"):
            assert needle in entry, needle

    def test_whats_new_kept(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Папка с УПД — одним движением", "«📁 Папка с УПД»", "по ИНН", "Какие расходы закрыты документами",
                       "«Закрытие документами»", "CSV"):
            assert needle in block, needle
        assert "'1.70.112': [" in js                         # история выпусков на месте

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Первичка»: папка с УПД; «Банк: выписка»: вкладка «Закрытие документами»"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«📁 Папка с УПД»", "«Проверяем, что уже\n  загружено…»", "«Закрытие документами» (между «Не нужны» и "
                       "«К возмещению»)", "«Привязать» и\n  «Отвязать»", "«Расхождения по контрагентам»", "ADR-43",
                       "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Банковская выписка", "Первичка (электронные УПД)"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        before = {**reg, "Банковская выписка": "1.70.99", "Первичка (электронные УПД)": "1.70.109"}
        diff = {d["name"]: (d["from"], d["to"]) for d in updater.diff_blocks(before, reg)}
        assert diff == {"Банковская выписка": ("1.70.99", reg["Банковская выписка"]),
                        "Первичка (электронные УПД)": ("1.70.109", reg["Первичка (электронные УПД)"])}

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-08"
        by_id = {s["id"]: s for s in SECTIONS}
        assert "<b>С версии 1.70.113 — «📁 Папка с УПД».</b>" in by_id["primary"]["html"]
        assert "<b>С версии 1.70.113 — вкладка «Закрытие документами».</b>" in by_id["bank"]["html"]
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_routes_and_audit(self):
        from app.main import app
        from app.services.audit_human import humanize_audit
        paths = app.openapi()["paths"]
        for path, method in (("/api/v1/bank/closing", "get"), ("/api/v1/bank/closing.csv", "get"),
                             ("/api/v1/bank/closing/candidates", "get"), ("/api/v1/bank/closing/link", "post"),
                             ("/api/v1/bank/closing/unlink", "post"), ("/api/v1/bank/closing/settle", "post"),
                             ("/api/v1/primary/known", "post"), ("/api/v1/primary/upload", "post")):
            assert method in paths[path], path
        upload = read("app/primary/router.py")
        assert 'route: Annotated[Literal["auto"] | None, Form()] = None' in upload
        title, details = humanize_audit("bank_closing_linked", {"company": "Альфа", "side": "income", "documents": 2})
        assert title == "платёж привязан к документам вручную" and "поступление, документов: 2" in details
        assert "bank_closing_linked: ['info', '🔗']" in read("app/static/js/app.js")

    def test_tables_registered(self):
        from app.bank.models import BankIncome, BankLink
        from app.database import Base
        assert {"bank_incomes", "bank_links"} <= set(Base.metadata.tables)
        assert BankIncome.__table__.c.counterparty_inn is not None and BankLink.__table__.c.side is not None
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert ('up.get("bank_tables") == ["bank_connections", "bank_incomes", "bank_links", "bank_operations",'
                in rehearsal)
        assert '"tests/test_bank_closing.py"' in read("qa/pg_local.py")
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 73

    def test_settlement_rules(self):
        from app.bank import closing
        assert closing.FUNCTIONS == ("СЧФ", "СЧФДОП", "ДОП") and closing.SET_MAX == 4
        assert (closing.WINDOW_BEFORE, closing.WINDOW_AFTER) == (120, 60)
        assert list(closing.HOW) == ["manual", "number", "sum", "set", "fifo", "receipt"]
        service = read("app/bank/service.py")
        assert "closing.settle_expense(db, company, refresh=False)" in service and "closing.settle_once(db, op.company_id)" in service
        assert "store_incomes(db, company.id, same.id, ops)" in service                 # повторная загрузка — поступления
        assert "service.store_incomes(db, company.id, row.id, ops)" in read("app/bank/connect.py")
        primary = read("app/primary/service.py")
        assert "return [d for d in docs if not is_sale(d, inns.get(d.company_id, \"\"))]" in primary

    def test_screen_modules(self):
        folder = read("app/static/js/primary_folder.js")
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", folder, re.M)
        assert tops and all(t.startswith(("primFolder", "PRIM_FOLDER")) for t in tops), tops
        for needle in ("form.append('route', 'auto');", "'/api/v1/primary/known'", "'/api/v1/bank/closing/settle'",
                       "webkitGetAsEntry"):
            assert needle in folder, needle
        primary = read("app/static/js/primary.js")
        assert '<input type="file" id="prim-folder" webkitdirectory multiple class="hidden">' in primary
        bank = read("app/static/js/bank.js")
        assert "['closing', 'Закрытие документами']" in bank and "export function bankClosingRowsHtml(rows, filter = 'all')" in bank

    def test_e2e_scenario(self):
        spec = read("tests/e2e/app.spec.js")
        assert "test('папка с УПД: документы по ИНН компании и «Закрытие документами» в выписке'" in spec
        assert "await page.locator('#prim-folder').setInputFiles(folder);" in spec
        assert "Company(name='E2E Закрытие', inn='7703001018')" in read("qa/playwright_seed.py")


# ====================================================================== документы и база знаний
class TestDocsAndKb:
    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-43](#adr-43) |" in arch and "## ADR-43" in arch
        adr = arch.split("## ADR-43", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.70.113, 2026-11-07", "`webkitdirectory`", "`/api/v1/primary/known`",
                       "`route=auto`", "`bank_incomes`", "`bank_links`", "`app/bank/closing.py`", "(FIFO)",
                       "[ADR-28](#adr-28)", "[ADR-09](#adr-09)", "[ADR-14](#adr-14)", "**требует уточнения**", f"[#63]({ISSUE})",
                       "T-79"):
            assert needle in adr, needle
        adr28 = arch.split("## ADR-28", 1)[1].split("## ADR-29", 1)[0]
        assert "- **Дополнено:** [ADR-43](#adr-43), 1.70.113" in adr28
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.17. Папка с УПД и закрытие выписки документами (с v1.70.113)" in guide and "### 7.14." not in guide

    def test_database_doc_and_research(self):
        db = read("docs/DATABASE.md")
        assert "73 объявлений `__tablename__`" in db and "все 73 таблиц приложения" in db
        assert "| Закрытие выписки документами (v1.70.113) | `bank_incomes`" in db
        design = read("docs/primary_docs_design.md")
        assert "### Комментарии владельца в #61 (06.11.2026): Indra OCR основным движком? — разбор 07.11.2026" in design
        assert "конкурирующий с продуктами Datalab" in design and "PP-OCRv6 Medium Cyrillic" in design

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t79 = tasks["T-79"]
        assert t79["Статус"] == "done" and t79["Задача"].endswith(f"выпущено в {VERSION}")
        assert t79["Issue/PR"] == ISSUE and t79["Связанные ADR"] == "ADR-28, ADR-43" and t79["Обновлено"] == "2026-11-07"
        t01 = tasks["T-01"]["Блокер"]
        m = re.search(r"Ставить сразу v([\d.]+)", t01)                 # v1.70.114: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert "без замороженного «Моего дела»" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"[#63]({ISSUE}) T-79" in roadmap and "T-62" in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert TestRelease._ver(ledger[-1]["Версия"]) >= TestRelease._ver(VERSION)
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
        row = next(r for r in ledger if r["Версия"] == VERSION)
        assert row["Опора репетиции"] == "да" and row["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        assert f"v{VERSION}" in kb and "issues #4–" in kb
