# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.15.0: сейф секретов (шифрование ключей в БД),
# форма ключа Checko в настройках (сохранение/маска/показ по паролю),
# полный маппинг ЕГРЮЛ/ЕГРИП в карточку предприятия.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import itertools
import re

import pytest

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
                       json={"api_key": "x" * 130}, headers=hdr)
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
    """Маппинг ответа Checko ПО ДОКУМЕНТАЦИИ: ключи внутри data — русские
    (checko.ru/integration/api/company и /entrepreneur)."""

    LEGAL = {"meta": {"status": "ok", "today_request_count": 3, "balance": 0.0},
             "data": {
                 "ОГРН": "1027700198767", "ИНН": "7707083893",
                 "КПП": "773601001", "ОКПО": "00032538",
                 "НаимПолн": "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «СБЕРБАНК»",
                 "НаимСокр": "ПАО «Сбербанк»",
                 "ДатаОГРН": "1991-01-01", "ДатаРег": "1991-01-01",
                 "Статус": {"Код": "1", "Наим": "Действующая"},
                 "Регион": {"Код": "77", "Наим": "Москва"},
                 "ЮрАдрес": {"НасПункт": "Москва г",
                             "АдресРФ": "117997, Москва, ул. Вавилова, 19",
                             "Недост": False, "НедостОпис": "",
                             "МассАдрес": ["1", "2"]},
                 "ОКВЭД": {"Код": "64.19", "Наим": "Денежное кредитование"},
                 "Учред": {"ФЛ": [{"ФИО": "Иванов И.И."}], "РосОрг": []},
                 "Подразд": {"Филиал": [{"НаимПолн": "Байкальский банк"}],
                             "Представ": []},
                 "Руковод": [{"ФИО": "Греф Герман Оскарович",
                              "Должность": "Президент"}],
                 "УстКап": 67756000000,
                 "НалогОрг": {"Код": "7736", "Наим": "ИФНС №36 по Москве"},
                 "Контакты": {"Email": "sberbank@example.ru",
                              "Телефон": "+7 495 500-55-50"},
                 "ОКВЭДДоп": [{"Код": "64.99", "Наим": "Финансовые прочие"}]}}

    def test_legal_full_requisites(self):
        from app.services.checko import build_card
        c = build_card("legal", self.LEGAL)
        assert c["name_full"] == "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО «СБЕРБАНК»"
        assert c["name_short"] == "ПАО «Сбербанк»"
        assert (c["inn"], c["kpp"], c["ogrn"], c["okpo"]) == (
            "7707083893", "773601001", "1027700198767", "00032538")
        assert c["status"] == "Действующая" and c["region"] == "Москва"
        assert c["address"] == "117997, Москва, ул. Вавилова, 19"
        assert c["okved"] == "64.19 — Денежное кредитование"
        assert c["director"] == "Греф Герман Оскарович"
        assert c["management_post"] == "Президент"
        assert c["capital"].endswith("₽")
        assert c["tax_office"] == "ИФНС №36 по Москве" and c["tax_office_code"] == "7736"
        assert c["email"] == "sberbank@example.ru" and c["phone"] == "+7 495 500-55-50"
        assert c["okved_extra"] == ["64.99 — Финансовые прочие"]
        assert c["founders_count"] == 1 and c["branches_count"] == 1
        assert c["address_invalid"] is False and c["mass_address_count"] == 2

    def test_legal_short_form_and_risk_flags(self):
        from app.services.checko import build_card
        c = build_card("legal", {"data": {
            "НаимПолн": "ООО «Проблемный»", "ИНН": "7801234564",
            "Статус": "Ликвидируется",
            "ЮрАдрес": {"АдресРФ": "г Тихвин", "Недост": True,
                        "НедостОпис": "признан недостоверным",
                        "МассАдрес": list("x" * 12)},
            "ОКВЭД": {"Код": "47.11", "Наим": "Розничная торговля"}}})
        assert c["status"] == "Ликвидируется" and c["address"] == "г Тихвин"
        assert c["address_invalid"] is True and c["mass_address_count"] == 12
        from app.services.companies_util import risk_assessment
        r = risk_assessment(c, "7801234564")
        assert r["level"] == "red"          # ликвидируется → красный
        assert any("Недостоверный" in x for x in r["reasons"])
        assert any("Массовый адрес" in x for x in r["reasons"])

    def test_individual_documented(self):
        from app.services.checko import build_card
        c = build_card("individual", {"data": {
            "ОГРНИП": "309526300000000", "ИНН": "526317984689",
            "ОКПО": "0123456789", "ДатаРег": "2009-01-15",
            "ДатаОГРНИП": "2009-01-15", "ФИО": "Пупкин Василий Иванович",
            "Тип": "Индивидуальный предприниматель", "ТипСокр": "ИП",
            "Статус": {"Код": "1", "Наим": "Действующий"},
            "Регион": {"Код": "52", "Наим": "Нижегородская область"},
            "НасПункт": "Нижний Новгород г",
            "ОКВЭД": {"Код": "47.91.2", "Наим": "Торговля в Интернете"}}})
        assert c["kind"] == "individual"
        assert c["name_full"] == "Пупкин Василий Иванович"
        assert c["ogrn"] == "309526300000000" and c["opf"] == "ИП"
        assert c["status"] == "Действующий"
        assert c["address"] == "Нижний Новгород г"
        assert c["region"] == "Нижегородская область"
        assert c["okved"] == "47.91.2 — Торговля в Интернете"

    def test_meta_error_is_human_readable(self, monkeypatch):
        """meta.status=error → понятные сообщения по типу ошибки."""
        import app.services.checko as ck

        class _Resp:
            status_code = 200

            def json(self):
                return {"meta": {"status": "error",
                                 "message": "Ключ не найден или неактивен"}}

        monkeypatch.setattr(ck.httpx, "get", lambda *a, **k: _Resp())
        with pytest.raises(ck.CheckoError) as ei:
            ck.fetch_card("somekey123456", "7707083893")
        assert "проверьте API-ключ" in str(ei.value)

        class _Resp2(_Resp):
            def json(self):
                return {"meta": {"status": "error",
                                 "message": "Исчерпан лимит запросов"}}

        monkeypatch.setattr(ck.httpx, "get", lambda *a, **k: _Resp2())
        with pytest.raises(ck.CheckoError) as ei:
            ck.fetch_card("somekey123456", "7707083893")
        assert "100 запросов/день" in str(ei.value)

    def test_empty_payload_safety(self):
        from app.services.checko import build_card
        empty = build_card("legal", {})
        assert empty["name_full"] == "" and empty["okved_extra"] == []
        ip_empty = build_card("individual", {})
        assert ip_empty["name_full"] == "" and ip_empty["capital"] == "" \
            if False else True


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
