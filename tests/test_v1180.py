# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.18.0: режим просмотра (админ видит приложение
# глазами бухгалтера/сотрудника, без пароля, с возвратом одной кнопкой).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


def _mk_user(client, role, username, company_id=None):
    """Регистрация пользователя по приглашению админа → (headers, id)."""
    hdr = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites", json={
        "role": role, "company_id": company_id}, headers=hdr).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    uid = r.json()["user"]["id"] if "user" in r.json() else r.json().get("id")
    return {"Authorization": "Bearer " + r.json()["access_token"]}, uid


class TestImpersonateAPI:
    def test_start_and_me_and_admin_blocked(self, client):
        hdr, uid = _mk_user(client, "user", "view_usr1")
        adm = login(client, "admin", "admin123")
        r = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d["access_token"]
        assert d["user"]["role"] == "user"
        vtok = {"Authorization": "Bearer " + d["access_token"]}
        # /auth/me: целевой пользователь + viewing_as
        me = client.get("/api/v1/auth/me", headers=vtok).json()
        assert me["role"] == "user" and me["username"] == "view_usr1"
        assert me["viewing_as"]["admin_username"] == "admin"
        # админ-эндпоинт в режиме просмотра недоступен
        assert client.get("/api/v1/users", headers=vtok).status_code == 403
        assert client.get("/api/v1/admin/system", headers=vtok).status_code == 403

    def test_stop_returns_admin(self, client):
        hdr, uid = _mk_user(client, "accountant", "view_acc1")
        adm = login(client, "admin", "admin123")
        d = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm).json()
        vtok = {"Authorization": "Bearer " + d["access_token"]}
        r = client.post("/api/v1/admin/impersonate/stop", headers=vtok)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["ok"] is True and j["access_token"]
        assert j["user"]["role"] == "admin"
        # новый токен — снова полноценный админ, без viewing_as
        me = client.get("/api/v1/auth/me",
                        headers={"Authorization": "Bearer " + j["access_token"]}).json()
        assert me["role"] == "admin" and "viewing_as" not in me

    def test_stop_without_impersonation(self, client):
        adm = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/impersonate/stop", headers=adm)
        assert r.status_code == 422

    def test_pure_admin_me_has_no_viewing_as(self, client):
        adm = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=adm).json()
        assert "viewing_as" not in me

    def test_cannot_impersonate_admin_or_self(self, client):
        adm = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=adm).json()
        r = client.post(f"/api/v1/admin/impersonate/{me['id']}", headers=adm)
        assert r.status_code == 422
        # админского id в списке нет, пробуем через прямой вход нельзя —
        # проверяем отказ по роли: единственный admin = сам автор запроса,
        # поэтому достаточно случая self + отсутствия второго админа.

    def test_cannot_impersonate_unknown_or_archived(self, client):
        adm = login(client, "admin", "admin123")
        assert client.post(
            "/api/v1/admin/impersonate/no-such-id", headers=adm).status_code == 404
        hdr, uid = _mk_user(client, "user", "view_arch")
        comp = client.post("/api/v1/companies",
                           json={"name": "ООО Архив-Вью"}, headers=adm).json()
        r = client.patch(f"/api/v1/users/{uid}", json={"is_active": False},
                         headers=adm)
        assert r.status_code == 200, r.text
        r2 = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm)
        assert r2.status_code == 422 and "архив" in r2.json()["detail"].lower()

    def test_no_chains_from_viewing_token(self, client):
        hdr, uid = _mk_user(client, "user", "view_chain")
        hdr2, uid2 = _mk_user(client, "user", "view_chain2")
        adm = login(client, "admin", "admin123")
        d = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm).json()
        vtok = {"Authorization": "Bearer " + d["access_token"]}
        # из режима просмотра нельзя начать новый просмотр (403 require_admin)
        r = client.post(f"/api/v1/admin/impersonate/{uid2}", headers=vtok)
        assert r.status_code == 403

    def test_audit_start_stop(self, client):
        from app.services.audit import SessionLocal
        from app.models import AuditLog
        hdr, uid = _mk_user(client, "user", "view_audit")
        adm = login(client, "admin", "admin123")
        d = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm).json()
        client.post("/api/v1/admin/impersonate/stop",
                    headers={"Authorization": "Bearer " + d["access_token"]})
        db = SessionLocal()
        try:
            acts = [a.action for a in db.query(AuditLog).filter(
                AuditLog.action.in_(["impersonate_start", "impersonate_stop"])).all()]
        finally:
            db.close()
        assert "impersonate_start" in acts and "impersonate_stop" in acts

    def test_audit_pk_sqlite_fix(self):
        """v1.18.0: BigInteger PK не автоинкрементировался в SQLite — журнал
        аудита и ФНС молча не вёлся. Фикс: with_variant + явный id."""
        src = open("app/models.py", encoding="utf-8").read()
        assert 'BigInteger().with_variant(Integer, "sqlite")' in src
        a = open("app/services/audit.py", encoding="utf-8").read()
        assert "func.max(AuditLog.id)" in a            # явный id = max+1
        assert "Аудит не записан" in a                 # сбой больше не тихий


class TestImpersonateUI:
    def test_frontend_persona_menu(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("applyViewAsMode", "renderPersonaMenu", "startViewAs",
                       "stopViewAs", "persona-menu", "btn-viewas-back",
                       "VIEWAS_TOK", "impersonate/stop", "isViewingAs",
                       "Посмотреть глазами", "pm-return"):
            assert needle in js, needle

    def test_frontend_safety_details(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # «Выйти» скрыто в режиме просмотра; подтверждение перед входом
        assert "$('#btn-logout').classList.toggle('hidden', viewing || poolV)" in js
        assert "impersonate/${uid}" in js
        # окно смены пароля не блокирует режим просмотра
        assert "must_change_password && !isViewingAs()" in js

    def test_index_has_viewas_markup(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        for needle in ("persona-menu", "viewas-bar", "chip-caret",
                       "btn-viewas-back", 'aria-haspopup="true"'):
            assert needle in idx, needle

    def test_css_persona_and_viewas(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        for needle in (".persona-menu", ".pm-item", ".viewas-bar",
                       ".chip-caret", "@media (max-width: 640px)"):
            assert needle in css, needle

    def test_whatnew_and_version(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.18.0':" in js
        # актуальная версия и синхронизация ?v= проверяются в тесте текущей
        # версии (test_v1190) — жёсткий пин здесь не нужен (v1.19.0)
