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
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.66) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.67"
    PREVIOUS = "1.70.66"

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
        for needle in ("мини-прилож", "кнопка", "старт", "кабинет"):
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
        for needle in ("startapp", "мини-приложение", "кнопка",
                       "кэш-бастер", "отклонено"):
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
                       "Отклонённые варианты", "мини-приложение",
                       "startapp", "кэш-бастер"):
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
        src = _read(RELEASE_66_TESTS)
        block = src.split("class TestRelease17066:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_66_TESTS)
        block = src.split("class TestRelease17066:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
