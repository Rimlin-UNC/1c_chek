"""v1.70.105: фото и PDF документов от сотрудников через ботов Telegram и MAX (T-64, ADR-35).

Поведение (привязанный сотрудник → очередь его компании → черновик у её бухгалтера, канал документа, отказы без
записи, группы молчат, суточный предел, getFile и мост Telegram, https и публичные адреса для ссылок MAX) —
tests/test_primary_bots.py; карточка «Входящие» и окно сверки с каналом — tests/js/primary_mail.test.js. Здесь —
синхронизация выпуска, схема, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.105"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.105 — исторические проверки (точные пины — в tests/test_v170106.py)."""

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
        assert updater._is_newer(VERSION, "1.70.104") is True
        assert updater._is_newer("1.70.104", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Фото документов от сотрудников", "резервная копия", "поверх 1.70.95–1.70.104 не меняются — шаг "
                       "pip пропускается", "откатится само", "2 новые колонки (channel и user_id) в primary_mail_items; "
                       "новых таблиц нет", "48 из 48", "1.70.82–1.70.104", "«📥 Входящие: почта и боты»",
                       "«✈️ Telegram»", "«💬 MAX»", "только личный чат", "до 20 МБ", "не больше 30 файлов",
                       "только по https", "одним PDF", "с подменёнными ботами", "T-75", "T-71"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Фото УПД прямо с объекта", "Только свои и без дублей", "«📥 Входящие: почта и боты»",
                       "не больше 30 файлов"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — фото документов от сотрудников через ботов: общая карточка «Входящие» и канал файла"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«📥 Входящие: почта и боты»", "«✈️ Telegram ·»", "«💬 MAX ·»", "«✈️ Из Telegram: имя, «подпись»»",
                       "«🔔 Telegram — личное»", "«🛡 MAX — личное»", "/help", "USER_GUIDE §7.13", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        new = "Фото документов из ботов"
        for name in ("Инструкция", "Настройки", "Первичка (электронные УПД)", "Первичка из почты", new):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != new}
        assert {"name": new, "from": "", "to": reg[new]} in updater.diff_blocks(before, reg)

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_two_columns_no_new_tables(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 65                                    # v1.70.108: снова 65 — таблицы 1.70.107 убраны
        from app.primary.models import PrimaryMailItem
        cols = PrimaryMailItem.__table__.columns
        assert cols["channel"].server_default is not None and cols["user_id"].server_default is not None
        assert cols["channel"].default.arg == "mail" and cols["user_id"].default.arg == ""
        db_py = read("app/database.py")
        assert "ALTER TABLE primary_mail_items ADD COLUMN channel VARCHAR(10) DEFAULT 'mail'" in db_py
        assert "ALTER TABLE primary_mail_items ADD COLUMN user_id VARCHAR(36) DEFAULT ''" in db_py
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"primary_mail_columns": sorted(c["name"] for c in inspect(engine).get_columns("primary_mail_items")' \
            in rehearsal
        assert 'and up.get("primary_mail_columns") == ["channel", "user_id"]' in rehearsal
        db = read("docs/DATABASE.md")
        assert re.search(r"\d+ объявлений `__tablename__`", db) and re.search(r"все \d+ таблиц(?:ы)? приложения", db)
        assert "С 1.70.105 — фото от сотрудников через ботов (ADR-35): в `primary_mail_items` колонки `channel`" in db


class TestWiring:
    def test_bots_hooks_and_ui(self):
        tg = read("app/services/telegram_bot.py")
        assert '_document_message(db, msg, chat_id)                 # v1.70.105 (T-64)' in tg
        assert 'and chat.get("type", "private") == "private":' in tg
        assert '"/file/bot" + token + "/" + path' in tg
        assert "Фото пока не разбираю" not in tg                 # недостижимая ветка убрана, подсказка — в новой
        mx = read("app/services/max_bot.py")
        assert '_document_message(db, msg, user_id, files)          # v1.70.105 (T-64)' in mx
        assert '(msg.get("recipient") or {}).get("chat_type", "dialog") == "dialog"' in mx
        assert "MAX_FILES_PER_MESSAGE = 10" in mx
        primary = read("app/static/js/primary.js")
        assert "import { CHANNEL_FROM, mailConnectPanel, mailInboxHtml } from './primary_mail.js';" in primary
        assert "const mailLabel = CHANNEL_FROM[rec.channel] || CHANNEL_FROM.mail;" in primary
        assert "${esc(opts.mailLabel || '📬 Из почты')}: ${esc(opts.mail)}" in primary
        mail_js = read("app/static/js/primary_mail.js")
        assert "export const CHANNEL_MARK = { telegram: '✈️ Telegram', max: '💬 MAX' };" in mail_js
        assert '<div class="card-title mail-title">📥 Входящие: почта и боты <span class="spacer"></span>' in mail_js
        css = read("app/static/css/app.css")
        assert ".mail-title { flex-wrap: wrap; }" in css and ".mail-title .btn { white-space: nowrap; }" in css
        app_js = read("app/static/js/app.js")
        assert app_js.count("📄 Фото УПД или накладной отправляйте боту — бухгалтер компании получит черновик на "
                            "сверку.") == 2

    def test_rules_catalog_and_manual(self):
        from app.primary import bots, mailbox
        assert bots.MAX_BYTES == 20 * 1024 * 1024 and bots.DAILY_LIMIT == 30 and bots.MAX_REDIRECTS == 3
        assert bots.BOT_CHANNELS == ("telegram", "max") and mailbox.MIN_IMAGE_BYTES == 15_000
        src = read("app/primary/mailbox.py")
        assert 'res = service.confirm(db, user, payload, channel=item.channel or "mail")' in src
        assert '"channel": item.channel or "mail",' in src
        from app.services import server_catalog
        flows = {f["id"]: f for f in server_catalog.DATA_FLOWS}
        assert "фото и PDF документов сотрудников скачивает для «Первички» (getFile" in flows["telegram"]["sends"]
        assert "только https и только публичные адреса" in flows["max"]["protection"]
        assert len(server_catalog.DATA_FLOWS) == 12                              # новых потоков нет
        from app.services import manual_content
        primary = next(s for s in manual_content.SECTIONS if s["id"] == "primary")
        assert "С версии 1.70.105 — фото документов от сотрудников." in primary["html"]
        assert "С версии 1.70.104 — документы из почты." in primary["html"]
        assert (ROOT / "tests/test_primary_bots.py").exists()


class TestDocsAndKb:
    def test_adr_guide_design(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-35](#adr-35) |" in arch and "## ADR-35" in arch
        assert "**с версии / даты:** 1.70.105, 2026-11-05" in arch
        assert "https://core.telegram.org/bots/api#getfile" in arch
        assert "https://dev.max.ru/docs-api/use-cases/sending-messages/attachment-types" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.13. Фото документов от сотрудников через Telegram и MAX (с v1.70.105)" in guide
        assert "### 7.12. Первичка из почты (с v1.70.104) — бухгалтер/админ" in guide
        assert "| 6г | **Сделано в 1.70.105:**" in read("docs/primary_docs_design.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-64"]
        assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}")
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/49" and "ADR-35" in row["Связанные ADR"]
        assert row["Источник"].endswith("docs/primary_docs_design.md")
        for tid, issue in (("T-71", 56), ("T-75", 59)):                  # v1.70.106: T-75 выпущена
            assert tasks[tid]["Статус"] in ("todo", "done", "blocked") and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.106: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.105):** [#49](https://github.com/Rimlin-UNC/1c_chek/issues/49) T-64" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.106: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.104")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
