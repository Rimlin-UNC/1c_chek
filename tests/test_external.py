# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.2.0: внешние источники данных чека,
# редактирование чеков по ролям, авто-привязка «от кого прислал».
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import json

import pytest

from tests.conftest import login

QR = "t=20260927T1529&s=2150.00&fn=7381440700130934&i=33079&fp=3673437411&n=1"


# ---------------------------------------------------------------------------
#  Помощники
# ---------------------------------------------------------------------------
def _make_user(client, role, username, full_name):
    """Уникальный пользователь на каждый вызов (БД живёт всю сессию тестов)."""
    import uuid
    username = f"{username}{uuid.uuid4().hex[:6]}"
    hdr = login(client, "admin", "admin123")
    r = client.post("/api/v1/invites", json={
        "role": role, "max_uses": 1, "expires_hours": 24, "note": full_name},
        headers=hdr)
    assert r.status_code == 200, r.text
    r2 = client.post("/api/v1/auth/register", json={
        "token": r.json()["token"], "username": username,
        "password": "secret2026", "full_name": full_name})
    assert r2.status_code == 200, r2.text
    return login(client, username, "secret2026")


def _scan(client, hdr, qr=QR, unique=True):
    if unique:
        # подменяем ФП на случайный — гарантирует уникальность ФН+ФД+ФП
        import random
        import re
        qr = re.sub(r"fp=\d+", "fp=" + str(random.randrange(10**9, 10**10)), qr)
    r = client.post("/api/v1/receipts/scan", json={"qr_data": qr, "source": "manual"},
                    headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["receipt"]


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    """В тестах отключаем вежливые паузы движка (2–7 с)."""
    from app.services.external import engine
    monkeypatch.setattr(engine, "_polite_wait", lambda provider: None)


# ---------------------------------------------------------------------------
#  Автоматическая привязка «от кого прислал»
# ---------------------------------------------------------------------------
class TestAutoAssign:
    def test_scan_by_user_sets_assignee(self, client):
        hdr = _make_user(client, "user", "petrov", "Петров П.П.")
        receipt = _scan(client, hdr)
        assert receipt["assignee"] == "Петров П.П."

    def test_scan_by_accountant_does_not_autofill(self, client):
        hdr = _make_user(client, "accountant", "glavbuh", "Смирнова А.А.")
        receipt = _scan(client, hdr,
                        "t=20260928T0930&s=3652.00&fn=7384440900770272&i=114955&fp=1734221535&n=13")
        assert receipt["assignee"] == ""


# ---------------------------------------------------------------------------
#  Правила PATCH: сотрудник vs бухгалтер
# ---------------------------------------------------------------------------
class TestPatchRules:
    def test_user_can_notify_and_comment_only(self, client):
        hdr = _make_user(client, "user", "petrov", "Петров П.П.")
        receipt = _scan(client, hdr)

        # уведомление + комментарий — можно
        r = client.patch(f"/api/v1/receipts/{receipt['id']}",
                         json={"notified": True, "comment": "Товар вернули, чек заменю"},
                         headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["notified"] is True
        assert "вернули" in r.json()["comment"]

        # попытка изменить сотрудника/сумму — нельзя
        r2 = client.patch(f"/api/v1/receipts/{receipt['id']}",
                          json={"assignee": "Кто-то другой"}, headers=hdr)
        assert r2.status_code == 400
        r3 = client.patch(f"/api/v1/receipts/{receipt['id']}",
                          json={"total_sum": 999}, headers=hdr)
        assert r3.status_code == 400

    def test_accountant_can_edit_all_and_items(self, client):
        admin_hdr = login(client, "admin", "admin123")
        receipt = _scan(client, admin_hdr)
        hdr = login(client, "admin", "admin123")

        r = client.patch(f"/api/v1/receipts/{receipt['id']}", json={
            "assignee": "Иванов А.А.", "merchant_name": "ООО «Пятёрочка»",
            "merchant_inn": "1234567890", "cashier": "Кассир №1",
            "total_sum": 2170.0,
            "items": [
                {"name": "Молоко 2.5% 1л", "quantity": 2, "price": 89.9, "total": 179.8},
                {"name": "Хлеб бородинский", "quantity": 1, "price": 55.2, "total": 55.2},
            ]}, headers=hdr)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["merchant_name"] == "ООО «Пятёрочка»"
        assert data["assignee"] == "Иванов А.А."
        assert len(data["items"]) == 2
        assert data["items"][0]["name"] == "Молоко 2.5% 1л"
        assert data["details_source"] == "manual_edit"

    def test_duplicate_receipt_number_conflict(self, client):
        hdr = login(client, "admin", "admin123")
        a = _scan(client, hdr)
        b = _scan(client, hdr, "t=20260929T1200&s=100.00&fn=1111111111111111&i=22&fp=333&n=1")
        r = client.patch(f"/api/v1/receipts/{b['id']}",
                         json={"fn": a["fn"], "fd": a["fd"], "fp": a["fp"]}, headers=hdr)
        assert r.status_code == 409

    def test_foreign_user_cannot_patch(self, client):
        hdr1 = _make_user(client, "user", "petrov", "Петров П.П.")
        receipt = _scan(client, hdr1)
        hdr2 = _make_user(client, "user", "sidorov", "Сидоров С.С.")
        r = client.patch(f"/api/v1/receipts/{receipt['id']}",
                         json={"comment": "чужой чек"}, headers=hdr2)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
#  Получение данных из внешних источников
# ---------------------------------------------------------------------------
class TestExternalFetch:
    def test_requires_accountant(self, client):
        hdr = _make_user(client, "user", "petrov", "Петров П.П.")
        receipt = _scan(client, hdr)
        r = client.post(f"/api/v1/receipts/{receipt['id']}/fetch-details", headers=hdr)
        assert r.status_code == 403

    def test_mock_provider_fills_details(self, client, monkeypatch):
        from app.services.external import engine
        hdr = login(client, "admin", "admin123")
        receipt = _scan(client, hdr)
        # без токенов в цепочке только mock; паузы отключены фикстурой
        assert engine.provider_chain.__self__ is engine
        r = client.post(f"/api/v1/receipts/{receipt['id']}/fetch-details", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["queued"] is True
        # BackgroundTasks в TestClient выполняются синхронно после ответа
        r2 = client.get(f"/api/v1/receipts/{receipt['id']}", headers=hdr)
        data = r2.json()
        assert data["details_source"] == "mock"
        assert data["details_fetched_at"] is not None

    def test_engine_rotates_on_429(self, client, monkeypatch):
        import app.services.external as ext_mod
        from app.services.external import engine
        # настраиваем токен proverkacheka, чтобы он попал в цепочку
        hdr = login(client, "admin", "admin123")
        client.put("/api/v1/settings/external",
                   json={"proverkacheka_token": "test-token-123"}, headers=hdr)
        calls = []

        def fake_pke(qr_raw, token):
            calls.append("pke")
            return False, "HTTP 429 (лимит/блокировка)", {}

        monkeypatch.setattr(ext_mod, "fetch_proverkacheka", fake_pke)
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            res = engine.fetch(db, QR, "7381440700130934", "33079",
                               "3673437411", 2150.0, None)
        finally:
            db.close()
        assert calls == ["pke"], "источник должен быть вызван ровно один раз до остывания"
        st = engine.status()
        assert st["proverkacheka"]["available"] is False
        assert st["proverkacheka"]["cooldown_sec"] > 0
        # mock подхватывает — чек не теряется
        assert res.source == "mock"

    def test_settings_masked(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/external",
                       json={"proverkacheka_token": "super-secret-token-value"},
                       headers=hdr)
        assert r.status_code == 200
        g = client.get("/api/v1/settings/external", headers=hdr).json()
        assert g["has_proverkacheka_token"] is True
        assert "super-secret-token-value" not in g["proverkacheka_token_masked"]
        assert "•" in g["proverkacheka_token_masked"]


# ---------------------------------------------------------------------------
#  Парсер ответов сервисов (масштаб сумм, позиции, реквизиты)
# ---------------------------------------------------------------------------
class TestParsing:
    FNS_LIKE = {
        "document": {"receipt": {
            "dateTime": "2026-09-27T15:29:00",
            "totalSum": 215000,                      # копейки!
            "operation": 1,
            "user": "ООО «Пятёрочка»", "userInn": "1234567890",
            "retailPlaceAddress": "г. Москва, ул. Ленина, 1",
            "operator": "Кассир Иванова",
            "cashTotalSum": 15000, "ecashTotalSum": 200000,
            "items": [
                {"name": "Молоко 2.5% 1л", "price": 8990, "quantity": 2.0,
                 "sum": 17980, "nds": 10, "ndsSum": 1634},
                {"name": "Хлеб", "price": 3520, "quantity": 1.0,
                 "sum": 3520, "nds": 0, "ndsSum": 0},
            ],
        }},
    }

    def test_kopecks_converted(self):
        from app.services.external import parse_receipt_payload
        res = parse_receipt_payload(self.FNS_LIKE, known_rub=2150.00)
        assert res.found is True
        assert res.total_sum == 2150.0
        assert res.merchant_name == "ООО «Пятёрочка»"
        assert res.merchant_inn == "1234567890"
        assert res.cash_sum == 150.0
        assert res.ecash_sum == 2000.0
        assert len(res.items) == 2
        assert res.items[0].price == 89.9
        assert res.items[0].total == 179.8
        assert res.date_time is not None

    def test_rubles_not_double_converted(self):
        from app.services.external import parse_receipt_payload
        data = {"receipt": {"totalSum": 2150.0, "dateTime": "2026-09-27T15:29:00",
                            "items": [{"name": "X", "price": 100, "quantity": 1, "sum": 100}]}}
        res = parse_receipt_payload(data, known_rub=2150.0)
        assert res.total_sum == 2150.0

    def test_no_receipt_in_answer(self):
        from app.services.external import parse_receipt_payload
        res = parse_receipt_payload({"error": "not found"})
        assert res.found is False
        assert res.ok is True
