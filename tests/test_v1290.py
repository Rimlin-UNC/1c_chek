# Ямастер Чек — тесты v1.29.0: Telegram-бот через прокси + диагностика.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Причина релиза: пользователь добавил токен, бот молчал. Диагностика показала,
# что api.telegram.org с сервера недоступен (TLS-блокировка). Здесь проверяем:
#  - прокси (БД → env) и что весь трафик бота идёт через него;
#  - внятную ошибку сети с подсказкой про прокси;
#  - диагностику (прямой путь / через прокси);
#  - самовосстановление воркера (выключен → поток завершается; настройки
#    подхватываются без перезапуска);
#  - маршруты настроек (сохранение/очистка прокси, диагностика);
#  - UI-пины и синхрон версий (пин текущей версии — здесь).
import re
import threading

from app.services import telegram_bot as tg
from tests.conftest import login


class TestProxyPlumbing:
    def test_get_proxy_db_first_then_env(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            from app.config import settings as cfg
            monkeypatch.setattr(cfg, "TELEGRAM_PROXY", "socks5://env:1@h:1080")
            assert tg.get_proxy(db) == "socks5://env:1@h:1080"     # env-fallback
            appsettings.set_setting(db, tg.SETT_PROXY, "socks5://db:2@h:1080")
            assert tg.get_proxy(db) == "socks5://db:2@h:1080"      # БД важнее
            appsettings.set_setting(db, tg.SETT_PROXY, "")
            monkeypatch.setattr(cfg, "TELEGRAM_PROXY", "")
        finally:
            db.close()

    def test_api_sends_proxy_and_hints_on_network_fail(self, monkeypatch):
        seen = {}

        def fake_post(url, json=None, timeout=None, proxy=None):
            seen["proxy"] = proxy
            raise RuntimeError("TLS closed")

        monkeypatch.setattr(tg.httpx, "post", fake_post)
        with __import__("pytest").raises(tg.TelegramError) as ei:
            tg.api("TOKEN", "getMe", proxy="socks5://x")
        assert seen["proxy"] == "socks5://x"
        assert "прокси" in str(ei.value).lower()

    def test_send_message_passes_proxy(self, monkeypatch):
        seen = {}

        def fake_api(token, method, payload=None, timeout=35.0, proxy=""):
            seen["proxy"] = proxy
            seen["method"] = method
            return {}

        monkeypatch.setattr(tg, "api", fake_api)
        tg.send_message("T", 42, "привет", proxy="socks5://p")
        assert seen == {"proxy": "socks5://p", "method": "sendMessage"}


class TestDiagnose:
    def test_proxy_works_verdict_ok(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_PROXY, "socks5://good@h:1080")
            monkeypatch.setattr(tg, "_probe",
                                lambda p: {"ok": bool(p), "error": "" if p else "TLS"})
            d = tg.diagnose(db)
            assert d["verdict"] == "ok" and "прокси" in d["message"].lower()
        finally:
            db.close()

    def test_blocked_without_proxy_verdict_bad(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_PROXY, "")
            monkeypatch.setattr(tg, "_probe",
                                lambda p: {"ok": False, "error": "ConnectError"})
            d = tg.diagnose(db)
            assert d["verdict"] == "bad"
            assert "api.telegram.org" in d["message"]
        finally:
            db.close()

    def test_direct_ok_verdict_ok(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_PROXY, "")
            monkeypatch.setattr(tg, "_probe",
                                lambda p: {"ok": True, "error": ""})
            assert tg.diagnose(db)["verdict"] == "ok"
        finally:
            db.close()


class TestWorkerLifecycle:
    def test_should_run_requires_enabled_and_token(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_TOKEN, "")   # изоляция от соседей
            appsettings.set_setting(db, tg.SETT, "0")
            assert tg._should_run(db) is False
            appsettings.set_setting(db, tg.SETT, "1")
            assert tg._should_run(db) is False            # токена нет
            appsettings.set_setting(db, tg.SETT_TOKEN, "123:ABC")
            assert tg._should_run(db) is True
            appsettings.set_setting(db, tg.SETT_TOKEN, "")
            appsettings.set_setting(db, tg.SETT, "0")
        finally:
            db.close()

    def test_worker_exits_when_disabled(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            monkeypatch.setattr(tg, "_should_run", lambda d: False)
            th = threading.Thread(target=tg._worker_loop, daemon=True)
            th.start()
            th.join(timeout=5)
            assert not th.is_alive()                      # завершился сам
        finally:
            db.close()

    def test_restart_worker_start_and_stop(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT, "0")
            tg.stop_worker()
            assert tg.restart_worker() is False           # выключен → не запускаем
        finally:
            db.close()

    def test_409_conflict_sleeps_longer(self, monkeypatch):
        """409 → пауза 30 с (не спамить), прочие ошибки — 15 с."""
        from app.database import SessionLocal
        sleeps = []
        monkeypatch.setattr(tg.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(tg, "get_token", lambda db: "T")
        monkeypatch.setattr(tg, "get_proxy", lambda db: "")
        monkeypatch.setattr(tg, "api",
                            lambda *a, **k: (_ for _ in ()).throw(
                                tg.TelegramError("Telegram: Conflict: 409")))
        db = SessionLocal()
        try:
            tg._poll_once(db, [0])
            assert sleeps and sleeps[0] == 30
        finally:
            db.close()


class TestHandleUpdateUsesProxy:
    def test_start_bind_sends_via_proxy(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings
        from app.models import User
        db = SessionLocal()
        try:
            appsettings.set_setting(db, tg.SETT_TOKEN, "123:TEST")
            appsettings.set_setting(db, tg.SETT_PROXY, "socks5://bind@h:1080")
            user = db.query(User).filter(User.username == "admin").first()
            code = tg.new_bind_code(db, user.id)
            sent = {}
            monkeypatch.setattr(tg, "send_message",
                                lambda token, chat_id, text, proxy="":
                                sent.update({"proxy": proxy, "text": text}))
            tg.handle_update(db, {"message": {"chat": {"id": "777"},
                                              "text": f"/start {code}"}})
            assert sent["proxy"] == "socks5://bind@h:1080"
            assert "Привязано" in sent["text"]
            db.refresh(user)
            assert user.telegram_chat_id == "777"
            user.telegram_chat_id = None
            appsettings.set_setting(db, tg.SETT_TOKEN, "")
            appsettings.set_setting(db, tg.SETT_PROXY, "")
            db.commit()
        finally:
            db.close()


class TestTelegramRoutes:
    def test_put_proxy_save_and_clear(self, client, monkeypatch):
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(tg, "get_me", lambda token, proxy="": {"username": "b"})
        monkeypatch.setattr(tg, "refresh_username", lambda db: "b")
        # неверная схема → 422
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"proxy": "ftp://x"})
        assert r.status_code == 422
        # сохранить валидный прокси
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"proxy": "socks5://u:p@h:1080"})
        assert r.status_code == 200
        d = client.get("/api/v1/settings/telegram", headers=adm).json()
        assert d["proxy_configured"] is True and d["proxy_masked"]
        # «-» убирает прокси
        r = client.put("/api/v1/settings/telegram", headers=adm, json={"proxy": "-"})
        assert r.status_code == 200
        d = client.get("/api/v1/settings/telegram", headers=adm).json()
        assert d["proxy_configured"] is False

    def test_put_token_via_proxy_ok(self, client, monkeypatch):
        adm = login(client, "admin", "admin123")
        calls = {}

        def fake_get_me(token, proxy=""):
            calls["proxy"] = proxy
            return {"username": "ymaster_chek_bot"}

        monkeypatch.setattr(tg, "get_me", fake_get_me)
        monkeypatch.setattr(tg, "refresh_username", lambda db: "ymaster_chek_bot")
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"bot_token": "123456789:AA" + "x" * 30,
                             "proxy": "socks5://u:p@h:1080"})
        assert r.status_code == 200
        assert calls["proxy"] == "socks5://u:p@h:1080"   # проверка токена через прокси
        # уборка секрета из тестовой БД
        r = client.put("/api/v1/settings/telegram", headers=adm,
                       json={"proxy": "-"})
        assert r.status_code == 200

    def test_diagnose_endpoint(self, client, monkeypatch):
        adm = login(client, "admin", "admin123")
        monkeypatch.setattr(tg, "_probe", lambda p: {"ok": False, "error": "TLS"})
        d = client.post("/api/v1/settings/telegram/diagnose", headers=adm).json()
        assert d["verdict"] == "bad" and d["direct"]["ok"] is False


class TestUiAndVersion:
    def test_ui_pins(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="tg-proxy"' in js and 'id="tg-diag"' in js
        assert "/api/v1/settings/telegram/diagnose" in js
        assert "if (prx) body.proxy = prx;" in js

    def test_requirements_socksio(self):
        req = open("requirements.txt", encoding="utf-8").read()
        assert "socksio" in req


class TestVersion1290:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # Пин конкретной версии перенесён в tests/test_v1300.py (тест текущей версии)
        # v1.29.0: assert ver == "1.29.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.29.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.29.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Telegram-бот через прокси (v1.29.0)" in manual

    def test_dev_plan_doc_exists(self):
        s = open("docs/dev_plan_checkpool.md", encoding="utf-8").read()
        assert "Чек-Пул" in s and "Этап 1" in s and "pool_enabled" in s
