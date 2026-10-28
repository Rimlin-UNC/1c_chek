"""v1.70.66 — заставка не виснет на входе по билету: ветка снимает её сама.

Задача владельца (дословно, существенная часть): «Не работает после
последовательных действий зависает как на скрине разберись, проведи
тесты». Скрин: заставка загрузки (логотип «Ямастер Чек» + спиннер) поверх
всего интерфейса в Chrome/WebAPK после последовательности «подтвердить
вход в MAX → открыть сайт».

Ревизия: ранняя ветка boot() с билетом — `#/max-login/` с v1.70.0 и
`?maxlogin=` с v1.70.65 — возвращалась из загрузки, не сняв заставку:
showSplash(false) стоял в ветках регистрации, токена и финальной, но не в
билетной. Заставка — div с z-index 300 поверх всего: вход и модальное окно
ожидания билета работали ПОД ней, со стороны это выглядело как зависание
на логотипе. Дефект латентный: до v1.70.65 переход на сайт по билету
редко доходил до загрузки (автооткрытие зависало раньше), а приглашения
владелец гонял редко.

Решение:
 1. билетная ветка boot() снимает заставку: showSplash(false) перед
    showLogin() и maxLoginFlow(...);
 2. страховка: enterApp() снимает заставку на любом пути входа — ни одна
    будущая ранняя ветка не сможет оставить её висеть;
 3. регрессия закрыта НАСТОЯЩИМ тестом интерфейса: tests/js/splash.test.js
    (Vitest, jsdom) поднимает реальный app.js с разметкой index.html и
    заглушкой fetch и сверяет, что заставка снята, а экран входа и окно
    ожидания видимы — для билета в запросе, билета во фрагменте и обычного
    входа без токена; на коде v1.70.65 тест падает, на v1.70.66 зелёный.

Что здесь проверяется:
 1. Замки исходников: билетная ветка и enterApp снимают заставку; все
    ранние ветки boot() содержат showSplash(false).
 2. jsdom-тест заставки существует и покрывает три сценария.
 3. Сквозной поток билета: подтверждение → web_link → вход по билету →
    билет одноразовый.
 4. Ритуал выпуска 1.70.66 и историзация TestRelease17065.
"""

import pathlib
import re

from tests.test_v17064 import _init_data, _setup

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
MAX_JS = STATIC / "maxapp" / "maxapp.js"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
SPLASH_TEST = ROOT / "tests" / "js" / "splash.test.js"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_65_TESTS = ROOT / "tests" / "test_v17065.py"

OWNER_QUOTE_A = ("не работает после последовательных действий зависает "
                 "как на скрине разберись")
OWNER_QUOTE_B = "проведи тесты"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestSplashLock:
    """Заставка снята на каждом пути загрузки, особенно на билетном."""

    def test_ticket_branch_hides_splash(self):
        js = _read(APP_JS)
        assert "if (mxl) { showSplash(false); showLogin(); " \
            "maxLoginFlow(mxl[1], true); return; }" in js

    def test_enterapp_hides_splash_as_insurance(self):
        js = _read(APP_JS)
        block = js.split("function enterApp() {", 1)[1].split("}", 1)[0]
        assert "showSplash(false);" in block

    def test_every_early_boot_branch_hides_splash(self):
        js = _read(APP_JS)
        boot = js.split("async function boot() {", 1)[1].split(
            "function showSplash(", 1)[0]
        # билет, регистрация, токен, финаль — ни одной ветки без снятия
        assert boot.count("showSplash(false)") >= 4
        for branch in ("if (mxl)", "if (m)", "if (getToken())"):
            assert branch in boot

    def test_jsdom_splash_test_covers_three_scenarios(self):
        src = _read(SPLASH_TEST)
        assert src.count("bootApp(") >= 4   # определение + 3 сценария
        for needle in ("'/?maxlogin=' + TICKET", "'/#/max-login/' + TICKET",
                       "bootApp('/')"):
            assert needle in src
        assert "expect(splash.style.display).toBe('none')" in src

    def test_ticket_flow_still_end_to_end(self, client):
        _setup()
        code = client.post("/api/v1/auth/max/start", json={}).json()["code"]
        sess = client.post("/api/v1/maxapp/session",
                           json={"init_data": _init_data(90001,
                                                         "splash66")}).json()
        hdr = {"Authorization": "Bearer " + sess["access_token"]}
        r = client.post("/api/v1/maxapp/confirm-login", headers=hdr,
                        json={"code": code,
                              "init_data": _init_data(90001, "splash66")})
        assert r.status_code == 200, r.text
        link = r.json()["web_link"]
        assert "/?maxlogin=" in link and "#" not in link
        ticket = link.split("/?maxlogin=", 1)[1]
        ok = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert ok.json()["status"] == "ok"
        again = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert again.json().get("status") == "expired"

    def test_max_path_untouched(self):
        js = _read(MAX_JS)
        block = js.split("async function showLoginConfirm(", 1)[1].split(
            "async function showInvite(", 1)[0]
        assert "setTimeout(() => openLink(" not in block
        assert "openLink(r.web_link)" in block
        assert "openLink(location.origin + '/')" in js


class TestRelease17066:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.65) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.66"
    PREVIOUS = "1.70.65"

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
        for needle in ("заставк", "билет", "вход", "касание"):
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
        flat = section.lower()
        for needle in ("заставк", "boot-splash", "билет", "кэш-бастер",
                       "отклонено"):
            assert needle in flat, f"в CHANGELOG нет «{needle}»"
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
                       "Отклонённые варианты", "заставк", "билет",
                       "кэш-бастер"):
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
        src = _read(RELEASE_65_TESTS)
        block = src.split("class TestRelease17065:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_65_TESTS)
        block = src.split("class TestRelease17065:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
