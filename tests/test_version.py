# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.3.0: единая версия по всей системе,
# новые источники (OFD-ру «QR Cash», несколько своих источников).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import json
import re

from tests.conftest import login


# ---------------------------------------------------------------------------
#  Версия: единый источник истины (app/config.py) — «чётко и понятно»
# ---------------------------------------------------------------------------
class TestVersion:
    def test_about_returns_config_version(self, client):
        from app.config import settings
        r = client.get("/api/v1/about")
        assert r.status_code == 200
        assert r.json()["version"] == settings.APP_VERSION

    def test_main_py_has_no_local_version(self):
        src = open("app/main.py", encoding="utf-8").read()
        assert not re.search(r'APP_VERSION\s*=\s*["\']\d', src), \
            "main.py не должен дублировать версию — только settings.APP_VERSION"

    def test_manifest_version_matches(self):
        from app.config import settings
        manifest = json.load(open("app/static/manifest.webmanifest", encoding="utf-8"))
        assert manifest.get("version") == settings.APP_VERSION

    def test_sw_cache_named_by_version(self):
        from app.config import settings
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{settings.APP_VERSION}" in sw

    def test_index_shows_version_dynamically(self):
        html = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="brand-version"' in html, "версия в шапке должна быть динамической"
        assert 'id="footer-version"' in html
        # жёстко зашитых версий вида v1.x в разметке быть не должно
        assert not re.search(r">v\d+\.\d+", html), "уберите хардкод версии из index.html"

    def test_client_uses_api_version_for_whats_new(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "state.appVersion" in js
        assert not re.search(r"CLIENT_VERSION\s*=\s*['\"]\d", js), \
            "версия клиента должна браться из /api/v1/about"


# ---------------------------------------------------------------------------
#  Парсер: OFD-ру и другие источники отдают ключи в разном регистре
# ---------------------------------------------------------------------------
class TestOfdRuParsing:
    OFD_LIKE = {
        "Code": 0,
        "Content": {"Document": {"Receipt": {
            "DateTime": "2026-09-27T15:29:00.000",
            "TotalSum": 215000,
            "Items": [
                {"ItemName": "Молоко 2.5% 1л", "ItemPrice": 8990,
                 "Quantity": 2.0, "ItemSum": 17980},
                {"ItemName": "Хлеб", "ItemPrice": 3520,
                 "Quantity": 1.0, "ItemSum": 3520},
            ],
            "User": "ООО «Пятёрочка»",
            "UserInn": "1234567890",
        }}},
    }

    def test_ofdru_style_keys_parsed(self):
        from app.services.external import parse_receipt_payload
        res = parse_receipt_payload(self.OFD_LIKE, known_rub=2150.0)
        assert res.found is True
        assert res.total_sum == 2150.0
        assert res.merchant_name == "ООО «Пятёрочка»"
        assert res.merchant_inn == "1234567890"
        assert len(res.items) == 2
        assert res.items[0].name == "Молоко 2.5% 1л"
        assert res.items[0].price == 89.9
        assert res.date_time is not None

    def test_ofdru_payload_builder(self):
        import datetime as dt
        from app.services.external import fetch_ofdru
        # без токена — сразу понятный отказ, без сети
        ok, msg, data = fetch_ofdru("fn", "fd", "fp", 100.0,
                                    dt.datetime(2026, 9, 27, 15, 29), 1, "")
        assert ok is False
        assert "tokenSecret" in msg


# ---------------------------------------------------------------------------
#  Несколько своих источников
# ---------------------------------------------------------------------------
class TestMultiCustom:
    def test_settings_accept_and_chain_uses_sources(self, client, monkeypatch):
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/external", json={
            "external_custom_urls": [
                {"name": "шлюз1", "url": "https://api.example.ru/check"},
                {"name": "шлюз2", "url": "https://api2.example.ru/check"},
            ]}, headers=hdr)
        assert r.status_code == 200, r.text

        from app.services.external import engine
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            chain = engine.provider_chain(db)
        finally:
            db.close()
        assert "custom::шлюз1" in chain and "custom::шлюз2" in chain
        assert chain[-1] == "mock"

    def test_bad_url_rejected(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/external", json={
            "external_custom_urls": [{"name": "x", "url": "ftp://bad"}]},
            headers=hdr)
        assert r.status_code == 400

    def test_get_settings_masks_and_lists(self, client):
        hdr = login(client, "admin", "admin123")
        g = client.get("/api/v1/settings/external", headers=hdr).json()
        assert "external_custom_urls" in g
        assert "has_ofd_ru_token" in g
