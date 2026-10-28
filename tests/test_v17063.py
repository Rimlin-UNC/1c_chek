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
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.62) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.63"
    PREVIOUS = "1.70.62"

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
        for needle in ("биометр", "ключ", "привязк", "отпечатк"):
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
        for needle in ("двойн", "упаков", "allowcredentials", "кэш-бастер",
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
                       "Отклонённые варианты", "двойн", "упаков",
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
        src = _read(RELEASE_62_TESTS)
        block = src.split("class TestRelease17062:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_62_TESTS)
        block = src.split("class TestRelease17062:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
