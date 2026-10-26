"""v1.70.59 — тест-код, фильтр страниц валидаторами, новые технологии.

Задача владельца (дословно, существенная часть): «Протестируй код, а
страницы профильтруй через валидаторы используй новые и совместимые
технологии исправь баги, по ходу исправления и нахождения ошибок что
добавить в программу чтоб она стала более удобной и понятной для
пользователя.»

Прогон фильтров (html-validate 11.17.0 по трём страницам, stylelint
17.16.0 по всем стилям, ESLint 9 flat-config по нашим JS-модулям) нашёл
семь ошибок ESLint, две из которых — живые дефекты интерфейса:

* `openInviteQr` была объявлена ВНУТРИ экрана «Пользователи», а вызов
  сидел в диалоге создания приглашения: клик по кнопке квадратного кода
  падал ReferenceError, кнопка была мертва (no-undef);
* обработчик массового обновления ЕГРЮЛ звал `load()` — функции нет в
  этой области видимости: таблица после обновления не перечитывалась,
  в catch прилетало «load is not defined» (no-undef);
* четыре мёртвых инициализатора (`let seen = ''`, `let reduced = false`,
  `let cfg = null`, `let items = []`) — значение гибло, не прочитанное
  (no-useless-assignment);
* ошибка камеры выбрасывалась без причины — исходное исключение терялось
  (preserve-caught-error).

По ходу добавлены удобства: светлая цветовая схема объявлена явно
(тёмная тема браузера больше не перекрашивает формы в тёмные на светлом
фоне), контейнер уведомлений получил роль статуса для экранного чтеца.

Что здесь проверяется:
 1. ESLint-ворота: bin есть — ноль ошибок на наших модулях; конфига
    хватает без флагов (vendor исключён в конфиге).
 2. Каждая починенная ошибка заперта источником.
 3. Удобства: цветовая схема на трёх страницах, роль статуса у тостов,
    автозаполнение входа цело.
 4. Ритуал выпуска 1.70.59 и историзация TestRelease17058.
"""

import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
APP_JS = STATIC / "js" / "app.js"
SCANNER_JS = STATIC / "js" / "scanner.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
ESLINT_CONFIG = ROOT / "eslint.config.mjs"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_58_TESTS = ROOT / "tests" / "test_v17058.py"

OWNER_QUOTE_A = "страницы профильтруй через валидаторы"
OWNER_QUOTE_B = "чтоб она стала более удобной и понятной для пользователя"

ESLINT_BIN = os.environ.get(
    "ESLINT_BIN", str(ROOT / "node_modules" / ".bin" / "eslint"))


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestEslintGate:
    """Новая совместимая технология: ESLint flat-config в репозитории."""

    def test_config_exists_and_ignores_vendor(self):
        src = _read(ESLINT_CONFIG)
        assert "js.configs.recommended" in src
        assert "app/static/js/vendor/**" in src
        assert "globals.browser" in src

    def test_zero_errors_on_our_modules(self):
        if not pathlib.Path(ESLINT_BIN).exists():
            import pytest
            pytest.skip("ESLint не установлен: npm i --no-save eslint "
                        "@eslint/js globals (не обязателен в CI)")
        proc = subprocess.run(
            [ESLINT_BIN, "app/static/js", "--quiet"],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
            check=False)
        assert proc.returncode == 0, proc.stdout[-2000:]


class TestBugFixes:
    """Семь ошибок ESLint починены и заперты."""

    def test_invite_qr_handler_is_module_level(self):
        js = _read(APP_JS)
        assert re.search(r"^async function openInviteQr\(inv, url\) \{",
                         js, re.M), "функция объявлена на уровне модуля"
        assert js.count("openInviteQr(") >= 3, "вызовы из диалога и списка"

    def test_invite_qr_is_not_nested_in_a_screen(self):
        js = _read(APP_JS)
        # ровно одно определение — без отступа, то есть уровень модуля
        assert len(re.findall(r"^async function openInviteQr", js, re.M)) == 1
        assert "  async function openInviteQr" not in js

    def test_bulk_refresh_reloads_the_screen(self):
        js = _read(APP_JS)
        assert ("console.warn('bulk-refresh errors:', r.errors);\n"
                "      route(true);") in js

    def test_dead_initializers_are_gone(self):
        js = _read(APP_JS)
        assert "let seen;\n" in js
        assert "let reduced;\n" in js
        assert "let cfg, rules = [];" in js
        assert "let items;\n" in js
        assert "let seen = '';" not in js
        assert "let reduced = false;" not in js

    def test_camera_error_keeps_its_cause(self):
        js = _read(SCANNER_JS)
        assert "{ cause: e }" in js
        assert "Нет доступа к камере" in js


class TestUxCompat:
    """Удобства и совместимость, добавленные по ходу фильтра."""

    def test_light_color_scheme_on_every_page(self):
        for page in (INDEX, MAX_HTML, CABINET):
            assert '<meta name="color-scheme" content="light">' in \
                _read(page), page.name

    def test_toasts_are_announced_to_screen_readers(self):
        html = _read(INDEX)
        assert 'id="toast-container" role="status" aria-live="polite"' in html

    def test_login_autocomplete_is_intact(self):
        html = _read(INDEX)
        assert 'autocomplete="username"' in html
        assert 'autocomplete="current-password"' in html

    def test_pages_still_pass_html_validate(self):
        binp = "/tmp/htmlval/node_modules/.bin/html-validate"
        if not pathlib.Path(binp).exists():
            import pytest
            pytest.skip("html-validate не установлен")
        proc = subprocess.run(
            [binp, "--formatter=json", str(INDEX), str(MAX_HTML),
             str(CABINET)],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
            check=False)
        import json
        data = json.loads(proc.stdout or "[]")
        errors = [m for f in data for m in f.get("messages", [])
                  if m["severity"] == 2]
        assert not errors, errors[:5]


class TestRelease17059:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.58) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.59"
    PREVIOUS = "1.70.58"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(
            APP / "config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read(SW)
        assert f'"version": "{self.VERSION}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        sw = _read(SW)
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_changed_assets_are_served_with_the_release_version(self):
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index

    def test_no_previous_version_left_in_sync_points(self):
        for path in (APP / "config.py", INDEX, SW, MANIFEST, MAX_HTML,
                     CABINET):
            text = _read(path)
            for form in (f"v={self.PREVIOUS}", f'"{self.PREVIOUS}"',
                         f"check-v{self.PREVIOUS}"):
                assert form not in text, f"{path.name}: {form}"

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self.VERSION, \
            "запись выпуска не первая"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("квадратн", "обновл", "цветов", "статус"):
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
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("eslint", "no-undef", "cause", "color-scheme",
                       "aria-live", "route(true)", "кэш-бастер",
                       "Отклонено", "валидатор"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section, f"в CHANGELOG нет «{head}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "eslint", "валидатор",
                       "причина", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            for needle in (OWNER_QUOTE_A, OWNER_QUOTE_B):
                assert needle in flat, f"{path.name}: нет цитаты владельца"

    def test_previous_release_class_is_history_only(self):
        src = _read(RELEASE_58_TESTS)
        block = src.split("class TestRelease17058:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_58_TESTS)
        block = src.split("class TestRelease17058:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
