# -*- coding: utf-8 -*-
"""v1.70.32 — вёрстка и дизайн: мелкий текст читаем, остатков тёмной темы нет.

Аудит интерфейса нашёл системный дефект: фирменные семантические цвета
(--ok/--bad/--warn/--info) и фирменный оранжевый (--accent) использовались как
цвет МЕЛКОГО текста (11–13 px), где WCAG AA требует 4.5:1, а они дают
2.21–4.25:1.

Тяжелейший случай — `.form-error`, сообщение о причине неудачного входа
(`#login-error`, `#register-error`): текст `#fca5a5` на собственной подложке
`#feeeee` давал **1.69:1**. Плашка выглядела почти пустой, и пользователь не
понимал, почему вход не получился.

Причина историческая: `#fca5a5`, `#c084fc` и наборы `rgba(248,113,113)`,
`rgba(52,211,153)`, `rgba(96,165,250)`, `rgba(251,191,36)`, `rgba(192,132,252)`
— оттенки Tailwind для ТЁМНОЙ темы, оставшиеся после перехода на единственную
светлую тему в v1.25.0.

Фирменная палитра не тронута (её фиксируют бренд и тесты test_v1190/1250/1583) —
исправлено ИСПОЛЬЗОВАНИЕ цвета: добавлены отдельные текст-токены, а подложки и
границы переведены на `color-mix` от токенов темы.

Что здесь доказано:
  1) каждый текст-токен даёт ≥4.5:1 не только на чистых фонах темы, но и на
     СОБСТВЕННОЙ подкраске поверх --bg — то есть на худшей реальной подложке;
  2) расчёт контраста сам не vacuous: он ловит исходный дефект (1.69:1);
  3) остатков тёмной палитры и оранжевого как текста в файле нет;
  4) `color-mix` вызовы корректны, CSS не сломан;
  5) фирменные токены не изменились;
  6) журнал интерфейса существует и ведётся по правилам (находка — только с
     измерением; ложные срабатывания зафиксированы; бэклог отделён);
  7) версия, CHANGELOG, «Что нового».

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru
"""
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.getcwd())

from app.config import settings

APP_CSS = "app/static/css/app.css"
APP_JS = "app/static/js/app.js"
CHANGELOG = "CHANGELOG.md"
JOURNAL = "docs/ui_journal.md"
VER = "1.70.32"
PREV = "1.70.31"

SYNC_POINTS = ("app/config.py", "app/static/index.html", "app/static/sw.js",
               "app/static/manifest.webmanifest", "app/static/maxapp/index.html")

# Фирменная палитра — менять нельзя, только использовать правильно.
BRAND = {"--accent": "#ff7a00", "--accent-2": "#4b0082", "--ok": "#2e8b57",
         "--bad": "#d9534f", "--warn": "#b45309", "--info": "#237a90",
         "--bg": "#f5f5f5", "--panel": "#ffffff", "--bg-soft": "#ececec",
         "--accent-text": "#9a4a00", "--accent-soft": "#fff1e3",
         "--text": "#333333"}

# Текст-токены v1.70.32 и оттенки, из которых они выведены.
TEXT_TOKENS = {"--ok-text": "#27764a", "--bad-text": "#c22e2a",
               "--warn-text": "#a24b08", "--info-text": "#217287"}

# Tailwind-палитра тёмной темы: (r, g, b) → токен темы.
DARK_LEFTOVERS = {(52, 211, 153): "--ok", (248, 113, 113): "--bad",
                  (96, 165, 250): "--info", (251, 191, 36): "--warn",
                  (192, 132, 252): "--accent-2"}
DARK_HEX = ("#fca5a5", "#c084fc")

AA = 4.5


def read(path):
    return open(path, encoding="utf-8").read()


# ---------------------------------------------------------------- контраст ---
def _lum(rgb):
    c = []
    for v in rgb:
        v /= 255.0
        c.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(fg, bg):
    l1, l2 = _lum(fg), _lum(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def hx(s):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def mix(color, pct, over):
    """color-mix(in srgb, C pct%, transparent), наложенный на фон `over`."""
    c = color if isinstance(color, tuple) else hx(color)
    return tuple(round(c[i] * pct / 100 + over[i] * (1 - pct / 100))
                 for i in range(3))


WHITE = (255, 255, 255)
BG = hx("f5f5f5")
SOFT = hx("ececec")


@pytest.fixture(scope="module")
def css():
    return read(APP_CSS)


def _tok(css, name):
    m = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", css)
    assert m, f"в app.css нет токена {name}"
    return m.group(1)


# ==============================================================================
#  1) Расчёт контраста сам не vacuous
# ==============================================================================
class TestContrastMathIsSound:
    def test_catches_the_original_defect(self):
        """Отрицательный контроль: расчёт обязан ловить исходный дефект.

        Без этой проверки «все пары проходят 4.5» могло бы означать, что
        функция контраста просто всегда возвращает большое число.
        """
        assert contrast(hx("#fca5a5"), hx("#feeeee")) == pytest.approx(1.69, abs=0.02)
        assert contrast(hx("#fca5a5"), WHITE) < AA
        assert contrast(hx("#c084fc"), WHITE) < AA
        assert contrast(hx("#ff7a00"), WHITE) < AA
        assert contrast(hx("#d9534f"), WHITE) < AA
        assert contrast(hx("#2e8b57"), WHITE) < AA

    def test_known_good_pairs_pass(self):
        assert contrast(hx("#333333"), WHITE) == pytest.approx(12.63, abs=0.02)
        assert contrast(hx("#4b0082"), WHITE) > 12
        assert contrast(WHITE, hx("#333333")) == \
            contrast(hx("#333333"), WHITE), "контраст симметричен"

    def test_tint_is_lighter_than_color_and_worse_for_contrast(self):
        """Подкраска светлее самого цвета, но контраст того же цвета на
        собственной подкраске НИЖЕ, чем на белом: подкраска темнее белого.
        Поэтому худший случай надо считать по подкраске поверх --bg, а не по
        белой карточке — на этом и попалась первая версия текст-токенов."""
        bad = hx(BRAND["--bad"])
        tint_on_white = mix(bad, 12, WHITE)
        tint_on_bg = mix(bad, 12, BG)
        assert _lum(tint_on_white) > _lum(bad), "подкраска должна быть светлее цвета"
        assert _lum(tint_on_white) < _lum(WHITE)
        assert contrast(bad, tint_on_white) < contrast(bad, WHITE)
        assert contrast(bad, tint_on_bg) < contrast(bad, tint_on_white), \
            "подкраска поверх --bg темнее, чем поверх белого"


# ==============================================================================
#  2) Токены: фирменные целы, текстовые — новые и читаемые
# ==============================================================================
class TestTokens:
    @pytest.mark.parametrize("name,value", sorted(BRAND.items()))
    def test_brand_tokens_unchanged(self, css, name, value):
        """Фирменная палитра не тронута: чинилось использование цвета."""
        assert _tok(css, name) == value

    @pytest.mark.parametrize("name,value", sorted(TEXT_TOKENS.items()))
    def test_text_tokens_added(self, css, name, value):
        assert _tok(css, name) == value

    @pytest.mark.parametrize("name", sorted(TEXT_TOKENS))
    def test_text_token_readable_on_plain_backgrounds(self, css, name):
        for bgname in ("--panel", "--bg", "--bg-soft"):
            r = contrast(hx(_tok(css, name)), hx(_tok(css, bgname)))
            assert r >= AA, f"{name} на {bgname} = {r:.2f}"

    @pytest.mark.parametrize("name,pct", [
        ("--ok-text", 9), ("--ok-text", 10),
        ("--bad-text", 9), ("--bad-text", 12),
        ("--warn-text", 9), ("--warn-text", 10), ("--warn-text", 14),
        ("--info-text", 9),
    ])
    def test_text_token_readable_on_own_tint(self, css, name, pct):
        """Худший реальный случай: собственная подкраска поверх --bg, а не
        поверх белой карточки. Первая версия значений на этом и провалилась
        (.form-error 4.25:1, .offline-indicator 4.41:1)."""
        base = {"--ok-text": "--ok", "--bad-text": "--bad",
                "--warn-text": "--warn", "--info-text": "--info"}[name]
        for over in (WHITE, BG):
            bg = mix(hx(_tok(css, base)), pct, over)
            r = contrast(hx(_tok(css, name)), bg)
            assert r >= AA, f"{name} на подкраске {base} {pct}% = {r:.2f}"

    def test_accent_text_readable_on_accent_soft(self, css):
        r = contrast(hx(_tok(css, "--accent-text")), hx(_tok(css, "--accent-soft")))
        assert r >= AA, r


# ==============================================================================
#  3) Остатков тёмной темы нет
# ==============================================================================
class TestNoDarkThemeLeftovers:
    @pytest.mark.parametrize("col", DARK_HEX)
    def test_dark_theme_hex_gone(self, css, col):
        assert col not in css, f"{col} — оттенок тёмной темы, на белом нечитаем"

    @pytest.mark.parametrize("rgb", sorted(DARK_LEFTOVERS))
    def test_dark_theme_rgba_gone(self, css, rgb):
        pat = r"rgba\(\s*%d\s*,\s*%d\s*,\s*%d" % rgb
        assert not re.search(pat, css), \
            f"rgba{rgb} — палитра Tailwind для тёмной темы"

    def test_form_error_uses_theme_error_color(self, css):
        """Главный дефект выпуска: причина неудачного входа была невидимой."""
        block = css.split(".form-error {")[1].split("}")[0]
        assert "color: var(--bad-text)" in block, block
        assert "color-mix(in srgb, var(--bad)" in block
        r = contrast(hx(_tok(css, "--bad-text")),
                     mix(hx(_tok(css, "--bad")), 12, WHITE))
        assert r >= AA, r

    def test_chip_exported_uses_theme_indigo(self, css):
        block = css.split(".chip.exported {")[1].split("}")[0]
        assert "color: var(--accent-2)" in block, block
        r = contrast(hx(_tok(css, "--accent-2")),
                     mix(hx(_tok(css, "--accent-2")), 9, WHITE))
        assert r >= AA, r


# ==============================================================================
#  4) Оранжевый — не цвет текста
# ==============================================================================
class TestOrangeIsNotTextColor:
    def test_no_color_var_accent(self, css):
        hits = re.findall(r"(?<![-\w])color\s*:\s*var\(--accent\)", css)
        assert not hits, "оранжевый #ff7a00 как текст даёт 2.21–2.61:1"

    @pytest.mark.parametrize("sel,min_ratio", [
        (".btn-icon:hover", AA),
        (".manual-sec h3 .manual-num", AA),
    ])
    def test_former_offenders_now_use_accent_text(self, css, sel, min_ratio):
        block = css.split(sel)[1].split("}")[0]
        assert "color: var(--accent-text)" in block, f"{sel}: {block}"

    def test_orange_still_present_as_brand(self, css):
        """Убрав оранжевый из текста, его нельзя было убрать совсем: границы,
        подложки и сплошные фоны кнопок остались фирменными."""
        assert "border-color: var(--accent)" in css
        assert "background: var(--accent)" in css
        assert "accent-color: var(--accent)" in css


# ==============================================================================
#  5) Мелкий текст переведён на текст-токены
# ==============================================================================
class TestSmallTextUsesTextTokens:
    SMALL = (".chip.new", ".chip.verifying", ".chip.verified", ".chip.failed",
             ".chip.valid", ".chip.invalid", ".chip.not_found",
             ".btn-ok", ".btn-bad", ".trend-up", ".trend-down",
             ".dl-badge.ok", ".dl-badge.warn", ".dl-badge.bad",
             ".pf-chip.ok", ".pf-chip.bad", ".heat-ok", ".heat-soon",
             ".heat-late", ".offline-indicator",
             '.ag-sw[data-pos="left"] .ag-sw-left',
             '.ag-sw[data-pos="right"] .ag-sw-right')

    @pytest.mark.parametrize("sel", SMALL)
    def test_selector_uses_text_token(self, css, sel):
        block = css.split(sel)[1].split("}")[0]
        m = re.search(r"(?<![-\w])color\s*:\s*var\((--[\w-]+)\)", block)
        assert m, f"{sel}: цвет текста не найден"
        assert m.group(1).endswith("-text") or m.group(1) == "--accent-2", \
            f"{sel}: {m.group(1)} — не текст-токен"

    def test_switch_semantics_kept(self, css):
        """Требование владельца: зелёный = включён, красный = выключен."""
        assert "color: var(--bad-text)" in \
            css.split('.ag-sw[data-pos="left"] .ag-sw-left')[1].split("}")[0]
        assert "color: var(--ok-text)" in \
            css.split('.ag-sw[data-pos="right"] .ag-sw-right')[1].split("}")[0]
        assert "#fdeeec" in css and "#eaf7f1" in css


# ==============================================================================
#  6) CSS не сломан
# ==============================================================================
class TestCssIsValid:
    def test_braces_balanced(self, css):
        assert css.count("{") == css.count("}")

    def test_no_declaration_without_colon(self, css):
        nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        bad = []
        for m in re.finditer(r"\{([^{}]*)\}", nc):
            for decl in m.group(1).split(";"):
                d = decl.strip()
                if d and ":" not in d:
                    bad.append(d[:60])
        assert not bad, bad

    def test_every_root_declaration_ends_with_semicolon(self, css):
        """Регресс-гард на правку токенов: при построчной замене однажды
        потерялись точки с запятой — объявления слиплись бы."""
        root = css[css.index(":root {"):]
        root = root[:root.index("\n}")]
        body = re.sub(r"/\*.*?\*/", "", root, flags=re.S)
        for line in body.split("\n"):
            line = line.strip()
            if not line or line in (":root {", "}"):
                continue
            assert line.endswith(";"), f"объявление без ';': {line!r}"

    def test_color_mix_calls_wellformed(self, css):
        total = css.count("color-mix(")
        good = len(re.findall(
            r"color-mix\(in srgb,\s*var\(--[\w-]+\)\s+[\d.]+%,\s*transparent\)",
            css))
        assert total == good, f"{total - good} некорректных color-mix"
        assert good >= 40, good

    def test_every_used_token_is_declared(self, css):
        """Ни одна var(--x) не висит на необъявленном токене.

        Необъявленная переменная БЕЗ запасного значения делает объявление
        «недействительным на этапе вычисления значения»: свойство ведёт себя
        как unset, и правило молча не применяется — вёрстка ломается без
        единой ошибки в консоли. Так в v1.70.32 нашлись `--bg-card` (липкая
        панель кнопок модалки оказывалась ПРОЗРАЧНОЙ, содержимое просвечивало
        под кнопками) и `--primary` (чекбоксы в модалке были синими —
        браузерная норма — вместо фирменного оранжевого).

        Объявления токенов ищутся ВО ВСЕХ блоках файла: в app.css два блока
        `:root` (строки ~12 и ~1019), и второй как раз держит шрифты и
        `--indigo`. Проверка только первого блока даёт ложную тревогу — на ней
        и попалась первая версия этого теста.
        """
        html = read("app/static/index.html")
        declared = set(re.findall(r"(--[\w-]+)\s*:", css))
        declared |= set(re.findall(r"(--[\w-]+)\s*:", html))
        uses = re.findall(r"var\(\s*(--[\w-]+)\s*(,)?", css)
        uses += re.findall(r"var\(\s*(--[\w-]+)\s*(,)?", html)
        hard = sorted({n for n, comma in uses if not comma and n not in declared})
        assert not hard, f"не объявлены и без запасного значения: {hard}"
        # с запасным значением правило работает, но живёт вне палитры темы
        soft = sorted({n for n, comma in uses if comma and n not in declared})
        assert not soft, f"висят только на запасном значении: {soft}"

    def test_two_root_blocks_both_seen(self, css):
        """Регресс-гард на ложную тревогу: шрифты и --indigo объявлены во
        ВТОРОМ блоке :root, поэтому искать объявления надо по всему файлу."""
        assert css.count(":root") >= 2
        assert "--font-mono" in css and "--font-accent" in css
        assert "--indigo:" in css

    def test_modal_actions_background_is_opaque(self, css):
        """Липкая панель кнопок модалки: --bg-card не объявлен, фон был
        прозрачным и содержимое просвечивало под кнопками."""
        blocks = [b.split("}")[0] for b in css.split(".modal-actions {")[1:]]
        sticky = [b for b in blocks if "sticky" in b]
        assert sticky, "не найдено липкой панели .modal-actions"
        assert any("background: var(--panel)" in b for b in sticky)
        # отсутствие токена проверяем по тексту БЕЗ комментариев: пояснение
        # v1.70.32 рядом с правилом само упоминает --bg-card
        assert "--bg-card" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def test_modal_checkbox_uses_brand_accent(self, css):
        """--primary не объявлен — accent-color откатывался к браузерной
        норме (синий) вместо фирменного оранжевого."""
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        assert "var(--primary)" not in body
        assert "accent-color: var(--accent)" in body

    def test_command_line_uses_theme_tokens(self, css):
        """Блок «Сервер и команды»: серые оттенки Tailwind с запасными
        значениями заменены токенами темы."""
        block = css.split(".cmd-line {")[1].split("}")[0]
        assert "--surface-2" not in block
        assert "#e5e7eb" not in block
        assert "var(--bg)" in block and "var(--border)" in block

    def test_dead_tokens_removed(self, css):
        """Объявлены, но ни разу не применены; --indigo-soft к тому же
        «для тёмного фона», удалённого в v1.25.0."""
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for dead in ("--font-num", "--indigo-soft"):
            assert not re.search(re.escape(dead) + r"\s*:", body), \
                f"{dead} снова объявлен"

    def test_markup_dividers_use_theme_border(self):
        """Разделители в разметке жили на --line с запасным #e5e7eb — оттенок
        Tailwind, а не граница темы. v1.70.33: инлайн-стили из разметки убраны
        целиком, те же четыре разделителя переехали в класс .alt-line, поэтому
        токен теперь проверяется в CSS, а в разметке — что класс действительно
        применён (иначе разделители молча пропали бы при переносе)."""
        html = read("app/static/index.html")
        assert "--line" not in html
        assert "#e5e7eb" not in html
        assert html.count("alt-line") >= 4, "разделители потеряны при переносе в классы"
        css = read(APP_CSS)
        assert re.search(r"\.alt-line\s*\{[^}]*background:\s*var\(--border\)", css), \
            ".alt-line больше не рисует разделитель границей темы"

    def test_loaded_webfonts_are_actually_used(self):
        """index.html грузит Inter, JetBrains Mono и Caveat; каждый обязан
        быть применён через токен, иначе шрифты скачиваются впустую."""
        html = read("app/static/index.html")
        for font in ("Inter", "JetBrains+Mono", "Caveat"):
            assert font in html, f"{font} не подключён"
        c = read(APP_CSS)
        assert '"JetBrains Mono"' in c
        assert '"Caveat"' in c
        assert '"Inter"' in c

    def test_single_light_theme_only(self, css):
        """Решение v1.25.0 в силе: тёмной темы нет."""
        assert "prefers-color-scheme: dark" not in css
        assert "color-scheme: light" in css


# ==============================================================================
#  7) Журнал интерфейса
# ==============================================================================
class TestJournal:
    def test_journal_exists(self):
        assert os.path.exists(JOURNAL), "журнал интерфейса не заведён"

    def test_journal_has_rules(self):
        j = read(JOURNAL)
        assert "Правила ведения" in j
        for needle in ("только с измерением", "воспроизводима",
                       "Ложные срабатывания", "Фирменная палитра не меняется"):
            assert needle in j, needle

    def test_journal_has_method_and_thresholds(self):
        j = read(JOURNAL)
        assert "4.5:1" in j and "3:1" in j
        assert "WCAG" in j
        assert "--bg-soft #ececec" in j

    def test_journal_records_measurements(self):
        """Находка без числа — не запись: журнал обязан содержать измерения."""
        j = read(JOURNAL)
        assert "1.69:1" in j and "4.57:1" in j
        assert "#fca5a5" in j and "#c084fc" in j
        for tok in TEXT_TOKENS.values():
            assert tok in j, tok

    def test_journal_records_false_positives(self):
        """kind-* оказались ложным срабатыванием скана — это записано, чтобы
        находку не искали повторно и не удалили живые классы."""
        j = read(JOURNAL)
        assert "Проверено и отвергнуто" in j
        assert "kind-ok" in j and "kind-${" in j

    def test_journal_separates_backlog(self):
        j = read(JOURNAL)
        assert "Бэклог: найдено, не исправлено" in j
        back = j[j.index("Бэклог: найдено, не исправлено"):]
        assert "требуют ручной проверки" in back

    def test_journal_does_not_overclaim(self):
        """Журнал не выдаёт непроверенное за исправленное."""
        j = read(JOURNAL)
        back = j[j.index("Бэклог: найдено, не исправлено"):]
        for cand in ("ao-preview", "catalog-grid", "manual-hero"):
            assert cand in back, f"{cand} должен лежать в бэклоге, а не в исправленном"

    def test_journal_mentions_company(self):
        assert "ООО «Ямастер»" in read(JOURNAL)
        assert "ymaster.ru" in read(JOURNAL)


# ==============================================================================
#  8) Версия и описание выпуска
# ==============================================================================
class TestVersion17032:
    def test_app_version(self):
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= \
            (1, 70, 32)

    @pytest.mark.parametrize("path", SYNC_POINTS)
    def test_sync_point_matches_current_version(self, path):
        assert settings.APP_VERSION in read(path), path

    def test_changelog_entry(self):
        md = read(CHANGELOG)
        assert md.count(f"## [{VER}]") == 1
        assert md.index(f"## [{VER}]") < md.index(f"## [{PREV}]")
        entry = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        for needle in ("1.69:1", "#fca5a5", "#c084fc", "--ok-text",
                       "--bad-text", "Tailwind", "color-mix",
                       "docs/ui_journal.md", "WCAG"):
            assert needle in entry, needle

    def test_changelog_says_brand_untouched(self):
        md = read(CHANGELOG)
        entry = md[md.index(f"## [{VER}]"):md.index(f"## [{PREV}]")]
        assert "фирменная палитра не тронута" in entry.lower() or \
               "Фирменная палитра не тронута" in entry

    def test_whats_new_entry(self):
        js = read(APP_JS)
        assert f"'{VER}':" in js
        assert js.index(f"'{VER}':") < js.index(f"'{PREV}':")
        entry = js[js.index(f"'{VER}':"):js.index(f"'{PREV}':")]
        for needle in ("1.69:1", "#fca5a5", "--ok-text", "ui_journal.md",
                       "WCAG AA"):
            assert needle in entry, needle

    def test_no_dark_theme_added(self):
        """Тёмная тема явно отвергнута в v1.25.0 — в выпуск она не попала."""
        js = read(APP_JS)
        entry = js[js.index(f"'{VER}':"):js.index(f"'{PREV}':")]
        assert "тёмн" in entry            # упомянуто как причина, а не как фича
        assert "переключатель темы" not in entry

    def test_node_syntax(self):
        r = subprocess.run(["node", "--check", APP_JS], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr
