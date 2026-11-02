"""v1.70.84: «Единая база», фаза 0 (#24) — выпуск.

Поведение пакетного перемещения (T-33) и журнала карточки без чужих
организаций (T-32) проверяет tests/test_unified_phase0.py; обновление и откат —
репетиция qa/upgrade_rehearsal.py (фазы BULK и OLD_AFTER_BULK). Здесь —
синхронизация выпуска и тексты для пользователя.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.84"


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
        assert read("app/static/b2b/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-02"
        entry = md.split("\n## [1.70.83]", 1)[0]
        # окно обновления показывает первые 4000 символов — запись видна целиком
        assert len(entry) <= 4000, len(entry)
        for needle in (
            # как пройдёт обновление
            "резервная копия", "Откат", "1.70.80", "1.70.82", "1.70.83",
            "Схема базы не менялась", "pip пропускается", "37 из 37",
            # T-33: пакетное перемещение и цифры замера
            "Переместить в компанию", "1000 чеков", "4,9 с", "9,6 с", "13 000",
            # T-32: сведения единого реестра без чужих организаций
            "бухгалтера и руководителя", "Администратор видит всё как раньше",
            # исправления: подотчётное лицо в месячной сумме, чистая установка на PostgreSQL
            "месячного лимита", "PostgreSQL",
        ):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Переместить в компанию", "1000 чеков", "месячного лимита",
                       "без чужих организаций", "Последних рабочих версиях"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — ")
        entry = journal.split("\n## v1.70.83", 1)[0]
        assert "Экраны не менялись" in entry and "Чек требует разбора перед перемещением" in entry

    def test_only_unified_block_changed(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(name for name, v in reg.items() if v == VERSION) == ["Единый реестр чеков"]
        # версия блока не может опережать версию программы
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 84) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (02.11.2026)" in readme
        assert "Последний выпуск — v1.70.83" not in readme


class TestRehearsal:
    def test_bulk_phases_are_wired(self):
        """Репетиция выпуска проверяет пакетное перемещение и откат после него."""
        from qa import upgrade_rehearsal as reh
        assert "/api/v1/receipts/move" in reh.BULK and "range(1000)" in reh.BULK
        assert "/api/v1/unified/documents/" in reh.BULK          # журнал карточки (T-32)
        assert "REH_BULK" in reh.OLD_AFTER_BULK
        src = read("qa/upgrade_rehearsal.py")
        assert 'run_phase("пакетный перенос", BULK' in src
        assert 'run_phase("откат после пакетного переноса", OLD_AFTER_BULK' in src

    def test_live_update_moves_a_batch(self):
        """Этап Б после push: пакетное перемещение на настоящем обновлённом сервере."""
        src = read("qa/live_update.py")
        assert '"/api/v1/receipts/move"' in src and '"created_by_hidden"' in src
        assert "fingerprint(db) == base_fp" in src.split("v1.70.84 (T-33, T-32)", 1)[1]


class TestUserFacingTexts:
    def test_manual_has_move_section(self):
        from app.services.manual_content import SECTIONS
        ids = [s["id"] for s in SECTIONS]
        assert len(ids) == len(set(ids)), "id разделов инструкции не уникальны"
        section = next(s for s in SECTIONS if s["id"] == "move")
        assert section["audience"] == "admin"
        for needle in ("Переместить в компанию", "1.70.84", "1000 чеков", "Разборе конфликтов",
                       "лимит месяца"):
            assert needle in section["html"], needle
        assert ids.index("move") < ids.index("conflicts")

    def test_texts_match_the_program(self):
        # подпись кнопки в реестре и текст ошибки — те же, что в справке и журнале
        assert "🏢 Переместить в компанию</button>" in read("app/static/js/app.js")
        assert "Чек требует разбора перед перемещением" in read("app/unified/service.py")
        assert "max_length=1000" in read("app/schemas.py").split("class ReceiptMoveBody", 1)[1][:300]

    def test_docs_describe_the_release(self):
        assert "### Перемещение чеков между компаниями (админ, v1.70.84)" in read("docs/USER_GUIDE.md")
        assert "Выпущено в 1.70.84" in read("docs/single_base_design.md")
