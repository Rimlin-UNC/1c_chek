# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.0: вход/регистрация через MAX на сайте
# (device-code: одноразовый код + подтверждение в MAX) и приглашения
# через бота MAX (отправка по MAX-логину + принятие в мини-приложении).
# Ноль реальных сетевых вызовов: отправка бота — monkeypatch send_rich.
# ======================================================================
import hashlib
import hmac as hmac_mod
import json
import re
import time
import urllib.parse

import pytest

BOT_TOKEN = "700001.ABCDEF"


def build_init_data(user_id: int, token: str = BOT_TOKEN,
                    username: str | None = None) -> str:
    """initData по доке MAX: launch_params + hash = HMAC(secret, launch),
    secret = HMAC("WebAppData", token)."""
    auth_date = int(time.time())
    user = {"id": user_id, "first_name": "Максим", "last_name": "Тестов",
            "username": username if username is not None else f"u{user_id}",
            "language_code": "ru", "photo_url": None}
    pairs = [("auth_date", str(auth_date)),
             ("query_id", f"q{user_id}-{auth_date}"),
             ("user", json.dumps(user, ensure_ascii=False))]
    pairs.sort(key=lambda x: x[0])
    launch = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hmac_mod.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    sig = hmac_mod.new(secret, launch.encode(), hashlib.sha256).hexdigest()
    pairs.append(("hash", sig))
    return "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                    for k, v in pairs)


# ------------------------------------------------------------------ фикс
@pytest.fixture(autouse=True)
def _iso_rate():
    from app.services import max_login
    max_login._ip_times.clear()
    yield
    max_login._ip_times.clear()


def _setup(client) -> None:
    from app.database import SessionLocal
    from app.services import appsettings
    s = SessionLocal()
    try:
        appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
        appsettings.set_setting(s, "pool_enabled", "1")
    finally:
        s.rollback(); s.close()


def _admin(client) -> dict:
    r = client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _miniapp_session(client, max_id: int, username: str | None = None) -> dict:
    _setup(client)
    r = client.post("/api/v1/maxapp/session",
                    json={"init_data": build_init_data(max_id, username=username)})
    assert r.status_code == 200, r.text
    return r.json()


def _create_invite(client, company="ООО МАКС-Веб", role="user",
                   max_uses=1) -> dict:
    h = _admin(client)
    r = client.post("/api/v1/invites", headers=h, json={
        "role": role, "expires_hours": 24, "max_uses": max_uses,
        "note": "для теста", "company_name": company})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ версии
class TestVersion1700:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.70.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.69.0" not in idx
        assert 'id="login-max"' in idx                    # форма входа через MAX
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.69.0" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
        mapp = open("app/static/maxapp/index.html", encoding="utf-8").read()
        assert f"maxapp.js?v={ver}" in mapp
        assert 'id="view-service"' in mapp                # экран сервиса

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.0]") == 1
        assert ch.index("## [1.70.0]") < ch.index("## [1.69.0]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.0':") < js.index("'1.69.0':")

    def test_blocks(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Вход и приглашения через MAX"] == "1.70.0"
        assert reg["Мини-приложение MAX"] == "1.69.0"     # не трогали

    def test_manual_section(self):
        from app.services.manual_content import SECTIONS
        sec = [s for s in SECTIONS if s.get("id") == "maxweb"]
        assert len(sec) == 1
        assert sec[0]["image"] == "/img/manual/maxweb.jpg"
        assert open("app/static/img/manual/maxweb.jpg", "rb").read(2) == b"\xff\xd8"

    def test_harness(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.0" in h and "maxLoginFlow" in h


# ------------------------------------------------------------------ вход
class TestMaxWebLogin:
    def test_start_creates_code_and_link(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["code"].startswith("login_") and len(d["code"]) >= 30
        assert d["max_link"] == ("https://max.ru/chek.ymaster.bot"
                                 "?startapp=" + d["code"])
        assert d["expires_in"] == 600

    def test_poll_pending_then_confirm_and_login(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        code = r.json()["code"]
        p = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p.status_code == 200 and p.json()["status"] == "pending"
        # подтверждение из мини-приложения (подпись MAX → пул-JWT)
        sess = _miniapp_session(client, 4242, username="webmax")
        h = {"Authorization": "Bearer " + sess["access_token"]}
        c = client.post("/api/v1/maxapp/confirm-login",
                        headers=h, json={"code": code})
        assert c.status_code == 200, c.text
        p2 = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p2.status_code == 200, p2.text
        d = p2.json()
        assert d["status"] == "ok" and d["access_token"]
        # сессия сайта выдана cookie + токен
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["username"] == d["user"]["username"]
        from app.database import SessionLocal
        from app.models import User
        s = SessionLocal()
        try:
            u = s.query(User).filter(User.max_user_id == "4242").first()
            assert u is not None and u.role == "user" and u.company_id is None
            assert u.username == "webmax"
        finally:
            s.rollback(); s.close()

    def test_relogin_same_max_no_dup(self, client):
        ids = []
        for _ in range(2):
            r = client.post("/api/v1/auth/max/start", json={})
            code = r.json()["code"]
            sess = _miniapp_session(client, 4343)
            client.post("/api/v1/maxapp/confirm-login",
                        headers={"Authorization": "Bearer " + sess["access_token"]},
                        json={"code": code})
            p = client.post("/api/v1/auth/max/poll", json={"code": code})
            assert p.json()["status"] == "ok"
            ids.append(p.json()["user"]["id"])
        assert ids[0] == ids[1]                            # дубликатов нет

    def test_confirm_bad_code_404(self, client):
        sess = _miniapp_session(client, 4244)
        r = client.post("/api/v1/maxapp/confirm-login",
                        headers={"Authorization": "Bearer " + sess["access_token"]},
                        json={"code": "login_no-such-code-000000000"})
        assert r.status_code == 404

    def test_confirm_requires_max_identity(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        code = r.json()["code"]
        # без пула-JWT подтвердить нельзя
        c = client.post("/api/v1/maxapp/confirm-login", json={"code": code})
        assert c.status_code in (401, 403)
        p = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p.json()["status"] == "pending"             # не подтвердился

    def test_poll_unknown_code_expired(self, client):
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": "login_zzzzzzzzzzzzzzzzzzzzzz"})
        assert p.status_code == 200 and p.json()["status"] == "expired"

    def test_single_use(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        code = r.json()["code"]
        sess = _miniapp_session(client, 4245)
        client.post("/api/v1/maxapp/confirm-login",
                    headers={"Authorization": "Bearer " + sess["access_token"]},
                    json={"code": code})
        p1 = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p1.json()["status"] == "ok"
        p2 = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p2.json()["status"] == "expired"            # повторно нельзя

    def test_register_disabled_403(self, client):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_web_register", "0")
        finally:
            s.rollback(); s.close()
        r = client.post("/api/v1/auth/max/start", json={})
        code = r.json()["code"]
        sess = _miniapp_session(client, 4246)              # новый MAX-id
        client.post("/api/v1/maxapp/confirm-login",
                    headers={"Authorization": "Bearer " + sess["access_token"]},
                    json={"code": code})
        p = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p.status_code == 403                        # регистрация закрыта
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_web_register", "1")
        finally:
            s.rollback(); s.close()

    def test_bot_chat_confirm(self, client, monkeypatch):
        from app.services import max_bot
        sent = []
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: sent.append(text) or True)
        r = client.post("/api/v1/auth/max/start", json={})
        code = r.json()["code"]
        _setup(client)
        from app.database import SessionLocal
        s = SessionLocal()
        try:
            max_bot.handle_update(s, {"update": {"message": {
                "body": {"text": "/start " + code},
                "sender": {"user_id": 5151, "username": "botuser",
                           "first_name": "Бот"}}}})
        finally:
            s.rollback(); s.close()
        assert sent and "✅" in sent[0]
        p = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p.json()["status"] == "ok"
        from app.models import User as _U
        s2 = SessionLocal()
        try:
            _u = s2.query(_U).filter(_U.max_user_id == "5151").first()
            assert _u is not None and _u.username == "botuser"
        finally:
            s2.rollback(); s2.close()

    def test_rate_limit_start(self, client):
        last = 0
        for _ in range(10):
            last = client.post("/api/v1/auth/max/start", json={}).status_code
        assert last == 200
        assert client.post("/api/v1/auth/max/start", json={}).status_code == 429


# ------------------------------------------------------------------ приглашения
class TestMaxInvites:
    def _pool_user(self, tg_id: str, username: str) -> None:
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        s = SessionLocal()
        try:
            s.add(PoolUser(tg_user_id=tg_id, tg_username=username))
            s.commit()
        finally:
            s.rollback(); s.close()

    def test_send_max_by_pool_username(self, client, monkeypatch):
        from app.services import max_bot
        _setup(client)
        captured = {}
        monkeypatch.setattr(
            max_bot, "send_rich",
            lambda t, uid, text, buttons: captured.update(
                {"uid": uid, "text": text, "buttons": buttons}) or True)
        self._pool_user("5111", "invitee")
        inv = _create_invite(client, company="ООО МАКС-Веб")
        h = _admin(client)
        r = client.post(f"/api/v1/invites/{inv['id']}/send-max",
                        headers=h, json={"max_login": "invitee"})
        assert r.status_code == 200, r.text
        assert r.json() == {"sent": True, "max_id": "5111"}
        assert str(captured["uid"]) == "5111"
        assert "МАКС-Веб" in captured["text"]
        urls = [b["url"] for b in captured["buttons"]]
        assert (f"https://max.ru/chek.ymaster.bot?startapp=inv_{inv['token']}"
                in urls)
        assert any(u.endswith("/#/register/" + inv["token"]) for u in urls)

    def test_send_max_by_digits(self, client, monkeypatch):
        from app.services import max_bot
        _setup(client)
        monkeypatch.setattr(max_bot, "send_rich",
                            lambda t, uid, text, buttons: True)
        inv = _create_invite(client)
        h = _admin(client)
        r = client.post(f"/api/v1/invites/{inv['id']}/send-max",
                        headers=h, json={"max_login": "@5112"})
        assert r.status_code == 200 and r.json()["max_id"] == "5112"

    def test_send_max_unknown_login_404(self, client):
        inv = _create_invite(client)
        h = _admin(client)
        r = client.post(f"/api/v1/invites/{inv['id']}/send-max",
                        headers=h, json={"max_login": "no-such-human"})
        assert r.status_code == 404

    def test_send_max_requires_admin(self, client):
        inv = _create_invite(client)          # _admin кладёт куку в client
        client.cookies.clear()                # проверяем именно без авторизации
        r = client.post(f"/api/v1/invites/{inv['id']}/send-max",
                        json={"max_login": "invitee"})
        assert r.status_code in (401, 403)

    def test_invite_accept_via_miniapp(self, client):
        inv = _create_invite(client, company="ООО МАКС-Инв", role="user")
        sess = _miniapp_session(client, 5252, username="guest52")
        h = {"Authorization": "Bearer " + sess["access_token"]}
        pv = client.get("/api/v1/maxapp/invite",
                        params={"token": inv["token"]})
        assert pv.status_code == 200 and pv.json()["valid"] is True
        assert pv.json()["company_name"] == "ООО МАКС-Инв"
        a = client.post("/api/v1/maxapp/invite/accept",
                        headers=h, json={"token": inv["token"]})
        assert a.status_code == 200, a.text
        d = a.json()
        assert d["status"] == "ok" and d["action"] == "registered"
        assert "/#/max-login/" in d["web_link"]
        code = d["web_link"].rsplit("/", 1)[1]
        p = client.post("/api/v1/auth/max/poll", json={"code": code})
        assert p.json()["status"] == "ok"
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        from app.database import SessionLocal
        from app.models import User, Invite as Inv
        s = SessionLocal()
        try:
            u = s.query(User).filter(User.max_user_id == "5252").first()
            assert u is not None and u.company_id and u.role == "user"
            i = s.get(Inv, inv["id"])
            assert i.used_count == 1
        finally:
            s.rollback(); s.close()

    def test_invite_accept_twice_same_company(self, client):
        inv = _create_invite(client, company="ООО МАКС-Два", max_uses=2)
        sess = _miniapp_session(client, 5353)
        h = {"Authorization": "Bearer " + sess["access_token"]}
        a1 = client.post("/api/v1/maxapp/invite/accept",
                         headers=h, json={"token": inv["token"]})
        assert a1.json()["action"] == "registered"
        a2 = client.post("/api/v1/maxapp/invite/accept",
                         headers=h, json={"token": inv["token"]})
        assert a2.status_code == 200 and a2.json()["action"] == "already"
        from app.database import SessionLocal
        from app.models import Invite as Inv
        s = SessionLocal()
        try:
            assert s.get(Inv, inv["id"]).used_count == 1   # дважды не списали
        finally:
            s.rollback(); s.close()

    def test_invite_accept_conflict_other_company(self, client):
        inv1 = _create_invite(client, company="ООО Первая")
        inv2 = _create_invite(client, company="ООО Вторая")
        sess = _miniapp_session(client, 5454)
        h = {"Authorization": "Bearer " + sess["access_token"]}
        a1 = client.post("/api/v1/maxapp/invite/accept",
                         headers=h, json={"token": inv1["token"]})
        assert a1.status_code == 200
        a2 = client.post("/api/v1/maxapp/invite/accept",
                         headers=h, json={"token": inv2["token"]})
        assert a2.status_code == 409                       # уже в компании

    def test_invite_accept_invalid_token_403(self, client):
        sess = _miniapp_session(client, 5555)
        r = client.post("/api/v1/maxapp/invite/accept",
                        headers={"Authorization": "Bearer " + sess["access_token"]},
                        json={"token": "no-such-token-123456"})
        assert r.status_code == 403


# ------------------------------------------------------------------ замки
class TestNoRegression:
    def test_v1690_miniapp_alive(self, client):
        sess = _miniapp_session(client, 5656)
        h = {"Authorization": "Bearer " + sess["access_token"]}
        me = client.get("/api/v1/maxapp/me", headers=h)
        assert me.status_code == 200 and "receipts" in me.json()

    def test_password_login_alive(self, client):
        r = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        assert "ymaster_token" in r.cookies

    def test_invite_standard_flow_alive(self, client):
        inv = _create_invite(client, company="ООО Классика")
        info = client.get("/api/v1/auth/invite-info",
                          params={"token": inv["token"]})
        assert info.status_code == 200 and info.json()["valid"] is True
