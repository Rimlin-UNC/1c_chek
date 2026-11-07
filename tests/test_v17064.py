"""v1.70.64 — подтверждение входа в MAX перекидывает на сайт, сайт спрашивает
«вернуться в мини-приложение или остаться», в профиле мини-приложения —
кнопка «Открыть приложение».

Задача владельца (дословно, существенная часть): «Нужно исправить, чтоб
при нажатии «подтвердить вход» перекидывало на сайт или приложение, а а
сайте или в приложении всплывало окошко с вопросом вернуться в мини
приложение Макс или остаться здесь (в нашем стиле корпоративном), если
нажать и перейти в мини приложение Макс то попадать в свой кабинет Макса
и работать там, в мини приложение в профиле добавить кнопку открыть
приложение и сделать переход в приложение».

Ревизия: после «Подтвердить вход» мини-приложение показывало статику
«Готово! Вернитесь на сайт — вход выполнится сам.» — человек оставался в
MAX и должен был сам возвращаться в браузер. Механика одноразового
веб-билета уже существовала для приглашений (make_webticket + маршрут
#/max-login/), но подтверждение входа её не использовало.

Решение:
 1. /api/v1/maxapp/confirm-login возвращает web_link с одноразовым
    билетом; мини-приложение через 600 мс само открывает его (openLink —
    нативный или окно браузера), сообщение «Готово! Открываем сайт…»;
 2. сайт по билету входит (прежний поллинг) и в корпоративном модальном
    окне спрашивает: «🛡 В мини-приложение» или «Остаться здесь»; с
    v1.70.67 диплинк несёт startapp=back — мини-приложение открывается
    сразу в кабинете участника, а не в чате бота (форма — в
    test_v17067); билет из адреса убирается history.replaceState;
 3. в профиле мини-приложения карточка с кнопкой «🌐 Открыть
    приложение» — переход на сайт (браузер или установленное приложение).

Что здесь проверяется:
 1. Сквозной поток: код → подтверждение из мини-приложения → web_link с
    билетом → поллинг билета даёт сессию сайта → билет одноразовый,
    исходный код живёт своей жизнью.
 2. Клиент сайта: диалог возврата (форма диплинка — в test_v17067),
    чистка адреса после билетного входа.
 3. Мини-приложение: автопереход после подтверждения и кнопка профиля.
 4. Ритуал выпуска 1.70.64 и историзация TestRelease17063.
"""

import hashlib
import hmac
import json
import pathlib
import re
import time
import urllib.parse

BOT_TOKEN = "700001.ABCDEF"

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
RELEASE_63_TESTS = ROOT / "tests" / "test_v17063.py"

OWNER_QUOTE_A = ("перекидывало на сайт или приложение, а а сайте или в "
                 "приложении всплывало окошко с вопросом вернуться в мини "
                 "приложение макс или остаться здесь")
OWNER_QUOTE_B = ("в мини приложение в профиле добавить кнопку открыть "
                 "приложение и сделать переход в приложение")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _setup(nick="chek.ymaster.bot"):
    from app.database import SessionLocal
    from app.services import appsettings
    s = SessionLocal()
    try:
        appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
        appsettings.set_setting(s, "max_bot_username", nick)
    finally:
        s.rollback(); s.close()


def _init_data(uid: int, uname: str) -> str:
    ad = int(time.time())
    raw = [("auth_date", str(ad)), ("query_id", f"q{uid}"),
           ("user", json.dumps({"id": uid, "username": uname}))]
    launch = "\n".join(f"{k}={v}" for k, v in sorted(raw))
    sec = hmac.new(b"WebAppData", BOT_TOKEN.encode(),
                   hashlib.sha256).digest()
    sig = hmac.new(sec, launch.encode(), hashlib.sha256).hexdigest()
    pairs = [(k, urllib.parse.quote(v, safe="")) for k, v in raw]
    pairs.append(("hash", sig))
    return "&".join(f"{k}={v}" for k, v in pairs)


def _pool_headers(client, uid: int, uname: str) -> dict:
    sess = client.post("/api/v1/maxapp/session",
                       json={"init_data": _init_data(uid, uname)}).json()
    return {"Authorization": "Bearer " + sess["access_token"]}


class TestMaxReturnFlow:
    """Подтверждение входа перекидывает на сайт одноразовым билетом."""

    def test_confirm_returns_single_use_weblink(self, client):
        _setup()
        code = client.post("/api/v1/auth/max/start", json={}).json()["code"]
        hdr = _pool_headers(client, 86001, "ret64")
        r = client.post("/api/v1/maxapp/confirm-login", headers=hdr,
                        json={"code": code, "init_data":
                              _init_data(86001, "ret64")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "confirmed"
        link = body["web_link"]
        # v1.70.65: без фрагмента — chooser/WebAPK открывают надёжно
        assert "/?maxlogin=" in link, "билет не в ссылке на сайт"
        assert "#/" not in link
        ticket = link.split("/?maxlogin=", 1)[1]
        # сайт по билету входит сам
        p = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert p.status_code == 200 and p.json()["status"] == "ok"
        assert p.json()["access_token"]
        # билет одноразовый
        p2 = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert p2.json().get("status") == "expired"
        # исходный код входа живёт своей жизнью (основной вклад сайта)
        p3 = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p3.json()["status"] == "ok"

    def test_confirm_without_pool_session_rejected(self, client):
        _setup()
        code = client.post("/api/v1/auth/max/start", json={}).json()["code"]
        r = client.post("/api/v1/maxapp/confirm-login",
                        json={"code": code, "init_data": ""})
        assert r.status_code in (401, 403)

    def test_site_dialog_offers_return_or_stay(self):
        js = _read(APP_JS)
        assert "function maxBackDialog()" in js
        assert "Продолжить в мини-приложении MAX?" in js
        assert "Остаться здесь" in js
        assert "В мини-приложение" in js
        # v1.70.67: кнопка ведёт в мини-приложение сразу (startapp=back);
        # форма диплинка в ведении test_v17067, здесь — присутствие ссылок
        assert "maxBotLink('back')" in js
        assert js.count("'https://max.ru/' + (window.MXBOT || "
                        "'chek.ymaster.bot')") >= 2

    def test_ticket_hash_cleaned_after_login(self):
        js = _read(APP_JS)
        block = js.split("if (p.status === 'ok') {", 1)[1]
        assert "history.replaceState(null, '', location.pathname + " \
            "'#/dashboard')" in block
        assert "maxBackDialog()" in block

    def test_miniapp_opens_site_by_tap_after_confirm(self):
        js = _read(MAX_JS)
        # v1.70.65: автооткрытия без касания больше нет — оно зависало
        # на chooser андроида; переход только из касания (путь профиля)
        block = js.split("async function showLoginConfirm(", 1)[1].split(
            "async function showInvite(", 1)[0]
        assert "setTimeout(() => openLink(r.web_link)" not in block
        assert 'id="svc-open"' in js or "id=\"svc-open\"" in js
        assert "openLink(r.web_link)" in js
        assert "Вход подтверждён — нажмите «Открыть сайт»" in js
        assert "Готово! Вернитесь на сайт — вход выполнится сам." in js, \
            "ручная подсказка осталась фолбэком"

    def test_site_reads_ticket_from_query(self):
        js = _read(APP_JS)
        assert "maxlogin=([A-Za-z0-9_-]{16,150})" in js
        assert "maxLoginFlow(mxl[1], true)" in js

    def test_profile_has_open_app_button(self):
        html = _read(MAX_HTML)
        assert 'id="btn-open-app"' in html
        assert "Открыть приложение" in html
        js = _read(MAX_JS)
        assert "$('btn-open-app')" in js
        assert "openLink(location.origin + '/')" in js

    def test_server_issues_ticket_in_confirm(self):
        src = _read(MAXAPP_PY)
        assert "make_webticket" in src
        block = src.split("def confirm_login(", 1)[1].split("\n@router", 1)[0]
        assert "web_link" in block and "make_webticket" in block


class TestRelease17064:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.65).
    Здесь остаётся запись выпуска 1.70.64 в истории."""

    VERSION = "1.70.64"
    PREVIOUS = "1.70.63"

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
        assert "кнопку открыть приложение и сделать переход" in flat

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
