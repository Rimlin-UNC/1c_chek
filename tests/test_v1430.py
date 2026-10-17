# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.43.0: быстрый вход (WebAuthn / passkey).
# Полная церемония выполняется программным аутентификатором (ES256,
# fmt="none"): регистрация → вход → отвязка. Биометрия на сервер не
# попадает — только публичный ключ (152-ФЗ).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import base64
import datetime as dt
import hashlib
import json
import re

import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from tests.conftest import login

RP_ID = "testserver"
ORIGIN = "http://testserver"
AAGUID = b"\x00" * 16


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    """Минимальный платформенный аутентификатор: ES256-ключ + подписи."""

    def __init__(self, rp_id=RP_ID):
        import os
        self.rp_id = rp_id
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)   # уникален: тесты в общей session-БД
        self.sign_count = 0

    def _cose_key(self) -> bytes:
        nums = self.priv.public_key().public_numbers()
        key = {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"),
               -3: nums.y.to_bytes(32, "big")}
        return cbor2.dumps(cbor2.CBOTag(24, key)) if False else cbor2.dumps(key)

    def _make_auth_data(self, add_attested: bool) -> bytes:
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        flags = 0x05                      # UP | UV
        if add_attested:
            flags |= 0x40                 # AT
        data = rp_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        if add_attested:
            data += AAGUID + len(self.cred_id).to_bytes(2, "big") + self.cred_id
            data += self._cose_key()
        return data

    def _client_data(self, typ: str, challenge_hex: str) -> bytes:
        return json.dumps({
            "type": typ, "challenge": _b64u(bytes.fromhex(challenge_hex)),
            "origin": ORIGIN,
        }).encode()

    def register(self, challenge_hex: str) -> dict:
        auth_data = self._make_auth_data(add_attested=True)
        client = self._client_data("webauthn.create", challenge_hex)
        att = cbor2.dumps({"fmt": "none", "attStmt": {},
                           "authData": auth_data})
        raw_id = self.cred_id
        return {
            "id": _b64u(raw_id), "rawId": _b64u(raw_id), "type": "public-key",
            "response": {"challenge_hex": challenge_hex,
                         "attestationObject": _b64u(att),
                         "clientDataJSON": _b64u(client)},
        }

    def assert_(self, challenge_hex: str) -> dict:
        self.sign_count += 1
        auth_data = self._make_auth_data(add_attested=False)
        client = self._client_data("webauthn.get", challenge_hex)
        sig = self.priv.sign(auth_data + hashlib.sha256(client).digest(),
                             ec.ECDSA(hashes.SHA256()))
        raw_id = self.cred_id
        return {
            "id": _b64u(raw_id), "rawId": _b64u(raw_id), "type": "public-key",
            "response": {"challenge_hex": challenge_hex,
                         "authenticatorData": _b64u(auth_data),
                         "clientDataJSON": _b64u(client),
                         "signature": _b64u(sig),
                         "userHandle": None},
        }


class TestVersion1430:
    def test_versions_synced(self):
        # пин 1.43.0 перенесён в tests/test_v1440.py (версия ушла вперёд)
        cfg = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "' in cfg
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw

    def test_whats_new_changelog_manual_reqs(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.43.0':" in js
        assert js.index("'1.43.0':") < js.index("'1.42.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.43.0]") == 1
        assert ch.index("## [1.43.0]") < ch.index("## [1.42.0]")
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "вход без пароля" in manual
        req = open("requirements.txt", encoding="utf-8").read()
        assert "webauthn" in req

    def test_ui_markers(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "waLoginCore" in js and "waRegisterCore" in js
        assert "maybeOfferPasskey" in js and "waBindSettingsCard" in js
        assert "login-passkey" in idx and "login-passkey-wrap" in idx
        assert "/api/v1/auth/passkey/login/options" in js
        hum = open("app/services/audit_human.py", encoding="utf-8").read()
        assert "passkey_added" in hum and "login_passkey" in hum


class TestPasskeyCeremony:
    def _setup(self, client, username="wusr1"):
        adm = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={"role": "user"},
                          headers=adm).json()
        r = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": username,
            "password": "parol123", "full_name": f"Тест {username}"})
        assert r.status_code in (200, 201), r.text
        d = r.json()
        uid = d["user"]["id"] if "user" in d else d["id"]
        hdr = {"Authorization": "Bearer " + d["access_token"]}
        return adm, hdr, uid

    def test_register_login_cycle(self, client):
        adm, hdr, uid = self._setup(client)
        auth = SoftAuthenticator()
        # --- регистрация: options ---
        o = client.post("/api/v1/auth/passkey/register/options",
                        json={}, headers=hdr)
        assert o.status_code == 200, o.text
        opts = o.json()
        assert opts["rp"]["id"] == RP_ID and opts["challenge_hex"]
        assert opts["user"]["name"] == "wusr1"
        # --- регистрация: verify (подпись реальным ES256-ключом) ---
        reg = auth.register(opts["challenge_hex"])
        v = client.post("/api/v1/auth/passkey/register/verify",
                        json={**reg, "label": "Телефон Марии"}, headers=hdr)
        assert v.status_code == 200, v.text
        assert v.json()["ok"] is True
        # ключ в БД, публичный ключ есть, пароля/биометрии нет
        from app.database import SessionLocal
        from app.models import WebauthnCredential
        db = SessionLocal()
        row = db.get(WebauthnCredential, reg["id"])
        assert row is not None and row.label == "Телефон Марии"
        assert row.public_key and "BEGIN" not in row.public_key[:5]
        db.close()
        # список моих ключей
        lst = client.get("/api/v1/auth/passkeys", headers=hdr).json()
        assert len(lst["items"]) == 1 and lst["items"][0]["label"] == "Телефон Марии"
        # --- вход без пароля ---
        lo = client.post("/api/v1/auth/passkey/login/options",
                         json={"username": "wusr1"})
        assert lo.status_code == 200
        lopts = lo.json()
        assert len(lopts["allowCredentials"]) == 1
        assertion = auth.assert_(lopts["challenge_hex"])
        lv = client.post("/api/v1/auth/passkey/login/verify", json=assertion)
        assert lv.status_code == 200, lv.text
        body = lv.json()
        assert body["access_token"] and body["user"]["username"] == "wusr1"
        # токен от входа по отпечатку работает
        me = client.get("/api/v1/auth/me", headers={
            "Authorization": "Bearer " + body["access_token"]}).json()
        assert me["username"] == "wusr1"
        # аудит
        from sqlalchemy import text
        db = SessionLocal()
        n = db.execute(text("SELECT count(*) FROM audit_log "
                            "WHERE action IN ('passkey_added','login_passkey')")
                       ).scalar()
        db.close()
        assert n >= 2

    def test_discoverable_login_and_last_used(self, client):
        adm, hdr, uid = self._setup(client, "wusr2")
        auth = SoftAuthenticator()
        o = client.post("/api/v1/auth/passkey/register/options",
                        json={}, headers=hdr).json()
        client.post("/api/v1/auth/passkey/register/verify",
                    json={**auth.register(o["challenge_hex"]), "label": "Планшет"},
                    headers=hdr)
        # discoverable: без username, allowCredentials пуст
        lo = client.post("/api/v1/auth/passkey/login/options", json={}).json()
        assert lo.get("allowCredentials") in (None, [])
        lv = client.post("/api/v1/auth/passkey/login/verify",
                         json=auth.assert_(lo["challenge_hex"]))
        assert lv.status_code == 200, lv.text
        assert lv.json()["user"]["username"] == "wusr2"
        from app.database import SessionLocal
        from app.models import WebauthnCredential
        db = SessionLocal()
        row = db.get(WebauthnCredential, _b64u(auth.cred_id))
        db.close()
        assert row is not None and row.last_used_at is not None

    def test_tampered_origin_rejected_and_challenge_single_use(self, client):
        adm, hdr, uid = self._setup(client, "wusr3")
        auth = SoftAuthenticator()
        o = client.post("/api/v1/auth/passkey/register/options",
                        json={}, headers=hdr).json()
        reg = auth.register(o["challenge_hex"])
        reg["response"]["clientDataJSON"] = _b64u(json.dumps({
            "type": "webauthn.create",
            "challenge": _b64u(bytes.fromhex(o["challenge_hex"])),
            "origin": "https://evil.example",
        }).encode())
        r = client.post("/api/v1/auth/passkey/register/verify",
                        json=reg, headers=hdr)
        assert r.status_code == 400
        # challenge одноразовый: повтор с тем же challenge_hex — 400
        reg2 = auth.register(o["challenge_hex"])   # тот же challenge
        r2 = client.post("/api/v1/auth/passkey/register/verify",
                         json=reg2, headers=hdr)
        assert r2.status_code == 400
        # неизвестный ключ при входе — 401
        lo = client.post("/api/v1/auth/passkey/login/options", json={}).json()
        bad = auth.assert_(lo["challenge_hex"])
        bad["rawId"] = _b64u(b"\x01" * 32)
        bad["id"] = bad["rawId"]
        rv = client.post("/api/v1/auth/passkey/login/verify", json=bad)
        assert rv.status_code == 401

    def test_remove_key(self, client):
        adm, hdr, uid = self._setup(client, "wusr4")
        auth = SoftAuthenticator()
        o = client.post("/api/v1/auth/passkey/register/options",
                        json={}, headers=hdr).json()
        reg = auth.register(o["challenge_hex"])
        client.post("/api/v1/auth/passkey/register/verify",
                    json={**reg, "label": "Старый телефон"}, headers=hdr)
        # чужой ключ удалить нельзя
        assert client.delete(f"/api/v1/auth/passkeys/{reg['id']}",
                             headers=adm).status_code == 404
        d = client.delete(f"/api/v1/auth/passkeys/{reg['id']}", headers=hdr)
        assert d.status_code == 200 and d.json()["ok"] is True
        # вход по отвязанному ключу — 401
        lo = client.post("/api/v1/auth/passkey/login/options",
                         json={"username": "wusr4"})
        assert lo.status_code == 404        # ключей у логина больше нет
        lo2 = client.post("/api/v1/auth/passkey/login/options", json={}).json()
        rv = client.post("/api/v1/auth/passkey/login/verify",
                         json=auth.assert_(lo2["challenge_hex"]))
        assert rv.status_code == 401

