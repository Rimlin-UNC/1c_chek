"""v1.70.103: SEO стартовой страницы и проверка вёрстки (T-74, ADR-33).

Поведение: robots.txt, sitemap.xml, 404 вместо копии главной, подстановка canonical, noindex и кодов подтверждения
по адресу сайта, настройки «Поисковики» и флаг заставки для гостей (API и журнал), разметка стартовой страницы
(один H1, title и description, Open Graph, микроданные), свои шрифты, вёрстка кнопок модалки на телефоне. Решения до
первой отрисовки (js/boot.js) — tests/js/boot.test.js, сценарий гостя — Playwright (tests/e2e/app.spec.js). Здесь же —
синхронизация выпуска, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from app.services import seo
from tests.conftest import login

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.103"
PROD = "chek.ymaster.ru"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.103 — исторические проверки (точные пины — в tests/test_v170104.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 5
        assert "'/js/boot.js'," in sw and "'/fonts/inter-cyrillic-wght-normal.woff2'" in sw
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_order(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.70.102") is True
        assert updater._is_newer("1.70.102", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("«проверка чека»", "резервная копия", "поверх 1.70.95–1.70.102 не меняются — шаг pip "
                       "пропускается", "откатится само", "Схема базы не меняется", "48 из 48", "1.70.82–1.70.102",
                       "/robots.txt и /sitemap.xml", "отвечает «404»", "только на рабочем адресе chek.ymaster.ru",
                       "«Настройки → 🔎 Поисковики»", "Яндекс не читает JSON-LD", "без заставки", "лицензия OFL",
                       "не дробятся по буквам", "T-75", "T-63", "T-64", "T-71"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Стартовую страницу находят по «проверке чека»", "Гостю — сразу содержимое, а не заставка",
                       "«Настройки → Поисковики»", "флажок в «Заставке загрузки»"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — стартовая страница для поисковиков, гостю без заставки, свои шрифты и проверка вёрстки"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«Проверка чека по QR-коду — баллы и кэшбэк за каждый чек»", "«Как проверить чек по QR-коду?»",
                       "«Показывать заставку и гостям на стартовой странице»", "«🔎 Поисковики»",
                       "«Авто — только chek.ymaster.ru»", "«Закрыт / ь»", "USER_GUIDE §7.11",
                       "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        new = "Стартовая страница и поисковики (SEO)"
        for name in ("Адаптивный интерфейс", "Заставка загрузки", "Настройки", new):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != new}
        assert {"name": new, "from": "", "to": reg[new]} in updater.diff_blocks(before, reg)     # v1.70.106: блок новее

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSeoService:
    """Чистые функции app/services/seo.py — без базы: настройки передаются словарём."""

    def test_host_cleaning(self):
        assert seo.clean_host("Chek.Ymaster.RU.") == PROD
        assert seo.clean_host("localhost:8000") == "localhost:8000" and seo.hostname("localhost:8000") == "localhost"
        for bad in ('evil"><script>.com', "a b", "", None, "[::1]:8000", "x" * 70 + ".ru"):
            assert seo.clean_host(bad) == "", bad

    def test_indexing_by_address_not_by_robot(self):
        auto = dict(seo.DEFAULTS)
        assert seo.indexing_enabled(auto, PROD) and seo.indexing_enabled(auto, f"{PROD}:443")
        assert not seo.indexing_enabled(auto, "localhost:8000") and not seo.indexing_enabled(auto, "chek.example.ru")
        assert seo.indexing_enabled({"mode": "on"}, "chek.example.ru")
        assert not seo.indexing_enabled({"mode": "on"}, 'bad"host')
        assert not seo.indexing_enabled({"mode": "off"}, PROD)
        src = read("app/services/seo.py")
        assert "user-agent" not in src.lower().replace("user-agent: *", "")       # решения по роботу нет

    def test_head_tags(self):
        cfg = {"mode": "auto", "yandex": "abc123def", "google": 'g-0_1"x'}
        tags = seo.head_tags(cfg, PROD, "http")
        assert '<link rel="canonical" href="https://chek.ymaster.ru/">' in tags     # рабочий — всегда https
        assert '<meta name="yandex-verification" content="abc123def">' in tags
        assert 'content="g-0_1&quot;x"' in tags                                      # экранирование
        other = seo.head_tags(dict(seo.DEFAULTS), "localhost:8000", "http")
        assert other == '<meta name="robots" content="noindex, nofollow">'
        on = seo.head_tags({"mode": "on"}, "chek.example.ru:8443", "https")
        assert '<link rel="canonical" href="https://chek.example.ru:8443/">' in on
        page = seo.render_index("<head>\n<!-- seo:head -->\n</head>", dict(seo.DEFAULTS), PROD, "https")
        assert seo.MARKER not in page and "canonical" in page

    def test_robots_and_sitemap(self):
        text = seo.robots_txt(dict(seo.DEFAULTS), PROD, "https")
        assert text.count("User-agent:") == 1 and "User-agent: *\nAllow: /\n" in text
        for path in ("/api/", "/onec/", "/ws/", "/b2b/", "/maxapp/"):
            assert f"Disallow: {path}\n" in text, path
        assert "Clean-param: maxlogin&utm_source&utm_medium&utm_campaign&utm_content&utm_term /\n" in text
        assert text.endswith("Sitemap: https://chek.ymaster.ru/sitemap.xml\n")
        closed = seo.robots_txt(dict(seo.DEFAULTS), "localhost", "http")
        assert closed.endswith("User-agent: *\nDisallow: /\n") and "Sitemap" not in closed
        xml = seo.sitemap_xml(dict(seo.DEFAULTS), PROD, "https", "2026-11-05")
        assert "<loc>https://chek.ymaster.ru/</loc><lastmod>2026-11-05</lastmod>" in xml
        assert "<loc>https://chek.ymaster.ru/oferta</loc>" not in xml and xml.count("<url>") >= 2
        assert seo.sitemap_xml(dict(seo.DEFAULTS), "localhost", "http", "2026-11-05") is None

    def test_verification_codes(self):
        assert seo.normalize_code('<meta name="yandex-verification" content="0a1b2c3d4e5f" />') == "0a1b2c3d4e5f"
        assert seo.normalize_code("  google-Code_123  ") == "google-Code_123"
        assert seo.normalize_code("") == "" and seo.normalize_code(None) == ""
        for bad in ("abc", "<script>alert(1)</script>", 'content="x y z 123"', "a" * 101):
            with pytest.raises(ValueError):
                seo.normalize_code(bad)

    def test_settings_never_break_main_page(self):
        def broken_factory():
            raise RuntimeError("база недоступна")

        class BrokenSession:
            def get(self, *_a, **_k):
                raise RuntimeError("блокировка")

            def close(self):
                pass

        assert seo.load_safely(broken_factory) == seo.DEFAULTS
        assert seo.load_safely(BrokenSession) == seo.DEFAULTS


class TestRoutes:
    def test_main_page_by_address(self, client):
        prod = client.get("/", headers={"host": PROD})
        assert prod.status_code == 200 and "x-robots-tag" not in prod.headers
        assert '<link rel="canonical" href="https://chek.ymaster.ru/">' in prod.text and seo.MARKER not in prod.text
        other = client.get("/", headers={"host": "localhost:8000"})
        assert other.status_code == 200 and other.headers["x-robots-tag"] == "noindex, nofollow"
        assert '<meta name="robots" content="noindex, nofollow">' in other.text
        index = client.get("/index.html", headers={"host": PROD})
        assert index.status_code == 200 and seo.MARKER not in index.text and 'rel="canonical"' in index.text
        assert seo.MARKER not in client.get("/index.html", headers={"host": "localhost"}).text
        assert client.head("/", headers={"host": PROD}).status_code == 200

    def test_unknown_address_is_404_with_shell(self, client):
        for path in ("/nope", "/dashboard", "/img/missing.png"):
            r = client.get(path, headers={"host": PROD})
            assert r.status_code == 404 and r.headers["x-robots-tag"] == "noindex, nofollow", path
            assert 'id="app-shell"' in r.text and r.headers["content-type"].startswith("text/html"), path

    def test_robots_sitemap_favicon_fonts(self, client):
        robots = client.get("/robots.txt", headers={"host": PROD})
        assert robots.status_code == 200 and robots.headers["content-type"].startswith("text/plain")
        assert "Sitemap: https://chek.ymaster.ru/sitemap.xml" in robots.text
        assert client.get("/robots.txt", headers={"host": "localhost"}).text.endswith("Disallow: /\n")
        sitemap = client.get("/sitemap.xml", headers={"host": PROD})
        assert sitemap.status_code == 200 and sitemap.headers["content-type"].startswith("application/xml")
        assert client.get("/sitemap.xml", headers={"host": "localhost"}).status_code == 404
        icon = client.get("/favicon.ico")
        assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
        font = client.get("/fonts/inter-cyrillic-wght-normal.woff2")
        assert font.status_code == 200 and font.content[:4] == b"wOF2"
        # HEAD — явно у служебных и публичных адресов: раньше на него отвечал SPA-фолбэк, теперь он даёт 404
        for path in ("/health", "/robots.txt", "/favicon.ico", "/oferta", "/maxapp/", "/b2b/"):
            assert client.head(path, headers={"host": PROD}).status_code == 200, path

    def test_seo_settings_api(self, client):
        from app.database import SessionLocal
        from app.services import appsettings
        admin = login(client, "admin", "admin123")
        try:
            got = client.get("/api/v1/settings/seo", headers={**admin, "host": PROD}).json()
            assert got["mode"] == "auto" and got["enabled"] is True and got["host"] == PROD
            assert [m["id"] for m in got["modes"]] == ["auto", "on", "off"]
            saved = client.put("/api/v1/settings/seo", headers={**admin, "host": PROD}, json={
                "mode": "off", "yandex": '<meta name="yandex-verification" content="ya1234567890" />',
                "google": "go-0987654321"})
            assert saved.status_code == 200
            assert saved.json()["enabled"] is False and saved.json()["yandex"] == "ya1234567890"
            page = client.get("/", headers={"host": PROD}).text
            assert '<meta name="robots" content="noindex, nofollow">' in page
            assert '<meta name="yandex-verification" content="ya1234567890">' in page
            assert '<meta name="google-site-verification" content="go-0987654321">' in page
            assert client.get("/robots.txt", headers={"host": PROD}).text.endswith("Disallow: /\n")
            bad = client.put("/api/v1/settings/seo", headers=admin, json={"yandex": "<script>"})
            assert bad.status_code == 422 and "Код подтверждения" in bad.json()["detail"]
            assert client.put("/api/v1/settings/seo", headers=admin, json={"mode": "maybe"}).status_code == 422
            with SessionLocal() as db:                       # ошибка в коде — ничего не сохранилось
                assert appsettings.get_setting(db, seo.KEY_YANDEX, "") == "ya1234567890"
            from fastapi.testclient import TestClient

            from app.main import app
            assert TestClient(app).get("/api/v1/settings/seo").status_code == 401   # без входа — нельзя
        finally:
            client.put("/api/v1/settings/seo", headers=admin, json={"mode": "auto", "yandex": "", "google": ""})
        assert '<link rel="canonical"' in client.get("/", headers={"host": PROD}).text

    def test_splash_guests_flag(self, client):
        admin = login(client, "admin", "admin123")
        assert client.get("/api/v1/about").json()["splash_guests"] is False              # по умолчанию — нет
        try:
            res = client.put("/api/v1/settings/splash", headers=admin, json={"seconds": 5, "guests": True})
            assert res.status_code == 200 and res.json()["guests"] is True
            assert client.get("/api/v1/about").json()["splash_guests"] is True
            assert client.get("/api/v1/settings/splash", headers=admin).json()["guests"] is True
            keep = client.put("/api/v1/settings/splash", headers=admin, json={"seconds": 7})  # не передан — не меняется
            assert keep.json()["guests"] is True
        finally:
            client.put("/api/v1/settings/splash", headers=admin, json={"seconds": 5, "guests": False})
        assert client.get("/api/v1/about").json()["splash_guests"] is False


class TestLanding:
    def test_head(self):
        html = read("app/static/index.html")
        head = html.split("</head>", 1)[0]
        title = re.search(r"<title>(.*?)</title>", head).group(1)
        assert title == "Проверка чека онлайн по QR-коду — Ямастер Чек" and len(title) <= 70
        desc = re.search(r'<meta name="description" content="([^"]+)">', head).group(1)
        assert desc.startswith("Сайт проверки чеков:") and len(desc) <= 160
        assert head.count(seo.MARKER) == 1 and "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
        for needle in ('<meta property="og:url" content="https://chek.ymaster.ru/">',
                       '<meta property="og:image" content="https://chek.ymaster.ru/img/manual/hero.jpg">',
                       '<meta name="twitter:card" content="summary_large_image">',
                       '<link rel="icon" href="/img/icon-192.png" sizes="192x192" type="image/png">',
                       '<link rel="preload" href="/fonts/inter-cyrillic-wght-normal.woff2" as="font" type="font/woff2" '
                       'crossorigin>'):
            assert needle in head, needle
        assert head.index('<script src="/js/boot.js"></script>') < head.index('<link rel="stylesheet" href="/css/splash.css">')
        assert "application/ld+json" not in html                                    # Яндекс читает микроданные

    def test_single_h1_and_keys_once(self):
        html = read("app/static/index.html")
        assert html.count("<h1") == 1
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
        assert re.sub(r"<[^>]+>", "", h1) == "Проверка чека онлайн —от QR-кода до 1С"
        assert '<p class="login-name">Ямастер <span class="grad-text">Чек</span></p>' in html
        assert html.count('<h2 class="screen-title">') == 3
        assert 'id="login-screen" class="login-screen">' in html                    # виден с первого кадра
        assert 'id="public-screen" class="login-screen hidden"' in html
        head = html.split("</head>", 1)[0]
        title = re.search(r"<title>(.*?)</title>", head).group(1)
        assert title.lower().count("проверка чека") == 1                            # без переспама

    def test_microdata_on_visible_text(self):
        html = read("app/static/index.html")
        assert '<div class="land-left" itemscope itemtype="https://schema.org/WebSite">' in html
        assert '<p class="land-lead" itemprop="description">Сканируйте чеки камерой телефона' in html
        assert 'itemprop="publisher" itemscope itemtype="https://schema.org/Organization"' in html
        assert '<span itemprop="name">ООО «Ямастер»</span>' in html and '<span itemprop="email">chek@ymaster.ru</span>' in html
        for faq in ("Как проверить чек по QR-коду?", "Как понять, что чек настоящий?", "Нужна ли регистрация?"):
            assert f"<summary>{faq}</summary>" in html, faq

    def test_fonts_self_hosted(self):
        css = read("app/static/css/app.css")
        assert css.count("@font-face") == 7 and css.count("font-display: swap") == 7
        for name in ("inter-cyrillic", "inter-latin", "inter-latin-ext", "jetbrains-mono-cyrillic",
                     "jetbrains-mono-latin", "caveat-cyrillic", "caveat-latin"):
            f = ROOT / "app/static/fonts" / f"{name}-wght-normal.woff2"
            assert f.is_file() and f.read_bytes()[:4] == b"wOF2", name
            assert f"url(/fonts/{name}-wght-normal.woff2)" in css, name
        for lic in ("LICENSE-Inter.txt", "LICENSE-JetBrainsMono.txt", "LICENSE-Caveat.txt"):
            assert "SIL Open Font License, Version 1.1" in read(f"app/static/fonts/{lic}"), lic
        total = sum(p.stat().st_size for p in (ROOT / "app/static/fonts").glob("*.woff2"))
        assert total < 400_000, total
        from app.services import server_catalog
        browser = next(e for e in server_catalog.DATA_FLOWS if e["id"] == "browser")
        assert all("fonts." not in h for h in browser["hosts"])

    def test_boot_and_splash_wiring(self):
        boot = read("app/static/js/boot.js")
        for needle in ("'ymaster_check_token'", "'ym-splash-guests'", "root.classList.add('ym-hold')",
                       "root.classList.add('ym-guest')"):
            assert needle in boot, needle
        splash = read("app/static/js/splash.js")
        assert "if (document.documentElement.classList.contains('ym-guest')) { done = true;" in splash
        assert "setGuests: function (on) {" in splash
        assert ".ym-guest .ym-splash { display: none; }" in read("app/static/css/splash.css")
        css = read("app/static/css/app.css")
        assert ".ym-hold #login-screen { display: none; }" in css
        app_js = read("app/static/js/app.js")
        assert "document.documentElement.classList.remove('ym-hold');" in app_js
        assert "window.ymSplash.setGuests(about.splash_guests);" in app_js
        assert '<div class="glass card" id="st-seo" data-sec="device" style="order:48">' in app_js
        assert app_js.index('id="st-seo"') > app_js.index('id="st-splash"')
        assert "if (isAdmin()) bindSeoCard();" in app_js and "'/api/v1/settings/seo'" in app_js
        assert (ROOT / "tests/js/boot.test.js").exists()

    def test_modal_buttons_do_not_break_by_letters(self):
        css = read("app/static/css/app.css")
        phone = css.split("/* --- Модалки на телефоне: нижняя шторка + sticky-кнопки --- */", 1)[1].split("@keyframes", 1)[0]
        assert ".modal-actions { flex-wrap: wrap; }" in phone
        assert ".modal-actions > .btn { overflow-wrap: break-word; }" in phone
        assert ".login-logo h1, .login-logo .login-name, .login-logo .screen-title { font-size: 1.875rem;" in css
        # обход разделов на 360 и 768 px: «↩ Откатиться» в «Последних рабочих версиях» тоже дробилась по буквам
        assert ".rb-table .btn { overflow-wrap: normal; }" in css
        assert "'<table class=\"data rb-table\" style=\"min-width:0\"><thead>'" in read("app/static/js/app.js")


class TestDocsAndKb:
    def test_adr_guide_kb_page(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-33](#adr-33) |" in arch and "## ADR-33" in arch
        assert "**с версии / даты:** 1.70.103, 2026-11-05" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.11. Стартовая страница и поисковики (с v1.70.103) — администратор" in guide
        page = read("docs/kb/12_seo.md")
        for needle in ("## 4. Семантическое ядро", "**требует уточнения**", "habr.com/ru/articles/889700",
                       "clean-param.html", "spam-policies", "avoid-intrusive-interstitials", "ADR-33", "T-75",
                       "не клоакинг"):
            assert needle in page, needle
        assert "[SEO стартовой страницы](12_seo.md)" in read("docs/kb/README.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-74"]
        assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}")
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/58" and "ADR-33" in row["Связанные ADR"]
        assert row["Источник"].endswith("docs/kb/12_seo.md")
        assert tasks["T-75"]["Статус"] in ("todo", "done") and tasks["T-75"]["Issue/PR"].endswith("/issues/59")
        for tid, issue in (("T-63", 48), ("T-64", 49), ("T-71", 56)):     # v1.70.104: T-63 выпущена
            assert tasks[tid]["Статус"] in ("todo", "done", "blocked") and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.104: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.103):** [#58](https://github.com/Rimlin-UNC/1c_chek/issues/58) T-74" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.104: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.102")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
