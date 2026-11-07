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

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.112", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-07"
        entry = md.split("\n## [1.70.112]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109–1.70.112 не меняются — шаг pip пропускается", "откатится само",
                       "добавляются 2 новые таблицы (bank_incomes — поступления, bank_links — ручные привязки); старые "
                       "таблицы не меняются", "прежняя версия новых таблиц просто не видит", "48 из 48",
                       "1.70.82–1.70.112", "T-79, #63, ADR-43", "«📁 Папка с УПД»", "по SHA-256", "покупатель — расход, "
                       "продавец — продажа", "Документы продажи в 1С как поступление не выгружаются",
                       "«Закрытие документами»", "остаток по очереди дат", "«закрыт частично» с остатком",
                       "загрузите тот же файл ещё раз", "SQLite и PostgreSQL", "T-62", "#47"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Папка с УПД — одним движением", "«📁 Папка с УПД»", "по ИНН", "Какие расходы закрыты документами",
                       "«Закрытие документами»", "CSV"):
            assert needle in block, needle
        assert "'1.70.112': [" in js                         # история выпусков на месте

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Первичка»: папка с УПД; «Банк: выписка»: вкладка «Закрытие документами»")
        entry = journal.split("\n## v1.70.112", 1)[0]
        for needle in ("«📁 Папка с УПД»", "«Проверяем, что уже\n  загружено…»", "«Закрытие документами» (между «Не нужны» и "
                       "«К возмещению»)", "«Привязать» и\n  «Отвязать»", "«Расхождения по контрагентам»", "ADR-43",
                       "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Банковская выписка", "Первичка (электронные УПД)"]
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 113) for v in reg.values())
        before = {**reg, "Банковская выписка": "1.70.99", "Первичка (электронные УПД)": "1.70.109"}
        diff = {d["name"]: (d["from"], d["to"]) for d in updater.diff_blocks(before, reg)}
        assert diff == {"Банковская выписка": ("1.70.99", VERSION), "Первичка (электронные УПД)": ("1.70.109", VERSION)}

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (07.11.2026)" in readme
        assert "Последний выпуск — v1.70.112" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.112.**" in readme
        section = readme.split(f"## 🆕 Последний выпуск — v{VERSION}", 1)[1].split("\n## ", 1)[0]
        assert "Добавляются 2 новые таблицы" in section and "**Папка с УПД:**" in section
        assert "**Закрытие документами:**" in section and "**Из 1.70.112:**" in section

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        assert build_manual()["updated"] == "2026-11-07"
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
        assert declared == 70

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
        assert "70 объявлений `__tablename__`" in db and "все 70 таблиц приложения" in db
        assert "| Закрытие выписки документами (v1.70.113) | `bank_incomes`" in db
        design = read("docs/primary_docs_design.md")
        assert "### Комментарии владельца в #61 (06.11.2026): Indra OCR основным движком? — разбор 07.11.2026" in design
        assert "конкурирующий с продуктами Datalab" in design and "PP-OCRv6 Medium Cyrillic" in design

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t79 = tasks["T-79"]
        assert t79["Статус"] == "done" and t79["Задача"].endswith(f"выпущено в {VERSION}")
        assert t79["Issue/PR"] == ISSUE and t79["Связанные ADR"] == "ADR-28, ADR-43" and t79["Обновлено"] == "2026-11-07"
        t62 = tasks["T-62"]
        assert t62["Статус"] == "todo" and t62["Приоритет"] == "P1" and "Печатная форма нового УПД" in t62["Задача"]
        assert "ТОРГ-12 и акты не рассматриваем" in t62["Задача"]
        t01 = tasks["T-01"]["Блокер"]
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.112 без замороженного «Моего дела»" in t01
        assert "с каждой из 1.70.82–1.70.112 — 48 из 48 проверок" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 07.11.2026 (выпуск v{VERSION}):** [#63]({ISSUE}) T-79" in roadmap
        assert f"T-01 — установить на рабочий сервер v{VERSION} (включает 1.70.82–1.70.112" in roadmap
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert ("- Дальше по порядку: [#47](https://github.com/Rimlin-UNC/1c_chek/issues/47) T-62 — печатная форма "
                "нового УПД") in agent
        assert "T-62 — ТОРГ-12" not in agent
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
        assert ledger[-1]["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", ledger[-1]["Коммит"])
        assert ledger[-2]["Версия"] == "1.70.112" and ledger[-2]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        assert f"| Последний выпуск | **v{VERSION}** — папка с УПД и закрытие выписки документами (T-79; ADR-43)" in kb
        assert "#3 — v1.70.82–v1.70.113 |" in kb and "issues #4–#64" in kb
