"""v1.70.63 — быстрый вход починен: идентификатор ключа без двойной упаковки.

Задача владельца (дословно, существенная часть): «Теперь не удаётся совсем
зайти по отпечатку пальцев эта версия не даёт возможности зайти скрины
сбросил посмотри… через настройки отвязал устройство заново привязал
устройство но история повторяется то есть нет возможности зайти…
Разберись и сделай как это делают программисты профессионалы… сделай так
чтобы комар носа не подточил».

Скрины владельца: лист Credential Manager «Ключи доступа не найдены — на
этом устройстве нет ключей доступа для приложения chek.ymaster.ru» при
живом ключе (Chrome 156, Android 14, WebAPK), затем «Вход отменён».
Перепривязка устройства не помогала — история повторялась.

Ревизия: дефект внесён в v1.70.62 вместе с привязкой церемонии к учётке.
Сервер строил дескрипторы PublicKeyCredentialDescriptor(id=c.id.encode()),
где c.id — сохранённая строка base64url; библиотека упаковывала эти
ascii-байты в base64url ещё раз. Платформа получала двойную упаковку
вместо сырого идентификатора ключа и честно докладывала: ключей для RP
нет. До v1.70.62 кнопка ходила discoverable-запросом (пустой список),
поэтому дефект дремал; тесты церемонии его не ловили, потому что
программный аутентификатор подписывает ответ своим идентификатором, не
глядя на allowCredentials.

Решение: _raw_id() декодирует сохранённую строку в сырые байты до
построения дескриптора (и в allow_credentials входа, и в
exclude_credentials привязки); библиотека пакует их один раз — провод
несёт ровно тот идентификатор, что вернуло устройство при привязке.

Что здесь проверяется:
 1. Сквозная церемония (программный аутентификатор ES256): привязка →
    опции входа с логином → идентификатор в allowCredentials равен
    сохранённому при привязке и декодируется в сырые байты ключа → вход
    по отпечатку даёт токен.
 2. Форма провода: exclude_credentials привязки несёт тот же формат;
    двойная упаковка исключена и в исходниках (нет c.id.encode()).
 3. Клиентский сценарий v1.70.62 сохранён: mediation 'immediate',
    привязка по логину, условный autofill.
 4. Ритуал выпуска 1.70.63 и историзация TestRelease17062.
"""

import base64
import pathlib
import re
import uuid

from tests.conftest import login
from tests.test_v1430 import RP_ID, SoftAuthenticator

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
WEBAUTHN_PY = APP / "routers" / "webauthn.py"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_62_TESTS = ROOT / "tests" / "test_v17062.py"

OWNER_QUOTE_A = ("теперь не удаётся совсем зайти по отпечатку пальцев "
                 "эта версия не даёт возможности зайти")
OWNER_QUOTE_B = "сделай так чтобы комар носа не подточил"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TestWireFormatLock:
    """Провод несёт сырой идентификатор ключа — и ничего больше."""

    def _register(self, client):
        """Привязка программного ключа от имени свежей учётки."""
        uname = "wa63" + uuid.uuid4().hex[:8]
        adm = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={"role": "user"},
                          headers=adm).json()
        r = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": uname,
            "password": "parol123", "full_name": "Тест v1.70.63"})
        assert r.status_code in (200, 201), r.text
        hdr = {"Authorization": "Bearer " + r.json()["access_token"]}
        auth = SoftAuthenticator()
        opts = client.post("/api/v1/auth/passkey/register/options",
                           json={}, headers=hdr).json()
        reg = auth.register(opts["challenge_hex"])
        v = client.post("/api/v1/auth/passkey/register/verify",
                        json={**reg, "label": "Телефон владельца"},
                        headers=hdr)
        assert v.status_code == 200, v.text
        return uname, hdr, auth, reg

    def test_ceremony_wire_id_equals_stored_id(self, client):
        uname, hdr, auth, reg = self._register(client)
        lo = client.post("/api/v1/auth/passkey/login/options",
                         json={"username": uname})
        assert lo.status_code == 200, lo.text
        lopts = lo.json()
        assert len(lopts["allowCredentials"]) == 1
        wire = lopts["allowCredentials"][0]["id"]
        # провод == сохранённая при привязке строка (устройство её узнаёт)
        assert wire == reg["id"], "двойная упаковка идентификатора"
        # клиент декодирует провод ровно в сырые байты ключа
        assert _b64u_dec(wire) == auth.cred_id

    def test_ceremony_login_by_fingerprint_works(self, client):
        uname, hdr, auth, reg = self._register(client)
        lopts = client.post("/api/v1/auth/passkey/login/options",
                            json={"username": uname}).json()
        assertion = auth.assert_(lopts["challenge_hex"])
        lv = client.post("/api/v1/auth/passkey/login/verify",
                         json=assertion)
        assert lv.status_code == 200, lv.text
        body = lv.json()
        assert body["access_token"] and body["user"]["username"] == uname

    def test_exclude_credentials_carry_raw_ids(self, client):
        uname, hdr, auth, reg = self._register(client)
        opts = client.post("/api/v1/auth/passkey/register/options",
                           json={}, headers=hdr).json()
        assert opts["excludeCredentials"], "привязанный ключ не исключён"
        wire = opts["excludeCredentials"][0]["id"]
        assert wire == reg["id"]
        assert _b64u_dec(wire) == auth.cred_id

    def test_no_double_packing_left_in_source(self):
        src = _read(WEBAUTHN_PY)
        assert "id=c.id.encode()" not in src
        assert src.count("id=_raw_id(c.id)") == 2, "allow и exclude"

    def test_raw_id_helper_roundtrip(self):
        from app.routers.webauthn import _raw_id

        raw = bytes(range(32))
        stored = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        assert _raw_id(stored) == raw
        assert _raw_id("не base64") == "не base64".encode()

    def test_client_scenario_of_v17062_is_kept(self):
        js = _read(APP_JS)
        assert "waLoginCore(waLoginUsername(), 'immediate')" in js
        assert "get.allowCredentials = opts.allowCredentials.map" in js
        assert "WA_B64.dec(c.id)" in js
        assert "waLoginCore('', 'conditional', signal)" in js
        assert 'autocomplete="username webauthn"' in _read(INDEX)

    def test_rp_id_still_single_for_both_ceremonies(self):
        src = _read(WEBAUTHN_PY)
        assert src.count("rp_id=_rp_id(request)") >= 2
        assert RP_ID == "testserver"


class TestRelease17063:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.64).
    Здесь остаётся запись выпуска 1.70.63 в истории."""

    VERSION = "1.70.63"
    PREVIOUS = "1.70.62"

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
        assert "комар носа не подточил" in flat

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
