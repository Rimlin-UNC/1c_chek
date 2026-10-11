# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.2: гостевые страницы прокручиваются.
# Регресс: body { overflow: hidden } (каркас приложения) не пускал
# прокрутку на гостевых экранах (вход/сдать чек/кабинет) — длинные
# страницы обрезались без возможности листать (в любой ориентации).
# Решение: showScreen вешает на body класс guest-scroll, пока показан
# не-app-shell; CSS возвращает странице прокрутку.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1592:
    """Строгий версионный замок релиза v1.59.2."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.59.3: структурная проверка — строгий замок версии в test_v1593
        assert tuple(int(x) for x in ver.split(".")) >= (1, 59, 2)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.2':") < js.index("'1.59.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.2]") == 1
        assert ch.index("## [1.59.2]") < ch.index("## [1.59.1]")


class TestGuestScroll:
    """Прокрутка гостевых страниц: класс на body + CSS-правило."""

    def test_showscreen_toggles_body_class(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function showScreen(" in js
        # переключатель управляет прокруткой страницы
        assert "guest-scroll" in js
        assert "document.body.classList.toggle('guest-scroll'" in js

    def test_css_allows_page_scroll_for_guests(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "body.guest-scroll" in css
        block = css.split("body.guest-scroll")[1].split("}")[0]
        assert "overflow: visible" in block      # страница прокручивается
        assert "overflow-x: hidden" in block     # горизонтали по-прежнему нет
