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
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.67).
    Здесь остаётся запись выпуска 1.70.66 в истории."""

    VERSION = "1.70.66"
    PREVIOUS = "1.70.65"

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
        assert "не работает после последовательных действий" in flat

    def test_journal_has_release(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_whats_new_entry_exists_in_history(self):
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        assert f"'{self.VERSION}': [" in js

    def test_trackers_reference_a_release(self):
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert re.search(r"\"1\.70\.\d+\"", text)

    def test_sw_cache_name_is_versioned(self):
        sw = SW.read_text(encoding="utf-8")
        assert re.search(r"ymaster-check-v1\.70\.\d+", sw)

    def test_no_placeholder_left(self):
        for path in (CHANGELOG, JOURNAL):
            assert "TODO" not in path.read_text(encoding="utf-8")
