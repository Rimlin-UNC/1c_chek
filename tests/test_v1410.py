# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.41.0: просмотр ЛЮБОЙ роли — участники Чек-Пула.
# Инвентаризация ролей: админ/бухгалтер/сотрудник (ядро), участник пула
# (отдельный контур), гость (vid, без аккаунта), партнёр API (ключ).
# Только администратор открывает любую роль; участники — через
# /pool-admin/impersonate-pool/* + раздел #/poolpeople.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

from tests.conftest import login


def _enable_pool(db):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1")
    db.close()


def _mk_core(client, role, username):
    """Ядро-пользователь через приглашение → (заголовки НОВОГО пользователя, id)."""
    adm = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites", json={"role": role},
                      headers=adm).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    uid = r.json()["user"]["id"] if "user" in r.json() else r.json()["id"]
    hdr = {"Authorization": "Bearer " + r.json()["access_token"]}
    return hdr, uid, adm


class TestVersion1410:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.41.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "v=1.40.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.41.0':" in js
        assert js.index("'1.41.0':") < js.index("'1.40.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.41.0]") == 1
        assert ch.index("## [1.41.0]") < ch.index("## [1.40.0]")
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "1.41.0" in manual and "кабинет участника" in manual

    def test_ui_and_routes(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "poolpeople" in js and "viewPoolPeople" in js
        assert "startViewAsPool" in js and "stopViewAsPool" in js
        assert "VIEWAS_POOL" in js
        assert "/api/v1/pool-admin/participants" in js
        assert "impersonate-pool/" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'data-view="poolpeople"' in idx and "#/poolpeople" in idx
        hum = open("app/services/audit_human.py", encoding="utf-8").read()
        assert "impersonate_pool_start" in hum and "impersonate_pool_stop" in hum


class TestParticipantsList:
    def test_admin_list_and_search(self, client):
        from app.database import SessionLocal
        db = SessionLocal(); _enable_pool(db)
        adm = _mk_core(client, "user", "pl_core1")[2]
        r = client.post("/api/v1/pool-auth/register", json={
            "email": "pool.list@test.ru", "password": "parol-1234"})
        assert r.status_code == 200, r.text
        d = client.get("/api/v1/pool-admin/participants", headers=adm).json()
        assert d["total"] >= 1
        row = next(u for u in d["items"] if u["email"] == "pool.list@test.ru")
        assert row["is_blocked"] is False and "receipts" in row and "points" in row
        # поиск по e-mail (ASCII-подстрока)
        d2 = client.get("/api/v1/pool-admin/participants?q=pool.list",
                        headers=adm).json()
        assert d2["total"] == 1
        # не-админ → 403
        acc_hdr = _mk_core(client, "accountant", "pl_acc1")[0]
        assert client.get("/api/v1/pool-admin/participants",
                          headers=acc_hdr).status_code == 403

    def test_cyrillic_search(self, client):
        from app.database import SessionLocal
        db = SessionLocal(); _enable_pool(db)
        adm = _mk_core(client, "user", "pl_core2")[2]
        client.post("/api/v1/pool-auth/register", json={
            "email": "кириллица@test.ru", "password": "parol-1234"})
        d = client.get("/api/v1/pool-admin/participants?q=кириллица",
                       headers=adm).json()
        assert d["total"] == 1


class TestPoolImpersonate:
    def test_start_view_and_audit(self, client):
        from app.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal(); _enable_pool(db)
        adm = _mk_core(client, "user", "pl_core3")[2]
        r = client.post("/api/v1/pool-auth/register", json={
            "email": "viewer.pool@test.ru", "password": "parol-1234"})
        pid = r.json()["user"]["id"]
        d = client.post(f"/api/v1/pool-admin/impersonate-pool/{pid}",
                        headers=adm).json()
        assert d["ok"] is True and d["pool_token"]
        assert d["user"]["email"] == "viewer.pool@test.ru"
        assert d["act"]["username"] == "admin"
        # pool-токен работает в кабинете участника
        me = client.get("/api/v1/pool-my/summary", headers={
            "Authorization": "Bearer " + d["pool_token"]}).json()
        assert me["email"] == "viewer.pool@test.ru"
        # pool-токен НЕ проходит в ядро (цепочки исключены)
        assert client.get("/api/v1/users", headers={
            "Authorization": "Bearer " + d["pool_token"]}).status_code in (401, 403)
        # аудит ядра: impersonate_pool_start записан
        db2 = SessionLocal()
        n = db2.execute(text(
            "SELECT count(*) FROM audit_log "
            "WHERE action='impersonate_pool_start'")).scalar()
        db2.close()
        assert n >= 1
        # выход — аудит
        st = client.post("/api/v1/pool-admin/impersonate-pool/stop",
                         headers=adm, json={"participant_id": pid})
        assert st.status_code == 200 and st.json()["ok"] is True

    def test_blocked_participant_rejected(self, client):
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        db = SessionLocal(); _enable_pool(db)
        adm = _mk_core(client, "user", "pl_core4")[2]
        r = client.post("/api/v1/pool-auth/register", json={
            "email": "blocked.pool@test.ru", "password": "parol-1234"})
        pid = r.json()["user"]["id"]
        db2 = SessionLocal()
        u = db2.get(PoolUser, pid)
        u.is_blocked = True
        db2.commit(); db2.close()
        resp = client.post(f"/api/v1/pool-admin/impersonate-pool/{pid}",
                           headers=adm)
        assert resp.status_code == 422
        assert "заблокирован" in resp.json()["detail"]

    def test_admin_only(self, client):
        from app.database import SessionLocal
        db = SessionLocal(); _enable_pool(db)
        acc_hdr = _mk_core(client, "accountant", "pl_acc2")[0]
        assert client.post("/api/v1/pool-admin/impersonate-pool/x",
                           headers=acc_hdr).status_code == 403
        assert client.post("/api/v1/pool-admin/impersonate-pool/stop",
                           headers=acc_hdr, json={}).status_code == 403
        # без заголовка — не-админ (cookie-фолбэк клиента даёт 403, чистый гость — 401)
        assert client.post("/api/v1/pool-admin/impersonate-pool/stop",
                           json={}).status_code in (401, 403)
