"""v1.70.83: экран «Разбор конфликтов» единого реестра (#9) — выпуск.

Поведение экрана и API проверяет tests/test_unified_review.py; здесь —
синхронизация выпуска и тексты для пользователя.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.83"


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
        assert f"ymaster-check-v{VERSION}" in sw
        assert sw.count(f"?v={VERSION}") == 2
        assert read("app/static/index.html").count(f"?v={VERSION}") == 2

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-02"
        entry = md.split("\n## [1.70.82]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in ("Разбор конфликтов", "резервная копия", "Откат", "1.70.80", "1.70.82",
                       "Схема базы не менялась", "pip пропускается"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert "Разбор конфликтов" in block and "Последних рабочих версиях" in block

    def test_journal_entry_first(self):
        assert read("docs/ui_journal.md").startswith(f"## v{VERSION} — ")

    def test_only_unified_block_changed(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == ["Единый реестр чеков"]
        # версия блока не может опережать версию программы
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 83) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (02.11.2026)" in readme
        assert "Последний выпуск — v1.70.82" not in readme


class TestUserFacingTexts:
    def test_manual_has_admin_section(self):
        from app.services.manual_content import SECTIONS
        ids = [s["id"] for s in SECTIONS]
        assert len(ids) == len(set(ids)), "id разделов инструкции не уникальны"
        section = next(s for s in SECTIONS if s["id"] == "conflicts")
        assert section["audience"] == "admin"
        assert "Разбор конфликтов" in section["html"] and "причина обязательна" in section["html"]

    def test_docs_describe_the_screen(self):
        assert "### Разбор конфликтов (админ, v1.70.83)" in read("docs/USER_GUIDE.md")
        assert "## Экран «Разбор конфликтов» (#9, выпуск 1.70.83)" in read("docs/unified_receipts.md")
        assert "/ b2badmin / conflicts |" in read("docs/rbac_matrix.md")
