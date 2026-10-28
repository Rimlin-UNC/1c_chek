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
    окне спрашивает: «🛡 В мини-приложение» (диплинк бота без startapp —
    мини-приложение открывается сразу в кабинете участника) или
    «Остаться здесь»; билет из адреса убирается history.replaceState;
 3. в профиле мини-приложения карточка с кнопкой «🌐 Открыть
    приложение» — переход на сайт (браузер или установленное приложение).

Что здесь проверяется:
 1. Сквозной поток: код → подтверждение из мини-приложения → web_link с
    билетом → поллинг билета даёт сессию сайта → билет одноразовый,
    исходный код живёт своей жизнью.
 2. Клиент сайта: диалог возврата, диплинк бота без startapp, чистка
    адреса после билетного входа.
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
        assert "/#/max-login/" in link, "билет не в ссылке на сайт"
        ticket = link.split("/#/max-login/", 1)[1]
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
        # диплинк бота без startapp — мини-приложение откроется в кабинете
        assert js.count("'https://max.ru/' + (window.MXBOT || "
                        "'chek.ymaster.bot')") >= 2

    def test_ticket_hash_cleaned_after_login(self):
        js = _read(APP_JS)
        block = js.split("if (p.status === 'ok') {", 1)[1]
        assert "history.replaceState(null, '', location.pathname + " \
            "'#/dashboard')" in block
        assert "maxBackDialog()" in block

    def test_miniapp_redirects_after_confirm(self):
        js = _read(MAX_JS)
        assert "'Готово! Открываем сайт…'" in js
        assert "openLink(r.web_link)" in js
        assert "Готово! Вернитесь на сайт — вход выполнится сам." in js, \
            "ручная подсказка осталась фолбэком"

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
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.63) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.64"
    PREVIOUS = "1.70.63"

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
        for needle in ("окошко", "кабинет", "сайт", "билет"):
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
        for needle in ("билет", "max-login", "окошко", "кэш-бастер",
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
                       "Отклонённые варианты", "билет", "окошко",
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
        src = _read(RELEASE_63_TESTS)
        block = src.split("class TestRelease17063:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_63_TESTS)
        block = src.split("class TestRelease17063:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
