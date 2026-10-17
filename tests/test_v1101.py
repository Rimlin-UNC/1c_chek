# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.10.1: пункт меню выбирается в приложении (PWA).
# РЕГРЕССИЯ 1.10.0: фон-затемнение (z-index 40) перекрывал меню (30) —
# на телефоне тап по пункту закрывал меню, раздел не открывался.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

CSS = open("app/static/css/app.css", encoding="utf-8").read()
JS = open("app/static/js/app.js", encoding="utf-8").read()
HTML = open("app/static/index.html", encoding="utf-8").read()


def _rules(css: str):
    """Списки (селекторы, тело) всех правил; комментарии вырезаются,
    тело не может содержать фигурные скобки — @media не «глотает» вложенные."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        yield [s.strip() for s in m.group(1).split(",")], m.group(2)


def _zindex(selector: str):
    """z-index правила с ТОЧНО указанным селектором (первое с числом)."""
    for selectors, body in _rules(CSS):
        if selector in selectors:
            z = re.search(r'z-index:\s*(-?\d+)', body)
            if z:
                return int(z.group(1))
    return None


class TestMenuSelectable:
    def test_backdrop_below_open_sidebar(self):
        """Фон-затемнение НИЖЕ ОТКРЫТОГО меню (иначе тап по пункту закрывает
        меню вместо перехода — баг 1.10.0) и ВЫШЕ шапки (затемняет контент)."""
        open_sidebar = _zindex(".sidebar.open")
        backdrop = _zindex(".sidebar-backdrop")
        topbar = _zindex(".topbar")
        assert open_sidebar is not None and backdrop is not None, \
            "z-index открытого меню и фона должен быть задан явно"
        assert backdrop < open_sidebar, \
            f"фон (z={backdrop}) перекрывает открытое меню (z={open_sidebar})"
        assert topbar is None or backdrop > topbar, "фон должен затемнять контент"

    def test_selecting_item_closes_menu(self):
        # v1.10.2: делегирование — переход по пункту выполняет приложение само
        assert "e.target.closest('a.nav-item')" in JS
        assert "closeSidebar();\n    if (location.hash === target) route();" in JS
        # переход по hash (назад/вперёд, ссылки) тоже закрывает
        assert re.search(r"hashchange[^}]*closeSidebar\(\)", JS, re.S)

    def test_safe_area_in_standalone(self):
        """В standalone «чёлка»/жест-бар не перекрывают шапку и подвал меню."""
        for selectors, body in _rules(CSS):
            if ".sidebar" in selectors and "position: fixed" in body:
                assert "env(safe-area-inset-top" in body
                assert "env(safe-area-inset-bottom" in body
                return
        raise AssertionError("мобильное правило .sidebar не найдено")

    def test_hamburger_aria(self):
        assert 'aria-expanded="false"' in HTML
        assert 'aria-controls="sidebar"' in HTML
        assert "setAttribute('aria-expanded'" in JS


class TestVersion1101:
    def test_version_everywhere(self):
        """Версия согласована во всех файлах (бамп-независимо)."""
        import re as _re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = _re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert f"ymaster-check-v{ver}" in open("app/static/sw.js", encoding="utf-8").read()
        assert f'"version": "{ver}"' in open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f"'{ver}'" in JS                      # WHATS_NEW
        assert f"## [{ver}]" in open("CHANGELOG.md", encoding="utf-8").read()  # свежий раздел
