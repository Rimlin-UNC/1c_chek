"""v1.70.65 — подтверждение входа в MAX не зависает: переход касанием,
билет без фрагмента.

Задача владельца (дословно, существенная часть): «Разберись не работает,
когда в приложении мини Макс нажимаешь на подтверждение то спрашивает как
андройд какое приложение при выборе приложения оно зависает, если
вернутся в Макс и перейти в основное меню и потом в профиль и нажать
перейти в приложение то всё запускается и работает хорошо, нужно исправить
видимо есть ошибки».

Ревизия: v1.70.64 открывала сайт программно, setTimeout(openLink(...))
через 600 мс после ответа сервера — БЕЗ касания человека. Внешнее открытие
ссылки без жеста пользовательского действия в WebView/андроиде ведёт себя
непредсказуемо: системный chooser «каким приложением открыть» и зависание
на выборе — ровно то, что видел владелец. Кнопка профиля из касания
(openLink в onclick) работала безупречно — владелец сам это проверил и
описал. Вторая слабость: ссылка с фрагментом (#/max-login/…) в chooser и
WebAPK ведёт себя иначе, чем проверенная владельцем ссылка без фрагмента.

Решение:
 1. автооткрытие без касания убрано вовсе: после подтверждения на экране
    мгновенно появляется большая кнопка «🌐 Открыть сайт» — открытие
    ссылки ИЗ касания, тот же проверенный путь, что кнопка профиля;
 2. web_link строится без фрагмента: SITE_URL/?maxlogin=<билет> — форма
    ссылки как у проверенной владельцем; сайт читает билет из запроса;
 3. сайт поддерживает оба входа билета: из запроса ?maxlogin= и прежний
    из фрагмента #/max-login/ (приглашения); билет из адреса убирается
    history.replaceState после входа, окошко возврата осталось.

Что здесь проверяется:
 1. Сквозной поток: подтверждение → web_link без фрагмента → сайт входит
    по билету из запроса → билет одноразовый.
 2. Экран подтверждения: автооткрытия нет, кнопка из касания есть.
 3. Сайт читает билет и из запроса, и из фрагмента.
 4. Ритуал выпуска 1.70.65 и историзация TestRelease17064.
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
MAXAPP_PY = APP / "routers" / "maxapp.py"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_64_TESTS = ROOT / "tests" / "test_v17064.py"

OWNER_QUOTE_A = ("спрашивает как андройд какое приложение при выборе "
                 "приложения оно зависает")
OWNER_QUOTE_B = ("нажать перейти в приложение то всё запускается и "
                 "работает хорошо")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestConfirmNoHang:
    """Переход на сайт — только из касания, ссылка без фрагмента."""

    def _confirm(self, client, uid=88001, uname="nohang65"):
        _setup()
        code = client.post("/api/v1/auth/max/start", json={}).json()["code"]
        sess = client.post("/api/v1/maxapp/session",
                           json={"init_data": _init_data(uid, uname)}).json()
        hdr = {"Authorization": "Bearer " + sess["access_token"]}
        r = client.post("/api/v1/maxapp/confirm-login", headers=hdr,
                        json={"code": code,
                              "init_data": _init_data(uid, uname)})
        assert r.status_code == 200, r.text
        return code, r.json()

    def test_weblink_has_no_fragment(self, client):
        code, body = self._confirm(client)
        link = body["web_link"]
        assert "/?maxlogin=" in link
        assert "#" not in link, "фрагмент в chooser/WebAPK вёл себя плохо"
        ticket = link.split("/?maxlogin=", 1)[1]
        p = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert p.json()["status"] == "ok"
        p2 = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert p2.json().get("status") == "expired"

    def test_confirm_screen_has_tap_button_not_autoopen(self):
        js = _read(MAX_JS)
        block = js.split("async function showLoginConfirm(", 1)[1].split(
            "async function showInvite(", 1)[0]
        assert "setTimeout(() => openLink(" not in block, \
            "автооткрытие без касания вернулось"
        assert "id=\"svc-open\"" in block
        assert "openLink(r.web_link)" in block
        assert "so.onclick" in block, "открытие только из касания"

    def test_site_reads_ticket_from_query_and_fragment(self):
        js = _read(APP_JS)
        assert "maxlogin=([A-Za-z0-9_-]{16,150})" in js
        assert "#\\/max-login\\/([A-Za-z0-9_-]{16,150})" in js
        assert "maxLoginFlow(mxl[1], true)" in js

    def test_ticket_cleaned_from_address_after_login(self):
        js = _read(APP_JS)
        block = js.split("if (p.status === 'ok') {", 1)[1]
        assert "history.replaceState(null, '', location.pathname + " \
            "'#/dashboard')" in block

    def test_return_dialog_still_offered(self):
        js = _read(APP_JS)
        assert "function maxBackDialog()" in js
        assert "Остаться здесь" in js and "В мини-приложение" in js

    def test_profile_button_untouched_and_working(self):
        js = _read(MAX_JS)
        assert "openLink(location.origin + '/')" in js
        assert 'id="btn-open-app"' in _read(MAX_HTML)

    def test_server_builds_query_form_link(self):
        src = _read(MAXAPP_PY)
        block = src.split("def confirm_login(", 1)[1].split("\n@router", 1)[0]
        assert "/?maxlogin={ticket}" in block
        assert "/#/max-login/{ticket}" not in block


class TestRelease17065:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.66).
    Здесь остаётся запись выпуска 1.70.65 в истории."""

    VERSION = "1.70.65"
    PREVIOUS = "1.70.64"

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
        assert "при выборе приложения оно зависает" in flat

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
