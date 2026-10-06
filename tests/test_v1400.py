# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.40.0: кнопка режима просмотра в списке
# пользователей (второй вход в режим «глазами сотрудника» из v1.18.0).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

from tests.conftest import login


class TestVersion1400:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.40.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "v=1.39.0" not in idx                     # старых пинов нет
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.40.0':" in js
        assert js.index("'1.40.0':") < js.index("'1.39.0':")   # сверху
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.40.0]") == 1
        assert ch.index("## [1.40.0]") < ch.index("## [1.39.0]")
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "1.40.0" in manual and "👁" in manual


class TestViewButton1400:
    def test_button_and_handler_in_users_table(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # кнопка в строке активного не-админа
        assert 'u-view' in js
        assert 'title="Посмотреть приложение глазами сотрудника (режим просмотра)"' in js
        # обработчик переиспользует существующий сценарий v1.18.0
        assert "$$('.u-view').forEach" in js
        assert "startViewAs(btn.dataset.id, btn.dataset.name)" in js
        # сценарий просмотра цел: баннер возврата и меню на месте
        assert 'viewas-bar' in js and "startViewAs" in js
        assert '/api/v1/admin/impersonate/' in js

    def test_impersonate_flow_still_works(self, client):
        """Сквозной сценарий: приглашение → просмотр → возврат (регресс v1.18.0)."""
        adm = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={"role": "accountant"},
                          headers=adm).json()
        r = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "v1400_acc",
            "password": "parol123", "full_name": "Тест 1400"})
        assert r.status_code in (200, 201), r.text
        uid = r.json()["user"]["id"] if "user" in r.json() else r.json()["id"]
        d = client.post(f"/api/v1/admin/impersonate/{uid}", headers=adm).json()
        assert d["ok"] is True and d["access_token"]
        vtok = {"Authorization": "Bearer " + d["access_token"]}
        me = client.get("/api/v1/auth/me", headers=vtok).json()
        assert me["role"] == "accountant" and me["username"] == "v1400_acc"
        assert me["viewing_as"]["admin_username"] == "admin"
        st = client.post("/api/v1/admin/impersonate/stop", headers=vtok).json()
        assert st["ok"] is True
        me2 = client.get("/api/v1/auth/me", headers={
            "Authorization": "Bearer " + st["access_token"]}).json()
        assert me2["role"] == "admin"
