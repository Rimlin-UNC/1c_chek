# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.46: аудит текста и дизайна, чистка мусора.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерий приёмки (указание владельца): «Проведи аудит программы чтоб
# текст и дизайн был валидным и читаймым. Почисти весь муссор».
#
# Что закреплено, чтобы найденное не вернулось:
#  1. палитра Tailwind не возвращается в стили интерфейса — линия
#     сканирования была последним местом, где она дожила до аудита;
#  2. удалённый мёртвый код не определяется снова, а в проверенных
#     модулях не появляется функций без единого обращения;
#  3. пользовательский текст проверяется по извлечённым литералам и
#     текстовым узлам, а не по строкам кода, и не имеет дефектов
#     пунктуации;
#  4. читаемость измерена: кегль не ниже нормы на всех трёх поверхностях,
#     контраст текста не ниже 4.5:1, линия сканирования читается и на
#     ярком кадре с камеры;
#  5. стили кабинета соответствуют его разметке и скрипту;
#  6. порядок правил, от которого зависит интерлиньяж поля ввода,
#     зафиксирован;
#  7. ритуал выпуска: точки синхронизации, кэш-бастеры, «Что нового»,
#     CHANGELOG и журнал вёрстки.
# ======================================================================
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "app/static/index.html"
CSS = ROOT / "app/static/css/app.css"
APP_JS = ROOT / "app/static/js/app.js"
SW = ROOT / "app/static/sw.js"
MANIFEST = ROOT / "app/static/manifest.webmanifest"
MAX_HTML = ROOT / "app/static/maxapp/index.html"
CABINET = ROOT / "app/static/b2b/index.html"
CABINET_CSS = ROOT / "app/static/b2b/b2b.css"
CABINET_JS = ROOT / "app/static/b2b/b2b.js"
ASSETS = ROOT / "app/b2b/assets"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs/ui_journal.md"

SVG_FILES = ("api-access.svg", "api-request-path.svg", "api-data-boundary.svg")

# Палитра Tailwind, от которой проект отказался в v1.70.35: шесть из семи
# цветов давали ниже 3:1 на светлом фоне (см. CHANGELOG, таблица палитры).
TAILWIND = ("#6366f1", "#22d3ee", "#34d399", "#fbbf24", "#f87171",
            "#60a5fa", "#c084fc", "#a78bfa")

# Объекты, удалённые аудитом: модуль → имена. На них не ссылался ни маршрут,
# ни тест, ни сценарий развертывания, ни документ.
DELETED = {
    "app/b2b/auth.py": ("_iso", "token_ttl_left"),
    "app/services/telegram_bot.py": ("wake", "_wake"),
    "app/services/max_bot.py": ("_public", "_TECH_NICK"),
    "app/services/external.py": ("_scale_money",),
    "app/services/webhooks.py": ("pending_count",),
    "app/pool/router_company.py": ("_like",),
    "app/services/adguard_setup.py": ("helper_script",),
}

# Модули, в которых проверено отсутствие функций без единого обращения.
AUDITED_MODULES = tuple(DELETED)

# Порог кегля по поверхностям: 10 px для интерфейса оператора
# (норма test_v17035.test_no_text_smaller_than_ten_px) и 12.5 px для
# кабинета, где текст читает внешний клиент.
FONT_FLOOR_PX = 10.0
FONT_FLOOR_CABINET_PX = 12.5
CONTRAST_FLOOR = 4.5
GRAPHIC_FLOOR = 3.0          # WCAG 1.4.11, непорожковый текст и графика


# ----------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------
def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _maxapp_style() -> str:
    """Инлайновые стили мини-приложения: отдельного файла у него нет."""
    return re.search(r"<style[^>]*>(.*?)</style>", _read(MAX_HTML), re.S).group(1)


def _rules(css: str):
    """Правила стилей без комментариев: селектор и его объявления."""
    code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", code):
        selector = " ".join(block.group(1).split())
        props = {m.group(1).strip(): m.group(2).strip()
                 for m in re.finditer(r"([\w-]+)\s*:\s*([^;]+)", block.group(2))}
        yield selector, props


def _declarations(css: str, selector: str) -> dict:
    for sel, props in _rules(css):
        if sel == selector:
            return props
    raise AssertionError(f"правило {selector!r} не найдено")


def _tokens(css: str) -> dict:
    """Значения фирменных токенов из блока :root."""
    root = css.split(":root", 1)
    assert len(root) > 1, "в стилях нет блока :root"
    return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})",
                           root[1].split("}", 1)[0]))


def _rgb(value: str):
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(rgb) -> float:
    linear = [((v / 255 + 0.055) / 1.055) ** 2.4 if v / 255 > 0.03928
              else (v / 255) / 12.92 for v in rgb]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    a, b = _luminance(_rgb(fg)), _luminance(_rgb(bg))
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _over(fg: str, alpha: float, bg: str) -> str:
    """Цвет полупрозрачного слоя поверх непрозрачного фона."""
    top, bottom = _rgb(fg), _rgb(bg)
    return "#{:02x}{:02x}{:02x}".format(
        *[round(t * alpha + b * (1 - alpha)) for t, b in zip(top, bottom)])


CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
LITERAL = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|"
                     r"`((?:[^`\\]|\\.)*)`", re.S)

# Поверхности, текст которых видит пользователь: скрипты, модули содержимого
# и три страницы. Проза документов проверяется замками своих выпусков.
TEXT_JS = ("app/static/js/app.js", "app/static/js/ui.js",
           "app/static/js/api.js", "app/static/js/charts.js",
           "app/static/js/scanner.js", "app/static/js/icons.js",
           "app/static/js/printpack.js", "app/static/sw.js",
           "app/static/maxapp/maxapp.js", "app/static/b2b/b2b.js")
TEXT_PY = ("app/b2b/content.py", "app/b2b/letters.py", "app/b2b/verify.py",
           "app/b2b/pipeline.py", "app/b2b/router_public.py",
           "app/b2b/router_admin.py", "app/pool/router_api.py",
           "app/pool/router_public.py", "app/services/manual_content.py")
TEXT_HTML = ("app/static/index.html", "app/static/maxapp/index.html",
             "app/static/b2b/index.html")

# Дефекты пунктуации в пользовательском тексте. Проверка ведётся только по
# извлечённым строкам: тот же набор шаблонов по строкам кода даёт тысячи
# ложных срабатываний — списки классов (class="btn btn-sm"), вызовы функций,
# имена файлов и классов в прозе («Опечатка в .env»), намеренные пробелы в
# префиксах форм собственности ('ООО ') и склейки строк, где кавычка
# закрывается в соседнем литерале.
TEXT_DEFECTS = (
    (r"[^\s] {2,}[^\s]", "двойной пробел"),
    (r"[а-яёА-ЯЁ]\s+[!?;:,]", "пробел перед знаком препинания"),
    # точка после пробела — дефект, кроме имён файлов и классов (.env, .gap-8)
    (r"[а-яёА-ЯЁ]\s+\.(?![\w])", "пробел перед точкой"),
    (r"(?<!\.)\.\.(?!\.)", "две точки подряд"),
    (r",,", "две запятые подряд"),
    (r"\b([а-яё]{3,})\s+\1\b", "повтор слова"),
    (r"[а-яё][A-Z][а-яё]", "слипание кириллицы и латиницы"),
)


def _user_visible_text():
    """Строки, которые видит пользователь: (файл, номер строки, текст)."""
    out = []
    for rel in TEXT_JS + TEXT_PY:
        path = ROOT / rel
        if not path.exists():
            continue
        text = _read(path)
        for match in LITERAL.finditer(text):
            value = match.group(1) or match.group(2) or match.group(3) or ""
            if CYRILLIC.search(value) and len(value.strip()) > 1:
                out.append((rel, text[:match.start()].count("\n") + 1, value))
    for rel in TEXT_HTML:
        text = _read(ROOT / rel)
        body = re.sub(r"<script.*?</script>", " ", text, flags=re.S)
        body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
        body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
        for match in re.finditer(r">([^<>]+)<", body):
            value = match.group(1)
            if CYRILLIC.search(value) and value.strip():
                out.append((rel, body[:match.start()].count("\n") + 1, value))
        for match in re.finditer(r"(?:placeholder|title|aria-label|alt|value)="
                                 r"\"([^\"]*)\"", body):
            if CYRILLIC.search(match.group(1)):
                out.append((rel, body[:match.start()].count("\n") + 1,
                            match.group(1)))
    return out


def _py_corpus() -> str:
    """Весь Python проекта плюс сценарии: по нему ищутся обращения."""
    parts = []
    for base in ("app", "tests", "qa"):
        for path in sorted((ROOT / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parts.append(path.read_text(encoding="utf-8"))
    for extra in ("deploy.sh", "run.py"):
        path = ROOT / extra
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


# ======================================================================
# 1. Палитра Tailwind не возвращается в стили интерфейса
# ======================================================================
class TestRejectedPaletteIsGoneFromUiStyles:
    """Аудит нашёл последнее место, где дожил цвет отклонённой палитры.

    Замок TAILWIND в test_v17035 сканирует только charts.js, поэтому
    `.scan-line` в app.css с градиентом на #22d3ee пережила v1.70.35.
    Здесь проверяются все три поверхности интерфейса.
    """

    def _declarations_of(self, path):
        """Объявления стилей без комментариев: в комментариях проекта цвета
        отклонённой палитры перечислены намеренно — там записаны измеренные
        причины отказа от них (1.81:1, 1.92:1 и так далее)."""
        return re.sub(r"/\*.*?\*/", " ", _read(path), flags=re.S).lower()

    def test_main_stylesheet_is_clean(self):
        text = self._declarations_of(CSS)
        found = [color for color in TAILWIND if color in text]
        assert not found, f"app.css: цвета отклонённой палитры {found}"

    def test_cabinet_stylesheet_is_clean(self):
        text = self._declarations_of(CABINET_CSS)
        found = [color for color in TAILWIND if color in text]
        assert not found, f"b2b.css: цвета отклонённой палитры {found}"

    def test_maxapp_inline_styles_are_clean(self):
        text = re.sub(r"/\*.*?\*/", " ", _maxapp_style(), flags=re.S).lower()
        found = [color for color in TAILWIND if color in text]
        assert not found, f"стили мини-приложения: {found}"

    def test_scripts_carry_no_rejected_palette(self):
        """В скриптах цвет как значение — это отдельный строковый литерал.
        В прозе «Что нового» те же имена перечислены как история правки, и
        трогать её нельзя: владелец читает, что и почему было исправлено."""
        for rel in TEXT_JS:
            text = _read(ROOT / rel)
            found = [color for color in TAILWIND
                     if re.search(rf"['\"]{color}['\"]", text)]
            assert not found, f"{rel}: цвета отклонённой палитры {found}"

    def test_documentation_diagrams_keep_their_dark_accent(self):
        """Схемы документации — не интерфейс: тёмная подложка, и там тот же
        цвет намеренный акцент, закреплённый STYLE_NEEDLES в test_v17044."""
        for name in SVG_FILES:
            assert "#22d3ee" in _read(ASSETS / name), name


# ======================================================================
# 2. Линия сканирования: фирменный цвет и читаемость на ярком кадре
# ======================================================================
class TestScanLineIsReadable:
    """Измерено: #22d3ee давал 1.81:1 на белом кадре с камеры, фирменный
    оранжевый даёт 2.61:1 — оба ниже порога 3:1 для графики (WCAG 1.4.11).
    Худший случай закрывает тёмный ореол: 4.54:1 поверх белого. На тёмном
    видоискателе линия даёт 7.75:1, а ореол там растворяется."""

    WHITE = "#ffffff"
    VIEWFINDER = "#030610"        # основа затемнения видоискателя
    HALO = "#030610"

    def test_line_uses_brand_token(self):
        props = _declarations(_read(CSS), ".scan-line")
        assert "var(--accent)" in props["background"], props["background"]
        for color in TAILWIND:
            assert color not in props["background"]

    def test_accent_token_is_corporate_orange(self):
        assert _tokens(_read(CSS))["--accent"].lower() == "#ff7a00"

    def test_line_carries_dark_halo(self):
        props = _declarations(_read(CSS), ".scan-line")
        assert "drop-shadow" in props.get("filter", ""), "ореол пропал"
        found = re.search(r"rgba\(3,\s*6,\s*16,\s*([.\d]+)\)", props["filter"])
        assert found, f"тёмный ореол не найден: {props['filter']}"
        alpha = float(found.group(1))
        assert 0.4 <= alpha <= 0.7, f"прозрачность ореола {alpha} вне запаса"
        halo = _over(self.HALO, alpha, self.WHITE)
        assert _contrast(halo, self.WHITE) >= GRAPHIC_FLOOR, \
            f"ореол {halo} на ярком кадре ниже {GRAPHIC_FLOOR}:1"

    def test_brand_color_beats_the_rejected_one_on_bright_frame(self):
        accent = _tokens(_read(CSS))["--accent"]
        assert _contrast(accent, self.WHITE) > _contrast("#22d3ee", self.WHITE)

    def test_line_stays_visible_on_dark_viewfinder(self):
        accent = _tokens(_read(CSS))["--accent"]
        assert _contrast(accent, self.VIEWFINDER) >= GRAPHIC_FLOOR

    def test_line_matches_frame_corners(self):
        """Линия и уголки рамки — один фирменный цвет, иначе видоискатель
        выглядит собранным из двух палитр."""
        corners = None
        for selector, props in _rules(_read(CSS)):
            if ".scan-frame::before" in selector and "border" in props:
                corners = props
                break
        assert corners, "общее правило уголков рамки не найдено"
        assert "var(--accent)" in corners["border"], corners["border"]


# ======================================================================
# 3. Мёртвый код не возвращается
# ======================================================================
class TestDeadCodeStaysDeleted:
    """Двенадцать объектов без единого обращения удалены аудитом."""

    def test_deleted_names_are_not_defined_again(self):
        for rel, names in DELETED.items():
            text = _read(ROOT / rel)
            for name in names:
                defined = re.search(rf"^(?:async\s+)?def\s+{re.escape(name)}\b",
                                    text, re.M) or \
                    re.search(rf"^{re.escape(name)}\s*=", text, re.M)
                assert not defined, f"{rel}: {name} определено снова"

    def test_auth_module_imports_only_what_it_uses(self):
        """Импорт сужен вместе с удалением функций: datetime и timezone
        использовались только ими."""
        text = _read(ROOT / "app/b2b/auth.py")
        assert "from datetime import timedelta" in text
        assert "from datetime import datetime" not in text

    def test_audited_modules_have_no_zero_reference_functions(self):
        """В проверенных аудитом модулях нет функций модуля, на которые никто
        не ссылается. Декорированные функции исключены: их вызывает фреймворк
        (пример — _set_sqlite_pragma, которую драйвер вызывает через
        @event.listens_for)."""
        corpus = _py_corpus()
        dead = []
        for rel in AUDITED_MODULES:
            path = ROOT / rel
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.decorator_list:
                    continue
                if node.name.startswith("__") and node.name.endswith("__"):
                    continue
                if corpus.count(node.name) <= 1:
                    dead.append(f"{rel}::{node.name}")
        assert not dead, f"функции без обращений: {dead}"


# ======================================================================
# 4. Пользовательский текст: пунктуация и опечатки
# ======================================================================
class TestUserVisibleTextIsClean:
    """Проверяются только строки, которые видит человек: литералы с
    кириллицей в скриптах и модулях содержимого, текстовые узлы разметки и
    видимые атрибуты (placeholder, title, aria-label, alt, value)."""

    def test_corpus_is_not_empty(self):
        rows = _user_visible_text()
        assert len(rows) > 500, f"извлечено подозрительно мало строк: {len(rows)}"

    def test_no_punctuation_defects(self):
        findings = []
        for rel, line, value in _user_visible_text():
            for pattern, title in TEXT_DEFECTS:
                for match in re.finditer(pattern, value):
                    findings.append(f"{rel}:{line} {title} → {match.group(0)!r}")
        assert not findings, findings[:10]

    def test_whats_new_entry_of_this_release_is_clean(self):
        """Запись выпуска читает владелец — она проверяется тем же набором."""
        block = _read(APP_JS).split("'1.70.46': [", 1)[1].split("'1.70.45': [", 1)[0]
        findings = []
        for pattern, title in TEXT_DEFECTS:
            for match in re.finditer(pattern, block):
                findings.append(f"{title} → {match.group(0)!r}")
        assert not findings, findings[:6]


# ======================================================================
# 5. Читаемость: кегль и контраст на всех трёх поверхностях
# ======================================================================
class TestReadableTypography:
    def _sizes(self, css):
        for selector, props in _rules(css):
            match = re.fullmatch(r"([\d.]+)(px|rem)", props.get("font-size", ""))
            if match:
                px = float(match.group(1)) * (16 if match.group(2) == "rem" else 1)
                yield selector, px

    def test_main_stylesheet_font_floor(self):
        small = [(s, px) for s, px in self._sizes(_read(CSS))
                 if px < FONT_FLOOR_PX]
        assert not small, f"кегль ниже {FONT_FLOOR_PX}px: {small[:6]}"

    def test_maxapp_font_floor(self):
        small = [(s, px) for s, px in self._sizes(_maxapp_style())
                 if px < FONT_FLOOR_PX]
        assert not small, f"мини-приложение: кегль ниже {FONT_FLOOR_PX}px {small[:6]}"

    def test_cabinet_font_floor(self):
        small = [(s, px) for s, px in self._sizes(_read(CABINET_CSS))
                 if px < FONT_FLOOR_CABINET_PX]
        assert not small, f"кабинет: кегль ниже {FONT_FLOOR_CABINET_PX}px {small[:6]}"

    def test_text_contrast_is_measured_where_resolvable(self):
        """Пары «цвет текста + непрозрачный фон» проверяются на 4.5:1.

        Полупрозрачные фоны (rgba) и градиенты здесь не разрешаются: их
        контраст измеряется в выпуске, который их ввёл, — так сделано для
        .camera-overlay в v1.70.35 (20.12:1 на тёмной плашке).
        """
        bad = []
        for name, css in (("app.css", _read(CSS)),
                          ("maxapp", _maxapp_style()),
                          ("b2b.css", _read(CABINET_CSS))):
            tokens = _tokens(css) if ":root" in css else dict(_tokens(_read(CSS)))
            for selector, props in _rules(css):
                fg = props.get("color", "").strip()
                bg = props.get("background", "").strip()
                fg = tokens.get(fg[4:-1]) if fg.startswith("var(--") else fg
                bg = tokens.get(bg[4:-1]) if bg.startswith("var(--") else bg
                if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", fg or ""):
                    continue
                if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", bg or ""):
                    continue          # прозрачный фон или градиент — не здесь
                ratio = _contrast(fg, bg)
                if ratio < CONTRAST_FLOOR:
                    bad.append(f"{name} {selector}: {fg} на {bg} = {ratio:.2f}:1")
        assert not bad, bad[:6]


# ======================================================================
# 6. Стили кабинета соответствуют разметке и скрипту
# ======================================================================
class TestCabinetStylesMatchMarkup:
    """Замок test_v17035 сверяет классы app.css с корпусом основного
    приложения; кабинет — отдельная поверхность, и её сверяет этот класс."""

    def _css_classes(self):
        classes = set()
        for selector, _props in _rules(_read(CABINET_CSS)):
            classes |= set(re.findall(r"\.([a-zA-Z][\w-]*)", selector))
        return classes

    def _used_classes(self):
        html = _read(CABINET)
        js = _read(CABINET_JS)
        used = {c for group in re.findall(r'class="([^"]+)"', html + js)
                for c in group.split()}
        used |= set(re.findall(r"\bb2b-[\w-]+", js))
        used |= set(re.findall(r"\bb2b-[\w-]+", html))
        return used

    def test_no_dead_rules(self):
        dead = sorted(c for c in self._css_classes()
                      if c.startswith("b2b") and c not in self._used_classes())
        assert not dead, f"мёртвые классы b2b.css: {dead[:8]}"

    def test_no_unstyled_classes(self):
        declared = self._css_classes()
        missing = sorted(c for c in self._used_classes()
                         if c.startswith("b2b") and c not in declared)
        assert not missing, f"классы без стиля: {missing[:8]}"


# ======================================================================
# 7. Порядок правил, от которого зависит интерлиньяж
# ======================================================================
class TestFieldOrderIsFixed:
    """`font: inherit` в общей группе сбрасывает line-height, поэтому правило
    `.b2b-field textarea` обязано стоять после неё. Перестановка сломала бы
    интерлиньяж поля комментария молча: валидатор стилей порядок не проверяет."""

    SHARED = ".b2b-field input,\n.b2b-field select,\n.b2b-field textarea {"
    OWN = ".b2b-field textarea { resize: vertical; line-height: 1.55; }"

    def test_own_rule_comes_after_shared_group(self):
        css = _read(CABINET_CSS)
        assert css.count(self.SHARED) == 1, "общая группа полей изменена"
        assert css.count(self.OWN) == 1, "правило поля комментария изменено"
        assert css.index(self.SHARED) < css.index(self.OWN)

    def test_dependency_is_documented(self):
        css = _read(CABINET_CSS)
        note = css[max(0, css.index(self.OWN) - 240):css.index(self.OWN)]
        assert "font: inherit" in note and "line-height" in note, \
            "комментарий о зависимости порядка пропал"

    def test_line_height_survives_the_reset(self):
        props = _declarations(_read(CABINET_CSS), ".b2b-field textarea")
        assert props.get("line-height") == "1.55"


# ======================================================================
# 8. Копия декодера в мини-приложении — не мусор
# ======================================================================
class TestMaxappStaysSelfContained:
    """Аудит мусора обязан был отметить эту пару: app/static/maxapp/jsQR.js и
    app/static/js/vendor/jsQR.js совпадают побайтово (256 885 Б каждый), и
    «дубль» такого размера — первая мишень для удаления.

    Копия намеренная. Мини-приложение MAX открывается внутри мессенджера и по
    решению проекта (docs/ui_journal.md, запись v1.70.45) не зависит от
    основного бандла: своя разметка, свои стили, свой скрипт. Свою камеру
    мини-приложение раскодирует именно этой копией — window.jsQR вызывается в
    maxapp.js дважды. Удаление «дубля» молча сломало бы сканирование в MAX, и
    до этого выпуска никакой тест такого удаления бы не поймал.

    Побайтовое равенство закреплено отдельно: у дубля есть вторая опасность —
    копии разъезжаются при обновлении библиотеки, и одна поверхность начинает
    распознавать коды хуже другой.
    """

    OWN = ROOT / "app/static/maxapp/jsQR.js"
    BUNDLE = ROOT / "app/static/js/vendor/jsQR.js"
    MAXAPP_JS = ROOT / "app/static/maxapp/maxapp.js"

    def test_miniapp_loads_its_own_copy(self):
        assert 'src="/maxapp/jsQR.js"' in _read(MAX_HTML)

    def test_miniapp_uses_the_decoder(self):
        assert _read(self.MAXAPP_JS).count("window.jsQR") >= 2

    def test_miniapp_does_not_reach_into_main_bundle(self):
        for path in (MAX_HTML, self.MAXAPP_JS):
            text = _read(path)
            for needle in ("/js/vendor/", "/js/app.js", "/css/app.css"):
                assert needle not in text, f"{path.name}: зависимость от бандла"

    def test_copies_have_not_diverged(self):
        own = self.OWN.read_bytes()
        assert own, "копия декодера в мини-приложении пуста"
        assert own == self.BUNDLE.read_bytes(), \
            "копии jsQR разошлись: обновлять библиотеку нужно в обоих местах"


# ======================================================================
# 9. Ритуал выпуска v1.70.46
# ======================================================================
class TestRelease17046:
    """v1.70.47: выпуск 1.70.46 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17047, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.45).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.
    """

    VERSION = "1.70.46"
    PREVIOUS = "1.70.45"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(ROOT / "app/config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 46)

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 6, "в записи выпуска не шесть пунктов"
        for needle in ("мёртвый код", "оранжевый", "ореол", "пунктуации",
                       "пикселей", "нагрузочного стенда", "кабинета"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        """Проза для владельца — русская; латиница только в имени API и в
        адресах почты."""
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"API", "chek", "ymaster", "ru"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("_set_sqlite_pragma", ".scan-line", "#22d3ee",
                       "var(--accent)", "load45", "font: inherit", "1.81:1",
                       "test_v17046", "2 344 312", "Проверено и отвергнуто"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"

    def test_changelog_quotes_the_owner(self):
        """Запись выпуска начинается требованием владельца дословно."""
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in ("проведи аудит программы чтоб текст и дизайн был валидным",
                       "почисти весь муссор"):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_changelog_records_rejected_findings(self):
        """Правило журнала: ложные срабатывания записываются, чтобы их не
        искали повторно. В выпуске аудита это половина результата."""
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("camera-overlay", "20.12:1", "icon-64.png",
                       "префиксы форм собственности"):
            assert needle in section, f"в отвергнутых находках нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("ореол", "2.61:1", "4.54:1", "7.75:1", "11.2:1",
                       "font: inherit", "2 344 312", "Проверено и отвергнуто"):
            assert needle in section, f"в журнале нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        """Журнал после v1.70.38 идёт по убыванию — новая запись выше."""
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")
