# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.15.0: сейф секретов (шифрование ключей в БД),
# форма ключа Checko в настройках (сохранение/маска/показ по паролю),
# полный маппинг ЕГРЮЛ/ЕГРИП в карточку предприятия.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import itertools
import re

from tests.conftest import login


class TestSecretbox:
    def test_roundtrip_and_prefix(self):
        from app.services import secretbox
        sealed = secretbox.seal("my-checko-key-1234567890")
        assert sealed.startswith("enc:v1:") and "my-checko" not in sealed
        assert secretbox.unseal(sealed) == "my-checko-key-1234567890"

    def test_legacy_passthrough_and_garbage(self):
        from app.services import secretbox
        assert secretbox.unseal("plain-key") == "plain-key"       # легаси
        assert secretbox.unseal("enc:v1:bitыйтокен") == ""        # чужой/битый
        assert secretbox.is_sealed(secretbox.seal("x"))
        assert not secretbox.is_sealed("x")

    def test_secret_keys_encrypted_at_rest(self, client):
        """Главный тест конфиденциальности: в БД нет открытого ключа."""
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/checko",
                       json={"api_key": "abcdef123456abcdef1234"}, headers=hdr)
        assert r.status_code == 200, r.text
        # читаем «сырую» строку настройки через ORM
        from app.database import SessionLocal
        from app.models import AppSetting
        s = SessionLocal()
        try:
            row = s.get(AppSetting, "checko_api_key")
            assert row is not None
            assert "abcdef123456" not in row.value      # открытого ключа НЕТ
            assert row.value.startswith("enc:v1:")
        finally:
            s.close()
        # GET отдаёт только маску
        d = client.get("/api/v1/settings/checko", headers=hdr).json()
        assert d["has_key"] is True and "abcdef123456" not in (d["key_masked"] or "")


class TestCheckoKeyForm:
    def test_put_validates_format(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/checko", json={"api_key": "короткий"},
                       headers=hdr)
        assert r.status_code == 422
        r = client.put("/api/v1/settings/checko",
                       json={"api_key": "x" * 100}, headers=hdr)
        assert r.status_code == 422

    def test_reveal_requires_password(self, client):
        hdr = login(client, "admin", "admin123")
        client.put("/api/v1/settings/checko",
                   json={"api_key": "abcdef123456abcdef1234"}, headers=hdr)
        # без пароля
        r = client.post("/api/v1/settings/checko/reveal",
                        json={"password": ""}, headers=hdr)
        assert r.status_code == 401
        # неверный
        r = client.post("/api/v1/settings/checko/reveal",
                        json={"password": "nope"}, headers=hdr)
        assert r.status_code == 401
        # верный (дефолтный пароль админа из конфикутеста)
        r = client.post("/api/v1/settings/checko/reveal",
                        json={"password": "admin123"}, headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["api_key"] == "abcdef123456abcdef1234"

    def test_reveal_forbidden_for_user(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": "ООО Секреты 315"}, headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "sec_user",
            "password": "parol123", "full_name": "Секрет Секретов"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        assert client.get("/api/v1/settings/checko", headers=hu).status_code == 403
        assert client.post("/api/v1/settings/checko/reveal",
                           json={"password": "parol123"}, headers=hu).status_code == 403


class TestCheckoCardMapping:
    def test_legal_full_requisites(self):
        from app.services.checko import _build_card
        data = {"Company": {
            "name_full": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «ВЕКТОР»",
            "name": "ООО «Вектор»", "inn": "7801234564", "kpp": "780101001",
            "ogrn": "1157847000000", "ogrn_date": "2015-03-12", "status": "active",
            "address": {"full_address": "г. Санкт-Петербург, Невский пр., 1"},
            "management": {"name": "Смирнов Пётр", "post": "Директор"},
            "capital": {"sum": 500000},
            "tax_office": {"code": "7801", "name": "Межрайонная ИФНС №15"},
            "opf": {"name": "ООО", "full": "Общество с ограниченной ответственностью"},
            "Okveds": {"main": {"code": "62.01", "name": "Разработка ПО"},
                       "additional": [{"code": "63.11", "name": "Обработка данных"},
                                      {"code": "62.02", "name": "Консультации"}]},
            "Emails": [{"email": "hi@vektor.ru"}], "Phones": [{"phone": "+7 812 111-22-33"}]}}
        c = _build_card("legal", data)
        assert c["name_full"].endswith("«ВЕКТОР»") and c["name_short"] == "ООО «Вектор»"
        assert c["capital"] == "500 000 ₽"
        assert c["tax_office"] == "Межрайонная ИФНС №15" and c["tax_office_code"] == "7801"
        assert c["director"] == "Смирнов Пётр" and c["management_post"] == "Директор"
        assert c["reg_date"] == "2015-03-12"
        assert len(c["okved_extra"]) == 2 and c["email"] == "hi@vektor.ru"
        assert c["phone"] == "+7 812 111-22-33" and c["opf"].startswith("Общество")

    def test_individual_and_empty_safety(self):
        from app.services.checko import _build_card
        ip = _build_card("individual", {"IndividualEntrepreneur": {
            "fio": "Пупкин Василий", "inn": "526317984689",
            "ogrnip": "309526300000000", "status": "active",
            "Okveds": {"main": {"code": "47.91", "name": "Торговля онлайн"}}}})
        assert ip["kind"] == "individual" and ip["ogrn"] == "309526300000000"
        assert ip["capital"] == "" and ip["okved_extra"] == []
        # пустой ответ — без исключений
        empty = _build_card("legal", {})
        assert empty["name_full"] == "" and empty["okved_extra"] == []


class TestRefreshAutofillsInn:
    def test_inn_sync_logic(self):
        """Если ИНН не указан — при обновлении карточки берётся из ЕГРЮЛ
        (логика в refresh-card; здесь проверяем её компоненты: пустой ИНН
        валиден, нормализация не ломает корректный)."""
        from app.services.companies_util import normalize_inn, inn_is_valid
        assert inn_is_valid(normalize_inn(" 7801234564 "))
        assert inn_is_valid(normalize_inn(""))            # пустой разрешён
        assert normalize_inn("7801 2345 64") == "7801234564"


class TestVersion1150:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        from app.config import settings
        assert settings.APP_VERSION == ver

    def test_ui_and_deps(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.15.0':" in js
        assert "checko-reveal" in js and "checko-save" in js
        assert "okved_extra" in js and "Уставный капитал" in js
        req = open("requirements.txt", encoding="utf-8").read()
        assert "cryptography" in req
        plan = open("docs/unique_features.md", encoding="utf-8").read()
        assert "Светофор контрагента" in plan
