"""v1.70.33 — вёрстка: валидные форматы и правильная структура под устройства.

Что закрепляет этот набор (дефекты были найдены измерением, а не на глаз):

1. Валидность разметки. html-validate 11.16.4 (пресет recommended) выдавал на
   трёх HTML-файлах проекта 64 ошибки, все severity 2:
     38 × no-inline-style      (31 в index.html, 7 в maxapp/index.html)
     13 × no-implicit-button-type
      2 × autocomplete-password
      1 × prefer-native-element  (#user-chip)
      1 × no-implicit-input-type (#qr-input в maxapp)
   в брендбуке дополнительно 1 × prefer-tbody.
   Сейчас — 0. Инлайн-стили переехали в классы, поэтому отдельно защищён
   перенос: каждый класс, появившийся в разметке, обязан существовать в CSS.

2. Вложенные интерактивные элементы. #user-chip был div[role=button] с
   кнопкой «Выйти» ВНУТРИ. Заменить такой div на <button> нельзя — получилась
   бы кнопка в кнопке. Поэтому переключатель профиля стал нативной кнопкой, а
   «Выйти» вынесена рядом, в обёртку .user-area. Структура зафиксирована.

3. Кольцо фокуса. Действующим был оранжевый --accent: 2.21:1 на --bg и
   2.61:1 на --panel — ниже порога 3.00:1 для нетекстового контраста
   (WCAG 1.4.11). Правило из блока v1.21.0 перебивало раннее правило с
   --accent-2, поэтому файл был «зелёным», а фокус — невидимым.

4. Тач-цели. Два блока @media (pointer: coarse) с противоречивыми числами:
   .nav-item 44px против 48px, .btn-sm 40px против 42px. Побеждал нижний,
   часть верхнего была мертва (и её пинил test_v1560).

5. Шапка на узких экранах. .topbar-right был flex: 0 0 auto — не сжимался,
   поэтому min-width: 0 у .user-meta не работал и длинное имя выдавливало
   шапку. Добавлена цепочка сжатия и обрезка многоточием.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "app/static/css/app.css"
INDEX = ROOT / "app/static/index.html"
MAXAPP = ROOT / "app/static/maxapp/index.html"
BRANDBOOK = ROOT / "brand/brandbook.html"
APP_JS = ROOT / "app/static/js/app.js"
HTML_FILES = (INDEX, MAXAPP, BRANDBOOK)


def js_modules() -> list[pathlib.Path]:
    """Все собственные скрипты клиента. Экраны рисуют не один app.js, а
    несколько модулей: кнопка закрытия модалки живёт в ui.js, интерфейс
    MAX-приложения — в maxapp/maxapp.js. Проверка только app.js пропустила бы
    их, поэтому инвариант задан на все модули разом (vendor не трогаем)."""
    out = sorted((ROOT / "app/static/js").glob("*.js"))
    out.append(ROOT / "app/static/maxapp/maxapp.js")
    return [p for p in out if "vendor" not in p.parts]

# Классы, которые не обязаны иметь правил в CSS: их переключает app.js,
# а видимость даёт утилита .hidden.
JS_HOOKS = {
    "admin-only", "accountant-only", "boss-only", "pool-only",
    "hidden", "open", "selected", "active", "disabled", "expanded", "loading",
}


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def no_comments(text: str) -> str:
    """Убрать HTML-комментарии: в них legitimately встречаются литералы
    разметки, которые объясняют правку (например «div с ролью button»)."""
    return re.sub(r"<!--[\s\S]*?-->", "", text)


# --------------------------------------------------------------------------
# Контраст: те же формулы, что в test_v17032, чтобы числа были сопоставимы
# --------------------------------------------------------------------------
def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcolor: str) -> float:
    h = hexcolor.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def token(name: str, css: str | None = None) -> str:
    css = css if css is not None else read(APP_CSS)
    m = re.search(re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{3,8})", css)
    assert m, f"токен {name} не объявлен"
    return m.group(1)


def css_rules(css: str) -> dict:
    r"""Селектор → объединение тел всех его правил.

    Наивный поиск `\.sel\s*{...}` врёт, когда правил несколько (у .user-chip их
    два: раскладка в начале файла и сброс нативной кнопки внизу) и когда
    селектор входит в список (`.user-name, .user-role {...}`). Здесь берётся
    объединение, то есть то, что браузер в итоге применит."""
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        for sel in m.group(1).split(","):
            out.setdefault(sel.strip(), []).append(m.group(2))
    return {k: " ".join(v) for k, v in out.items()}


def declarations(css: str, selector: str) -> str:
    rules = css_rules(css)
    assert selector in rules, f"правила для {selector} в CSS нет"
    return rules[selector]


def js_code(js: str) -> str:
    """Убрать построчные комментарии: в них legitimately встречаются литералы
    тегов и селекторов, объясняющие правку."""
    return "\n".join(ln for ln in js.split("\n") if not ln.strip().startswith("//"))


def tag_of(html: str, needle: str) -> str:
    """Полный открывающий тег, содержащий needle."""
    i = html.index(needle)
    start = html.rindex("<", 0, i)
    end = html.index(">", i)
    return html[start:end + 1]


def element_slice(html: str, opener_needle: str) -> str:
    """Содержимое элемента от открывающего тега до его закрывающего тега.
    Годится для плоской разметки шапки, где одноимённых вложений нет."""
    i = html.index(opener_needle)
    start = html.rindex("<", 0, i)
    name = re.match(r"<(\w+)", html[start:]).group(1)
    depth = 0
    for m in re.finditer(r"<(/?)%s\b" % name, html[start:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return html[start:start + m.end()] + ">"
    raise AssertionError("не найден закрывающий тег для " + opener_needle)


# ==========================================================================
# 1. Инлайн-стили убраны из разметки — и перенос ничего не потерял
# ==========================================================================
class TestNoInlineStyles:
    @pytest.mark.parametrize("path", HTML_FILES, ids=lambda p: p.name + ":" + p.parent.name)
    def test_no_style_attribute(self, path):
        body = no_comments(read(path))
        assert 'style="' not in body, \
            "в разметке остался инлайн-стиль — он не проходит no-inline-style"

    def test_converted_classes_are_actually_applied(self):
        """Не-вакуумность переноса: утилиты не должны остаться мёртвым CSS —
        каждая из заменённых обязана реально стоять в разметке. Проверяется без
        обращения к git, чтобы тест не зависел от глубины клона."""
        idx, mx = read(INDEX), read(MAXAPP)
        for cls, count in (("alt-line", 4), ("alt-row", None), ("screen-title", None),
                           ("form-foot", None), ("consent-label", None),
                           ("visually-hidden", None)):
            n = len(re.findall(r'class="[^"]*\b%s\b' % re.escape(cls), idx))
            assert n > 0, f"класс .{cls} объявлен, но в index.html не применён"
            if count is not None:
                assert n == count, f".{cls}: ожидалось {count}, применено {n}"
        assert 'class="big"' in mx or "big" in mx

    def test_dividers_survived_the_move(self):
        """Четыре разделителя «или» переехали из style= в .alt-line."""
        assert read(INDEX).count("alt-line") == 4
        css = read(APP_CSS)
        assert re.search(r"\.alt-line\s*\{[^}]*background:\s*var\(--border\)", css)

    @pytest.mark.parametrize("path", (INDEX, MAXAPP), ids=lambda p: p.parent.name)
    def test_every_class_in_markup_is_defined(self, path):
        """Главная защита переноса: класс, которого нет в CSS, молча лишает
        элемент оформления — браузер не сообщит об ошибке."""
        html = no_comments(read(path))
        css = read(APP_CSS)
        if path is MAXAPP:                      # у MAX-приложения свой <style>
            css = css + "\n" + re.search(r"<style>([\s\S]*?)</style>", html).group(1)
        css_body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css_body))
        used = set()
        for m in re.finditer(r'\bclass="([^"]*)"', html):
            used.update(c for c in m.group(1).split() if c and "${" not in c)
        missing = sorted(used - defined - JS_HOOKS)
        assert not missing, f"классы из разметки не определены в CSS: {missing}"

    def test_utility_block_exists(self):
        """Классы, заменившие инлайн-стили, объявлены одним блоком в конце
        файла — там же, где и положено утилитам (побеждают при равной
        специфичности за счёт порядка)."""
        css = read(APP_CSS)
        for util in (".alt-row", ".alt-line", ".screen-title", ".login-card-wide",
                     ".form-foot", ".form-link", ".badge-center", ".hint-center",
                     ".consent-label", ".visually-hidden", ".mt-6", ".mb-6",
                     ".gap-8", ".nowrap", ".text-center"):
            assert util in css, f"утилита {util} не объявлена"


# ==========================================================================
# 2. Кнопки: явный type everywhere — и в разметке, и в шаблонах app.js
# ==========================================================================
class TestButtonType:
    @pytest.mark.parametrize("path", (INDEX, MAXAPP), ids=lambda p: p.parent.name)
    def test_static_buttons_have_explicit_type(self, path):
        html = no_comments(read(path))
        tags = re.findall(r"<button\b[^>]*>", html)
        assert tags, "кнопок не найдено — проверка выродилась"
        bad = [t for t in tags if not re.search(r"\btype\s*=", t)]
        assert not bad, f"кнопки без type (по умолчанию submit): {bad}"

    def test_generated_buttons_have_explicit_type(self):
        """Экраны рисуют шаблоны, поэтому кнопки без type в них — тот же
        дефект, что и в статической оболочке. Кнопка без type внутри <form>
        означает submit: случайный клик отправляет форму. В app.js было 273
        таких, ещё одна — в ui.js (кнопка закрытия ВСТАВЛЯЕТСЯ В КАЖДУЮ
        модалку), четыре — в maxapp.js."""
        total, bad = 0, []
        for p in js_modules():
            tags = re.findall(r"<button\b[^>]*>", read(p))
            total += len(tags)
            bad += [(p.name, t) for t in tags if not re.search(r"\btype\s*=", t)]
        assert total > 250, f"шаблонов кнопок подозрительно мало: {total}"
        assert not bad, f"{len(bad)} кнопок без type, например {bad[:3]}"

    def test_modal_close_button_is_typed(self):
        """Отдельно: кнопка закрытия создаётся в ui.js для любой модалки,
        поэтому её type важнее остальных."""
        ui = read(ROOT / "app/static/js/ui.js")
        m = re.search(r"<button\b[^>]*modal-close[^>]*>", ui)
        assert m and 'type="button"' in m.group(0), m.group(0) if m else "не найдена"

    def test_generated_forms_have_exactly_one_submit(self):
        js = read(APP_JS)
        assert js.count('type="submit"') == 3
        for form_id in ("pf-login", "pf-reg", "pf-magic"):
            assert f'<form id="{form_id}"' in js

    def test_no_button_was_turned_into_submit_by_the_fix(self):
        """Не-вакуумность обратной стороны: явных submit ровно три, все — в
        формах входа/регистрации/магической ссылки, и ни одного лишнего."""
        js = read(APP_JS)
        submits = re.findall(r"<button\b[^>]*type=\"submit\"[^>]*>", js)
        assert len(submits) == 3
        for s in submits:
            assert "btn-block" in s

    def test_qr_input_has_explicit_type(self):
        html = read(MAXAPP)
        assert 'type="text" id="qr-input"' in html or 'id="qr-input" type="text"' in html

    def test_no_implicit_input_types(self):
        for path in (INDEX, MAXAPP):
            html = no_comments(read(path))
            for m in re.finditer(r"<input\b[^>]*>", html):
                assert re.search(r'\btype\s*=', m.group(0)), \
                    f"{path.name}: input без type → {m.group(0)[:80]}"


# ==========================================================================
# 3. #user-chip: нативная кнопка, «Выйти» — снаружи
# ==========================================================================
class TestUserChipStructure:
    def test_chip_is_a_native_button(self):
        tag = tag_of(read(INDEX), 'id="user-chip"')
        assert tag.startswith("<button"), f"чип всё ещё не кнопка: {tag[:70]}"
        assert 'type="button"' in tag

    def test_chip_has_no_role_or_tabindex(self):
        """Нативной кнопке role=button и tabindex=0 не нужны и вредны:
        они и привели к prefer-native-element."""
        tag = tag_of(read(INDEX), 'id="user-chip"')
        assert "role=" not in tag
        assert "tabindex" not in tag

    def test_no_pseudo_buttons_left_in_markup(self):
        for path in HTML_FILES:
            html = no_comments(read(path))
            assert not re.search(r'<div[^>]*role="button"', html), \
                f"{path.name}: остался div с ролью button"

    def test_logout_is_not_nested_inside_chip(self):
        """Кнопка в кнопке — вложенные интерактивные элементы: невалидно и
        недоступно (скринридер не объявит вложенное действие)."""
        chip = element_slice(read(INDEX), 'id="user-chip"')
        assert "btn-logout" not in chip, "«Выйти» снова внутри переключателя профиля"
        assert "<button" not in chip[chip.index(">") + 1:], \
            "внутри чипа появилась ещё одна кнопка"

    def test_both_live_in_user_area(self):
        area = element_slice(read(INDEX), 'class="user-area"')
        assert 'id="user-chip"' in area
        assert 'id="btn-logout"' in area
        assert area.count("<button") == 2

    def test_persona_menu_stays_a_sibling(self):
        """Меню позиционируется от .topbar-right, поэтому оно обязано
        оставаться снаружи чипа — иначе оно обрезалось бы кнопкой."""
        html = read(INDEX)
        area = element_slice(html, 'class="user-area"')
        assert 'id="persona-menu"' not in area
        topbar = element_slice(html, 'class="topbar-right"')
        assert 'id="persona-menu"' in topbar

    def test_chip_children_are_spans(self):
        """Внутри <button> допустим только фразовый контент: div там
        невалиден, поэтому имя/роль/аватар стали span."""
        chip = element_slice(read(INDEX), 'id="user-chip"')
        assert "<div" not in chip, f"в кнопке остался div: {chip[:200]}"
        for cls in ("avatar", "user-meta", "user-name", "user-role", "chip-caret"):
            assert f'class="{cls}"' in chip or f'class="{cls} ' in chip, cls

    def test_chip_keeps_aria(self):
        tag = tag_of(read(INDEX), 'id="user-chip"')
        assert 'aria-haspopup="true"' in tag
        assert 'aria-expanded="false"' in tag

    def test_no_nested_interactive_in_generated_markup(self):
        js = js_code("\n".join(read(p) for p in js_modules()))
        assert not re.findall(r"<button\b[^>]*>(?:(?!</button>)[\s\S])*?<button\b", js), \
            "в шаблонах кнопка вложена в кнопку"
        assert not re.findall(r"<button\b[^>]*>(?:(?!</button>)[\s\S])*?<a\s", js), \
            "в шаблонах ссылка вложена в кнопку"


# ==========================================================================
# 4. Поведение чипа в JS после превращения в нативную кнопку
# ==========================================================================
class TestChipBehaviour:
    def test_no_keydown_handler_left(self):
        """Нативная кнопка сама превращает Enter/Space в click. Прежний
        onkeydown с preventDefault() вместе с этим дал бы двойное
        переключение меню."""
        js = read(APP_JS)
        assert "chip.onkeydown" not in js
        assert "chip.addEventListener('keydown'" not in js

    def test_inactive_state_uses_disabled(self):
        """Неинтерактивный чип объявляется честно: disabled убирает его из
        порядка обхода и не даёт курсора-указателя."""
        js = read(APP_JS)
        assert "chip.disabled = !chipActive" in js
        assert "const chipActive = isAdmin() || viewing" in js

    def test_outside_click_guard_follows_the_wrapper(self):
        """Клик мимо закрывает меню; область проверки расширена до .user-area,
        иначе клик по «Выйти» считался бы кликом мимо и закрывал меню."""
        js = read(APP_JS)
        assert "!e.target.closest('.user-area')" in js
        assert "!e.target.closest('#user-chip')" not in js

    def test_dead_logout_guard_removed(self):
        js = js_code(read(APP_JS))
        assert "closest('#btn-logout')" not in js, \
            "проверка на потомка «Выйти» недостижима — кнопка больше не внутри"

    def test_legacy_pins_intact(self):
        """Правка не должна была задеть закреплённое другими версиями."""
        js = read(APP_JS)
        assert "$('#btn-logout').classList.toggle('hidden', viewing || poolV)" in js
        assert "$('#btn-logout').onclick = showLogoutDialog" in js

    def test_chip_css_disables_pointer_when_inactive(self):
        css = read(APP_CSS)
        assert ".user-chip:hover:not(:disabled)" in css
        assert ".user-chip:disabled { cursor: default; }" in css


# ==========================================================================
# 5. Кольцо фокуса: контраст нетекстового элемента (WCAG 1.4.11, порог 3:1)
# ==========================================================================
class TestFocusRing:
    def rules(self):
        css = read(APP_CSS)
        return re.findall(r":focus-visible\s*\{([^}]*)\}", css)

    def test_single_global_rule(self):
        """Два правила :focus-visible с разным цветом = одно из них мертво,
        а какое именно — зависит от порядка в файле."""
        assert len(self.rules()) == 1, f"правил :focus-visible {len(self.rules())}"

    def test_outline_color_passes_non_text_contrast(self):
        body = self.rules()[0]
        css = read(APP_CSS)
        m = re.search(r"outline:\s*\d+px\s+solid\s+var\((--[\w-]+)\)", body)
        assert m, f"цвет контура не через токен: {body}"
        color = token(m.group(1), css)
        for bg_name in ("--bg", "--panel", "--bg-soft"):
            bg = token(bg_name, css)
            ratio = contrast(color, bg)
            assert ratio >= 3.0, (
                f"кольцо фокуса {m.group(1)}={color} на {bg_name}={bg}: "
                f"{ratio:.2f}:1 — ниже 3.00:1 (WCAG 1.4.11)")

    def test_orange_accent_would_fail(self):
        """Не-вакуумность: прежний --accent действительно не проходит порог,
        поэтому правка не косметическая."""
        css = read(APP_CSS)
        accent = token("--accent", css)
        assert contrast(accent, token("--bg", css)) < 3.0
        assert contrast(accent, token("--panel", css)) < 3.0

    def test_focus_rule_does_not_reshape_the_element(self):
        """:focus-visible применяется к самому элементу, поэтому border-radius
        в нём ломал собственную геометрию (карточка 14px → 6px под фокусом)."""
        assert "border-radius" not in self.rules()[0]

    def test_outline_offset_kept(self):
        assert "outline-offset: 2px" in self.rules()[0]


# ==========================================================================
# 6. Тач-цели: один блок, действующие значения
# ==========================================================================
class TestTouchTargets:
    def coarse(self):
        # v1.70.35: по коду БЕЗ комментариев — пояснение к общему правилу
        # чекбоксов называет этот блок, иначе проверка падает на своём же тексте
        css = re.sub(r"/\*.*?\*/", "", read(APP_CSS), flags=re.S)
        assert css.count("@media (pointer: coarse)") == 1, \
            "блоков тач-целей снова несколько — значения разъедутся"
        return re.search(r"@media \(pointer: coarse\)\s*\{([\s\S]*?)\n\}", css).group(1)

    def test_effective_values(self):
        block = self.coarse()
        assert ".btn { min-height: 44px; }" in block
        assert ".btn-icon { width: 44px; height: 44px; }" in block
        assert ".nav-item { min-height: 48px; }" in block
        assert ".segmented button { min-height: 44px; }" in block
        assert ".filter-bar .btn { min-height: 44px; }" in block
        assert ".btn-sm { min-height: 42px; min-width: 42px; padding: 9px 14px; }" in block

    def test_dead_values_are_gone(self):
        """44px у .nav-item и 40px у .btn-sm были мертвы: их перебивал нижний
        блок. Оставлять их — значит снова развести два противоречия."""
        block = self.coarse()
        assert ".nav-item { min-height: 44px; }" not in block
        assert ".btn-sm { min-height: 40px; }" not in block

    def test_inputs_and_close_button_covered(self):
        block = self.coarse()
        assert 'input[type="checkbox"] { width: 22px; height: 22px; }' in block
        assert "select, .company-select { min-height: 44px; }" in block
        assert ".modal-close { min-width: 44px; min-height: 44px; }" in block

    def test_no_conflicting_touch_rules_outside_the_block(self):
        """Вне блока тач-целей не должно быть правил, перебивающих min-height
        у тех же селекторов, иначе «единый блок» снова станет фикцией."""
        css = re.sub(r"/\*.*?\*/", "", read(APP_CSS), flags=re.S)
        i = css.index("@media (pointer: coarse)")
        j = css.index("\n}", i)
        outside = css[:i] + css[j:]
        for sel in (".nav-item", ".btn-sm"):
            for m in re.finditer(re.escape(sel) + r"\s*\{([^}]*)\}", outside):
                assert "min-height" not in m.group(1), \
                    f"{sel} вне блока тач-целей снова задаёт min-height"


# ==========================================================================
# 7. Шапка под узкие экраны: цепочка сжатия и обрезка
# ==========================================================================
class TestTopbarShrink:
    def test_topbar_right_can_shrink(self):
        css = read(APP_CSS)
        m = re.search(r"^\.topbar-right\s*\{([^}]*)\}", css, re.M)
        assert m
        assert "flex: 0 1 auto" in m.group(1), \
            "flex-shrink 0 не даёт блоку сжиматься — min-width: 0 бесполезен"
        assert "min-width: 0" in m.group(1)

    def test_user_area_and_chip_can_shrink(self):
        css = read(APP_CSS)
        area = declarations(css, ".user-area")
        assert "min-width: 0" in area and "flex: 0 1 auto" in area
        assert "min-width: 0" in declarations(css, ".user-chip")

    def test_name_and_role_truncate(self):
        css = read(APP_CSS)
        m = re.search(r"\.user-name,\s*\.user-role\s*\{([^}]*)\}", css)
        assert m, "правила обрезки имени нет"
        for prop in ("overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"):
            assert prop in m.group(1), prop

    def test_meta_children_are_block(self):
        """span внутри flex-контейнера без display:block выстроил бы имя и
        роль в одну строку."""
        css = read(APP_CSS)
        for sel in (".user-meta", ".user-name", ".user-role"):
            assert "display: block" in declarations(css, sel), sel

    def test_native_button_reset(self):
        """У нативной <button> свои рамка, фон, центровка и шрифт — без сброса
        профиль в шапке поедет, а на iOS ещё и нарисуется системная краска."""
        css = read(APP_CSS)
        chip = declarations(css, ".user-chip")
        for prop in ("border: 0", "background: transparent", "font: inherit",
                     "color: inherit", "text-align: left", "appearance: none",
                     "-webkit-appearance: none"):
            assert prop in chip, f"в сбросе .user-chip нет {prop}"

    def test_menu_anchor_untouched(self):
        """.persona-menu позиционируется абсолютно от .topbar-right."""
        css = read(APP_CSS)
        assert ".topbar-right { position: relative; }" in css


# ==========================================================================
# 8. Прочее под устройства
# ==========================================================================
class TestDeviceStructure:
    def test_viewport_allows_zoom(self):
        html = read(INDEX)
        m = re.search(r'<meta name="viewport" content="([^"]*)"', html)
        assert m, "нет viewport"
        assert "width=device-width" in m.group(1)
        assert "user-scalable=no" not in m.group(1), \
            "запрет зума ломает WCAG 1.4.4"
        assert "maximum-scale=1" not in m.group(1)

    def test_viewport_fit_cover_and_safe_area(self):
        assert "viewport-fit=cover" in read(INDEX)
        css = read(APP_CSS)
        assert css.count("env(safe-area-inset") >= 8

    def test_dynamic_viewport_height_has_fallback(self):
        """100dvh не понимают старые браузеры, поэтому перед ним обязателен
        обычный 100vh — иначе высота схлопнется."""
        css = read(APP_CSS)
        for m in re.finditer(r"min-height:\s*100dvh|height:\s*100dvh", css):
            line_start = css.rindex("\n", 0, m.start() - 60)
            before = css[max(0, m.start() - 90):m.start()]
            assert "100vh" in before, f"100dvh без фолбэка: {css[line_start:m.end()]!r}"

    def test_reduced_motion_respected(self):
        assert read(APP_CSS).count("prefers-reduced-motion") >= 4

    def test_tables_scroll_on_phones(self):
        """Минимальная ширина таблицы чеков 720px (880px на узких) больше
        экрана телефона, поэтому прокрутка обязана быть внутри таблицы, а не
        по всей странице."""
        css = read(APP_CSS)
        assert ".table-wrap { overflow-x: auto;" in css
        assert "table.data { display: block; max-width: 100%; overflow-x: auto;" in css
        assert "min-width: 720px" in css

    def test_inputs_are_16px_on_phones(self):
        """Меньше 16px — iOS Safari зумит страницу при фокусе на поле."""
        css = read(APP_CSS)
        assert "input, select, textarea { font-size: 16px; }" in css


# ==========================================================================
# 9. Пароли: autocomplete (валидатор: autocomplete-password)
# ==========================================================================
class TestAutocomplete:
    def test_login_password(self):
        tag = tag_of(read(INDEX), 'id="login-password"')
        assert 'autocomplete="current-password"' in tag

    def test_register_password(self):
        tag = tag_of(read(INDEX), 'id="reg-password"')
        assert 'autocomplete="new-password"' in tag

    def test_generated_password_fields_too(self):
        js = read(APP_JS)
        for field in ("pl-pass", "pr-pass"):
            tag = re.search(r'<input id="%s"[^>]*>' % field, js).group(0)
            assert "autocomplete=" in tag, field

    @pytest.mark.parametrize("field,value", [
        ("land-email", "email"),
        ("login-username", "username"),
        ("reg-fullname", "name"),
        ("reg-username", "username"),
    ])
    def test_autofill_tokens_on_phone(self, field, value):
        """Пароль без парного username менеджеры паролей не связывают, а на
        телефоне автозаполнение тогда не срабатывает вовсе."""
        tag = tag_of(read(INDEX), f'id="{field}"')
        assert f'autocomplete="{value}"' in tag, f"{field}: {tag[:100]}"

    def test_honeypot_is_hidden_from_everyone(self):
        """Поле-ловушка для ботов обязано быть скрыто и от скринридера, и от
        порядка обхода, иначе автозаполнение попадает в него вместо почты."""
        tag = tag_of(read(INDEX), 'id="land-hp"')
        assert "visually-hidden" in tag
        assert 'tabindex="-1"' in tag and 'aria-hidden="true"' in tag
        assert 'autocomplete="off"' in tag


# ==========================================================================
# 10. Структура таблиц в брендбуке (prefer-tbody)
# ==========================================================================
class TestBrandbook:
    def test_rows_are_grouped(self):
        html = read(BRANDBOOK)
        assert "<tbody>" in html and "</tbody>" in html
        assert "<thead>" in html
        for m in re.finditer(r"<table[^>]*>([\s\S]*?)</table>", html):
            inner = m.group(1).lstrip()
            assert inner.startswith("<!--") or inner.startswith("<thead") \
                or inner.startswith("<tbody") or inner.startswith("<caption"), \
                f"<tr> прямо в <table>: {inner[:60]!r}"

    def test_swatch_classes_reference_tokens(self):
        html = read(BRANDBOOK)
        assert ".sw-orange{background:var(--orange)}" in html
        assert ".sw-navy{background:var(--navy)}" in html
        assert "--line:" in html, "--line обязан быть объявлен в :root брендбука"

    def test_brandbook_has_no_undefined_tokens(self):
        """В app.css токен --line не объявлен (его чинили в v1.70.32), а в
        брендбуке — свой :root, и там он есть. Проверка, чтобы эту разницу
        никто не «починил» вслепую."""
        html = read(BRANDBOOK)
        used = set(re.findall(r"var\((--[\w-]+)", html))
        declared = set(re.findall(r"(--[\w-]+)\s*:", html))
        assert used - declared == set(), f"не объявлены: {sorted(used - declared)}"


# ==========================================================================
# 11. Мёртвые запасные значения var()
# ==========================================================================
class TestVarFallbacks:
    def test_no_var_fallbacks_at_all(self):
        """var(--x, fallback) при объявленном --x никогда не применяет
        fallback, а при необъявленном — маскирует опечатку. На .user-chip:hover
        и .pm-item:hover это рисовало белый по белому: --panel-strong объявлен
        как #ffffff, а запасное rgba(128,128,128,.12) молча игнорировалось.
        В v1.70.32–v1.70.33 запасные значения убраны вовсе, и это инвариант."""
        body = re.sub(r"/\*.*?\*/", "", read(APP_CSS), flags=re.S)
        left = re.findall(r"var\(\s*--[\w-]+\s*,", body)
        assert not left, f"в app.css снова {len(left)} var() с запасным значением"
        assert body.count("var(--") > 100, "токены темы исчезли — проверка выродилась"

    def test_hover_uses_the_hover_token(self):
        css = read(APP_CSS)
        assert ".user-chip:hover:not(:disabled) { background: var(--hover-bg); }" in css
        assert "--hover-bg:" in css

    def test_pm_item_hover_too(self):
        css = read(APP_CSS)
        m = re.search(r"\.pm-item:hover\s*\{([^}]*)\}", css)
        assert m and "var(--hover-bg)" in m.group(1)


# ==========================================================================
# 12. Внешний валидатор, если он установлен
# ==========================================================================
class TestExternalValidator:
    BIN = os.environ.get(
        "HTML_VALIDATE_BIN",
        "/tmp/htmlval/node_modules/.bin/html-validate",
    )

    @pytest.mark.skipif(
        not pathlib.Path("/tmp/htmlval/node_modules/.bin/html-validate").exists()
        and not os.environ.get("HTML_VALIDATE_BIN"),
        reason="html-validate не установлен: npm i html-validate (не обязателен в CI)",
    )
    def test_zero_errors(self):
        cmd = [self.BIN, "--formatter=json", *(str(p) for p in HTML_FILES)]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
        data = json.loads(proc.stdout or "[]")
        errors = [(m["ruleId"], m["line"], f["filePath"])
                  for f in data for m in f.get("messages", []) if m["severity"] == 2]
        assert not errors, f"html-validate: {len(errors)} ошибок, например {errors[:5]}"


# ==========================================================================
# 13. Версия, журнал, changelog
# ==========================================================================
class TestRelease:
    # v1.70.35: выпуск отгружен, точки синхронизации живут дальше.
    # Проверку «своя версия везде» для текущего выпуска ведёт test_v17035.
    # v1.70.41: точки синхронизации живут дальше; свою версию и
    # требование «своя запись первая» проверяет test_v17041.
    # v1.70.45: VERSION здесь — ТЕКУЩАЯ версия (класс сверяет точки
    # синхронизации, CHANGELOG и журнал), поэтому поднимается каждый выпуск.
    VERSION = "1.70.47"

    def test_all_sync_points(self):
        points = [
            ROOT / "app/config.py",
            INDEX,
            ROOT / "app/static/sw.js",
            ROOT / "app/static/manifest.webmanifest",
            MAXAPP,
        ]
        for p in points:
            assert self.VERSION in read(p), f"{p.name}: версия не обновлена"

    def test_stylesheet_cache_buster(self):
        html = read(INDEX)
        assert f"/css/app.css?v={self.VERSION}" in html

    def test_whats_new_entry(self):
        """v1.70.34: запись выпуска остаётся в истории WHATS_NEW.

        Требование «своя версия — первая» снято: оно по смыслу принадлежит
        текущему выпуску и проверяется в test_v17034, а здесь означало бы, что
        каждый следующий релиз обязан править этот тест.
        """
        js = read(APP_JS)
        assert "'1.70.33'" in js, "запись v1.70.33 пропала из истории WHATS_NEW"

    def test_changelog_entry(self):
        md = read(ROOT / "CHANGELOG.md")
        assert f"## [{self.VERSION}]" in md

    def test_journal_entry(self):
        """Журнал вёрстки — прямое требование владельца («вести лог журнал»)."""
        j = read(ROOT / "docs/ui_journal.md")
        assert self.VERSION in j
        assert "1.4.11" in j, "в журнале нет измеренного порога контраста кольца фокуса"
        assert "pointer: coarse" in j
