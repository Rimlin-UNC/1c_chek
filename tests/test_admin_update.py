# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.4.0: обновление из приложения, статьи расходов,
# расширенные действия администратора, виджеты дашборда.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import uuid

from app.config import settings
from tests.conftest import login


def _mk_user(client, role="user"):
    """Создаёт пользователя по приглашению; возвращает заголовки АДМИНА, id,
    имя и — с v1.70.38 — заголовки самого нового пользователя.

    Раньше токен созданного пользователя выбрасывался, поэтому проверки
    «эндпоинт только для админа» уходили без авторизации и проверяли
    анонима вместо обычного пользователя (см. test_check_requires_admin)."""
    hdr = login(client, "admin", "admin123")
    uname = f"usr{uuid.uuid4().hex[:6]}"
    r = client.post("/api/v1/invites", json={
        "role": role, "max_uses": 1, "expires_hours": 24, "note": uname},
        headers=hdr)
    assert r.status_code == 200
    r2 = client.post("/api/v1/auth/register", json={
        "token": r.json()["token"], "username": uname,
        "password": "secret2026", "full_name": uname.upper()})
    assert r2.status_code == 200
    uid = r2.json()["user"]["id"]
    tok = r2.json().get("access_token") or ""
    uhdr = {"Authorization": f"Bearer {tok}"} if tok else {}
    return hdr, uid, uname, uhdr


class TestUpdater:
    def test_check_update_reaches_github(self, client, monkeypatch):
        from app.services import updater
        monkeypatch.setattr(updater, "_remote_version", lambda repo, branch: {
            "version": "9.9.9", "checked_at": "2026-10-01T00:00:00Z"})
        monkeypatch.setattr(updater, "_remote_changelog", lambda repo, branch: "## [9.9.9] — тест")
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/check", headers=hdr)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["update_available"] is True
        assert d["remote_version"] == "9.9.9"
        assert d["current_version"] == settings.APP_VERSION
        assert "9.9.9" in d["changelog_excerpt"]

    def test_check_requires_admin(self, client):
        """v1.70.38: тест был ФИКТИВНЫМ. Строка
        `h = login(...) if False else None` превращала `assert h is None`
        в тавтологию, а запрос уходил вообще без токена — проверялся аноним,
        а не «обычный пользователь не может проверять обновления».
        Теперь проверяются оба случая и свежим клиентом: общий `client`
        несёт cookie прежнего админ-входа, с ней ответ был бы 200 не
        потому, что доступ разрешён."""
        from fastapi.testclient import TestClient
        from app.main import app
        _hdr, _uid, _uname, uhdr = _mk_user(client, "user")
        assert uhdr, "регистрация не вернула токен пользователя"
        with TestClient(app) as anon:
            r_user = anon.get("/api/v1/admin/update/check", headers=uhdr)
            r_anon = anon.get("/api/v1/admin/update/check")
        # вошедший пользователь без прав — 403, аноним — 401/403, и ни в том
        # ни в другом случае сведений об обновлении не отдаётся
        assert r_user.status_code == 403, r_user.text
        assert "update_available" not in (r_user.text or "")
        assert r_anon.status_code in (401, 403)

    def test_apply_when_no_update(self, client, monkeypatch):
        from app.services import updater
        monkeypatch.setattr(updater, "_remote_version", lambda repo, branch: {
            "version": settings.APP_VERSION, "checked_at": "2026-10-01T00:00:00Z"})
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/admin/update/apply", headers=hdr)
        assert r.status_code == 200
        # версия та же — job не запускается
        d = r.json()
        assert d["updated"] is False
        assert "уже последняя" in d["message"]

    def test_status_and_changelog(self, client):
        hdr = login(client, "admin", "admin123")
        st = client.get("/api/v1/admin/update/status", headers=hdr)
        assert st.status_code == 200
        assert "job" in st.json() and "history" in st.json()
        cl = client.get("/api/v1/admin/update/changelog", headers=hdr)
        assert cl.status_code == 200
        assert "## [" in cl.json()["changelog"], "в CHANGELOG есть записи версий"

    def test_system_info(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/system", headers=hdr)
        assert r.status_code == 200
        d = r.json()
        assert d["version"] == settings.APP_VERSION
        assert "db_size_mb" in d and "counts" in d

    def test_backup_download(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/backup", headers=hdr)
        assert r.status_code == 200
        assert len(r.content) > 1000, "файл БД не пустой"
        assert "ymaster-backup" in r.headers.get("content-disposition", "")


class TestAdminUserActions:
    def test_change_role(self, client):
        hdr, uid, uname, _uhdr = _mk_user(client, "user")
        r = client.patch(f"/api/v1/admin/users/{uid}/role",
                         json={"role": "accountant"}, headers=hdr)
        assert r.status_code == 200, r.text
        r2 = client.patch(f"/api/v1/admin/users/{uid}/role",
                          json={"role": "admin"}, headers=hdr)
        assert r2.status_code == 422, "роль admin через смену роли недоступна"

    def test_reset_password_gives_temp(self, client):
        hdr, uid, uname, _uhdr = _mk_user(client, "user")
        r = client.post(f"/api/v1/admin/users/{uid}/reset-password", headers=hdr)
        assert r.status_code == 200
        temp = r.json()["temp_password"]
        assert len(temp) >= 8
        # временный пароль работает
        r2 = client.post("/api/v1/auth/login",
                         json={"username": uname, "password": temp})
        assert r2.status_code == 200
        assert r2.json()["user"]["must_change_password"] is True

    def test_admin_password_reset_forbidden(self, client):
        hdr = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=hdr).json()
        r = client.post(f"/api/v1/admin/users/{me['id']}/reset-password", headers=hdr)
        assert r.status_code == 400

    def test_unarchive_restores_login(self, client):
        hdr, uid, uname, _uhdr = _mk_user(client, "user")
        # архивируем штатным способом
        r = client.delete(f"/api/v1/users/{uid}", headers=hdr)
        assert r.status_code == 200
        r2 = client.post(f"/api/v1/admin/users/{uid}/unarchive", headers=hdr)
        assert r2.status_code == 200, r2.text
        me = client.get("/api/v1/auth/me", headers=hdr)  # сессия админа, просто проверка ответа
        # пользователь восстановлен и виден в списке
        users = client.get("/api/v1/users", headers=hdr).json()
        names = [u["username"] for u in (users["items"] if isinstance(users, dict) else users)]
        assert uname in names

    def test_transfer_receipts(self, client):
        from tests.test_external import _scan  # хелпер со случайным ФП
        hdr_admin = login(client, "admin", "admin123")
        hdr_a, uid_a, _, uha = _mk_user(client, "user")
        hdr_b, uid_b, _, uhb = _mk_user(client, "user")
        # v1.70.39: чек создаёт САМ сотрудник (uha), а не админ. Первый
        # возвращаемый _mk_user заголовок — админский, поэтому раньше и чек,
        # и проверка видимости шли от имени админа: админ видит все чеки,
        # значит проверка «получатель видит чек» была пустой, а падала она
        # лишь тогда, когда чек не попадал в первые 50 строк общего списка.
        receipt = _scan(client, uha)
        r = client.post(f"/api/v1/admin/users/{uid_a}/transfer-receipts",
                        json={"to_user_id": uid_b}, headers=hdr_admin)
        assert r.status_code == 200, r.text
        # чек должен быть виден получателю (у role=user — только свои).
        # v1.70.39: запрашиваем именно этот чек (ids), а не первую страницу
        # списка — иначе результат зависел от того, сколько чеков накопилось
        # в общем сеансе БД и в каком порядке тесты выполнялись.
        # Изоляция пространства при этом проверяется: ids не отменяет scope.
        mine = client.get("/api/v1/receipts", params={"ids": receipt["id"]},
                          headers=uhb).json()
        assert any(x["id"] == receipt["id"] for x in mine["items"]), \
            "после передачи чек виден новому владельцу"
        # прежний владелец чек больше не видит (role=user → только свои)
        gone = client.get("/api/v1/receipts", params={"ids": receipt["id"]},
                          headers=uha).json()
        assert not any(x["id"] == receipt["id"] for x in gone["items"]), \
            "после передачи чек не виден прежнему владельцу"
        # админ по-прежнему видит чек и знает, кому он теперь принадлежит
        both = client.get("/api/v1/receipts", params={"ids": receipt["id"]},
                          headers=hdr_admin).json()
        assert any(x["id"] == receipt["id"] for x in both["items"])
        assert hdr_a and hdr_b  # заголовки админа использованы выше


class TestCategory:
    def test_patch_and_filter(self, client):
        hdr = login(client, "admin", "admin123")
        from tests.test_external import _scan
        receipt = _scan(client, hdr)
        r = client.patch(f"/api/v1/receipts/{receipt['id']}",
                         json={"category": "Канцелярия"}, headers=hdr)
        assert r.status_code == 200
        assert r.json()["category"] == "Канцелярия"
        lst = client.get("/api/v1/receipts?category=канцелярия", headers=hdr)
        assert any(x["id"] == receipt["id"] for x in lst.json()["items"])

    def test_dashboard_widgets(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/dashboard/stats?days=7", headers=hdr)
        assert r.status_code == 200
        d = r.json()
        for key in ("attention_count", "notified_count", "vat_month", "by_assignee"):
            assert key in d
