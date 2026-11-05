"""v1.70.107: «Моё дело» по API — этап 1, акты покупок (T-71, ADR-37).

Поведение (ключ только зашифрованным, проверка чтением, разбор документа, акт с явными суммами, поставщик по
ИНН, защита от повторов, пакет, журнал без ключа) — tests/test_moedelo.py и tests/js/moedelo.test.js. Здесь —
синхронизация выпуска, проводка, схема, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.107"


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

    def test_updater_order(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.70.106") is True
        assert updater._is_newer("1.70.106", VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-05"
        entry = md.split("\n## [1.70.106]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("«Моё дело» по API", "резервная копия", "поверх 1.70.95–1.70.106 не меняются — шаг pip "
                       "пропускается", "откатится само", "2 новые таблицы (moedelo_connections и moedelo_links)",
                       "новые таблицы просто не видит", "48 из 48", "1.70.82–1.70.106", "«📒 «Моё дело»»",
                       "«Настройки → Интеграция с партнёрами»", "только чтение", "зашифрованным", "до 50 за раз",
                       "по ИНН", "повторно не уходит", "НДС исчисляет налоговый агент", "T-76", "«Проверить ключ»"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("«Моё дело» без ручного ввода", "Без дублей и со сверкой сумм", "📒 В «Моё дело»", "по ИНН"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Моё дело»: ключ в карточке компании, кнопка в карточке документа "
                                  "и пакет на экране «Первичка»")
        entry = journal.split("\n## v1.70.106", 1)[0]
        for needle in ("«📒 «Моё дело»»", "«Проверить ключ»", "«Это услуги — отправить актом покупки»",
                       "«Забыть отправку»", "рядом с «⬇ В 1С»", "«Уйдёт актами покупки: N»", "USER_GUIDE §7.14",
                       "ADR-37", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        new = "«Моё дело» по API (акты покупок)"
        assert sorted(n for n, v in reg.items() if v == VERSION) == sorted(["Первичка (электронные УПД)", new])
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 107) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != new}
        assert {"name": new, "from": "", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (05.11.2026)" in readme
        assert "Последний выпуск — v1.70.106" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.106.**" in readme
        assert "Схема базы: 2 новые таблицы «Моего дела»,\nстарые не меняются" in readme


class TestWiring:
    def test_module_router_and_tables(self):
        main = read("app/main.py")
        assert "from .moedelo import router as moedelo_router" in main
        assert "app.include_router(moedelo_router.router)" in main
        assert main.index("app.include_router(moedelo_router.router)") < main.index('@app.api_route("/{full_path:path}"')
        assert "from .moedelo import models as _moedelo_models" in read("app/database.py")
        assert "from app.moedelo import models as _moedelo" in read("tests/test_migration_compat_postgres.py")
        from app.moedelo.models import MoedeloConnection, MoedeloLink
        assert MoedeloConnection.__tablename__ == "moedelo_connections" and MoedeloLink.__tablename__ == "moedelo_links"
        assert any(getattr(c, "name", "") == "uq_moedelo_connections_company"
                   for c in MoedeloConnection.__table__.constraints)
        assert any(getattr(c, "name", "") == "uq_moedelo_links_document" for c in MoedeloLink.__table__.constraints)
        assert not any(col.foreign_keys for t in (MoedeloConnection, MoedeloLink) for col in t.__table__.columns)
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 67                                                   # + 2 таблицы «Моего дела»

    def test_rehearsal_sees_new_tables(self):
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"moedelo_tables": sorted(t for t in inspect(engine).get_table_names() if t.startswith("moedelo_"))' \
            in rehearsal
        assert 'and up.get("moedelo_tables") == ["moedelo_connections", "moedelo_links"]' in rehearsal
        assert 'MOEDELO_TABLES = ["moedelo_connections", "moedelo_links"]' in read("qa/migration_e2e.py")

    def test_api_contract_from_public_docs(self):
        from app.moedelo import client, service
        assert client.BASE_URL == "https://restapi.moedelo.org" and client.DOCS_URL.endswith("/docs")
        assert service.KONTRAGENTS_PATH == "/kontragents/api/v1/kontragent"
        assert service.KONTRAGENT_BY_INN_PATH == "/kontragents/api/v1/kontragent/inn"
        assert service.ACT_PATH == "/accounting/api/v1/purchases/act"
        assert service.BATCH_LIMIT == 50
        src = read("app/moedelo/client.py")
        assert '"md-api-key": key' in src and "MockTransport" in src

    def test_ui_wiring(self):
        app_js = read("app/static/js/app.js")
        assert "import { mdConnectPanel } from './moedelo.js';" in app_js
        assert '<div class="info-callout" id="cc-moedelo"></div>' in app_js
        assert "mdConnectPanel(slot.querySelector('#cc-moedelo'), c.id);" in app_js
        assert "moedelo_document_sent: ['ok', '📒']" in app_js
        primary = read("app/static/js/primary.js")
        assert "import { mdBatchSummaryHtml, mdDocPanel, mdResultSummary } from './moedelo.js';" in primary
        assert 'id="prim-md" title="Акты покупок в «Моё дело»">📒 В «Моё дело»</button>' in primary
        assert "mdDocPanel(document.getElementById('prim-md-doc'), id, { onSent: () => load() });" in primary
        assert "'/api/v1/moedelo/send/check'" in primary and "'/api/v1/moedelo/send'" in primary
        assert '"printpack.js", "moedelo.js", "clients.js"' in read("tests/test_v1121.py")
        md_js = read("app/static/js/moedelo.js")
        names = re.findall(r"^export (?:async )?(?:function|const) (\w+)", md_js, re.M)
        assert names and all(n.startswith(("md", "MD_")) for n in names), names


class TestDocsAndKb:
    def test_adr_database_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-37](#adr-37) |" in arch and "## ADR-37" in arch
        assert "**с версии / даты:** 1.70.107, 2026-11-05" in arch
        adr = arch.split("## ADR-37", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("`PurchasesActModel`", "`StockProductId`", "`moedelo_links`", "«Без ключа, проверю у себя»",
                       "https://restapi.moedelo.org/docs", "#60"):
            assert needle in adr, needle
        db = read("docs/DATABASE.md")
        assert "67 объявлений `__tablename__`" in db and "все 67 таблиц приложения" in db
        assert "| «Моё дело» по API (v1.70.107) |" in db and "`key_enc` (secretbox)" in db
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.14. «Моё дело»: акты покупок без ручного ввода (с v1.70.107) — бухгалтер/админ" in guide
        assert guide.index("### 7.14.") < guide.index("## 8. Маппинг реквизитов")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-71"]
        assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}")
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/56" and row["Блокер"] == ""
        assert row["Связанные ADR"] == "ADR-29, ADR-30, ADR-37" and row["Источник"] == "https://restapi.moedelo.org/docs"
        nxt = tasks["T-76"]
        assert nxt["Статус"] == "todo" and nxt["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/60"
        assert "StockProductId" in nxt["Задача"] and nxt["Связанные ADR"] == "ADR-37"
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.106" in tasks["T-01"]["Блокер"]
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.107):** [#56](https://github.com/Rimlin-UNC/1c_chek/issues/56) T-71" \
            in roadmap
        assert "T-01 — установить на рабочий сервер v1.70.107 (включает 1.70.82–1.70.106; P0)" in roadmap
        assert "[#60](https://github.com/Rimlin-UNC/1c_chek/issues/60) T-76" in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert ledger[-2]["Версия"] == "1.70.106" and ledger[-2]["Откат на неё"] == "можно"
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
