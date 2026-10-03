# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.8.2: QR-код приглашения (PNG), права, фикс
# api.download (GET без тела). ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


class TestInviteQr:
    def _invite(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "user", "expires_hours": 24,
                                "max_uses": 3, "note": "QR тест"}).json()
        return hdr, inv

    def test_qr_png(self, client):
        hdr, inv = self._invite(client)
        r = client.get(f"/api/v1/invites/{inv['id']}/qr", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("image/png")
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"          # PNG-магия
        assert len(r.content) > 500
        url = r.headers.get("x-invite-url", "")
        assert "/#/register/" in url and inv["token"] in url

    def test_qr_requires_admin(self, client):
        hdr, inv = self._invite(client)
        client.cookies.clear()
        assert client.get(f"/api/v1/invites/{inv['id']}/qr").status_code in (401, 403)

    def test_qr_user_role_forbidden(self, client):
        hdr, inv = self._invite(client)
        emp = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "user"}).json()
        client.post("/api/v1/auth/register",
                    json={"token": emp["token"], "username": "qruser",
                          "password": "secret2026", "full_name": "Юзер"})
        uhdr = login(client, "qruser", "secret2026")
        r = client.get(f"/api/v1/invites/{inv['id']}/qr", headers=uhdr)
        assert r.status_code in (401, 403)

    def test_qr_revoked(self, client):
        hdr, inv = self._invite(client)
        client.post(f"/api/v1/invites/{inv['id']}/revoke", json={}, headers=hdr)
        r = client.get(f"/api/v1/invites/{inv['id']}/qr", headers=hdr)
        assert r.status_code == 400
        assert "отозвано" in r.json()["detail"]

    def test_qr_unknown(self, client):
        hdr = login(client, "admin", "admin123")
        assert client.get("/api/v1/invites/no-such-id/qr",
                          headers=hdr).status_code == 404


class TestApiDownloadGet:
    def test_download_uses_get_without_body(self):
        js = open("app/static/js/api.js", encoding="utf-8").read()
        assert "b === undefined" in js and "method: 'GET'" in js
        # старое безусловное POST не должно остаться
        assert "method: 'POST', headers, body: JSON.stringify(b || {})" not in js

    def test_ui_has_qr_button(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "openInviteQr" in js and "iv-qr" in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".invite-qr-box" in css
