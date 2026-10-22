r"""v1.70.35 — текст на сайте читаем, CSS валиден и соответствует разметке.

Задача выпуска (формулировка владельца): «кнопки получили оранжевый фон… но
важно, чтобы читаемый текст на сайте был виден; продумай CSS, чтобы он был
валидный и соответствовал HTML».

Что закрепляет набор:

1. **Читаемость как инвариант, а не как список мест.** Проверяется КАЖДОЕ
   объявление `color` в `app.css`: контраст с собственным фоном правила либо с
   худшей реальной поверхностью темы обязан быть ≥ 4.5:1, для крупного текста
   ≥ 3.0:1 (WCAG 1.4.3). До правки таких мест было восемь:

   | Правило | Было | Стало |
   |---|---|---|
   | `.viewas-warn` | `#f59e0b` — 2.15:1 | `var(--warn-text)` — 5.92:1 |
   | `.ao1-sig`, `.ao1-under` | `#8a8a8a` 9.5px — 3.45:1 | `var(--text-faint)` 10.5px — 5.33:1 |
   | `.echeck-sub`, `.echeck-items th` | `#8a7a5e` — 4.11:1 | `#6f6250` — 5.84:1 |
   | `.echeck-head` | `#b45309` — 4.94:1 | `var(--warn-text)` — 5.83:1 |
   | `.land-badge` | `#b45309` — 4.17:1 | `var(--warn-text)` — 4.91:1 |
   | `.land-accent` | `#c2410c` — 4.75:1 | `var(--accent-text)` — 5.74:1 |
   | `.snav-cnt` | `--text-faint` на `--bg-soft` — 4.51:1 | `var(--text-dim)` — 5.66:1 |
   | `.ao-foot` | `#666` 9.5px — 5.74:1 | `var(--text-dim)` 10.5px — 6.69:1 |

2. **Цвета, заданные из JS, audited наравне с CSS.** Аудит одного `app.css`
   не видит цветов, которые подставляют шаблоны: их 21, и до правки девять не
   проходили порог. Худшие — предупреждения в карточке контрагента
   (`#fbbf24` 1.67:1 и `#f87171` 2.77:1) и зелёный `var(--ok)` как текст
   (4.25:1) в пяти местах.

3. **Графики были размечены под отменённую тёмную тему.** `charts.js` рисовал
   сетку `rgba(255,255,255,.07)` и кольцо пончика `rgba(255,255,255,.06)` —
   на белой карточке это 1.00:1, то есть невидимо, а заголовок в центре
   пончика `#e8ecf8` давал 1.18:1: число «всего чеков» на дашборде нельзя
   было прочесть. Цвета вынесены в CSS-классы, чтобы тема и аудит до них
   дотягивались.

4. **CSS соответствует дизайн-системе.** Двадцать объявлений цвета текста были
   заданы литералом, равным токену (`#333` ×12, `#333333` ×8, `#6b6b6b`,
   `#4B0082`) — палитра жила в двух местах.

5. **Валидность внешним валидатором.** `stylelint` 16 с конфигом репозитория
   (`.stylelintrc.json`, только правила корректности) даёт 0 замечаний по
   `app.css` и по встроенным `<style>` MAX-приложения и брендбука. До правки
   было 21.

6. **Мёртвых правил нет.** Удалены 10 объявлений, перекрытых правилом ниже,
   слиты два соседних блока `@media (max-width: 920px)` и удалены 11 классов,
   не встречающихся ни в разметке, ни в шаблонах JS, ни в Python.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from tests.test_v17034 import (APP_CSS, APP_JS, INDEX, MAXAPP, conflicts,
                               contrast, declarations, mix, read,
                               rules_with_context, tokens)

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "app/static/js"
BRANDBOOK = ROOT / "brand/brandbook.html"
CONFIG = ROOT / ".stylelintrc.json"

# Реальные поверхности темы, на которых лежит текст. Худшая из них и задаёт
# порог: если цвет проходит на самой тёмной подложке, он проходит везде.
SURFACES = {
    "--panel": "#ffffff",
    "--bg": "#f5f5f5",
    "--bg-soft": "#ececec",
    "--accent-soft": "#fff1e3",
    ".echeck (бумага чека)": "#fffdf8",
}
GRAD_STOPS = ("#ff7a00", "#ff9a3d")

# Классы-хуки: стиль им не нужен, по ним JS переключает `hidden`.
JS_HOOK_CLASSES = {"admin-only", "accountant-only", "boss-only", "pool-only"}

# Удалённые мёртвые классы — не должны вернуться.
DELETED_DEAD = ("ao-preview", "catalog-grid", "cc-actions", "manual-hero",
                "mapping-grid", "mapping-head", "mg-label", "print-cut",
                "rcpt-totals")

# Остатки отменённой тёмной темы и палитры Tailwind в графиках.
DARK_THEME_LEFTOVERS = ("#6b7494", "#e8ecf8", "rgba(255,255,255,.07)",
                        "rgba(255,255,255,.06)", "#6366f1", "#22d3ee")
TAILWIND = ("#6366f1", "#22d3ee", "#34d399", "#fbbf24", "#f87171",
            "#c084fc", "#60a5fa", "#e5770f", "#c0392b", "#2471a3")


def css_code() -> str:
    """CSS без комментариев: литералы в пояснениях к правкам не должны
    попадать в проверки (на этом горели четыре выпуска подряд)."""
    return re.sub(r"/\*.*?\*/", "", read(APP_CSS), flags=re.S)


def js_code(path: pathlib.Path) -> str:
    r"""JS без ПОСТРОЧНЫХ комментариев — по той же причине, что и css_code().

    Блочные комментарии здесь снимать НЕЛЬЗЯ: в шаблонных строках app.js
    встречаются последовательности `*/`, и регулярка `/\*.*?\*/` с флагом S
    съедала 147 179 символов настоящего кода (26 % файла) — после чего скан
    «класс не применяется» находил 17 живых классов, а подсчёт токенов терял
    три из пяти. Все пояснения к правкам в этом проекте пишутся построчно.
    """
    return re.sub(r"^\s*//.*$", "", read(path), flags=re.M)


def _resolve(value: str):
    """Значение цвета → hex. Понимает токен, hex и color-mix с прозрачностью
    поверх белой карточки (так устроены подложки статусных чипов)."""
    value = value.strip().strip("'\"")
    tok = tokens()
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        return tok.get(m.group(1))
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value
    m = re.match(r"color-mix\(in srgb,\s*var\((--[\w-]+)\)\s*(\d+)%,\s*"
                 r"(transparent|var\(--[\w-]+\))\)", value)
    if m:
        base = tok.get(m.group(1))
        share = int(m.group(2)) / 100
        other = "#ffffff" if m.group(3) == "transparent" else tok.get(
            re.fullmatch(r"var\((--[\w-]+)\)", m.group(3)).group(1))
        if base and other:
            return mix(base, other, share)
    return None


def _composite(rgba: str, base: str = "#ffffff"):
    """rgba(r,g,b,a) поверх base — подложки тостов, модалок, плашек."""
    m = re.match(r"rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)", rgba)
    if not m:
        return None
    r, g, b, a = (float(x) for x in m.groups())
    bb = [int(base.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(f * a + bb[i] * (1 - a))
                                   for i, f in enumerate((r, g, b)))


def _font_size(props: dict) -> float:
    """Размер шрифта правила; для clamp() берётся минимальный — худший случай."""
    fs = props.get("font-size", "")
    px = [float(x) for x in re.findall(r"([\d.]+)px", fs)]
    rem = [float(x) * 16 for x in re.findall(r"([\d.]+)rem", fs)]
    return min(px + rem) if (px or rem) else 15.0


def text_pairs():
    """Все пары «цвет текста + поверхность» из app.css с порогом WCAG."""
    out = []
    for _ctx, sel, _ln, props in rules_with_context():
        if sel in ("from", "to") or sel.endswith("%"):
            continue
        raw = props.get("color")
        if not raw or raw in ("inherit", "transparent", "currentColor"):
            continue
        ink = _resolve(raw)
        if ink is None:
            continue
        size = _font_size(props)
        weight = props.get("font-weight", "")
        bold = weight == "bold" or (weight.isdigit() and int(weight) >= 700)
        threshold = 3.0 if (size >= 24 or (size >= 18.66 and bold)) else 4.5
        bg_raw = props.get("background") or props.get("background-color")
        backs = []
        if bg_raw:
            if "--grad" in bg_raw:
                backs += list(GRAD_STOPS)
            else:
                got = _resolve(bg_raw) or _composite(bg_raw)
                if got:
                    backs.append(got)
        if not backs:
            backs = list(SURFACES.values())
        out.append((min(contrast(ink, b) for b in backs), threshold, sel, raw, ink, size))
    return out


def js_colors():
    """Цвета, которые подставляют шаблоны JS: (порог, контраст, место, свойство, значение)."""
    out = []
    for path in sorted(JS_DIR.glob("*.js")):
        for i, line in enumerate(js_code(path).split("\n"), 1):
            for m in re.finditer(r"(?<![-\w])(color|fill|stroke|border|border-left|"
                                 r"border-top|border-bottom|border-right|border-color)"
                                 r"\s*:\s*([^;\"'`}]+)", line):
                prop, val = m.group(1), m.group(2)
                hexv = _resolve(val)
                if not hexv:
                    continue
                text = prop in ("color",) or (prop == "fill" and
                                              ("<text" in line or "text-anchor" in line))
                thr = 4.5 if text else 3.0
                out.append((thr, contrast(hexv, "#ffffff"), f"{path.name}:{i}", prop, val.strip()))
            for m in re.finditer(r"\.style\.(color)\s*=\s*['\"]([^'\"]+)['\"]", line):
                hexv = _resolve(m.group(2))
                if hexv:
                    out.append((4.5, contrast(hexv, "#ffffff"), f"{path.name}:{i}",
                                ".style.color", m.group(2)))
    return out


class TestScannerIntegrity:
    """Сканер не должен терять код: иначе он «находит» несуществующие дефекты
    и пропускает настоящие. Обе ошибки этого выпуска были именно такими."""

    def test_js_comment_strip_keeps_the_code(self):
        r"""Регресс на собственную ошибку: снятие блочных комментариев регуляркой
        /\*.*?\*/ с флагом S съедало 147 179 символов кода app.js (26 %),
        потому что в шаблонных строках встречаются последовательности `*/`.
        Сканер после этого «находил» 17 живых классов как мёртвые и терял три
        из пяти вхождений токена."""
        stripped = js_code(APP_JS)
        for marker in ("item-row", "whats-new", "dropzone", "pool-off-banner"):
            assert marker in stripped, f"{marker} потерян при снятии комментариев"
        assert stripped.count("var(--ok-text)") == 5
        # в app.js законно 6.4 % построчных комментариев; при ошибке с блочными
        # комментариями оставалось 74 %, поэтому порог 0.90 различает их
        assert len(stripped) >= 0.90 * len(read(APP_JS))

    def test_css_comment_strip_keeps_the_rules(self):
        """В CSS блочные комментарии снимать безопасно.

        Равенство числа скобок с исходником здесь НЕ верно и не должно быть:
        в пояснении к правке v1.70.35 процитировано правило с фигурными
        скобками, и оно снимается вместе с комментарием. Инвариант другой —
        скобки остаются сбалансированными, то есть ни одно правило не исчезло
        наполовину.
        """
        code = css_code()
        assert code.count("{") == code.count("}")
        assert code.count("{") >= 700, f"правил подозрительно мало: {code.count('{')}"


# ==========================================================================
# 1. Читаемость текста в CSS
# ==========================================================================
class TestTextIsReadable:
    def test_every_text_color_passes_wcag(self):
        pairs = text_pairs()
        assert len(pairs) > 100, f"пар собрано {len(pairs)} — скан выродился"
        bad = [(round(r, 2), t, sel, ink, size) for r, t, sel, _raw, ink, size in pairs
               if r < t]
        assert not bad, f"текст ниже порога: {bad}"

    def test_audit_is_not_vacuous(self):
        """Контроль: скан действительно видит цвета. Янтарный #f59e0b на белой
        панели — 2.15:1, серый #8a8a8a — 3.45:1; вернись они как ЦВЕТ ТЕКСТА,
        проверка выше обязана упасть."""
        assert contrast("#f59e0b", "#ffffff") < 4.5
        assert contrast("#8a8a8a", "#ffffff") < 4.5
        assert not re.search(r"(?<![-\w])color:\s*(?:#f59e0b|#8a8a8a|#8a7a5e|#c2410c|#b45309)",
                             css_code())

    @pytest.mark.parametrize("sel,token", [
        (".viewas-warn", "--warn-text"),
        (".ao1-sig", "--text-faint"),
        (".ao1-under", "--text-faint"),
        (".ao-foot", "--text-dim"),
        (".echeck-head", "--warn-text"),
        (".land-badge", "--warn-text"),
        (".land-accent", "--accent-text"),
        (".snav-cnt", "--text-dim"),
    ])
    def test_fixed_sites_use_the_token(self, sel, token):
        d = declarations(sel)
        assert f"color: var({token})" in d, f"{sel}: цвет не переведён на {token}"
        assert contrast(tokens()[token], "#ffffff") >= 4.5

    @pytest.mark.parametrize("sel", [".echeck-sub", ".echeck-items th"])
    def test_receipt_ink_on_paper(self, sel):
        """Чек — реплика бумаги: фон #fffdf8, поэтому чернила проверяются на
        нём. Прежний #8a7a5e давал 4.11:1."""
        d = declarations(sel)
        assert "color: #6f6250" in d
        assert contrast("#6f6250", "#fffdf8") >= 4.5

    @pytest.mark.parametrize("sel", [".ao1-sig", ".ao1-under", ".ao-foot"])
    def test_document_captions_are_not_below_ten_px(self, sel):
        """Подписи в акте были 9.5px. Документы НЕ внутри @media print — они
        показываются на экране в окне предпросмотра."""
        size = float(re.search(r"font-size:\s*([\d.]+)px", declarations(sel)).group(1))
        assert size >= 10.5, f"{sel}: {size}px"

    def test_no_text_smaller_than_ten_px(self):
        small = []
        for _ctx, sel, _ln, props in rules_with_context():
            if "font-size" not in props or "color" not in props:
                continue
            m = re.fullmatch(r"([\d.]+)px", props["font-size"].strip())
            if m and float(m.group(1)) < 10:
                small.append((sel, props["font-size"]))
        assert not small, f"текст мельче 10px: {small}"

    def test_placeholder_is_readable(self):
        d = declarations("input::placeholder")
        assert "color: var(--text-faint)" in d
        assert contrast(tokens()["--text-faint"], "#ffffff") >= 4.5

    def test_faint_text_never_sits_on_soft_background(self):
        """--text-faint на --bg-soft даёт 4.51:1 — запас 0.01. Такое сочетание
        запрещено: либо поверхность светлее, либо текст темнее."""
        for _ctx, sel, _ln, props in rules_with_context():
            if props.get("color", "").strip() != "var(--text-faint)":
                continue
            bg = (props.get("background") or props.get("background-color") or "").strip()
            assert bg != "var(--bg-soft)", f"{sel}: --text-faint на --bg-soft"

    def test_disabled_states_are_declared_not_faked(self):
        """Неактивные элементы exempt по WCAG 1.4.3, но глушить их разрешено
        только прозрачностью и курсором, а не цветом текста."""
        d = declarations(".btn:disabled")
        assert "opacity" in d and "cursor: not-allowed" in d
        assert "color" not in d


# ==========================================================================
# 2. Нетекстовый контраст: индикаторы состояния
# ==========================================================================
class TestNonTextContrast:
    """Порог 3.00:1 (WCAG 1.4.11) для графики, которая передаёт состояние."""

    @pytest.mark.parametrize("sel", [".pm-dot.acc", ".pm-dot.usr", ".live-dot",
                                     ".live-dot.on", ".live-dot.off",
                                     ".upd-dot--ok", ".upd-dot--err",
                                     ".upd-dot--new", ".upd-dot--wait"])
    def test_status_dots_are_visible(self, sel):
        """Точки смысла несут: в меню профиля они показывают роль и блокировку,
        в шапке — состояние обновления. До правки .pm-dot.acc был #f59e0b
        (2.15:1), а .pm-dot.usr — #22c55e (2.28:1), остаток палитры Tailwind."""
        d = declarations(sel)
        bg = re.search(r"(?<![-\w])background:\s*([^;]+)", d)
        assert bg, f"{sel}: точка без фона"
        hexv = _resolve(bg.group(1))
        assert hexv, f"{sel}: фон не разбирается ({bg.group(1)})"
        assert contrast(hexv, "#ffffff") >= 3.0, f"{sel}: {contrast(hexv, '#ffffff'):.2f}:1"

    def test_status_dots_use_brand_tokens(self):
        for sel, token in ((".pm-dot.acc", "--warn"), (".pm-dot.usr", "--ok")):
            assert f"background: var({token})" in declarations(sel), sel

    def test_focus_ring_is_visible(self):
        """Кольцо фокуса — тоже нетекстовый индикатор (порог 3:1)."""
        d = declarations(":focus-visible")
        assert "outline: 2px solid var(--accent-text)" in d
        assert contrast(tokens()["--accent-text"], "#ffffff") >= 3.0

    def test_badge_outline_not_required(self):
        """.nav-badge — счётчик: информацию несёт цифра (--text-dim, 6.69:1),
        а белая плашка на полупрозрачном сайдбаре — не индикатор состояния.
        Проверка фиксирует это решение, чтобы скан не «чинил» его впустую."""
        d = declarations(".nav-badge")
        assert "color: var(--text-dim)" in d
        assert contrast(tokens()["--text-dim"], "#ffffff") >= 4.5


# ==========================================================================
# 3. Цвета, заданные из JS
# ==========================================================================
class TestColorsSetFromJs:
    def test_every_js_color_passes(self):
        found = js_colors()
        assert len(found) >= 15, f"найдено {len(found)} — скан выродился"
        bad = [(round(r, 2), t, where, prop, val) for t, r, where, prop, val in found if r < t]
        assert not bad, f"цвета из JS ниже порога: {bad}"

    def test_audit_is_not_vacuous(self):
        """Контроль: янтарный #fbbf24 как текст — 1.67:1. Вернись он в шаблон,
        проверка выше обязана упасть."""
        assert contrast("#fbbf24", "#ffffff") < 4.5
        code = js_code(APP_JS)
        for bad in ("#fbbf24", "#f87171", "#e5770f", "#c0392b", "#2471a3", "#777"):
            assert bad not in code, f"в коде app.js остался {bad}"

    def test_counterparty_warnings_are_readable(self):
        """«Адрес признан недостоверным» и «Массовый адрес» — предупреждения
        из ЕГРЮЛ, их обязан быть видно: 2.77:1 и 1.67:1 → 5.65:1 и 5.92:1."""
        code = js_code(APP_JS)
        assert 'style="color:var(--bad-text)">⚠ Адрес признан недостоверным' in code
        assert 'style="color:var(--warn-text)">⚠ Массовый адрес' in code

    def test_green_as_text_uses_the_text_token(self):
        """--ok #2e8b57 как ТЕКСТ даёт 4.25:1 — ниже 4.5. Текст-токен --ok-text
        #27764a даёт 5.56:1. Правило из v1.70.32, которое не распространялось
        на цвета, подставляемые из JS."""
        code = js_code(APP_JS)
        # --ok законен как ГРАФИКА (полоса светофора, заливка узла графа —
        # порог 3:1, у --ok 4.07–4.25:1), но не как текст
        assert not re.search(r"(?<![-\w])color\s*:\s*var\(--ok\)", code), \
            "--ok использован как цвет текста (4.25:1 < 4.5)"
        assert not re.search(r"\.style\.color\s*=\s*'var\(--ok\)'", code)
        assert code.count("var(--ok-text)") >= 5, "зелёный текст не переведён на --ok-text"

    def test_risk_traffic_light_uses_tokens(self):
        """Цвет светофора контрагента идёт в border-left: 4px solid — порог 3:1.
        Жёлтый #fbbf24 давал 1.67:1, красный #f87171 — 2.77:1."""
        code = js_code(APP_JS)
        block = code[code.index("const RISK = {"):][:600]
        for token in ("var(--ok)", "var(--warn)", "var(--bad)"):
            assert token in block, f"в светофоре нет {token}"
        for token, thr in (("--ok", 3.0), ("--warn", 3.0), ("--bad", 3.0)):
            assert contrast(tokens()[token], "#ffffff") >= thr

    def test_fraud_graph_uses_tokens_and_style(self):
        """Граф связей: узлы, рёбра и легенда. Оранжевый #e5770f давал 2.88:1
        на канве и 3.01:1 как текст легенды, запасной цвет ребра #999 — 2.73:1.
        var() перенесён из атрибутов в style: в presentation-атрибутах он
        разрешается не во всех браузерах."""
        code = js_code(APP_JS)
        m = re.search(r"const FRAUD_EDGE_COLORS = \{([^}]*)\}", code)
        assert m, "карта цветов рёбер не найдена"
        for token in ("var(--bad-text)", "var(--warn)", "var(--info)"):
            assert token in m.group(1), f"в рёбрах нет {token}"
        assert "'var(--text-faint)'" in code, "запасной цвет ребра не на токене"
        assert 'style="stroke:${c}"' in code
        assert 'style="fill:${fill}"' in code
        fill = re.search(r"const fill = ([^;]+);", code).group(1)
        for token in ("var(--bad-text)", "var(--warn)", "var(--ok)"):
            assert token in fill, f"в узлах нет {token}"

    def test_progress_bar_fill_contrasts_the_track(self):
        """Полоса прогресса против дорожки: #e5770f на #e8e8e8 давало 2.6:1."""
        code = js_code(APP_JS)
        assert "background:var(--bg-soft);border-radius:6px;height:10px" in code
        assert "background:var(--accent-text);height:100%" in code
        r = contrast(tokens()["--accent-text"], tokens()["--bg-soft"])
        assert r >= 3.0, f"{r:.2f}:1"

    def test_no_var_fallbacks_in_js(self):
        """Запасные значения var() прячут необъявленный токен. Так было дважды:
        var(--line,#eef0f4) — токена --line в app.css нет вовсе (он объявлен
        только в брендбуке), и var(--ok, #34d399) — с Tailwind-зелёным."""
        for path in sorted(JS_DIR.glob("*.js")):
            code = js_code(path)
            found = re.findall(r"var\(--[\w-]+\s*,[^)]*\)", code)
            assert not found, f"{path.name}: запасные значения {found}"

    def test_standalone_documents_use_literals(self):
        """Выгружаемый HTML и окно печати — автономные документы без наших
        токенов, поэтому там литералы; но порог они обязаны проходить.
        #777 давал 4.48:1, заменён на значение --text-faint."""
        code = js_code(APP_JS)
        assert code.count("color:#6b6b6b") >= 2
        assert "#777" not in code
        assert contrast("#6b6b6b", "#ffffff") >= 4.5
        assert tokens()["--text-faint"] == "#6b6b6b", \
            "литерал в автономном документе разошёлся с токеном"


# ==========================================================================
# 4. Графики: цвета в CSS, а не в JS
# ==========================================================================
class TestChartsAreThemed:
    CHART_CLASSES = ("chart-grid", "chart-track", "chart-axis", "chart-center",
                     "chart-center-sub", "chart-bar-from", "chart-bar-to")

    @pytest.mark.parametrize("name", CHART_CLASSES)
    def test_class_exists_in_both(self, name):
        charts = js_code(ROOT / "app/static/js/charts.js")
        assert f'class="{name}"' in charts, f"{name} не применяется в charts.js"
        assert f".{name}" in css_code(), f"{name} не объявлен в app.css"

    def test_no_dark_theme_leftovers(self):
        """charts.js был размечен под тёмную тему, отменённую в v1.25.0."""
        charts = js_code(ROOT / "app/static/js/charts.js")
        for bad in DARK_THEME_LEFTOVERS:
            assert bad not in charts, f"в charts.js остался {bad}"

    def test_chart_text_uses_theme_tokens(self):
        css = css_code()
        assert ".chart-center { fill: var(--text); }" in css
        assert "fill: var(--text-faint)" in css
        assert contrast(tokens()["--text"], "#ffffff") >= 4.5
        # было: #e8ecf8 на белой карточке — 1.18:1, число «всего чеков» не читалось
        assert contrast("#e8ecf8", "#ffffff") < 1.5

    def test_series_palette_is_brand_and_passes(self):
        """Палитра серий была палитрой Tailwind: шесть цветов из семи ниже 3:1."""
        charts = js_code(ROOT / "app/static/js/charts.js")
        m = re.search(r"const PALETTE = \[([^\]]*)\]", charts)
        assert m, "PALETTE не найдена"
        colors = re.findall(r"'(#[0-9a-fA-F]{6})'", m.group(1))
        assert len(colors) == 7, colors
        tok_values = set(tokens().values())
        for c in colors:
            assert c in tok_values, f"{c} не является значением фирменного токена"
            assert contrast(c, "#ffffff") >= 3.0, f"{c}: {contrast(c, '#ffffff'):.2f}:1"
        for bad in TAILWIND:
            assert bad not in charts, f"в charts.js остался {bad}"

    def test_donut_segments_match_status_language(self):
        """Сектора пончика статусов ФНС — те же токены, что у чипов в списке
        чеков: язык цвета один и там, и там."""
        code = js_code(APP_JS)
        block = code[code.index("{ label: 'Действителен'"):][:900]
        for token in ("#2e8b57", "#b45309", "#d9534f", "#6b6b6b"):
            assert token in block, f"в сегментах пончика нет {token}"
        assert tokens()["--ok"] == "#2e8b57" and tokens()["--warn"] == "#b45309"
        assert tokens()["--bad"] == "#d9534f" and tokens()["--text-faint"] == "#6b6b6b"

    def test_charts_still_parse(self):
        """Правка шаблонов не должна сломать синтаксис (шаблонные строки)."""
        r = subprocess.run(["node", "--check", str(ROOT / "app/static/js/charts.js")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:400]
        r = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:400]


# ==========================================================================
# 5. CSS соответствует дизайн-системе
# ==========================================================================
class TestColorsFollowTokens:
    def test_no_literal_duplicates_a_token(self):
        """Цвет текста, равный токену, обязан быть токеном: иначе палитра живёт
        в двух местах и правка токена не доходит до экрана."""
        by_value = {}
        for name, value in tokens().items():
            v = value.lower()
            if len(v) == 4:
                v = "#" + "".join(c * 2 for c in v[1:])
            by_value.setdefault(v, name)
        offenders = []
        for _ctx, sel, _ln, props in rules_with_context():
            raw = props.get("color", "").strip()
            if "background" in props or "background-color" in props:
                continue          # белый текст на цветном фоне — не дубль --panel
            m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", raw)
            if not m:
                continue
            v = raw.lower()
            if len(v) == 4:
                v = "#" + "".join(c * 2 for c in v[1:])
            if v in by_value:
                offenders.append((sel, raw, by_value[v]))
        assert not offenders, f"литерал вместо токена: {offenders}"

    def test_remaining_literals_are_deliberate_families(self):
        """Не-вакуумность предыдущей проверки и учёт остатка: это осознанные
        семейства, а не дубли токенов — чернила документов (#111, #444),
        тёплая палитра чека (#2a2418, #6f6250, #7c2d12), индиго переключателя
        AdGuard (#4b4566, #2b2440), фирменный тёмный текст на оранжевом
        (#1a1a1a) и белый на терракоте (#fff)."""
        lits = set(re.findall(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,8})", css_code()))
        allowed = {"#111", "#444", "#2a2418", "#6f6250", "#7c2d12",
                   "#4b4566", "#2b2440", "#1a1a1a", "#fff"}
        assert lits <= allowed, f"неожиданные литералы цвета: {sorted(lits - allowed)}"
        assert len(lits) >= 6, "литералов подозрительно мало — скан выродился"

    def test_tokens_are_declared_once(self):
        """В БЕЗУСЛОВНОМ :root токен объявлен ровно один раз: два объявления
        одного имени в одном контексте — тот же класс дефекта, что и два
        правила одного селектора (именно так кнопка стала чёрной).

        Переопределение токена внутри @media законно: --topbar-h укорачивается
        в landscape, чтобы на телефоне шапка не съедала экран.
        """
        counts, all_names = {}, set()
        for ctx, sel, _l, props in rules_with_context():
            if sel != ":root":
                continue
            for k in props:
                if not k.startswith("--"):
                    continue
                all_names.add(k)
                if not ctx:
                    counts[k] = counts.get(k, 0) + 1
        doubled = {k: v for k, v in counts.items() if v > 1}
        assert not doubled, f"токены объявлены дважды в :root: {doubled}"
        assert len(all_names) >= 20, f"токенов всего {len(all_names)} — скан выродился"

    def test_no_undefined_token_is_used(self):
        used = set(re.findall(r"var\((--[\w-]+)[),]", css_code()))
        declared = {k for _c, sel, _l, props in rules_with_context()
                    if sel == ":root" for k in props if k.startswith("--")}
        assert used <= declared, f"необъявленные токены: {sorted(used - declared)}"

    def test_no_var_fallbacks_in_css(self):
        assert not re.search(r"var\(--[\w-]+\s*,", css_code())


# ==========================================================================
# 6. Валидность CSS внешним валидатором
# ==========================================================================
def _stylelint():
    for cand in ("/tmp/csslint/node_modules/.bin/stylelint", shutil.which("stylelint") or ""):
        if cand and pathlib.Path(cand).exists():
            return cand
    return None


def _extract_inline_style(html_path: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    blocks = re.findall(r"<style[^>]*>([\s\S]*?)</style>", read(html_path))
    out.write_text("\n".join(blocks), encoding="utf-8")
    return out


class TestCssIsValid:
    def test_config_exists_and_checks_correctness(self):
        assert CONFIG.exists(), "в репозитории нет .stylelintrc.json"
        rules = json.loads(read(CONFIG))["rules"]
        for name in ("color-no-invalid-hex", "block-no-empty", "property-no-unknown",
                     "unit-no-unknown", "at-rule-no-unknown",
                     "declaration-block-no-shorthand-property-overrides",
                     "declaration-block-no-duplicate-properties"):
            assert name in rules, f"в конфиге нет правила {name}"
        # дубль свойств разрешён ТОЛЬКО как фолбэк с разными значениями
        # (min-height: 100vh; min-height: 100dvh) — приём запинен test_v1551
        assert rules["declaration-block-no-duplicate-properties"][1]["ignore"] == \
            ["consecutive-duplicates-with-different-values"]
        # no-duplicate-selectors намеренно не включён — см. ниже
        assert "no-duplicate-selectors" not in rules

    @pytest.mark.skipif(_stylelint() is None, reason="stylelint не установлен")
    @pytest.mark.parametrize("source", ["app.css", "maxapp", "brandbook"])
    def test_zero_findings(self, source, tmp_path):
        """0 замечаний валидатора по всем трём источникам CSS.

        До правки app.css давал 21: 17 дублей селекторов, 2 дубля свойств
        внутри блока, пустой блок .rcpt .r-name { } и сокращение overflow,
        глушившее overflow-x из v1.56.0.
        """
        if source == "app.css":
            target = APP_CSS
        elif source == "maxapp":
            target = _extract_inline_style(MAXAPP, tmp_path / "maxapp.css")
        else:
            target = _extract_inline_style(BRANDBOOK, tmp_path / "brand.css")
        r = subprocess.run([_stylelint(), "--config", str(CONFIG), "--formatter", "json",
                            str(target)], capture_output=True, text=True, timeout=240)
        raw = (r.stdout or "") + (r.stderr or "")
        i = raw.find("[")
        assert i >= 0, f"валидатор не вернул отчёт: {raw[:300]}"
        warnings = [w for f in json.loads(raw[i:]) for w in f["warnings"]]
        assert not warnings, [(w["line"], w["rule"], w["text"][:60]) for w in warnings]

    def test_why_duplicate_selectors_are_allowed(self):
        """Почему no-duplicate-selectors выключен намеренно.

        Слияние дублей НЕ безопасно: положение правила относительно media-блоков
        решает, кто победит. Пример из этого файла — .topbar: в шапке у него
        padding, в конце файла в блоке safe-area — padding-left/right:
        env(safe-area-inset-*), а МЕЖДУ ними
        @media (max-width: 860px) { .topbar { padding: 0 12px } }. Перенеси
        safe-area наверх — сокращение padding из media-блока перебьёт env() на
        телефоне, и «чёлка» iPhone наедет на содержимое. Поэтому вместо слияния
        проверяется отсутствие ПРОТИВОРЕЧИЙ (test_no_contradictory_duplicates).
        """
        topbar = [p for ctx, sel, _l, p in rules_with_context() if sel == ".topbar"]
        assert len(topbar) >= 2
        assert any("env(safe-area-inset-left)" in p.get("padding-left", "") for p in topbar)
        media = [ctx for ctx, sel, _l, p in rules_with_context()
                 if sel == ".topbar" and ctx and "padding" in p]
        assert media, "media-правило .topbar с padding исчезло"

    def test_shorthand_does_not_override_longhand(self):
        """body: overflow-x: hidden (v1.56.0) глушился сокращением overflow.
        Заменено на overflow-y — вычисленное значение то же, намерение видно."""
        base = [p for ctx, sel, _l, p in rules_with_context() if sel == "body" and not ctx]
        assert len(base) == 1, f"безусловных правил body: {len(base)}"
        d = " ".join(f"{k}: {v}" for k, v in base[0].items())
        assert "overflow-x: hidden" in d
        assert "overflow-y: hidden" in d
        assert not re.search(r"(?<![-\w])overflow:\s", d), "сокращение overflow вернулось"
        # в @media print сокращение законно: страница должна прокручиваться
        printed = [p for ctx, sel, _l, p in rules_with_context()
                   if sel == "body" and ctx == ("@media print",)]
        assert printed and printed[0].get("overflow") == "visible"

    def test_fallback_idiom_is_kept(self):
        """min-height: 100vh; min-height: 100dvh — не дубль-дефект, а фолбэк для
        браузеров без dvh. Приём запинен test_v1551, поэтому конфиг линтера
        разрешает подряд идущие дубли с РАЗНЫМИ значениями."""
        d = declarations(".login-screen")
        assert "min-height: 100vh" in d and "min-height: 100dvh" in d
        shell = declarations(".app-shell")
        assert "height: 100vh" in shell and "height: 100dvh" in shell

    def test_no_empty_blocks(self):
        assert not re.search(r"\{\s*\}", css_code()), "в CSS пустой блок"

    def test_braces_balanced(self):
        code = read(APP_CSS)
        assert code.count("{") == code.count("}")


# ==========================================================================
# 7. Мёртвых правил нет; CSS соответствует разметке
# ==========================================================================
class TestNoDeadCss:
    def test_dead_classes_stay_deleted(self):
        code = css_code()
        for name in DELETED_DEAD:
            assert f".{name}" not in code, f"мёртвый класс .{name} вернулся"

    def test_grow_utility_is_gone_but_row_lives(self):
        """.filter-bar .grow не применялся нигде, а .mapping-row применяется —
        удалено только мёртвое."""
        code = css_code()
        assert ".filter-bar .grow" not in code
        assert ".mapping-row" in code

    def test_every_css_class_is_used(self):
        """Обратное соответствие: класс из CSS обязан встречаться в разметке,
        шаблонах JS, Python или собираться динамически."""
        js = "\n".join(js_code(p) for p in sorted(JS_DIR.glob("*.js")))
        js += js_code(ROOT / "app/static/maxapp/maxapp.js")
        corpus = "\n".join([read(INDEX), read(MAXAPP), read(BRANDBOOK), js])
        corpus += "\n".join(read(p) for p in sorted((ROOT / "app").rglob("*.py")))
        dyn = {m + "-" for m in re.findall(r"([\w-]+)-\$\{", js)}
        dyn |= {m + "--" for m in re.findall(r"([\w-]+)--\$\{", js)}
        labels = set(re.findall(r"^\s*(\w+):",
                                re.search(r"STATUS_LABELS = \{([\s\S]*?)\}", js).group(1), re.M))
        dead = set()
        for _ctx, sel, _ln, _props in rules_with_context():
            for name in re.findall(r"\.([A-Za-z][\w-]*)", sel):
                if any(name.startswith(p) for p in dyn) or name in labels:
                    continue
                if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", corpus):
                    continue
                dead.add(name)
        assert not sorted(dead), f"классы CSS без применения: {sorted(dead)}"

    def test_every_markup_class_is_styled_or_a_hook(self):
        """Прямое соответствие: класс из index.html обязан иметь правило в CSS
        или быть объявленным JS-хуком."""
        html = re.sub(r"<!--[\s\S]*?-->", "", read(INDEX))
        used = set()
        for attr in re.findall(r'class="([^"]*)"', html):
            used |= set(attr.split())
        defined = {n for _c, sel, _l, _p in rules_with_context()
                   for n in re.findall(r"\.([A-Za-z][\w-]*)", sel)}
        unstyled = sorted(c for c in used if c not in defined and c not in JS_HOOK_CLASSES)
        assert not unstyled, f"классы разметки без стиля и не хуки: {unstyled}"

    def test_js_hook_classes_really_are_hooks(self):
        """Не-вакуумность: четыре класса-хука действительно ищутся из JS."""
        js = js_code(APP_JS)
        for name in sorted(JS_HOOK_CLASSES):
            assert f".{name}" in js, f"{name} больше не используется из JS"

    def test_no_contradictory_duplicates_anywhere(self):
        """Ни в одном контексте, ни между контекстами.

        В одном контексте противоречие означает, что верхнее объявление мертво
        (так кнопка стала чёрной). Между контекстами дефект только один:
        media-правило ВЫШЕ глушится безусловным правилом НИЖЕ — так умерли
        отступы тостов на телефоне и одноколоночная форма в v1.70.34.
        Media-правило НИЖЕ безусловного — штатное уточнение, не дефект.
        """
        assert not conflicts()
        rules = rules_with_context()
        dead = []
        for i, (ctx, sel, ln, props) in enumerate(rules):
            if not ctx or sel in ("from", "to") or sel.endswith("%"):
                continue
            for prop, val in props.items():
                for ctx2, sel2, ln2, props2 in rules[i + 1:]:
                    if sel2 != sel or prop not in props2 or props2[prop] == val:
                        continue
                    if not ctx2:
                        dead.append((sel, prop, ln, ln2))
                    break
        assert not dead, f"media-объявления, которые глушит безусловное правило: {dead}"

    def test_landing_breakpoint_merged(self):
        """Два СОСЕДНИХ блока с одинаковым max-width — источник мёртвых
        значений: для лендинга такой блок теперь один.

        Соседство блоков одного breakpoint'а в разных местах файла законно
        (≤640px есть и у модалок, и у тостов): дефект — не соседство, а
        противоречие, его ловит проверка выше.
        """
        code = read(APP_CSS)
        assert code.count("@media (max-width: 920px) {") == 1
        assert "1.5625rem" not in css_code()      # мёртвый размер не вернулся


# ==========================================================================
# 8. Релиз
# ==========================================================================
class TestRelease:
    # v1.70.41: точки синхронизации живут дальше; свою версию и
    # требование «своя запись первая» проверяет test_v17041.
    VERSION = "1.70.43"

    def test_all_sync_points(self):
        for p in (ROOT / "app/config.py", INDEX, ROOT / "app/static/sw.js",
                  ROOT / "app/static/manifest.webmanifest", MAXAPP):
            assert self.VERSION in read(p), f"{p.name}: версия не обновлена"

    def test_cache_busters(self):
        assert f"/css/app.css?v={self.VERSION}" in read(INDEX)
        assert f"/js/app.js?v={self.VERSION}" in read(INDEX)
        assert f"maxapp.js?v={self.VERSION}" in read(MAXAPP)

    def test_whats_new_is_first_and_names_the_numbers(self):
        """v1.70.36: требование «своя версия первая» принадлежит текущему
        выпуску и проверяется в test_v17036; здесь остаётся запись в истории
        и её измеренные числа."""
        js = read(APP_JS)
        assert "'1.70.35':" in js, "запись v1.70.35 пропала из истории"
        entry = js[js.index("'1.70.35':"):][:12000]
        for needle in ("1.18", "1.67", "stylelint"):
            assert needle in entry, f"в описании выпуска нет {needle!r}"

    def test_changelog_entry(self):
        assert f"## [{self.VERSION}]" in read(ROOT / "CHANGELOG.md")

    def test_journal_entry(self):
        j = read(ROOT / "docs/ui_journal.md")
        assert self.VERSION in j
        for needle in ("2.15", "4.11", "stylelint", "no-duplicate-selectors",
                       "#8a8a8a", "#e8ecf8", "1.67"):
            assert needle in j, f"в журнале нет {needle!r}"
