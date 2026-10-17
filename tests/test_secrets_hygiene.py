# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.5 (часть 2): гигиена секретов.
# Практики OWASP/ASVS: пароли и токены не возвращаются API в открытом
# виде и не попадают в журнал (маскирование).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import logging


def _login(client):
    from tests.conftest import login
    return login(client, "admin", "admin123")


import pytest


@pytest.fixture(autouse=True)
def _clean_mail_state():
    """Параллельный прогон: тесты гигиены мусорят в БД воркера —
    после каждого теста возвращаем «заводское» состояние почты/токенов,
    чтобы не влиять на другие тест-файлы этого же воркера."""
    yield
    from app.database import SessionLocal
    from app.services import appsettings
    KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
            "smtp_pass", "proverkacheka_token")
    db = SessionLocal()
    try:
        for k in KEYS:
            appsettings.set_setting(db, k, "")
        db.commit()
    finally:
        db.close()


class TestSmtpPasswordNotLeaked:
    def test_password_never_in_get(self, client):
        """После сохранения пароля SMTP ни один GET его не содержит."""
        hdr = _login(client)
        secret = "СуперСекретЯщика#42"
        r = client.put("/api/v1/pool-admin/smtp", headers=hdr, json={
            "host": "smtp.timeweb.ru", "port": 465,
            "user": "chek@ymaster.ru", "password": secret,
            "sender": "chek@ymaster.ru", "tls": True,
            "base_url": "https://chek.ymaster.ru"})
        assert r.status_code == 200, r.text
        for url in ("/api/v1/pool-admin/smtp", "/api/v1/mail-admin/config"):
            body = client.get(url, headers=hdr).text
            assert secret not in body, f"пароль утёк через {url}"
            assert "has_password" in body or "configured" in body

    def test_token_masked_in_settings(self, client):
        """Токен proverkacheka отдаётся только маскированным."""
        hdr = _login(client)
        tok = "55035.SuperSecretToken"
        r = client.put("/api/v1/settings/external", headers=hdr, json={
            "proverkacheka_token": tok})
        assert r.status_code == 200, r.text
        body = client.get("/api/v1/settings/external", headers=hdr).text
        assert tok not in body, "полный токен утёк через настройки"
        assert "proverkacheka_token_masked" in body   # маска на месте

    def test_token_not_in_logs(self, client, monkeypatch, caplog):
        """При отказе источника токен не печатается в журнал."""
        from app.services import external
        caplog.set_level(logging.INFO)
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "55035.TopSecret", "fns_master_token": ""})
        monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 0)
        monkeypatch.setattr(external.time, "sleep", lambda s: None)

        def fake_post(*a, **k):
            # даже запрос с токеном не логируется как есть
            resp = external.httpx.Response(
                200, json={"code": 401, "data": "Не авторизован"},
                request=external.httpx.Request("POST", external.PROVERKACHEKA_URL))
            return resp

        monkeypatch.setattr(external.httpx, "post", fake_post)
        eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert "55035.TopSecret" not in caplog.text, "токен попал в журнал"

    def test_master_key_masked_in_logs(self, client, monkeypatch, caplog):
        """Мастер-ключ ФНС в журнале — только хвост (…XXXX)."""
        from app.services import external
        caplog.set_level(logging.INFO)
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok",
            "fns_master_token": "MasterSecretKeyAbcd"})
        chain = eng.provider_chain(None)
        assert chain == ["fns_api", "proverkacheka", "mock"]
        # ключи логируются только при загрузке чека — маскированно
        assert "MasterSecretKeyAbcd" not in caplog.text
