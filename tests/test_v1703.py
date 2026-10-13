# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.3: диагностика несовпадения токена бота.
#  1) подпись проверяется и в доковском формате, и с двойным энкодом;
#  2) ошибка сессии называет бота, чей токен сохранён (диагностика);
#  3) в карточке MAX видно, чей токен (token_bot/token_mismatch).
# ======================================================================
import hashlib
import hmac as hmac_mod
import json
import re
import time
import urllib.parse

import pytest

BOT_TOKEN = "700001.ABCDEF"


def _sign(pairs_b64, token: str) -> str:
    pairs = sorted(pairs_b64, key=lambda x: x[0])
    launch = "\n".join(f"{k}={v}" for k, v in pairs if k != "hash")
    secret = hmac_mod.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac_mod.new(secret, launch.encode(), hashlib.sha256).hexdigest()


def init_data_encoded(user_id: int, token: str = BOT_TOKEN,
                      extra_encode: bool = False) -> str:
    """initData в доковском виде: значения один раз URL-закодированы
    (extra_encode=True — имитация двойного энкода клиента/платформы)."""
    ad = int(time.time())
    raw_user = json.dumps({"id": user_id, "first_name": "Диагностика",
                           "username": "diag", "language_code": "ru"},
                          ensure_ascii=False)
    raw = [("auth_date", str(ad)),
           ("query_id", f"q{user_id}-{ad}"),
           ("user", raw_user)]
    # подпись — по ДЕКОДИРОВАННЫМ значениям (как в документации MAX)
    sig = _sign(raw, token)
    pairs = [(k, urllib.parse.quote(v, safe="")) for k, v in raw]
    if extra_encode:
        pairs = [(k, urllib.parse.quote(v, safe="")) for k, v in pairs]
    pairs.append(("hash", sig))
    return "&".join(f"{k}={v}" for k, v in pairs)


def bad_init(user_id: int = 71004) -> str:
    """Свежие данные с испорченной подписью → ровно bad_hash."""
    ad = int(time.time())
    u = urllib.parse.quote(json.dumps({"id": user_id}), safe="")
    return f"auth_date={ad}&query_id=x&user={u}&hash=" + "0" * 64


def _setup(client) -> None:
    from app.database import SessionLocal
    from app.services import appsettings
    s = SessionLocal()
    try:
        appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
    finally:
        s.rollback(); s.close()


# ------------------------------------------------------------------ валидатор
class TestValidator1703:
    def test_doc_format_ok(self, client):
        from app.services import maxapp as ma
        user, err = ma.validate_init_data(
            init_data_encoded(71001), BOT_TOKEN)
        assert err is None and user["id"] == 71001

    def test_double_encoded_ok(self, client):
        """Клиент/платформа могут отдать значения с двойным энкодом —
        подпись та же строка до первого декодирования, значит валидатор
        обязан попробовать и второй вариант."""
        from app.services import maxapp as ma
        user, err = ma.validate_init_data(
            init_data_encoded(71002, extra_encode=True), BOT_TOKEN)
        assert err is None and user["id"] == 71002

    def test_wrong_token_still_rejected(self, client):
        from app.services import maxapp as ma
        _, err = ma.validate_init_data(
            init_data_encoded(71003), "999999.OTHER")
        assert err == "bad_hash"


# ------------------------------------------------------------------ диагностика
class TestDiag1703:
    def test_tech_persisted_and_mismatch(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "id4712030034_bot"})
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
            appsettings.set_setting(s, "max_bot_username", "chek.ymaster.bot")
            max_bot.refresh_username(s)
            # служебное имя сохранено отдельно (диагностика)
            assert appsettings.get_setting(s, "max_bot_tech", "") == \
                "id4712030034_bot"
            # v1.70.6: username токен-бота сохраняется и в настройках
            assert appsettings.get_setting(
                s, "max_bot_username", "") == "id4712030034_bot"
            st = max_bot.bot_settings(s)
            assert st["token_bot"] == "id4712030034_bot"
            assert st["token_mismatch"] is False   # настройка == токен-боту
        finally:
            s.rollback(); s.close()
            appsettings.set_setting(s, "max_bot_tech", "")
            appsettings.set_setting(s, "max_bot_username", "")
            max_bot._username_cache = ""           # изоляция глобального кэша

    def test_no_mismatch_when_same(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_me",
                            lambda t: {"username": "chek.ymaster.bot"})
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
            appsettings.set_setting(s, "max_bot_username", "chek.ymaster.bot")
            max_bot.refresh_username(s)
            st = max_bot.bot_settings(s)
            assert st["token_mismatch"] is False
        finally:
            s.rollback(); s.close()
            appsettings.set_setting(s, "max_bot_tech", "")

    def test_session_401_names_token_bot(self, client, monkeypatch):
        from app.services import max_bot
        _setup(client)
        monkeypatch.setattr(max_bot, "tech_username",
                            lambda db: "id4712030034_bot")
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data": bad_init()})
        assert r.status_code == 401
        assert "id4712030034_bot" in r.json()["detail"]

    def test_session_401_generic_without_tech(self, client):
        _setup(client)
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data": bad_init()})
        assert r.status_code == 401
        assert "токен" in r.json()["detail"].lower()


# ------------------------------------------------------------------ версии
class TestVersions1703:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.4: строгий замок версии переехал в test_v1704
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 3)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and "?v=1.70.2" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.70.2" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.3]") == 1
        assert ch.index("## [1.70.3]") < ch.index("## [1.70.2]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.3':") < js.index("'1.70.2':")

    def test_card_shows_token_bot(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "token_bot" in js and "token_mismatch" in js


# ------------------------------------------------------------------ замки
class TestNoRegression1703:
    def test_login_flow_alive(self, client):
        _setup(client)
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_worker_gate_alive(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import appsettings, max_bot
        monkeypatch.setattr(max_bot, "get_token", lambda db: "tok")
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_enabled", "1")
            appsettings.set_setting(s, "assistants_channel", "telegram")
            assert max_bot._should_run(s) is True
        finally:
            s.rollback(); s.close()
