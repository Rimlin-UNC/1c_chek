"""v1.70.106: страницы под кластеры запросов без дорвеев (T-75, ADR-36).

Поведение (рабочий адрес — canonical, копии — noindex, один H1, крошки микроданными, без скриптов и скрытого
текста, страницы не повторяют друг друга, ключ — один раз в title, карта сайта, ссылки, html-validate) —
tests/test_seo_pages.py. Здесь — синхронизация выпуска, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.106"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.106 — исторические проверки (точные пины — в tests/test_v170107.py)."""

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
        assert updater._is_newer(VERSION, "1.70.105") is True
        assert updater._is_newer("1.70.105", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Три страницы для поисковиков", "резервная копия", "поверх 1.70.95–1.70.105 не меняются — шаг "
                       "pip пропускается", "откатится само", "Схема базы не меняется", "48 из 48", "1.70.82–1.70.105",
                       "/kak-proverit-chek", "/cheki-sotrudnikov-v-1c", "/dlya-buhgalterov", "без JavaScript",
                       "микроданными", "только на chek.ymaster.ru", "5 адресов", "«Полезное»", "Без дорвеев",
                       "без цен", "T-71"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Три страницы для поисковиков", "Без дорвеев и санкций", "«Полезное»", "5 адресов"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — страницы для поисковиков: «Как проверить чек», «Чеки сотрудников в 1С», «Для бухгалтеров»"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("`/kak-proverit-chek`", "«Войти»", "«Ещё по теме»", "«📸 Проверить чек»", "до 900 px",
                       "строка «Полезное»", "USER_GUIDE §7.11", "12_seo.md, раздел 7", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Стартовая страница и поисковики (SEO)"]) >= self._ver(VERSION)
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestWiring:
    def test_router_before_spa_and_sitemap(self):
        main = read("app/main.py")
        assert "app.include_router(_seo_pages.router)" in main
        assert "app.include_router(_blog_pages.router)" in main
        assert main.index("app.include_router(_seo_pages.router)") < main.index('@app.api_route("/{full_path:path}"')
        assert main.index("app.include_router(_blog_pages.router)") < main.index('@app.api_route("/{full_path:path}"')
        from app.services import seo, seo_pages
        assert seo.CONTENT_PAGES == ("/kak-proverit-chek", "/cheki-sotrudnikov-v-1c", "/dlya-buhgalterov")
        assert seo.BLOG_INDEX == "/blog" and len(seo.BLOG_PAGES) == 10
        assert seo.PAGES == ("/", *seo.CONTENT_PAGES, seo.BLOG_INDEX, *seo.BLOG_PAGES)
        assert tuple(p.path for p in seo_pages.PAGES) == seo.CONTENT_PAGES
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 65                                    # v1.70.108: снова 65 — таблицы 1.70.107 убраны

    def test_start_page_links_and_styles(self):
        index = read("app/static/index.html")
        assert '<p class="land-more"><span>Полезное:</span>' in index
        assert index.count('href="/kak-proverit-chek"') == 2 and index.count('href="/dlya-buhgalterov"') == 2
        assert index.count('href="/cheki-sotrudnikov-v-1c"') == 2 and index.count("<h1") == 1
        css = read("app/static/css/app.css")
        assert ".land-more { display: flex; flex-wrap: wrap;" in css and ".land-more a {" in css
        assert css.count("@media (max-width: 520px)") == 1 and css.count("@font-face") == 7
        pages_css = read("app/static/css/pages.css")
        assert "@media (max-width: 900px)" in pages_css and "@media (max-width: 560px)" in pages_css
        assert "word-break: break-all;" in pages_css                             # строка QR без прокрутки
        src = read("app/services/seo_pages.py")
        assert "settings.APP_VERSION" in src and "<script" not in src             # без новой точки синхронизации


class TestDocsAndKb:
    def test_adr_guide_seo_kb(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-36](#adr-36) |" in arch and "## ADR-36" in arch
        assert "**с версии / даты:** 1.70.106,\n  2026-11-05" in arch
        assert "https://developers.google.com/search/docs/essentials/spam-policies" in arch.split("## ADR-36", 1)[1]
        guide = read("docs/USER_GUIDE.md")
        assert "- **Страницы для поисковиков (с 1.70.106).**" in guide
        assert "- **Блог (с T-82, без повышения версии).**" in guide
        assert "(карта: главная, оферта, три страницы для\n  поисковиков, индекс блога и десять статей)" in guide
        seo_md = read("docs/kb/12_seo.md")
        assert "## 7. Страницы под кластеры запросов (T-75, 1.70.106)" in seo_md
        assert "общий\n  словарь любой пары страниц — меньше 45 %" in seo_md
        assert "цены для\n  компаний на страницах не указаны — **требует уточнения**" in seo_md

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-75"]
        assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}")
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/59"
        assert row["Связанные ADR"] == "ADR-33, ADR-36" and row["Источник"].endswith("docs/kb/12_seo.md")
        assert row["Нужно подтверждение владельца"] == "тексты — по поручению владельца 05.11.2026; цены — требует уточнения"
        assert tasks["T-71"]["Статус"] in ("todo", "done", "blocked") and tasks["T-71"]["Issue/PR"].endswith("/issues/56")
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.107: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.106):** [#59](https://github.com/Rimlin-UNC/1c_chek/issues/59) T-75" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.107: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.105")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
