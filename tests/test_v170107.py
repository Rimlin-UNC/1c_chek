"""v1.70.107: «Моё дело» по API — этап 1 (T-71, ADR-37); в 1.70.108 модуль убран — проект заморожен (ADR-38).

Здесь — только история выпуска: запись CHANGELOG, журнал интерфейса, реестр, ADR и база знаний. Отсутствие модуля,
маршрутов, таблиц и упоминаний в программе проверяет tests/test_v170108.py.
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
    """Выпуск 1.70.107 — исторические проверки (точные пины — в tests/test_v170108.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 5
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_order(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.70.106") is True
        assert updater._is_newer("1.70.106", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("«Моё дело» по API", "резервная копия", "поверх 1.70.95–1.70.106 не меняются — шаг pip "
                       "пропускается", "откатится само", "2 новые таблицы (moedelo_connections и moedelo_links)",
                       "новые таблицы просто не видит", "48 из 48", "1.70.82–1.70.106", "T-76", "«Проверить ключ»"):
            assert needle in entry, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = (f"## v{VERSION} — «Моё дело»: ключ в карточке компании, кнопка в карточке документа "
                "и пакет на экране «Первичка»")
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«Проверить ключ»", "рядом с «⬇ В 1С»", "USER_GUIDE §7.14", "ADR-37", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Первичка (электронные УПД)"]) >= self._ver(VERSION)
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestDocsAndKb:
    def test_adr_kept_as_history(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-37](#adr-37) |" in arch and "## ADR-37" in arch       # разделы ADR не удаляются
        assert "**с версии / даты:** 1.70.107, 2026-11-05" in arch
        adr = arch.split("## ADR-37", 1)[1].split("## ADR-38", 1)[0]
        for needle in ("`PurchasesActModel`", "`StockProductId`", "«Без ключа, проверю у себя»",
                       "https://restapi.moedelo.org/docs", "#60"):
            assert needle in adr, needle

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-71"]
        assert row["Статус"] in ("done", "blocked") and f"выпущено в {VERSION}" in row["Задача"]
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/56"
        assert tasks["T-76"]["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/60"
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.108: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.107):** [#56](https://github.com/Rimlin-UNC/1c_chek/issues/56) T-71" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.108: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.106")]["Откат на неё"].startswith("можно")
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
