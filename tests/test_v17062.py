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
        import base64
        import os

        from webauthn.helpers import bytes_to_base64url

        from app.database import SessionLocal
        from app.models import WebauthnCredential

        raw = os.urandom(32)                 # как сырой id устройства
        cred_id = bytes_to_base64url(raw)    # так сервер хранит ключ
        login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me").json()
        db = SessionLocal()
        try:
            db.add(WebauthnCredential(id=cred_id, user_id=me["id"],
                                      label="Тест"))
            db.commit()
            data = client.post("/api/v1/auth/passkey/login/options",
                               json={"username": "admin"}).json()
            assert data.get("allowCredentials"), "нет привязки к учётке"
            # провод несёт ровно тот id, что вернуло устройство при
            # привязке: двойная упаковка (дефект v1.70.62) исключена
            assert [c["id"] for c in data["allowCredentials"]] == [cred_id]
            wire = data["allowCredentials"][0]["id"]
            dec = base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))
            assert dec == raw, "клиент декодирует в сырые байты ключа"
            empty = client.post("/api/v1/auth/passkey/login/options",
                                json={"username": None}).json()
            assert not empty.get("allowCredentials"), \
                "discoverable-запрос обязан быть без списка"
        finally:
            db.query(WebauthnCredential).filter(
                WebauthnCredential.id == cred_id).delete()
            db.commit()
            db.close()

    def test_unknown_login_falls_back_to_discoverable(self, client):
        data = client.post("/api/v1/auth/passkey/login/options",
                           json={"username": "nosuch-v17062"}).json()
        assert not data.get("allowCredentials")
        assert data.get("challenge")


class TestRelease17062:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.63).
    Здесь остаётся запись выпуска 1.70.62 в истории."""

    VERSION = "1.70.62"
    PREVIOUS = "1.70.61"

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
        assert "запрашивается ещё дополнительная информация" in flat

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
