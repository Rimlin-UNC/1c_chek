"""v1.70.58 — читаемые оглавления блоков: сетка заголовка и ориентации.

Жалоба владельца (дословно): «Исправь вёрстку чтоб она была читайма
потому что оглавления блоков ужасная вёрстка, проведи ориентацию.»
Скриншоты с телефона: оглавление карточки «Мониторинг OpenStack»
свернулось в столбики — «МОНИТ / ОРИНГ / OPENST / ACK» и версия
«(V 1. 70 .5 1)» по букве на строку; у «Почтового центра» название и
подзаголовок стоят тесными колонками.

Причина (замер по стилям): `.card-title` — нерастягиваемая флекс-строка
`display: flex; align-items: center`, а правило v1.70.52
`.card-title, .modal-title, h1, h2, h3 { overflow-wrap: anywhere; }`
разрешает перенос внутри слова, поэтому минимальная ширина текстового
фрагмента — одна буква. На узком экране значок состояния и стрелка
съедают место, и текст сжимается в столбики слогов. Безымянный текстовый
узел флекса нельзя адресовать стилями — отсюда отдельный пролёт
`.ct-text`, который сборщик сворачиваемых карточек создаёт сам.

Решение: на телефоне (портрет до 640px) и в низком ландшафте
(ориентация проведена: обе ориентации одним списком медиаусловий)
оглавление становится сеткой — строка 1: название и стрелка, строка 2:
подзаголовок, строка 3: значок состояния. Заголовок страницы на телефоне
переносится на вторую строку вместо обрезания точками; базовое правило
v1.12.2 (одна строка с «…» для планшета и выше) не тронуто.

Что здесь проверяется:
 1. Сборщик сворачиваемых карточек оборачивает ведущий текст в .ct-text;
    замки сворачивания v1.21.0 целы.
 2. Сетка оглавления: колонки, строки для текста/стрелки/подзаголовка/
    значка, скрытый распорка; базовый флекс и overflow-wrap целы.
 3. Ориентация проведена: правило действует и в портрете до 640px, и в
    ландшафте при высоте до 560px.
 4. Заголовок страницы: мобильный перенос есть, базовая одна строка
    с «…» и fluid-размер целы.
 5. Ритуал выпуска 1.70.58 и историзация TestRelease17057.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CSS = (APP / "static" / "css" / "app.css").read_text(encoding="utf-8")
APP_JS = (APP / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = APP / "static" / "index.html"
MAX_HTML = APP / "static" / "maxapp" / "index.html"
CABINET = APP / "static" / "b2b" / "index.html"
SW = APP / "static" / "sw.js"
MANIFEST = APP / "static" / "manifest.webmanifest"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_57_TESTS = ROOT / "tests" / "test_v17057.py"

OWNER_QUOTE = ("исправь вёрстку чтоб она была читайма потому что "
               "оглавления блоков ужасная вёрстка, проведи ориентацию")


def _media_block(css: str, header: str) -> str:
    """Тело блока @media, начинающегося с указанного заголовка условий."""
    idx = css.index(header)
    start = css.index("{", idx)
    depth = 0
    for pos in range(start, len(css)):
        if css[pos] == "{":
            depth += 1
        elif css[pos] == "}":
            depth -= 1
            if depth == 0:
                return css[start:pos + 1]
    raise AssertionError("непарные скобки в медиаблоке")


class TestCtTextWalker:
    """Сборщик сворачиваемых карточек даёт тексту оглавления имя."""

    def test_walker_wraps_leading_text(self):
        assert "ctText.className = 'ct-text';" in APP_JS
        assert "title.firstChild.nodeType === 3" in APP_JS
        assert "title.insertBefore(ctText, title.firstChild)" in APP_JS

    def test_wrap_happens_before_chevron(self):
        js = APP_JS
        i_wrap = js.index("ctText.className = 'ct-text';")
        i_chev = js.index("ch.className = 'fold-chevron';")
        assert i_wrap < i_chev, "пролёт создаётся раньше стрелки"

    def test_fold_locks_are_intact(self):
        # замок v1.21.0: механиз сворачивания не тронут
        for needle in ("makeSettingsCollapsible", "fold-chevron",
                       "fold-body", "ymaster-fold", "foldable"):
            assert needle in APP_JS, needle
        assert ".foldable.collapsed > .fold-body" in CSS
        assert ".fold-chevron" in CSS


class TestHeaderGrid:
    """Оглавление блока на узком экране — сетка, а не сжатая строка."""

    MEDIA = ("@media (max-width: 640px), (orientation: landscape) "
             "and (max-height: 560px)")

    def test_ct_text_span_is_styled(self):
        assert ".card-title > .ct-text { min-width: 0; }" in CSS

    def test_grid_replaces_flex_row(self):
        block = _media_block(CSS, self.MEDIA)
        head = block.split(".glass.card.foldable > .card-title {", 1)[1]
        head = head.split("}", 1)[0]
        assert "display: grid" in head
        assert "grid-template-columns: minmax(0, 1fr) auto" in head

    def test_rows_are_assigned(self):
        block = _media_block(CSS, self.MEDIA)
        assert "> .ct-text { grid-column: 1; grid-row: 1; }" in block
        assert "> .fold-chevron {" in block and "grid-row: 1" in block
        assert "> .form-hint { grid-column: 1 / -1; grid-row: 2; }" in block
        assert "> .chip { grid-column: 1 / -1; grid-row: 3;" in block
        assert "> .spacer { display: none; }" in block

    def test_base_flex_and_wrap_locks_are_intact(self):
        base = re.search(r"\.card-title \{[^}]*\}", CSS).group(0)
        assert "display: flex" in base
        assert ".card-title .spacer { flex: 1; }" in CSS
        assert (".card-title, .modal-title, h1, h2, h3 "
                "{ overflow-wrap: anywhere; }") in CSS


class TestOrientationAudit:
    """Ориентация проведена: портрет и ландшафт покрыты одним правилом."""

    def test_media_list_covers_portrait_and_landscape(self):
        idx = CSS.index(TestHeaderGrid.MEDIA)
        cond = CSS[idx:CSS.index("{", idx)]
        assert "max-width: 640px" in cond
        assert "orientation: landscape" in cond
        assert _media_block(CSS, TestHeaderGrid.MEDIA)

    def test_landscape_short_height_is_the_landscape_case(self):
        # низкий ландшафт телефона: ширина больше 640, высота меньше 560
        assert "max-height: 560px" in TestHeaderGrid.MEDIA

    def test_other_landscape_rules_are_intact(self):
        assert "@media (max-height: 560px) and (orientation: landscape) {" \
            in CSS
        assert ".login-card { padding: 18px 20px 12px; }" in CSS


class TestPageTitleMobile:
    """Заголовок страницы: на телефоне перенос, выше — одна строка."""

    def test_base_rule_still_one_line_with_ellipsis(self):
        base = re.search(r"\.page-title \{[^}]*\}", CSS).group(0)
        assert "text-overflow: ellipsis" in base
        assert "white-space: nowrap" in base
        assert "min-width: 0" in base

    def test_mobile_override_wraps_instead_of_clipping(self):
        block = _media_block(CSS, "@media (max-width: 640px) {")
        # последний такой блок в файле — мобильный перенос v1.70.58
        tail = CSS.rsplit("@media (max-width: 640px) {", 1)[1]
        rule = tail.split(".page-title {", 1)[1].split("}", 1)[0]
        assert "white-space: normal" in rule
        assert "text-overflow: clip" in rule
        assert "font-size: 17px" in rule
        assert block

    def test_fluid_type_lock_is_intact(self):
        assert ("#page-title { font-size: clamp(19px, 2.6vw, 26px); "
                "line-height: 1.2; }") in CSS


class TestRelease17058:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.59).
    Здесь остаётся запись выпуска 1.70.58 в истории."""

    VERSION = "1.70.58"
    PREVIOUS = "1.70.57"

    def test_version_is_release(self):
        src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        # живое значение сверяет выпуск-владелец; здесь — форма релиза
        assert re.search(r"APP_VERSION: str = \"1\.70\.\d+\"", src)

    def test_changelog_has_release(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        assert f"## [{self.VERSION}]" in text

    def test_changelog_quotes_the_owner(self):
        section = CHANGELOG.read_text(encoding="utf-8").split(
            f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        assert "оглавления блоков ужасная вёрстка" in flat

    def test_journal_has_release(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_whats_new_entry_exists_in_history(self):
        assert f"'{self.VERSION}': [" in APP_JS

    def test_trackers_reference_a_release(self):
        # история: трекеры существуют и сверяют версию выпуска формой
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert re.search(r'"1\.70\.\d+"', text)

    def test_sw_cache_name_is_versioned(self):
        sw = SW.read_text(encoding="utf-8")
        assert re.search(r"ymaster-check-v1\.70\.\d+", sw)

    def test_no_placeholder_left(self):
        for path in (CHANGELOG, JOURNAL):
            assert "TODO" not in path.read_text(encoding="utf-8")
