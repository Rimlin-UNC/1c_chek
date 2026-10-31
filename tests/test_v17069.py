"""v1.70.69 — вход и регистрация через Telegram на сайте «как у Макса».

Задача владельца (дословно): «Регистрация через Telegram «как у Макса» —
следующий выпуск» (сразу после выпуска прокси 1.70.68, где объём был
подтверждён: «только ТЗ по прокси», регистрация — следующим шагом).

Приём тот же, что у MAX (v1.70.0), поэтому человеку не приходится учить
новый порядок действий: сайт создаёт одноразовый код на десять минут и
хранит только его хэш; подтверждение приходит из чата с ботом — по
кнопке «Открыть Telegram» (deep-link «/start tglogin_…») или просто
кодом в сообщении; подтверждённый код даёт обычную сессию сайта
(create_access_token + HttpOnly-cookie), как при входе по паролю.

Что здесь проверяется:
 1. Поток кода: старт без токена бота (409), код и ссылка t.me,
    ожидание, подтверждение ботом, вход, повторный вход без дублей,
    одноразовость, истечение, лимит кодов на IP.
 2. Регистрация: личный аккаунт «пользователь», выключается настройкой
    telegram_web_register, отключённая учётная запись — 403.
 3. Соседство с привязкой уведомлений: короткий 6-значный код привязки
    работает по-прежнему, длинный код входа его не перехватывает.
 4. Интерфейс: кнопка на экране входа, поток в app.js, адрес API.
 5. Ритуал выпуска 1.70.69 и историзация TestRelease17068.
"""

import pathlib
import re
from datetime import timedelta

import pytest

from app.models import TelegramLoginCode, utcnow

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
CONFIG = APP / "config.py"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_68_TESTS = ROOT / "tests" / "test_v17068.py"

OWNER_QUOTE_A = "регистрация через telegram"
OWNER_QUOTE_B = "как у макса"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _iso_rate():
    """Лимит кодов — счётчик процесса; между тестами сбрасываем."""
    from app.services import telegram_login
    telegram_login._ip_times.clear()
    yield
    telegram_login._ip_times.clear()


def _token(db) -> None:
    """Токен бота — иначе поток кода честно отказывает (409)."""
    from app.services import appsettings
    from app.services import telegram_bot as tg
    appsettings.set_setting(db, tg.SETT_TOKEN, "123456:TEST-TOKEN")


def _send(db, chat_id: str, text: str, username: str = "",
          first_name: str = "Тест") -> None:
    """Сообщение боту — как из Telegram (сеть не нужна: отправка
    подменена в фикстуре)."""
    from app.services import telegram_bot as tg
    tg.handle_update(db, {"message": {
        "chat": {"id": chat_id}, "text": text,
        "from": {"id": chat_id, "username": username,
                 "first_name": first_name}}})


class TestTelegramWebLogin:
    """Поток одноразового кода — зеркало TestMaxWebLogin (v1.70.0)."""

    def test_start_requires_bot_token(self, client):
        r = client.post("/api/v1/auth/telegram/start", json={})
        assert r.status_code == 409, r.text
        assert "не настроен" in r.json()["detail"]

    def test_start_makes_code_and_link(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "_bot_username", "chek_ymaster_bot")
        db = SessionLocal()
        try:
            _token(db)
            r = client.post("/api/v1/auth/telegram/start", json={})
        finally:
            db.close()
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["code"].startswith("tglogin_") and len(d["code"]) >= 30
        # start-параметр Telegram ограничен 64 символами
        assert len(d["code"]) <= 64
        assert d["tg_link"] == ("https://t.me/chek_ymaster_bot"
                                f"?start={d['code']}")
        assert d["expires_in"] == 600

    def test_start_without_username_has_no_link(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "_bot_username", "")
        db = SessionLocal()
        try:
            _token(db)
            r = client.post("/api/v1/auth/telegram/start", json={})
        finally:
            db.close()
        assert r.status_code == 200 and "tg_link" not in r.json()

    def test_deep_link_confirm_then_login(self, client, monkeypatch):
        """Полный цикл: код → «/start tglogin_…» в чате → сессия сайта."""
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        sent = []
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            sent.append((chat_id, text)) or True)
        db = SessionLocal()
        try:
            _token(db)
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            p = client.post("/api/v1/auth/telegram/poll", json={"code": code})
            assert p.json()["status"] == "pending"
            _send(db, "700100200", "/start " + code, username="tgbotuser")
        finally:
            db.close()
        assert sent and "Вход на сайт подтверждён" in sent[0][1]
        p2 = client.post("/api/v1/auth/telegram/poll", json={"code": code})
        assert p2.status_code == 200, p2.text
        d = p2.json()
        assert d["status"] == "ok" and d["access_token"]
        assert d["user"]["username"] == "tgbotuser"
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["username"] == "tgbotuser"
        from app.models import User
        db = SessionLocal()
        try:
            u = (db.query(User)
                 .filter(User.telegram_chat_id == "700100200").first())
            assert u is not None
            assert u.role == "user" and u.company_id is None
        finally:
            db.close()

    def test_bare_code_also_confirms(self, client, monkeypatch):
        """Код без «/start» — человек просто вставил его в чат."""
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        sent = []
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            sent.append(text) or True)
        db = SessionLocal()
        try:
            _token(db)
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            _send(db, "700100201", "  " + code + "  ")
        finally:
            db.close()
        assert sent and "подтверждён" in sent[0]
        p = client.post("/api/v1/auth/telegram/poll", json={"code": code})
        assert p.json()["status"] == "ok"

    def test_relogin_same_chat_no_dup(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            True)
        ids = []
        db = SessionLocal()
        try:
            _token(db)
            for _ in range(2):
                code = client.post("/api/v1/auth/telegram/start",
                                   json={}).json()["code"]
                _send(db, "700100202", code, username="tgdup")
                p = client.post("/api/v1/auth/telegram/poll",
                                json={"code": code})
                assert p.json()["status"] == "ok"
                ids.append(p.json()["user"]["id"])
        finally:
            db.close()
        assert ids[0] == ids[1]                        # дубликатов нет

    def test_single_use(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            True)
        db = SessionLocal()
        try:
            _token(db)
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            _send(db, "700100203", code)
        finally:
            db.close()
        p1 = client.post("/api/v1/auth/telegram/poll", json={"code": code})
        assert p1.json()["status"] == "ok"
        p2 = client.post("/api/v1/auth/telegram/poll", json={"code": code})
        assert p2.json()["status"] == "expired"        # повторно нельзя

    def test_unknown_code_expired(self, client):
        p = client.post("/api/v1/auth/telegram/poll",
                        json={"code": "tglogin_" + "z" * 32})
        assert p.status_code == 200 and p.json()["status"] == "expired"

    def test_short_code_rejected_by_schema(self, client):
        p = client.post("/api/v1/auth/telegram/poll", json={"code": "abc"})
        assert p.status_code == 422                    # границы схемы

    def test_register_disabled_403(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            True)
        db = SessionLocal()
        try:
            _token(db)
            appsettings.set_setting(db, "telegram_web_register", "0")
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            _send(db, "700100204", code)
        finally:
            db.close()
        p = client.post("/api/v1/auth/telegram/poll", json={"code": code})
        assert p.status_code == 403                    # регистрация закрыта
        assert "выключена" in p.json()["detail"]

    def test_inactive_account_403(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.models import User
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            True)
        db = SessionLocal()
        try:
            _token(db)
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            _send(db, "700100205", code, username="tgoff")
        finally:
            db.close()
        assert client.post("/api/v1/auth/telegram/poll",
                           json={"code": code}).json()["status"] == "ok"
        db = SessionLocal()
        try:
            u = (db.query(User)
                 .filter(User.telegram_chat_id == "700100205").first())
            uid = u.id
            u.is_active = False
            db.commit()
        finally:
            db.close()
        code2 = client.post("/api/v1/auth/telegram/start",
                            json={}).json()["code"]
        db = SessionLocal()
        try:
            _send(db, "700100205", code2)               # тот же чат
        finally:
            db.close()
        p = client.post("/api/v1/auth/telegram/poll", json={"code": code2})
        assert p.status_code == 403
        db = SessionLocal()
        try:
            db.get(User, uid).is_active = True          # вернуть как было
            db.commit()
        finally:
            db.close()

    def test_expired_code_is_refused(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            True)
        db = SessionLocal()
        try:
            _token(db)
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            from app.services import telegram_login as tl
            row = (db.query(TelegramLoginCode)
                   .filter(TelegramLoginCode.code_hash == tl._code_hash(code))
                   .first())
            assert row is not None
            row.expires_at = utcnow() - timedelta(seconds=5)
            db.commit()
            _send(db, "700100206", code)
        finally:
            db.close()
        p = client.post("/api/v1/auth/telegram/poll", json={"code": code})
        assert p.json()["status"] == "expired"

    def test_rate_limit_start(self, client):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _token(db)
        finally:
            db.close()
        last = 0
        for _ in range(10):
            last = client.post("/api/v1/auth/telegram/start",
                               json={}).status_code
        assert last == 200, last
        assert client.post("/api/v1/auth/telegram/start",
                           json={}).status_code == 429

    def test_audit_log_written(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.models import AuditLog
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            True)
        db = SessionLocal()
        try:
            _token(db)
            code = client.post("/api/v1/auth/telegram/start",
                               json={}).json()["code"]
            _send(db, "700100207", code, username="tgaudit")
        finally:
            db.close()
        assert client.post("/api/v1/auth/telegram/poll",
                           json={"code": code}).json()["status"] == "ok"
        db = SessionLocal()
        try:
            row = (db.query(AuditLog)
                   .filter(AuditLog.action == "login_telegram")
                   .order_by(AuditLog.id.desc()).first())
            assert row is not None
            assert "tgaudit" in (row.details or "")
        finally:
            db.close()


class TestBindCodeNotHijacked:
    """Короткий код привязки уведомлений и длинный код входа не путаются."""

    def test_short_bind_code_still_binds(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.models import User
        from app.services import telegram_bot as tg
        sent = []
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text, proxy="", base="":
                            sent.append(text) or True)
        db = SessionLocal()
        try:
            _token(db)
            admin = db.query(User).filter(User.username == "admin").first()
            code = tg.new_bind_code(db, admin.id)
            assert re.fullmatch(r"\d{6}", code)
            _send(db, "700100301", "/start " + code)
            db.refresh(admin)
            assert admin.telegram_chat_id == "700100301"
            admin.telegram_chat_id = None       # вернуть как было
            db.commit()
        finally:
            db.close()
        assert sent and "Привязано" in sent[0]

    def test_login_code_is_not_a_bind_code(self):
        """Префикс и длина кода входа исключают столкновение."""
        from app.services import telegram_login
        assert telegram_login.CODE_TTL_S == 600
        assert telegram_login.RATE_MAX == 10
        raw = "tglogin_" + "A" * 32
        assert not re.fullmatch(r"\d{6}", raw)
        assert "tglogin_" not in raw[7:]
        assert TelegramLoginCode.__tablename__ == "telegram_login_codes"

    def test_login_branch_precedes_bind_branch(self):
        """Ветка кода входа обрабатывается раньше привязки: deep-link
        приходит как «/start tglogin_…», и 6-значный разбор его бы
        потерял."""
        src = _read(APP / "services" / "telegram_bot.py")
        i_login = src.index('re.search(r"tglogin_')
        i_bind = src.index('if text_low.startswith("/start")')
        assert i_login < i_bind
        assert "telegram_login.confirm" in src


class TestBotUsernameRespectsApiBase:
    """Найдено живым прогоном v1.70.69: имя бота обновлялось в обход
    настройки «Адрес API» (v1.70.12). В условиях блокировки это ломало
    ссылку t.me в окне входа: оставался только путь «код текстом»."""

    def test_refresh_username_passes_api_base(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        from app.services import telegram_bot as tg
        seen = {}

        def fake_me(token, proxy="", base=""):
            seen.update({"token": token, "proxy": proxy, "base": base})
            return {"username": "chek_ymaster_bot", "name": "Чек"}

        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_TOKEN, "123456:TEST")
            appsettings.set_setting(db, tg.SETT_API_BASE,
                                    "http://127.0.0.1:8080")
            monkeypatch.setattr(tg, "get_me", fake_me)
            got = tg.refresh_username(db)
        finally:
            db.close()
        assert got == "chek_ymaster_bot"
        assert seen["base"] == "http://127.0.0.1:8080", seen
        # имя доступно настройкам — значит попадёт и в ссылку t.me
        assert tg.bot_settings(SessionLocal())["bot_username"] == \
            "chek_ymaster_bot"

    def test_login_link_appears_after_refresh(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, telegram_login
        from app.services import telegram_bot as tg
        db = SessionLocal()
        try:
            monkeypatch.setattr(tg, "_bot_username", "chek_ymaster_bot")
            appsettings.set_setting(db, tg.SETT_TOKEN, "123456:TEST")
            out = telegram_login.create_code(db, "10.0.0.1")
        finally:
            db.close()
        assert out["tg_link"].startswith("https://t.me/chek_ymaster_bot"
                                         "?start=tglogin_")


class TestTelegramLoginUi:
    """Кнопка и поток на сайте — без ручных шагов в чате бота."""

    def test_login_button_on_login_screen(self):
        html = _read(INDEX)
        assert 'id="login-telegram"' in html
        assert "через Telegram" in html

    def test_flow_uses_api_and_polling(self):
        js = _read(APP_JS)
        assert "async function telegramLoginFlow(code, tgLink)" in js
        assert "/api/v1/auth/telegram/start" in js
        assert "/api/v1/auth/telegram/poll" in js
        assert "telegramLoginFlow(r.code, r.tg_link || '')" in js
        assert "$('#login-telegram')" in js

    def test_flow_opens_link_only_on_tap(self):
        """v1.65: в WebView без жеста window.open зависает — ссылка
        открывается только кнопкой «Открыть Telegram»."""
        js = _read(APP_JS)
        block = js.split("async function telegramLoginFlow", 1)[1].split(
            "function maxBackDialog", 1)[0]
        assert "window.open(tgLink, '_blank')" in block
        assert "document.querySelector('#tgl-open')" not in block
        # без имени бота кнопки нет — остаётся код и подсказка
        assert "${tgLink ?" in block

    def test_flow_closes_modal_on_success(self):
        block = _read(APP_JS).split("async function telegramLoginFlow",
                                    1)[1].split("function maxBackDialog",
                                                1)[0]
        assert "setToken(p.access_token)" in block
        assert "enterApp()" in block
        assert "expired" in block


class TestConcurrentPoll:
    """Два одновременных опроса одного подтверждённого кода не должны
    создать две учётные записи на один чат (две вкладки, двойной клик)."""

    def test_parallel_polls_make_one_account(self, monkeypatch):
        import threading as _th

        from app.database import SessionLocal
        from app.models import User
        from app.services import appsettings
        from app.services import telegram_bot as tg
        from app.services import telegram_login as tl
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_TOKEN, "123456:TEST")
            code = tl.create_code(db, "10.1.2.3")["code"]
            assert tl.confirm(db, code, "700900777",
                              username="raceuser", display="Гонка")
        finally:
            db.close()
        results: list[dict] = []
        lock = _th.Lock()

        def worker() -> None:
            s = SessionLocal()
            try:
                r = tl.poll(s, code)
            finally:
                s.close()
            with lock:
                results.append(r)

        threads = [_th.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        ok = [r for r in results if r.get("status") == "ok"]
        assert len(ok) == 1, [r.get("status") for r in results]
        assert all(r.get("status") in ("ok", "expired") for r in results)
        db = SessionLocal()
        try:
            n = db.query(User).filter(User.telegram_chat_id == "700900777").count()
            assert n == 1, f"аккаунтов на чат: {n}"
        finally:
            db.close()

    def test_address_map_is_bounded(self):
        from app.services import telegram_login as tl
        assert tl.MAX_KEYS >= 1000
        src = _read(APP / "services" / "telegram_login.py")
        assert "len(_ip_times) > MAX_KEYS" in src


class TestLiveHarness:
    """Стенд живой проверки (qa/) должен оставаться безопасным и рабочим:
    поддельный Bot API слушает только петлевой адрес, а сам сценарий
    проверяет то, ради чего написан."""

    QA = ROOT / "qa"

    def test_fake_api_is_loopback_only(self):
        src = _read(self.QA / "fake_telegram_api.py")
        assert '"127.0.0.1"' in src
        assert "0.0.0.0" not in src
        assert "getMe" in src and "sendMessage" in src
        assert "ThreadingHTTPServer" in src

    def test_scenario_covers_the_whole_chain(self):
        src = _read(self.QA / "e2e_telegram_login.py")
        for needle in ("/api/v1/auth/telegram/start", "/api/v1/auth/telegram/poll",
                       "/api/v1/settings/telegram", "refresh-bot",
                       "/api/v1/auth/me", "handle_update",
                       "telegram_login_codes", "login_telegram"):
            assert needle in src, needle
        # итог печатается и код возврата честный
        assert "ИТОГ:" in src and "return 0 if not bad else 1" in src

    def test_scripts_compile(self):
        import ast
        for name in ("fake_telegram_api.py", "e2e_telegram_login.py"):
            ast.parse(_read(self.QA / name))


class TestSchemaUpgrade:
    """Прод обновляется без миграций: недостающая таблица создаётся при
    старте (Base.metadata.create_all в init_db). Проверено и живьём:
    копия рабочей базы без telegram_login_codes после старта получила
    таблицу, старые данные (пользователи, коды MAX) остались целы."""

    def test_table_registered_in_metadata(self):
        from app.database import Base
        assert "telegram_login_codes" in Base.metadata.tables

    def test_old_database_gets_the_new_table(self, tmp_path):
        from sqlalchemy import create_engine, inspect

        from app.database import Base
        eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
        # «старая» база: только таблица кодов MAX, как до выпуска
        Base.metadata.tables["max_login_codes"].create(eng)
        assert "telegram_login_codes" not in inspect(eng).get_table_names()
        Base.metadata.create_all(eng)              # как init_db при старте
        names = inspect(eng).get_table_names()
        assert "telegram_login_codes" in names
        assert "max_login_codes" in names          # старое не тронуто
        cols = {c["name"] for c in
                inspect(eng).get_columns("telegram_login_codes")}
        assert {"code_hash", "tg_chat_id", "tg_username", "tg_name",
                "ip", "expires_at", "used_at"} <= cols


class TestRelease17069:
    """Точки синхронизации версии ведёт файл текущего выпуска. Прежние
    классы (1.70.38…1.70.68) оставляют у себя только запись выпуска в
    истории — приём тот же, что в предыдущих релизах."""

    VERSION = "1.70.69"
    PREVIOUS = "1.70.68"

    def test_all_sync_points(self):
        """Точки синхронизации идут за текущей версией (не отстают)."""
        ver = self._current()
        assert tuple(int(x) for x in ver.split(".")) >= tuple(
            int(x) for x in self.VERSION.split("."))
        assert f"ymaster-check-v{ver}" in _read(SW)
        assert f'"version": "{ver}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={ver}" in index
        assert f"app.js?v={ver}" in index
        assert f"maxapp.js?v={ver}" in _read(MAX_HTML)

    def _current(self) -> str:
        """Текущая версия программы (кэш-метки всегда идут за ней)."""
        cfg = _read(CONFIG)
        return re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)

    def test_cache_busters_in_service_worker(self):
        sw = _read(SW)
        ver = self._current()
        assert f"'/css/app.css?v={ver}'" in sw
        assert f"'/js/app.js?v={ver}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        ver = self._current()
        assert f"/b2b/b2b.css?v={ver}" in html
        assert f"/b2b/b2b.js?v={ver}" in html

    def test_changed_assets_are_served_with_the_release_version(self):
        index = _read(INDEX)
        ver = self._current()
        assert f"app.css?v={ver}" in index
        assert f"app.js?v={ver}" in index

    def test_no_previous_version_left_in_sync_points(self):
        for path in (CONFIG, INDEX, SW, MANIFEST, MAX_HTML, CABINET):
            text = _read(path)
            for form in (f"v={self.PREVIOUS}", f'"{self.PREVIOUS}"',
                         f"check-v{self.PREVIOUS}"):
                assert form not in text, f"{path.name}: {form}"

    def test_whats_new_present_and_ordered(self):
        """v1.70.70: точный пин «первая запись» переехал в файл текущего
        выпуска (tests/test_v17070.py) — здесь остаётся порядок записей."""
        js = _read(APP_JS)
        assert f"'{self.VERSION}': [" in js
        assert js.index(f"'{self.VERSION}': [") < js.index(
            f"'{self.PREVIOUS}': [")
        current = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'",
                            js)
        assert current and tuple(int(x) for x in current.group(1).split(".")) \
            >= tuple(int(x) for x in self.VERSION.split("."))

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("Telegram", "регистрац", "Код", "пароль"):
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
        for needle in ("telegram", "tglogin", "одноразов", "регистрац",
                       "хэш", "отклонено"):
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
                       "Отклонённые варианты", "telegram", "код",
                       "одноразов"):
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
        src = _read(RELEASE_68_TESTS)
        block = src.split("class TestRelease17068:", 1)[1]
        assert "test_all_sync_points" not in block
        assert "test_cache_busters_in_service_worker" not in block
        assert "test_whats_new_is_first" not in block
        assert "test_version_is_release" in block
        assert f'VERSION = "{self.PREVIOUS}"' in block

    def test_previous_release_keeps_exactly_nine_methods(self):
        src = _read(RELEASE_68_TESTS)
        block = src.split("class TestRelease17068:", 1)[1]
        methods = re.findall(r"^    def (\w+)", block, re.M)
        assert len(methods) == 9, methods
