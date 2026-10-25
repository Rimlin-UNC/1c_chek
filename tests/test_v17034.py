"""v1.70.34 — кнопок чёрного цвета в программе нет; единый корпоративный оранжевый.

Что закрепляет набор (все числа измерены, метод — в docs/ui_journal.md):

1. Чёрная основная кнопка. `.btn-primary` была объявлена ДВАЖДЫ: в начале
   файла — фирменный оранжевый градиент `var(--grad)`, ниже — графит
   `#333333` с ховером `#454545`. Нижнее правило побеждало по порядку
   каскада, поэтому основная кнопка программы (92 применения: 4 в разметке и
   88 в шаблонах) рисовалась чёрной. Графит — остаток тёмной темы, отменённой
   в v1.25.0: там же, рядом, лежал комментарий «v1.25.0: тёмная тема удалена».

2. Белый текст на фирменном оранжевом. Шесть правил рисовали белый текст на
   `--grad`/`--accent`: `.btn-primary`, `.nav-badge.accent`, `.avatar`,
   `.segmented button.active`, `.step-num`, `.seg .btn.active`. Контраст
   2.11:1 (светлый стоп градиента #ff9a3d) … 2.61:1 (#ff7a00) при пороге
   4.5:1. Тёмный `#1a1a1a` даёт 6.66:1 и 8.24:1 — приём уже был закреплён в
   проекте для `.btn-accent` (test_v1210::test_accent_button_dark_text,
   «был #fff (2.3:1)»), то есть правило существовало, но применялось
   непоследовательно.

3. Терракота. `.btn-danger` — белый на `--bad #d9534f` = 3.96:1 (провал AA),
   а тёмный на нём = 4.39:1 (тоже провал): сам `--bad` для текста кнопки
   слишком светлый. Фон переведён на уже существовавший тёмный оттенок
   `--bad-text #c22e2a` → белый даёт 5.65:1. Насыщенность и «опасный» вид
   сохранены, токен палитры не изменён.

4. MAX-приложение. Запасной оранжевый был `#e8721c` — не корпоративный
   `#ff7a00`; белый текст на нём 3.06:1, оранжевый как текст на карточке
   3.06:1 и на фоне 2.72:1. Запасные значения приведены к фирменным, приоритет
   темы хоста (`--tg-theme-*`) сохранён — это контракт Mini App.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "app/static/css/app.css"
APP_JS = ROOT / "app/static/js/app.js"
INDEX = ROOT / "app/static/index.html"
MAXAPP = ROOT / "app/static/maxapp/index.html"

DARK_TEXT = "#1a1a1a"          # принятый в проекте текст на оранжевом
GRAD_STOPS = ("#ff7a00", "#ff9a3d")   # --grad 135deg: третья точка на 160% не видна

# Фирменная палитра закреплена владельцем и тестами v1190/v1250/v1583 —
# эти значения не должны меняться ни при какой правке кнопок.
BRAND = {
    "--accent": "#ff7a00",
    "--accent-2": "#4b0082",
    "--accent-text": "#9a4a00",
    "--ok": "#2e8b57",
    "--bad": "#d9534f",
    "--bg": "#f5f5f5",
    "--panel": "#ffffff",
    "--text": "#333333",
}


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def css_body() -> str:
    return re.sub(r"/\*.*?\*/", "", read(APP_CSS), flags=re.S)


def tokens() -> dict:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", css_body()))


def rules(body: str) -> list[tuple[str, str]]:
    """Плоские правила (селектор, объявления). Вложенные в @media тоже
    попадают: у них селектор — внутреннее правило."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        out.append((m.group(1).strip().replace("\n", " "), m.group(2)))
    return out


def declarations(selector: str) -> str:
    """Объединение объявлений всех правил селектора: у .btn-primary их было
    два, и именно поэтому «в файле есть градиент» не означало «кнопка
    оранжевая»."""
    found = [d for s, d in rules(css_body())
             if selector in [x.strip() for x in s.split(",")]]
    assert found, f"правила для {selector} в CSS нет"
    return " ".join(found)


def _lum(hx: str) -> float:
    hx = hx.strip().lstrip("#")
    if len(hx) == 3:
        hx = "".join(c * 2 for c in hx)
    ch = []
    for i in (0, 2, 4):
        c = int(hx[i:i + 2], 16) / 255
        ch.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, share_a: float) -> str:
    """color-mix(in srgb, A share, B) — то же, что делает браузер."""
    pa, pb = a.lstrip("#"), b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        va, vb = int(pa[i:i + 2], 16), int(pb[i:i + 2], 16)
        out.append(round(va * share_a + vb * (1 - share_a)))
    return "#%02x%02x%02x" % tuple(out)


# ==========================================================================
# 1. Чёрных кнопок нет
# ==========================================================================
class TestNoBlackButtons:
    def test_graphite_primary_is_gone(self):
        """Графит #333333 / ховер #454545 — это и есть «чёрные кнопки».
        Смотрим на код без комментариев: в комментарии к правке эти литералы
        названы специально."""
        body = css_body()
        assert "background: #333333" not in body
        assert "#454545" not in body
        assert tokens()["--text"] == "#333333", "цвет текста темы изменён"

    def test_primary_rule_is_single(self):
        """Два правила .btn-primary и были причиной: нижнее глушило верхнее."""
        body = css_body()
        own = [s for s, _ in rules(body)
               if ".btn-primary" in [x.strip() for x in s.split(",")]]
        assert own == [".btn-primary"], f"правил .btn-primary: {own}"

    def test_primary_is_corporate_gradient(self):
        d = declarations(".btn-primary")
        assert "background: var(--grad)" in d
        assert "color: #1a1a1a" in d
        assert "#fff" not in d, "белый текст на оранжевом вернулся"

    def test_gradient_is_the_brand_orange(self):
        css = read(APP_CSS)
        m = re.search(r"--grad:\s*([^;]+);", css)
        assert m, "--grad не объявлен"
        for stop in GRAD_STOPS:
            assert stop in m.group(1), f"в градиенте нет фирменного {stop}"

    def test_primary_is_actually_used(self):
        """Не-вакуумность: кнопка основная, её видят на каждом экране."""
        html = read(INDEX)
        js = read(APP_JS)
        # v1.70.37: на странице входа появилась пятая основная кнопка —
        # «Регистрация через MAX» в подсказке первого визита. Смысл проверки —
        # не-вакуумность (основная кнопка реально встречается в разметке),
        # поэтому точное количество больше не фиксируем.
        assert html.count("btn-primary") >= 4
        assert js.count("btn-primary") >= 80

    @pytest.mark.parametrize("sel", [
        ".btn", ".btn-primary", ".btn-accent", ".btn-ok", ".btn-bad",
        ".btn-danger", ".btn-icon", ".seg .btn.active",
        ".segmented button.active", ".nav-item", ".pm-item",
    ])
    def test_no_neutral_black_background_on_controls(self, sel):
        """«Чёрная кнопка» — это тёмный И ненасыщенный фон: графит #333333
        (светлота 0.033, хрома 0) и его ховер #454545 (0.060, 0). Тёмная
        терракота --bad-text #c22e2a (светлота 0.136, хрома 0.60) — не графит,
        а семантика опасного действия, и белый текст на ней даёт 5.65:1."""
        d = declarations(sel)
        bg = re.search(r"(?<![-\w])background(?:-color)?\s*:\s*([^;]+)", d)
        if not bg:
            return                                   # фон наследуется — не дефект
        val = bg.group(1)
        if val.strip() == "transparent":
            return          # прозрачный фон: элемент красится подложкой родителя
        colors = list(re.findall(r"#[0-9a-fA-F]{6}", val))
        for t in re.findall(r"var\((--[\w-]+)\)", val):
            if t != "--grad" and t in tokens():
                colors.append(tokens()[t])
        for hx in colors:
            rgb = [int(hx.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
            chroma = (max(rgb) - min(rgb)) / 255
            assert _lum(hx) >= 0.35 or chroma >= 0.12, \
                f"{sel}: чёрный/графитовый фон {hx} (L={_lum(hx):.3f}, хрома={chroma:.2f})"

    def test_camera_viewfinder_stays_dark(self):
        """Единственная законная тёмная поверхность — видоискатель камеры:
        на нём видно рамку наведения. Кнопкой не является."""
        d = declarations(".camera-viewport")
        assert "#05070f" in d


# ==========================================================================
# 2. Белый текст — только там, где проходит 4.5:1
# ==========================================================================
class TestWhiteText:
    def pairs(self):
        out = []
        for sel, dec in rules(css_body()):
            if not re.search(r"color:\s*#fff\b|color:\s*#ffffff\b", dec):
                continue
            bg = re.search(r"(?<![-\w])background(?:-color)?\s*:\s*([^;]+)", dec)
            if not bg:
                continue
            backs = list(GRAD_STOPS) if "--grad" in bg.group(1) else []
            for t in re.findall(r"var\((--[\w-]+)\)", bg.group(1)):
                if t in tokens():
                    backs.append(tokens()[t])
            backs += re.findall(r"#[0-9a-fA-F]{6}", bg.group(1))
            for b in backs:
                out.append((sel, b))
        return out

    def test_every_white_text_passes_aa(self):
        pairs = self.pairs()
        assert pairs, "белых текстов нет вовсе — проверка выродилась"
        bad = [(s, b, contrast("#ffffff", b)) for s, b in pairs
               if contrast("#ffffff", b) < 4.5]
        assert not bad, f"белый текст ниже 4.5:1: {[(s, b, round(r, 2)) for s, b, r in bad]}"

    def test_orange_backgrounds_no_longer_carry_white(self):
        """Не-вакуумность: до правки таких пар было шесть."""
        before = [(s, b) for s, b in self.pairs()
                  if b in GRAD_STOPS or b == tokens()["--accent"]]
        assert not before, f"белый на оранжевом остался: {before}"

    @pytest.mark.parametrize("sel", [
        ".btn-primary", ".nav-badge.accent", ".avatar",
        ".segmented button.active", ".step-num", ".seg .btn.active",
    ])
    def test_dark_text_on_orange_passes_aa(self, sel):
        d = declarations(sel)
        assert "color: #1a1a1a" in d, f"{sel}: тёмный текст не применён"
        bg = re.search(r"(?<![-\w])background(?:-color)?\s*:\s*([^;]+)", d).group(1)
        backs = list(GRAD_STOPS) if "--grad" in bg else [tokens()["--accent"]]
        for b in backs:
            r = contrast(DARK_TEXT, b)
            assert r >= 4.5, f"{sel}: {r:.2f}:1 на {b}"

    def test_danger_button_passes_aa(self):
        """Белый на --bad = 3.96:1, тёмный на --bad = 4.39:1 — оба провал,
        поэтому фон переведён на тёмный оттенок той же терракоты."""
        d = declarations(".btn-danger")
        assert "background: var(--bad-text)" in d
        assert "color: #fff" in d
        bg = tokens()["--bad-text"]
        assert contrast("#ffffff", bg) >= 4.5, contrast("#ffffff", bg)

    def test_danger_keeps_its_hue(self):
        """Кнопка обязана остаться узнаваемо «опасной», а не стать оранжевой:
        красный — семантика, а не оформление."""
        bad = tokens()["--bad"]
        bad_text = tokens()["--bad-text"]
        assert bad_text != tokens()["--accent"]
        assert abs(_lum(bad) - _lum(bad_text)) < 0.25, "оттенок уехал от терракоты"


# ==========================================================================
# 3. Палитра не изменена
# ==========================================================================
class TestBrandPaletteUntouched:
    @pytest.mark.parametrize("name,value", sorted(BRAND.items()))
    def test_token(self, name, value):
        assert tokens()[name] == value, f"{name} изменён: {tokens()[name]}"

    def test_accent_button_dark_text_still_pinned(self):
        """.btn-accent — образец, по которому выровнены остальные."""
        d = declarations(".btn-accent")
        assert "background: var(--accent)" in d
        assert "color: #1a1a1a" in d

    def test_no_dark_theme_returned(self):
        css = read(APP_CSS)
        assert "prefers-color-scheme: dark" not in css
        assert "color-scheme: dark" not in css
        assert "color-scheme: light" in css

    def test_soft_tints_are_still_orange_family(self):
        """Оттенки оранжевого, а не посторонние цвета: --accent-soft светлее
        --accent и того же тона (красный канал максимальный)."""
        t = tokens()
        soft = t["--accent-soft"]
        assert _lum(soft) > _lum(t["--accent"])
        r, g, b = (int(soft.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        assert r > g > b, f"--accent-soft {soft} не оранжевый оттенок"


# ==========================================================================
# 4. MAX-приложение: корпоративный оранжевый и тема хоста
# ==========================================================================
def maxapp_root() -> str:
    html = read(MAXAPP)
    style = re.search(r"<style>([\s\S]*?)</style>", html).group(1)
    return re.search(r":root\s*\{([\s\S]*?)\}", style).group(1)


def maxapp_style() -> str:
    return re.search(r"<style>([\s\S]*?)</style>", read(MAXAPP)).group(1)


def maxapp_fallback(var_name: str) -> str:
    m = re.search(re.escape(var_name) + r"\s*:\s*var\(--tg-theme-[\w-]+,\s*(#[0-9a-fA-F]{3,8})\)",
                  maxapp_root())
    assert m, f"{var_name}: нет запасного значения"
    return m.group(1)


class TestMaxAppBranding:
    def test_accent_fallback_is_corporate(self):
        """Был #e8721c — свой оранжевый, не фирменный."""
        assert maxapp_fallback("--accent") == "#ff7a00"

    def test_accent_fg_fallback_is_dark(self):
        """Белый на #e8721c давал 3.06:1, на #ff7a00 — 2.61:1."""
        fg = maxapp_fallback("--accent-fg")
        assert contrast(fg, maxapp_fallback("--accent")) >= 4.5

    def test_host_theme_still_wins(self):
        """Контракт Mini App: цвета хоста в приоритете, свои не навязываем."""
        root = maxapp_root()
        for host_var in ("--tg-theme-bg-color", "--tg-theme-text-color",
                         "--tg-theme-button-color", "--tg-theme-button-text-color",
                         "--tg-theme-hint-color", "--tg-theme-secondary-bg-color"):
            assert host_var in root, f"{host_var} больше не используется"

    def test_accent_as_text_is_a_separate_shade(self):
        """Один цвет не может быть и фоном кнопки с тёмным текстом (нужна
        светлота ≥0.22), и текстом на светлом фоне (нужна ≤0.18) — поэтому
        оттенок выводится примесью цвета текста темы и читается в обеих темах."""
        root = maxapp_root()
        m = re.search(r"--accent-text:\s*color-mix\(in srgb,\s*var\(--accent\)\s*(\d+)%,\s*var\(--fg\)\)", root)
        assert m, "нет --accent-text, выведенного из темы"
        share = int(m.group(1)) / 100
        accent = maxapp_fallback("--accent")
        fg = maxapp_fallback("--fg")
        derived = mix(accent, fg, share)
        for bg in (maxapp_fallback("--card"), maxapp_fallback("--bg")):
            r = contrast(derived, bg)
            assert r >= 4.5, f"оранжевый как текст на {bg}: {r:.2f}:1"

    def test_secondary_button_uses_the_shade(self):
        style = maxapp_style()
        sec = re.search(r"\.btn\.secondary\s*\{([^}]*)\}", style).group(1)
        assert "color: var(--accent-text)" in sec
        assert "border: 2px solid var(--accent-text)" in sec
        assert "var(--accent)" not in sec.replace("var(--accent-text)", "")

    def test_active_nav_item_uses_the_shade(self):
        style = maxapp_style()
        act = re.search(r"nav button\.active\s*\{([^}]*)\}", style).group(1)
        assert "color: var(--accent-text)" in act

    def test_hint_fallback_is_readable(self):
        """.hint — 13 px; #7d7693 давал 3.82:1 на --bg и 4.30:1 на --card."""
        hint = maxapp_fallback("--hint")
        for bg in (maxapp_fallback("--card"), maxapp_fallback("--bg")):
            assert contrast(hint, bg) >= 4.5, f"подсказка на {bg}: {contrast(hint, bg):.2f}:1"

    def test_main_text_is_readable(self):
        assert contrast(maxapp_fallback("--fg"), maxapp_fallback("--bg")) >= 4.5


class TestMaxAppDevices:
    """MAX-приложение открывается только на телефоне, поэтому его структура
    проверяется отдельно от основного интерфейса."""

    def test_viewport(self):
        html = read(MAXAPP)
        m = re.search(r'<meta name="viewport" content="([^"]*)"', html)
        assert m and "width=device-width" in m.group(1)
        assert "user-scalable=no" not in m.group(1)

    def test_inputs_are_16px(self):
        """Меньше 16px — iOS зумит страницу при фокусе на поле."""
        style = maxapp_style()
        inp = re.search(r"^input\s*\{([\s\S]*?)\}", style, re.M).group(1)
        assert "font-size: 16px" in inp

    def test_safe_area_under_fixed_nav(self):
        style = maxapp_style()
        nav = re.search(r"^nav\s*\{([\s\S]*?)\}", style, re.M).group(1)
        assert "position: fixed" in nav and "bottom: 0" in nav
        assert "env(safe-area-inset-bottom)" in nav, \
            "нижняя панель уползает под индикатор дома"

    def test_body_leaves_room_for_the_nav(self):
        """Контент не должен прятаться под фиксированной нижней панелью."""
        style = maxapp_style()
        body = re.search(r"^body\s*\{([\s\S]*?)\}", style, re.M).group(1)
        m = re.search(r"padding:\s*[\d.]+px\s+[\d.]+px\s+([\d.]+)px", body)
        assert m and float(m.group(1)) >= 80, f"снизу всего {m.group(1) if m else 0}px"

    def test_touch_targets(self):
        """Высота цели = верхний и нижний padding плюс высота строки, а не
        размер шрифта: у body задан line-height 1.45, и именно он растягивает
        пункт нижней панели до 45.8px при font-size 15px."""
        style = maxapp_style()
        body = re.search(r"^body\s*\{([\s\S]*?)\}", style, re.M).group(1)
        lh = float(re.search(r"font:\s*[\d.]+px/([\d.]+)", body).group(1))

        def height(block: str) -> float:
            pad = float(re.search(r"padding:\s*([\d.]+)px", block).group(1))
            size = float(re.search(r"font-size:\s*([\d.]+)px", block).group(1))
            return 2 * pad + size * lh

        btn = height(re.search(r"^\.btn\s*\{([\s\S]*?)\}", style, re.M).group(1))
        nav = height(re.search(r"nav button\s*\{([\s\S]*?)\}", style).group(1))
        assert btn >= 44, f"кнопка {btn:.1f}px — ниже 44px"
        assert nav >= 44, f"пункт нижней панели {nav:.1f}px — ниже 44px"

    def test_fluid_widths(self):
        """Одна колонка на всю ширину — @media не нужны, но ширины обязаны
        быть относительными, иначе на узком экране появится прокрутка."""
        style = maxapp_style()
        assert "width: 100%" in style
        assert not re.search(r"width:\s*[3-9]\d\dpx", style), \
            "в MAX-приложении появилась фиксированная ширина ≥300px"


# ==========================================================================
# 5. Дубли правил одного селектора (скан с учётом контекста at-правил)
# ==========================================================================
def rules_with_context():
    """Правила вместе с контекстом: (цепочка at-правил, селектор, строка, объявления).

    Без контекста скан даёт ~170 «конфликтов», потому что считает штатное
    переопределение внутри @media и шаги разных @keyframes противоречиями.
    С контекстом остаётся 11 — и среди них был настоящий дефект.
    """
    clean = re.sub(r"/\*.*?\*/",
                   lambda m: "\n" * m.group(0).count("\n"), read(APP_CSS), flags=re.S)
    out, stack, buf, i, n = [], [], "", 0, len(clean)
    while i < n:
        ch = clean[i]
        if ch == "{":
            sel = re.sub(r"\s+", " ", buf.strip())
            if sel.startswith("@"):
                stack.append(sel)
                buf = ""
            else:
                depth, j = 1, i + 1
                while j < n and depth:
                    if clean[j] == "{":
                        depth += 1
                    elif clean[j] == "}":
                        depth -= 1
                    j += 1
                props = {}
                for d in clean[i + 1:j - 1].split(";"):
                    if ":" in d:
                        k, v = d.split(":", 1)
                        props[k.strip()] = re.sub(r"\s+", " ", v.strip())
                out.append((tuple(stack), sel, clean[:i].count("\n") + 1, props))
                buf = ""
                i = j
                continue
        elif ch == "}":
            if stack:
                stack.pop()
            buf = ""
        else:
            buf += ch
        i += 1
    return out


def conflicts() -> list[tuple[str, str, tuple, int, int]]:
    """Пары (селектор, свойство, контекст, первая строка, вторая строка), где
    одно и то же свойство одного селектора в ОДНОМ контексте задано дважды
    разными значениями. Действует нижнее — верхнее мёртво."""
    seen: dict = {}
    out = []
    for ctx, sel, line, props in rules_with_context():
        if sel in ("from", "to") or sel.endswith("%"):
            continue                      # шаги разных @keyframes — не конфликт
        for p, v in props.items():
            key = (ctx, sel, p)
            if key in seen and seen[key][0] != v:
                out.append((sel, p, ctx, seen[key][1], line))
            elif key not in seen:
                seen[key] = (v, line)
    return out


class TestDuplicateRules:
    def test_form_grid_is_single_column_on_phones(self):
        """Главный найденный дефект вёрстки этого выпуска.

        Блок «Средние экраны: 3 колонки формы ужимаем в 2» задавал
        `.form-grid { grid-template-columns: 1fr 1fr }` при том же
        `max-width: 860px`, что и мобильный блок с `1fr`. Он был написан, когда
        база давала три колонки; база давно две, поэтому блок только глушил
        мобильное правило — форма не становилась одноколоночной ни на одной
        ширине, включая телефон 360 px.
        """
        base = declarations(".form-grid")
        assert "grid-template-columns: 1fr 1fr" in base
        mobile = [(ctx, props) for ctx, sel, _ln, props in rules_with_context()
                  if sel == ".form-grid" and ctx]
        assert len(mobile) == 1, f"правил .form-grid в media-контекстах: {len(mobile)}"
        ctx, props = mobile[0]
        assert ctx == ("@media (max-width: 860px)",)
        assert props["grid-template-columns"] == "1fr"

    def test_empty_state_padding_declared_once(self):
        """48px 20px был мёртв: ниже то же правило задаёт 18px 10px."""
        pads = [props["padding"] for _ctx, sel, _l, props in rules_with_context()
                if sel == ".empty-state" and "padding" in props]
        assert pads == ["18px 10px"], pads

    def test_body_font_family_is_the_token(self):
        """Стек был задан дважды: литералом в шапке и токеном в разделе
        типографики — значения совпадали посимвольно, то есть одно из двух
        было мёртвым. Оставлен токен в одном месте."""
        fams = [props["font-family"] for _ctx, sel, _l, props in rules_with_context()
                if sel == "body" and "font-family" in props]
        assert fams == ["var(--font-main)"], fams

    def test_conflict_ratchet(self):
        """Трещотка: противоречивых дублей правил в одном контексте нет вовсе.

        В v1.70.34 одиннадцать таких пар были записаны как допустимые (мёртвые
        ранние объявления, где нижнее правило осознанно уточняет верхнее).
        В v1.70.35 все одиннадцать вычищены, поэтому допуск сужен до нуля:
        новое противоречие уронит тест. Именно этот класс дефекта трижды давал
        реальные баги — цвет кольца фокуса, мёртвые тач-цели и чёрная кнопка.
        """
        got = {(sel, prop) for sel, prop, _ctx, _a, _b in conflicts()}
        assert not got, f"противоречивые дубли правил: {sorted(got)}"


# ==========================================================================
# 5. Релиз
# ==========================================================================
class TestRelease:
    # v1.70.35: точки синхронизации живут дальше, свою версию проверяет
    # test_v17035; здесь остаётся соответствие файлов друг другу.
    # v1.70.41: точки синхронизации живут дальше; свою версию и
    # требование «своя запись первая» проверяет test_v17041.
    VERSION = "1.70.58"

    def test_all_sync_points(self):
        for p in (ROOT / "app/config.py", INDEX, ROOT / "app/static/sw.js",
                  ROOT / "app/static/manifest.webmanifest", MAXAPP):
            assert self.VERSION in read(p), f"{p.name}: версия не обновлена"

    def test_cache_busters(self):
        assert f"/css/app.css?v={self.VERSION}" in read(INDEX)
        assert f"/js/app.js?v={self.VERSION}" in read(INDEX)
        assert f"maxapp.js?v={self.VERSION}" in read(MAXAPP)

    def test_whats_new_is_first(self):
        """v1.70.35: требование «своя версия первая» снято — оно принадлежит
        текущему выпуску и проверяется в test_v17035, а здесь означало бы, что
        каждый следующий релиз обязан править этот тест. Запись остаётся."""
        js = read(APP_JS)
        assert "'1.70.34'" in js, "запись v1.70.34 пропала из истории WHATS_NEW"

    def test_whats_new_names_the_defect(self):
        js = read(APP_JS)
        entry = js[js.index("'1.70.34':"):][:6000]
        for needle in ("чёрной", "333333", "6.66"):
            assert needle in entry, f"в описании выпуска нет {needle!r}"

    def test_changelog_entry(self):
        assert f"## [{self.VERSION}]" in read(ROOT / "CHANGELOG.md")

    def test_journal_entry(self):
        j = read(ROOT / "docs/ui_journal.md")
        assert self.VERSION in j
        for needle in ("#333333", "2.11", "6.66", "#e8721c"):
            assert needle in j, f"в журнале нет {needle!r}"
