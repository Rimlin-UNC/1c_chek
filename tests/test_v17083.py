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
    """Выпуск 1.70.83 — исторические проверки.

    v1.70.84: точные пины перенесены в tests/test_v17084.py. Здесь остаётся то,
    что верно и после следующих выпусков: версия только растёт, запись о
    выпуске на месте и видна целиком, точки синхронизации совпадают с текущей
    версией.
    """

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert current in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw
        assert sw.count(f"?v={current}") == 5
        assert read("app/static/index.html").count(f"?v={current}") == 3

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert "WIP" not in md.split("\n", 1)[0]
        head = f"## [{VERSION}] - 2026-11-02"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("Разбор конфликтов", "резервная копия", "Откат", "1.70.80", "1.70.82",
                       "Схема базы не менялась", "pip пропускается"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert "Разбор конфликтов" in block and "Последних рабочих версиях" in block

    def test_journal_entry(self):
        assert f"## v{VERSION} — " in read("docs/ui_journal.md")

    def test_unified_block_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Единый реестр чеков"]) >= self._ver(VERSION)
        # версия блока не может опережать версию программы
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


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
