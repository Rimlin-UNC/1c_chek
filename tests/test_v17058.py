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
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.57) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.58"
    PREVIOUS = "1.70.57"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in (
            APP / "config.py").read_text(encoding="utf-8")
        assert f"ymaster-check-v{self.VERSION}" in SW.read_text(
            encoding="utf-8")
        assert f'"version": "{self.VERSION}"' in MANIFEST.read_text(
            encoding="utf-8")
        index = INDEX.read_text(encoding="utf-8")
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index
        assert f"maxapp.js?v={self.VERSION}" in MAX_HTML.read_text(
            encoding="utf-8")

    def test_cache_busters_in_service_worker(self):
        sw = SW.read_text(encoding="utf-8")
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = CABINET.read_text(encoding="utf-8")
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_changed_assets_are_served_with_the_release_version(self):
        index = INDEX.read_text(encoding="utf-8")
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index

    def test_no_previous_version_left_in_sync_points(self):
        for path in (APP / "config.py", INDEX, SW, MANIFEST, MAX_HTML,
                     CABINET):
            text = path.read_text(encoding="utf-8")
            for form in (f"v={self.PREVIOUS}", f'"{self.PREVIOUS}"',
                         f"check-v{self.PREVIOUS}"):
                assert form not in text, f"{path.name}: {form}"

    def test_whats_new_is_first(self):
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'",
                          APP_JS)
        assert found and found.group(1) == self.VERSION, \
            "запись выпуска не первая"

    def _block(self):
        return APP_JS.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("сетк", "ориентац", "строк", "пролёт"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"MAX", "Telegram", "service", "worker"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("сетк", "ориентац", "ct-text", "перенос",
                       "кэш-бастер", "Отклонено", "overflow-wrap"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section, f"в CHANGELOG нет «{head}»"

    def test_changelog_quotes_the_owner(self):
        section = CHANGELOG.read_text(encoding="utf-8").split(
            f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        assert OWNER_QUOTE in flat, "нет дословной жалобы владельца"

    def test_journal_entry(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "сетк", "ориентац",
                       "портрет", "ландшафт", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ",
                          path.read_text(encoding="utf-8")).lower()
            assert OWNER_QUOTE in flat, f"{path.name}: нет жалобы владельца"

    def test_previous_release_class_is_history_only(self):
        src = RELEASE_57_TESTS.read_text(encoding="utf-8")
        block = src.split("class TestRelease17057:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = RELEASE_57_TESTS.read_text(encoding="utf-8")
        block = src.split("class TestRelease17057:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
