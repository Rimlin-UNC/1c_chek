"""v1.70.104: первичка из почтового ящика компании (T-63, ADR-34).

Поведение (только чтение ящика, XML и ZIP — в реестр, PDF и сканы — черновики на сверку, дубли, фильтр отправителей,
UIDVALIDITY, пределы, ошибки входа, изоляция компаний, срок хранения файла, защита от SSRF) — tests/test_primary_mail.py;
панель ящика, карточка «Из почты» и сверка черновика — tests/js/primary_mail.test.js. Здесь — синхронизация выпуска,
схема, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.104"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.104 — исторические проверки (точные пины — в tests/test_v170105.py)."""

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
        assert updater._is_newer(VERSION, "1.70.103") is True
        assert updater._is_newer("1.70.103", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Первичка из почты", "резервная копия", "поверх 1.70.95–1.70.103 не меняются — шаг pip "
                       "пропускается", "откатится само", "3 новые таблицы (primary_mailboxes, primary_mail_items, "
                       "primary_mail_files) и 1 новая колонка channel в primary_documents", "48 из 48",
                       "1.70.82–1.70.103", "«⚙ Почтовый ящик»", "Раз в 15 минут", "«Ждут сверки»", "«Не нужен»",
                       "черновиком не становится", "узнаётся по хешу", "«не УПД»", "только на чтение",
                       "не дольше 30 дней", "поддельным IMAP-сервером", "T-64", "T-71", "T-75"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("УПД из почты — сами в реестр", "Только чтение, без дублей", "«⚙ Почтовый ящик»",
                       "не дольше 30 дней"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — первичка из почты: ящик компании, черновики на сверку и журнал вложений"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«📬 Почта для документов»", "«Подключить и проверить»", "«Сохранить и проверить»",
                       "«⬇ Загрузить сейчас»", "«📬 Из почты»", "«Ждут сверки: N»", "«Сверить»", "«Не нужен»",
                       "«📄 Открыть", "«Последние вложения»", "USER_GUIDE §7.12", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        new = "Первичка из почты"
        for name in ("Инструкция", "Первичка (электронные УПД)", new):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != new}
        assert {"name": new, "from": "", "to": reg[new]} in updater.diff_blocks(before, reg)

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_three_tables_and_channel_column(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 65                                    # v1.70.108: снова 65 — таблицы 1.70.107 убраны
        from app.primary.models import (
            PrimaryDocument,
            PrimaryMailbox,
            PrimaryMailFile,
            PrimaryMailItem,
        )
        for model in (PrimaryMailbox, PrimaryMailItem, PrimaryMailFile):
            assert not model.__table__.foreign_keys, model.__tablename__                     # ADR-09
        assert {c.name for c in PrimaryMailbox.__table__.columns} == {
            "id", "company_id", "host", "port", "security", "username", "password_enc", "folder", "senders",
            "start_date", "status", "uidvalidity", "last_uid", "last_check_at", "last_ok_at", "last_error",
            "last_result_json", "created_by", "created_at", "updated_by", "updated_at"}
        assert any(getattr(c, "name", "") == "uq_primary_mailboxes_company" for c in PrimaryMailbox.__table__.constraints)
        assert {"result", "file_sha256", "draft_json", "document_id", "sender"} <= {
            c.name for c in PrimaryMailItem.__table__.columns}
        assert {c.name for c in PrimaryMailFile.__table__.columns} == {"item_id", "media_type", "data", "created_at"}
        channel = PrimaryDocument.__table__.columns["channel"]
        assert channel.server_default is not None                                            # старая версия пишет без неё
        assert "ALTER TABLE primary_documents ADD COLUMN channel VARCHAR(10) DEFAULT ''" in read("app/database.py")
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"primary_columns": sorted(c["name"] for c in inspect(engine).get_columns("primary_documents")' \
            in rehearsal
        assert 'and up.get("primary_columns") == ["channel", "source"]' in rehearsal
        assert 'and up.get("primary_mail_tables") == ["primary_mail_files", "primary_mail_items", "primary_mailboxes"]' \
            in rehearsal
        assert 'PRIMARY_MAIL_TABLES = ["primary_mail_files", "primary_mail_items", "primary_mailboxes"]' \
            in read("qa/migration_e2e.py")
        db = read("docs/DATABASE.md")
        assert re.search(r"\d+ объявлений `__tablename__`", db) and re.search(r"все \d+ таблиц приложения", db)
        assert "| Первичка из почты (v1.70.104) |" in db and "`password_enc` (secretbox)" in db


class TestWiring:
    def test_router_order_tick_and_audit(self):
        main = read("app/main.py")
        assert main.index("app.include_router(primary_mail_router.router)") < \
            main.index("app.include_router(primary_router.router)")              # иначе /inbox поймал бы /{doc_id}
        tick = main.split("def _doc_requests_once() -> None:", 1)[1].split("async def _doc_requests_loop", 1)[0]
        assert "primary_mailbox.check_due(db)" in tick and 'log.exception("primary mailbox tick failed")' in tick
        app_js = read("app/static/js/app.js")
        assert "import { mailConnectPanel } from './primary_mail.js';" in app_js
        assert '<div class="info-callout" id="cc-mail"></div>' in app_js
        assert "mailConnectPanel(slot.querySelector('#cc-mail'), c.id);" in app_js
        assert "primary_mailbox_saved: ['info', '📬']" in app_js and "primary_mail_checked: ['ok', '📬']" in app_js
        primary = read("app/static/js/primary.js")
        assert "mailConnectPanel, mailInboxHtml } from './primary_mail.js';" in primary   # v1.70.105: + CHANNEL_FROM
        assert '<div class="glass card" id="prim-mail">' in primary and "loadMail();" in primary
        assert "confirmUrl: `${base}/confirm`" in primary
        assert '"goods.js",\n           "primary_mail.js", "api.js", "bank.js", "primary.js",' in read("tests/test_v1121.py")
        css = read("app/static/css/app.css")
        assert ".mail-draft {" in css and ".mail-draft-tools .btn { white-space: nowrap; }" in css
        from app.services.audit_human import humanize_audit
        assert humanize_audit("primary_mailbox_removed", {"company": "Ромашка"}) == (
            "почтовый ящик для первички отключён", "компания: „Ромашка“")

    def test_safety_constants_and_catalog(self):
        from app.primary import mailbox
        assert (mailbox.CHECK_EVERY.total_seconds(), mailbox.MAX_MESSAGES, mailbox.DRAFT_DAYS) == (900, 30, 30)
        assert mailbox.PORTS == (993, 143) and mailbox.MAX_PER_TICK == 3
        src = read("app/primary/mailbox.py")
        assert "readonly=True" in src and "(BODY.PEEK[])" in src
        for forbidden in ("STORE", "EXPUNGE", "MOVE", "COPY", "\\\\Deleted"):
            assert f'"{forbidden}"' not in src, forbidden                 # ящик не меняется
        from app.services import server_catalog
        flow = next(f for f in server_catalog.DATA_FLOWS if f["id"] == "mail_in")
        assert flow["log"] == "" and "только чтение" in flow["service"] and flow["source"] == "app/primary/mailbox.py"
        assert "YM_MAIL_PRIVATE_HOSTS" in read(".env.example")
        from app.services import manual_content
        primary = next(s for s in manual_content.SECTIONS if s["id"] == "primary")
        assert "С версии 1.70.104 — документы из почты." in primary["html"]
        assert (ROOT / "tests/test_primary_mail.py").exists() and (ROOT / "tests/js/primary_mail.test.js").exists()


class TestDocsAndKb:
    def test_adr_guide_design(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-34](#adr-34) |" in arch and "## ADR-34" in arch
        assert "**с версии / даты:** 1.70.104, 2026-11-05" in arch
        assert "https://docs.paperless-ngx.com/usage/#usage-email" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.12. Первичка из почты (с v1.70.104) — бухгалтер/админ" in guide
        assert "`YM_MAIL_PRIVATE_HOSTS=1`" in guide
        assert "| 6в | **Сделано в 1.70.104:**" in read("docs/primary_docs_design.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-63"]
        assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}")
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/48" and "ADR-34" in row["Связанные ADR"]
        assert row["Источник"].endswith("docs/primary_docs_design.md")
        for tid, issue in (("T-64", 49), ("T-71", 56), ("T-75", 59)):     # v1.70.105: T-64 выпущена
            assert tasks[tid]["Статус"] in ("todo", "done", "blocked") and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.105: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.104):** [#48](https://github.com/Rimlin-UNC/1c_chek/issues/48) T-63" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.105: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.103")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
