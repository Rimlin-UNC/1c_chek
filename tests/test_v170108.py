"""v1.70.108: «Моё дело» заморожено — модуль убран из программы целиком (ADR-38, решение владельца 06.11.2026).

Проверяется сама «операция»: модуля, панели, маршрутов, строк журнала и упоминаний сервиса в программе нет;
таблицы выпуска 1.70.107 больше не создаются, а там, где они остались, init_db() их не трогает; пакет для других
учётных систем (открытые форматы) работает как прежде. Плюс синхронизация выпуска, документация и база знаний.
"""
from __future__ import annotations

import csv
import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.108"
C60 = "https://github.com/Rimlin-UNC/1c_chek/issues/60#issuecomment-6671891019"
# след сервиса в коде и текстах: имя модуля, заголовок ключа API и название во всех падежах
TRACE = re.compile(r"moedelo|md-api-key|\b[Мм]о[её]\s+дел|\b[Мм]оего\s+дела|\b[Мм]о[её]м\s+деле|\b[Мм]оему\s+делу"
                   r"|\b[Мм]оим\s+делом")
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".html", ".css", ".json", ".webmanifest", ".txt", ".md", ".xml",
                 ".xsd", ".svg", ".sh", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".conf", ".sql", ".bsl"}
# DDL таблиц 1.70.107 (из app/moedelo/models.py коммита d21dc99) — для проверки «оставшиеся не трогаем»
DDL_107 = (
    "CREATE TABLE moedelo_connections (id VARCHAR(36) NOT NULL, company_id VARCHAR(36), key_enc TEXT, "
    "key_tail VARCHAR(4), status VARCHAR(10), last_check_at DATETIME, last_ok_at DATETIME, last_error VARCHAR(300), "
    "created_by VARCHAR(36), created_at DATETIME, updated_by VARCHAR(36), updated_at DATETIME, PRIMARY KEY (id), "
    "CONSTRAINT uq_moedelo_connections_company UNIQUE (company_id))",
    "CREATE TABLE moedelo_links (id VARCHAR(36) NOT NULL, company_id VARCHAR(36), document_id VARCHAR(36), "
    "md_kind VARCHAR(10), md_id VARCHAR(20), md_number VARCHAR(100), kontragent_id VARCHAR(20), "
    "kontragent_new INTEGER, sum_sent FLOAT, sum_md FLOAT, status VARCHAR(10), message VARCHAR(300), "
    "created_by VARCHAR(36), created_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_moedelo_links_document "
    "UNIQUE (document_id))",
)


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def program_files():
    """Файлы программы и её инструментов — всё, кроме документации, истории выпусков и тестов."""
    for top in ("app", "qa", "deploy", "integration_1c", "supabase", "brand", ".github"):
        base = ROOT / top
        if base.exists():
            for p in base.rglob("*"):
                if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and "__pycache__" not in p.parts:
                    yield p
    for p in ROOT.iterdir():
        if p.is_file() and (p.suffix.lower() in TEXT_SUFFIXES or p.name in ("Dockerfile", ".env.example")) \
                and p.name not in ("CHANGELOG.md", "README.md", "AGENTS.md"):
            yield p


class TestRelease:
    """Выпуск 1.70.108 — исторические проверки (точные пины — в tests/test_v170109.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 2
        assert read("app/static/index.html").count(f"?v={current}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.107", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-06"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("«Моё дело» заморожено", "резервная копия", "поверх 1.70.95–1.70.107 не меняются — шаг pip "
                       "пропускается", "откатится само", "Схема базы не меняется", "Таблицы moedelo_connections и "
                       "moedelo_links больше не создаются", "не читает и не удаляет", "48 из 48", "1.70.82–1.70.107",
                       "ADR-38", "побайтно", "УПД 5.03 по схеме ФНС", "EnterpriseData", "«📦 Пакета за месяц»",
                       "T-71 и T-76 заморожены", "T-41", "T-46"):
            assert needle in entry, needle
        # запись 1.70.107 остаётся историей, с пометкой (как «yanked» в Keep a Changelog): её начало попадает
        # в окно обновления установленной версии — читатель сразу видит, что модуль убран
        assert md.split("## [1.70.107] - 2026-11-05\n\n", 1)[1].startswith(
            "> Модуль «Моё дело» из этого выпуска заморожен владельцем 06.11.2026 и убран в 1.70.108 (ADR-38).\n")

    def test_whats_new_kept(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Без лишнего — как в 1.70.106", "Учётные системы — открытыми форматами",
                       "данные и настройки не менялись", "Без внешних сервисов и ключей"):
            assert needle in block, needle
        assert "'1.70.107': [" not in js                     # карточки убранного модуля не хранятся
        assert "'1.70.106': [" in js                         # история прочих выпусков на месте

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — «Моё дело» убрано: карточка компании и «Первичка» — как в 1.70.106"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«📬 Почтой для документов»", "«⬇ В 1С» и «Обновить»", "«⬇ Файл для 1С» и «Удалить документ»",
                       "значков 📒", "«для других учётных систем»", "USER_GUIDE §7.14 убран", "ADR-38",
                       "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Первичка (электронные УПД)"]) >= self._ver(VERSION)
        assert "«Моё дело» по API (акты покупок)" not in reg
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSurgery:
    """Модуль вырезан целиком; всё, во что он был встроен, — как в 1.70.106."""

    def test_module_files_gone(self):
        for rel in ("app/moedelo", "app/static/js/moedelo.js", "tests/test_moedelo.py", "tests/js/moedelo.test.js"):
            assert not (ROOT / rel).exists(), rel
        assert importlib.util.find_spec("app.moedelo") is None

    def test_no_traces_in_program(self):
        found = []
        for p in program_files():
            text = p.read_text(encoding="utf-8", errors="ignore")
            found += [f"{p.relative_to(ROOT)}: {m.group(0)}" for m in TRACE.finditer(text)]
        assert not found, found[:10]
        js = read("app/static/js/app.js") + read("app/static/js/primary.js")
        for needle in ("prim-md", "cc-moedelo", "mdConnectPanel", "mdDocPanel", "openMd(", "📒"):
            assert needle not in js, needle
        assert '"printpack.js", "clients.js"' in read("tests/test_v1121.py")      # модулей интерфейса — как в 1.70.106

    def test_routes_gone(self, client):
        from app.main import app
        assert not [r.path for r in app.routes if "moedelo" in getattr(r, "path", "")]
        r = client.get("/api/v1/moedelo/connection")
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}

    def test_tables_not_declared(self):
        from app.database import Base
        assert not [t for t in Base.metadata.tables if t.startswith("moedelo_")]
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 65                                                   # снова 65, как в 1.70.106
        assert "moedelo" not in read("qa/upgrade_rehearsal.py") + read("qa/migration_e2e.py")
        assert "moedelo" not in read("tests/test_migration_compat_postgres.py")

    def test_leftover_tables_from_107_untouched(self, tmp_path):
        """Там, где стояла 1.70.107: init_db() новой версии не трогает её таблицы — не читает и не удаляет."""
        db = tmp_path / "after107.db"
        with sqlite3.connect(db) as conn:
            for ddl in DDL_107:
                conn.execute(ddl)
            conn.execute("INSERT INTO moedelo_connections (id, company_id, key_enc, key_tail, status) "
                         "VALUES ('c1', 'co1', 'sealed', 'abcd', 'active')")
            conn.execute("INSERT INTO moedelo_links (id, company_id, document_id, md_id, status, sum_sent) "
                         "VALUES ('l1', 'co1', 'd1', '777', 'sent', 1200.5)")
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "YM_NO_REEXEC": "1", "YM_FETCHQ_OFF": "1",
               "YM_DOCREQ_OFF": "1", "YM_OSMON_OFF": "1"}
        run = subprocess.run([sys.executable, "-c", "from app.database import init_db; init_db(); print('INIT-OK')"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False)
        assert run.returncode == 0 and "INIT-OK" in run.stdout, (run.stdout + run.stderr)[-800:]
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"receipts", "companies", "primary_documents"} <= tables          # схема программы создана
            assert conn.execute("SELECT key_tail, status FROM moedelo_connections").fetchall() == [("abcd", "active")]
            assert conn.execute("SELECT document_id, md_id, sum_sent FROM moedelo_links").fetchall() == \
                [("d1", "777", 1200.5)]


class TestOpenFormatsKept:
    """Здоровая ткань: пакет для других учётных систем и его тексты — без названия сервиса, формат прежний."""

    def test_package_and_texts(self):
        from app.exportpkg.service import format_text
        fmt = format_text()
        assert "## Как загрузить" in fmt and "- **1С** — файлы УПД из папки `upd`" in fmt
        assert "- **Системы, которые принимают УПД в формате ФНС** — файлы из папки `upd`" in fmt
        assert "- **Другие системы** — JSON или CSV по описанию выше" in fmt
        from app.services.manual_content import build_manual
        manual = build_manual()
        assert manual["updated"] >= "2026-11-06"
        html = "".join(s.get("html", "") for s in manual["sections"])
        assert "их принимают 1С и другие учётные системы через ЭДО" in html
        assert 'title="JSON, CSV и УПД за месяц — для других учётных систем"' in read("app/static/js/clients.js")
        splash = read("app/static/js/splash.js")
        tips = splash.split("var TIPS = [", 1)[1].split("];", 1)[0]
        assert tips.count("'") == 14 and "'Пакет JSON, CSV и УПД за месяц — для других учётных систем'," in tips


class TestDocsAndKb:
    def test_adr(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-38](#adr-38) |" in arch and "## ADR-38" in arch
        assert "накладные с товарами — этап 2 (T-76) | заменено ADR-38 (заморожено владельцем 06.11.2026) |" in arch
        adr37 = arch.split("## ADR-37", 1)[1].split("## ADR-38", 1)[0]
        assert "**заменено [ADR-38](#adr-38)** (владелец 06.11.2026" in adr37
        adr = arch.split("## ADR-38", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in (C60, "**с версии / даты:** 1.70.108, 2026-11-06", "заменяет\n  [ADR-37](#adr-37)",
                       "d21dc99a245b755a8cbe6e6bccfdb8c4e6589b14", "побайтно", "снова 65 таблиц",
                       "с открытым кодом и бесплатными решениями, либо с не критичными лимитами", "T-71, T-76"):
            assert needle in adr, needle

    def test_database_and_guide(self):
        db = read("docs/DATABASE.md")
        assert "65 объявлений `__tablename__`" in db and "все 65 таблиц приложения" in db
        assert "| «Моё дело» по API (v1.70.107) |" not in db
        assert "- **Таблицы только выпуска 1.70.107.**" in db and "программа эти таблицы не\n  создаёт и не читает" in db
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.14." not in guide and not TRACE.search(guide)
        assert guide.index("### 7.13. Фото документов от сотрудников через Telegram и MAX (с v1.70.105)") \
            < guide.index("## 8. Маппинг реквизитов")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t71, t76 = tasks["T-71"], tasks["T-76"]
        assert t71["Статус"] == "blocked" and t71["Задача"].endswith(f"выпущено в 1.70.107 и убрано в {VERSION} "
                                                                    "(заморожено)")
        assert t71["Блокер"].startswith("Заморожено решением владельца 06.11.2026 (комментарий в #60)")
        assert t71["Источник"] == C60 and t71["Связанные ADR"] == "ADR-29, ADR-30, ADR-37, ADR-38"
        assert t76["Статус"] == "blocked" and t76["Связанные ADR"] == "ADR-37, ADR-38"
        assert t76["Блокер"] == "Заморожено решением владельца 06.11.2026 вместе с T-71 (комментарий в #60)"
        assert t71["Issue/PR"].endswith("/issues/56") and t76["Issue/PR"].endswith("/issues/60")
        t01 = tasks["T-01"]["Блокер"]
        m = re.search(r"Ставить сразу v([\d.]+)", t01)                         # v1.70.109: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert "без замороженного «Моего дела»" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        m = re.search(r"## Срез на (\d{4}-\d{2}-\d{2})", roadmap)
        assert m and m.group(1) >= "2026-11-06"
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.109: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert f"**Заморожено 06.11.2026 (выпуск v{VERSION}):** [#56](https://github.com/Rimlin-UNC/1c_chek/issues/56) " \
               "T-71 и [#60](https://github.com/Rimlin-UNC/1c_chek/issues/60) T-76" in roadmap
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert "[#26](https://github.com/Rimlin-UNC/1c_chek/issues/26) T-41" in agent and "T-76" not in agent
        rows = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        assert rows[VERSION]["Опора репетиции"] == "да"
        assert all("," not in v for v in rows[VERSION].values()), "в полях реестра нет запятых"
        assert rows["1.70.107"]["Откат на неё"] == f"не стоит: в ней замороженный модуль «Моё дело» (убран в {VERSION})"
        assert rows["1.70.106"]["Откат на неё"] == "можно: на ней рабочий сервер (отчёт владельца 06.11.2026)"
