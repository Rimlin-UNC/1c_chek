"""v1.70.62 — быстрый вход: биометрия сразу, без лишнего листа выбора.

Задача владельца (дословно, существенная часть): «В программе у нас
реализована вход по отпечатку пальца или по лицу посмотри как это делают
профессионалы и подправь потому что у нас при выборе по отпечатку пальца
запрашивается ещё дополнительная информация сделай так чтобы появлялась
сразу без запроса как на скрине. Как это реализовано в других программах.
А так всё работает хорошо».

Ревизия: серверная часть церемонии здорова (v1.43.0, программный
аутентификатор в тестах), но клиент слал discoverable-запрос: без логина,
с пустым allowCredentials и медиацией по умолчанию. Именно поэтому над
экраном входа всплывал системный лист выбора аккаунта (Google Менеджер
паролей / Credential Manager) с кнопкой «Продолжить» — та самая
«дополнительная информация» со скриншота владельца.

Решение по профессиональным образцам (web.dev, MDN, Chrome IO25):
  1) mediation: 'immediate' — есть ключ на устройстве → браузер сразу
     просит биометрию, нет → молча выходит без всяких листов;
  2) allowCredentials по логину (поле или последний успешный вход) —
     церемония привязана к учётке, платформе нечего предлагать на выбор;
  3) условный autofill: autocomplete="username webauthn" на поле логина
     и фоновый запрос с mediation: 'conditional' и AbortController —
     passkey в подсказках клавиатуры, касание даёт биометрию без модалки.

Что здесь проверяется:
 1. Кнопка быстрого входа идёт через mediation 'immediate' и привязку
    allowCredentials; старый браузер (TypeError на словаре опций) и
    отсутствие ключа (NotFoundError) обработаны отдельно от отмены.
 2. Условный autofill зарегистрирован корректно: фиче-детект
    isConditionalMediationAvailable, пустой allowCredentials,
    AbortController, одномоментно один живой WebAuthn-запрос.
 3. Сервер отдаёт allowCredentials по логину и discoverable-ответ без
    логина / для неизвестного логина (фолбэк клиента).
 4. Ритуал выпуска 1.70.62 и историзация TestRelease17061.
"""

import pathlib
import re

from tests.conftest import login

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_61_TESTS = ROOT / "tests" / "test_v17061.py"

OWNER_QUOTE_A = ("при выборе по отпечатку пальца запрашивается ещё "
                 "дополнительная информация")
OWNER_QUOTE_B = "посмотри как это делают профессионалы"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestPasskeyImmediateUx:
    """Клиентская церемония идёт к биометрии без модального селектора."""

    def test_button_requests_immediate_mediation(self):
        js = _read(APP_JS)
        assert "waLoginCore(waLoginUsername(), 'immediate')" in js

    def test_ceremony_pinned_by_allowcredentials(self):
        js = _read(APP_JS)
        assert "get.allowCredentials = opts.allowCredentials.map" in js

    def test_last_login_feeds_the_ceremony(self):
        js = _read(APP_JS)
        assert "ymaster-wa-last" in js
        assert "function waLoginUsername()" in js
        assert "waRememberLogin(state.me);" in js

    def test_old_browser_fallback_without_mediation(self):
        js = _read(APP_JS)
        assert "e instanceof TypeError" in js

    def test_no_key_distinct_from_cancel(self):
        js = _read(APP_JS)
        assert "NotFoundError" in js
        assert "err.waNoKey = true" in js
        assert "waNoKey" in js.split("el.textContent = e.waNoKey", 1)[0] or \
            "e.waNoKey" in js

    def test_conditional_autofill_registered(self):
        js = _read(APP_JS)
        assert "isConditionalMediationAvailable" in js
        assert "new AbortController()" in js
        assert "'conditional'" in js

    def test_conditional_keeps_allowcredentials_empty(self):
        js = _read(APP_JS)
        assert "waLoginCore('', 'conditional', signal)" in js

    def test_single_outstanding_webauthn_request(self):
        js = _read(APP_JS)
        btn = js.split("$('#login-passkey').onclick", 1)[1]
        assert btn.index("waAbortConditional()") < btn.index(
            "waLoginCore(waLoginUsername(), 'immediate')")
        assert "waAbortConditional();                     " in js or \
            "waAbortConditional();" in js.split("function enterApp()", 1)[1]

    def test_login_input_announces_webauthn_autofill(self):
        assert 'autocomplete="username webauthn"' in _read(INDEX)

    def test_server_pins_allowcredentials_to_username(self, client):
        from app.database import SessionLocal
        from app.models import WebauthnCredential

        login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me").json()
        db = SessionLocal()
        try:
            db.add(WebauthnCredential(id="v17062-cred-a",
                                      user_id=me["id"], label="Тест"))
            db.commit()
            data = client.post("/api/v1/auth/passkey/login/options",
                               json={"username": "admin"}).json()
            assert data.get("allowCredentials"), "нет привязки к учётке"
            import base64
            ids = [base64.urlsafe_b64decode(
                c["id"] + "=" * (-len(c["id"]) % 4)).decode()
                for c in data["allowCredentials"]]
            assert "v17062-cred-a" in ids
            empty = client.post("/api/v1/auth/passkey/login/options",
                                json={"username": None}).json()
            assert not empty.get("allowCredentials"), \
                "discoverable-запрос обязан быть без списка"
        finally:
            db.query(WebauthnCredential).filter(
                WebauthnCredential.id == "v17062-cred-a").delete()
            db.commit()
            db.close()

    def test_unknown_login_falls_back_to_discoverable(self, client):
        data = client.post("/api/v1/auth/passkey/login/options",
                           json={"username": "nosuch-v17062"}).json()
        assert not data.get("allowCredentials")
        assert data.get("challenge")


class TestRelease17062:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.61) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.62"
    PREVIOUS = "1.70.61"

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
        for needle in ("биометр", "подсказк", "касание", "ключ"):
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
        for needle in ("биометри", "селектор", "allowcredentials",
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
                       "Отклонённые варианты", "биометри", "подсказк",
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
        src = _read(RELEASE_61_TESTS)
        block = src.split("class TestRelease17061:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_61_TESTS)
        block = src.split("class TestRelease17061:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
