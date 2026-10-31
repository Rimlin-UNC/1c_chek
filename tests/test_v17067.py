"""v1.70.67 — кнопка «В мини-приложение» открывает кабинет MAX сразу.

Задача владельца (дословно, существенная часть): «когда я нажимаю
перейти в мини приложение то он меня переслал в бота а оттуда мне
пришлось руками нажимать старт и попадать в приложение макс это не каждый
сможет понять» и «теперь всё работает но мне кажется что есть ошибки
проверь и исправь потому что не все гладко проходит как то коряво и
чопорно».

Ревизия: кнопка «🛡 В мини-приложение» в диалоге возврата открывала
голый диплинк бота https://max.ru/<ник>. Голый диплинк приводит в ЧАТ
БОТА: человеку приходилось вручную нажимать «Старт» и только потом
попадать в мини-приложение. Остальные переходы сайта в MAX (вход,
привязка, приглашения, реферальные ссылки) всегда несли параметр
?startapp=… — по ним мини-приложение открывается сразу. Решение v1.70.64
«голая ссылка откроет кабинет MAX сразу» опровергнуто полевым отчётом
владельца.

Решение:
 1. Кнопка «🛡 В мини-приложение» открывает maxBotLink('back') —
    диплинк со startapp=back: мини-приложение стартует сразу в кабинете
    участника (afterBoot параметр back не разбирает специально — человек
    попадает в обычный кабинет, что и нужно);
 2. Текст диалога говорит по-человечески: «ваш кабинет участника
    откроется сразу, ничего нажимать в чате бота не придётся»;
 3. Голых диплинков бота в app.js не осталось: все три вхождения адреса
    несут ?startapp=…;
 4. Замок v1.70.64 «диплинк без startapp» сужен до присутствия диалога,
    форма диплинка теперь здесь.

Что здесь проверяется:
 1. Замки исходников: кнопка диалога, форма maxBotLink, отсутствие
    голых диплинков, текст диалога, afterBoot без изменений.
 2. Сквозной поток билета и одноразовость (путь v1.70.65 цел).
 3. Ритуал выпуска 1.70.67 и историзация TestRelease17066.
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
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_66_TESTS = ROOT / "tests" / "test_v17066.py"
RELEASE_64_TESTS = ROOT / "tests" / "test_v17064.py"

OWNER_QUOTE_A = ("когда я нажимаю перейти в мини приложение то он меня "
                 "переслал в бота а оттуда мне пришлось руками нажимать "
                 "старт и попадать в приложение макс это не каждый сможет "
                 "понять")
OWNER_QUOTE_B = ("теперь всё работает но мне кажется что есть ошибки "
                 "проверь и исправь потому что не все гладко проходит "
                 "как то коряво и чопорно")

BOT_URL = "'https://max.ru/' + (window.MXBOT || 'chek.ymaster.bot')"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestBackLinkLock:
    """Кнопка «В мини-приложение» ведёт сразу в мини-приложение."""

    def test_back_button_uses_startapp(self):
        js = _read(APP_JS)
        block = js.split("function maxBackDialog() {", 1)[1].split(
            "function showLogin() {", 1)[0]
        assert "maxBotLink('back')" in block
        assert "window.open(maxBotLink('back'), '_blank')" in block

    def test_maxbotlink_form(self):
        js = _read(APP_JS)
        assert "?startapp=' + encodeURIComponent(startParam)" in js

    def test_no_bare_bot_deeplink_left(self):
        js = _read(APP_JS)
        # каждое вхождение адреса бота несёт параметр запуска
        parts = js.split(BOT_URL)[1:]
        assert len(parts) >= 3
        for tail in parts:
            head = tail[:120]
            assert "startapp" in head, "найден голый диплинк бота"

    def test_dialog_text_is_human(self):
        js = _read(APP_JS)
        block = js.split("function maxBackDialog() {", 1)[1].split(
            "function showLogin() {", 1)[0]
        assert "ничего нажимать в чате бота не" in block
        assert "Остаться здесь" in block
        assert "В мини-приложение" in block

    def test_afterboot_still_routes_login_and_invite_only(self):
        js = _read(MAX_JS)
        block = js.split("async function afterBoot(sp) {", 1)[1].split(
            "// ---- v1.70.37", 1)[0]
        assert "if (sp.indexOf('login_') === 0) showLoginConfirm(sp);" in block
        assert "else if (sp.indexOf('inv_') === 0) showInvite(sp);" in block
        # back специально не разбирается: человек попадает в обычный кабинет

    def test_v64_lock_narrowed_not_deleted(self):
        src = _read(RELEASE_64_TESTS)
        assert "test_site_dialog_offers_return_or_stay" in src
        assert "maxBotLink('back')" in src
        assert "диплинк бота без startapp" not in src

    def test_ticket_flow_still_end_to_end(self, client):
        _setup()
        code = client.post("/api/v1/auth/max/start", json={}).json()["code"]
        sess = client.post("/api/v1/maxapp/session",
                           json={"init_data": _init_data(90002,
                                                         "back67")}).json()
        hdr = {"Authorization": "Bearer " + sess["access_token"]}
        r = client.post("/api/v1/maxapp/confirm-login", headers=hdr,
                        json={"code": code,
                              "init_data": _init_data(90002, "back67")})
        assert r.status_code == 200, r.text
        link = r.json()["web_link"]
        assert "/?maxlogin=" in link and "#" not in link
        ticket = link.split("/?maxlogin=", 1)[1]
        ok = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert ok.json()["status"] == "ok"
        again = client.post("/api/v1/auth/max/poll", json={"code": ticket})
        assert again.json().get("status") == "expired"

    def test_miniapp_tap_path_untouched(self):
        js = _read(MAX_JS)
        block = js.split("async function showLoginConfirm(", 1)[1].split(
            "async function showInvite(", 1)[0]
        assert "setTimeout(() => openLink(" not in block
        assert "openLink(r.web_link)" in block


class TestRelease17067:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.68).
    Здесь остаётся запись выпуска 1.70.67 в истории."""

    VERSION = "1.70.67"
    PREVIOUS = "1.70.66"

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
        assert "когда я нажимаю перейти в мини приложение" in flat

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
