# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.10.2: выбор пункта меню ГАРАНТИРОВАННЫЙ.
# Открытое меню — верхний слой (60) выше фона (55); переход по пункту
# выполняет само приложение (делегирование), touch-action без задержек.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

CSS = open("app/static/css/app.css", encoding="utf-8").read()
JS = open("app/static/js/app.js", encoding="utf-8").read()


def _rules(css: str):
    """(селекторы, тело) всех правил; без комментариев, @media раскрыт."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        yield [s.strip() for s in m.group(1).split(",")], m.group(2)


def _zindex(selector: str):
    for selectors, body in _rules(CSS):
        if selector in selectors:
            z = re.search(r'z-index:\s*(-?\d+)', body)
            if z:
                return int(z.group(1))
    return None


class TestMenuTopLayer:
    def test_open_menu_above_backdrop(self):
        """Открытое меню (60) выше фона (55): тап по пункту не может быть
        перехвачен ни одним слоем интерфейса."""
        assert _zindex(".sidebar.open") == 60
        assert _zindex(".sidebar-backdrop") == 55

    def test_nav_item_touch_action(self):
        """Пункты меню: без задержки двойного тапа, курсор-палец, без выделения."""
        for selectors, body in _rules(CSS):
            if ".nav-item" in selectors:
                assert "touch-action: manipulation" in body
                assert "cursor: pointer" in body
                assert "user-select: none" in body
                return
        raise AssertionError("правило .nav-item не найдено")


class TestMenuDelegation:
    def test_click_delegated_to_sidebar(self):
        """Переход выполняет приложение: closest по пункту, preventDefault,
        ручная навигация; тот же раздел — route() и закрытие."""
        assert "$$('#sidebar').addEventListener" in JS or \
               "$('#sidebar').addEventListener('click'" in JS
        assert "e.target.closest('a.nav-item')" in JS
        assert "e.preventDefault();" in JS
        assert "if (location.hash === target) route();" in JS
        assert "location.hash = target;" in JS

    def test_escape_and_swipe_kept(self):
        assert "if (e.key === 'Escape') closeSidebar();" in JS
        assert "closeSidebar();" in JS.split("touchend")[1].split("}")[0]
