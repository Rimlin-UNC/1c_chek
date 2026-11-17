"""Замки выпуска 1.73.0: приёмная папка первички (T-109, #109, ADR-60).

Поведение — tests/test_primary_inbox.py (синтетика, локальная папка), экран — tests/js/primary_inbox.test.js.
Здесь: синхронизация версии и ресурсов, реестр блоков, текст окна обновления, документы, маршруты, схема,
отсутствие пароля СБИС в базе и на сервере, а также то, что прежний путь «Папка с УПД» не сломан.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.0"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/109"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in value.split("."))


# ====================================================================== выпуск
class TestRelease:
    def test_config_version(self):
        from app.config import settings
        assert settings.APP_VERSION == VERSION

    def test_static_versions_synced(self):
        ver = read("app/config.py").split('APP_VERSION: str = "', 1)[1].split('"', 1)[0]
        sw = read("app/static/sw.js")
        idx = read("app/static/index.html")
        assert f"ymaster-check-v{ver}" in sw
        assert sw.count(f"?v={ver}") == 5
        assert idx.count(f"?v={ver}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={ver}") == 2
        assert f"/maxapp/maxapp.js?v={ver}" in read("app/static/maxapp/index.html")
        assert f'"version": "{ver}"' in read("app/static/manifest.webmanifest")
        assert f"server_monitor.js?v={ver}" in read("app/static/js/osm.js")

    def test_changelog_entry_opens_with_this_version(self):
        ch = read("CHANGELOG.md")
        assert ch.count(f"## [{VERSION}]") == 1
        assert ch.index(f"## [{VERSION}]") < ch.index("## [1.72.38]")
        entry = ch.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)                  # окно обновления показывает первые 4000
        for needle in ("T-109", "#109", "ADR-60", "СБИС", "опрос", "inotify", "SHA-256", "quarantine",
                       "processed", "YM_PRIMARY_INBOX_ROOTS", "primary_inbox_settings", "primary_inbox_files",
                       "в прод не устанавливать", "Приёмка на рабочей ВМ", "Обмен с контрагентами"):
            assert needle in entry, needle

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert reg["Приёмная папка первички"] == VERSION
        assert _ver(reg["Первичка (электронные УПД)"]) >= _ver(VERSION)
        assert _ver(reg["Настройки"]) >= _ver(VERSION)
        assert all(_ver(v) <= _ver(VERSION) for v in reg.values())

    def test_whats_new_present(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Приёмная папка", "СБИС", "по ИНН", "processed", "карантин", "SHA-256"):
            assert needle in block, needle
        assert js.index(f"'{VERSION}':") < js.index("'1.72.38':")   # новая выше прежней

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} —"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("📥 Приёмная папка", "processed", "quarantine", "журнал"):
            assert needle in entry, needle

    def test_readme_latest_release(self):
        readme = read("README.md")
        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", readme)
        assert match and _ver(match.group(1)) >= _ver(VERSION)

    def test_release_ledger_row(self):
        rows = read("docs/kb/09_releases.csv").strip().splitlines()
        last = rows[-1].split(",")
        assert last[0] == VERSION and last[1] == "2026-11-16"

    def test_release_ledger_row_has_no_commas_inside_fields(self):
        """Замок прошлых выпусков: в полях реестра нет запятых — строка читается и без csv.

        Проверка живёт здесь, потому что ловила реальную ошибку: свободный текст с запятыми
        превращал последнюю строку в набор лишних колонок ещё до CI.
        """
        import csv as _csv
        lines = read("docs/kb/09_releases.csv").strip().splitlines()
        header = next(_csv.reader([lines[0]]))
        last = next(_csv.reader([lines[-1]]))
        assert len(last) == len(header), f"колонок {len(last)} вместо {len(header)}"
        assert not [value for value in last if "," in value], "в полях реестра нет запятых"
        assert last[0] == VERSION
        assert last[2].startswith("требует уточнения"), last[2][:40]   # хеш уточняется после CI


# ====================================================================== код и маршруты
class TestCode:
    def test_routes_registered_before_document_card(self):
        source = read("app/main.py")
        inbox_at = source.index("primary_inbox_router.router")
        primary_at = source.index("app.include_router(primary_router.router)")
        assert inbox_at < primary_at, "маршрут /inbox/log должен идти раньше карточки /{doc_id}"

    def test_api_contract(self):
        from app.main import app
        paths = set(app.openapi()["paths"])
        for path in ("/api/v1/primary/inbox/state", "/api/v1/primary/inbox/settings",
                     "/api/v1/primary/inbox/run", "/api/v1/primary/inbox/log"):
            assert path in paths, path

    def test_only_admin_may_change_settings(self):
        from app.primary import inbox_router
        source = Path(inbox_router.__file__).read_text(encoding="utf-8")
        block = source.split('@router.put("/inbox/settings")', 1)[1].split("@router", 1)[0]
        assert "Admin" in block and "require_admin" in source
        logs = source.split('@router.get("/inbox/log")', 1)[1].split("@router", 1)[0]
        assert "Accountant" in logs                             # журнал читает бухгалтер своей компании

    def test_no_sbis_password_is_stored(self):
        """Пароль и ключи СБИС в базу не попадают: приём — папкой, а не логином к чужой системе."""
        models = read("app/primary/models.py")
        block = models.split("class PrimaryInboxSetting(Base):", 1)[1].split("class ", 1)[0]
        for word in ("password", "login", "token", "secret"):
            assert word not in block.lower(), word
        assert "Паролей здесь нет" in block

    def test_inbox_channel_used_for_document(self):
        source = read("app/primary/inbox.py")
        assert 'channel="inbox"' in source
        assert "accept_routed" in source                        # тот же разбор, что у «Папки с УПД»

    def test_path_guard_is_present(self):
        source = read("app/primary/inbox.py")
        for needle in ("YM_PRIMARY_INBOX_ROOTS", "def check_path", "403", "is_symlink"):
            assert needle in source, needle

    def test_polling_not_filesystem_events(self):
        """На сетевом диске события не приходят — обход обязан быть опросом по таймеру."""
        source = read("app/primary/inbox.py")
        assert "inotify" not in source.split('"""', 2)[2]       # только в объяснении, не в коде
        assert "def due(" in source and "def tick(" in source
        main = read("app/main.py")
        assert "primary_inbox.tick(db)" in main

    def test_background_available_to_tests_off(self):
        assert "YM_PRIMARY_INBOX_OFF" in read("app/primary/inbox.py")

    def test_previous_folder_import_untouched(self):
        """Старый путь «📁 Папка с УПД» остаётся и вызывает тот же сервис без явного списка компаний."""
        js = read("app/static/js/primary_folder.js")
        assert "primFolderUpload" in js
        service = read("app/primary/service.py")
        assert "def accept_routed(db: Session, user: User, data: bytes, file_name: str, channel: str = \"\"," in service
        assert "allowed: list[str] | None = None" in service

    def test_table_names_and_indexes(self):
        from app.database import Base
        from app.primary import models as _models  # noqa: F401
        for table in ("primary_inbox_settings", "primary_inbox_files"):
            assert table in Base.metadata.tables, table
        indexes = {i.name for i in Base.metadata.tables["primary_inbox_files"].indexes}
        assert "ix_primary_inbox_files_status" in indexes


# ====================================================================== схема и репетиция
class TestSchema:
    def test_no_new_columns_on_old_tables(self):
        """Только добавление таблиц: у прежних таблиц первички новых колонок нет (ADR-03, ADR-09)."""
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert 'up.get("primary_columns") == ["channel", "print_json", "source"]' in rehearsal
        assert "primary_inbox_tables" in rehearsal
        assert '["primary_inbox_files", "primary_inbox_settings"]' in rehearsal

    def test_database_doc_updated(self):
        doc = read("docs/DATABASE.md")
        assert "77 объявлений" in doc
        assert "Приёмная папка первички (v1.73.0)" in doc
        assert "primary_inbox_settings" in doc and "primary_inbox_files" in doc


# ====================================================================== документы
class TestDocs:
    def test_architecture_decision(self):
        doc = read("docs/ARCHITECTURE.md")
        assert "## ADR-60" in doc
        assert "[ADR-60](#adr-60)" in doc
        block = doc.split("## ADR-60", 1)[1].split("\n## ADR-", 1)[0]
        for needle in ("приёмная папка", "опрос", "inotify", "СБИС", ISSUE, "T-109", "1.73.0"):
            assert needle.lower() in block.lower(), needle

    def test_design_document(self):
        doc = read("docs/primary_docs_design.md")
        assert "Приёмная папка" in doc
        for needle in ("processed", "quarantine", "YM_PRIMARY_INBOX_ROOTS", "inotify"):
            assert needle in doc, needle

    def test_rbac_matrix(self):
        doc = read("docs/rbac_matrix.md")
        assert any("приёмная папка" in line.lower() for line in doc.splitlines())
        block = [line for line in doc.splitlines() if "приёмная папка" in line.lower()]
        assert any("✓ | ✓ | — | — | —" in line for line in block), block

    def test_runbook_and_env(self):
        runbook = read("docs/RUNBOOK.md")
        assert "Приёмная папка первички" in runbook
        for needle in ("processed", "quarantine", "YM_PRIMARY_INBOX_ROOTS"):
            assert needle in runbook, needle
        env = read(".env.example")
        for key in ("YM_PRIMARY_INBOX_ROOTS", "YM_PRIMARY_INBOX_PATH", "YM_PRIMARY_INBOX_OFF"):
            assert key in env, key

    def test_manual(self):
        from app.services.manual_content import SECTIONS
        by_id = {s["id"]: s for s in SECTIONS}
        assert "С версии 1.73.0 — «📥 Приёмная папка" in by_id["primary"]["html"]

    def test_readme_mentions_feature(self):
        readme = read("README.md")
        assert "Приёмная папка" in readme
        assert "СБИС" in readme


# ====================================================================== прежние замки
class TestPreviousLocks:
    def test_updater_sees_this_version_newer(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.72.38") is True
        assert updater._is_newer("1.72.38", VERSION) is False
