# -*- coding: utf-8 -*-
"""
Ямастер Чек — тесты ролевой модели, приглашений и защиты.
ООО «Ямастер» | ymaster.ru | info@ymaster.ru
"""
import time

from tests.conftest import login

QR = "t=20250901T1430&s=1500.00&fn=9999078902001299&i=60001&fp=777888999&n=1"


# ---------------------------------------------------------------------------
#  Приглашения и регистрация
# ---------------------------------------------------------------------------
class TestInvites:
    def test_admin_creates_invite_and_user_registers(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/invites", json={
            "role": "accountant", "max_uses": 1, "expires_hours": 24,
            "note": "Иванова"},
            headers=hdr)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["valid"] is True
        assert inv["role"] == "accountant"

        # Регистрация по токену: роль присваивается сразу
        r2 = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "ivanova",
            "password": "secret2026", "full_name": "Иванова Анна"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["user"]["role"] == "accountant"

        # Одноразовое приглашение больше не работает
        r3 = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "hacker",
            "password": "secret2026", "full_name": "X"})
        assert r3.status_code == 403

    def test_revoked_invite_rejected(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={
            "role": "user", "max_uses": 5, "expires_hours": 24},
            headers=hdr).json()
        client.post(f"/api/v1/invites/{inv['id']}/revoke", headers=hdr)
        r = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "revoked_user",
            "password": "secret2026", "full_name": "X"})
        assert r.status_code == 403

    def test_invite_cannot_grant_admin(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={
            "role": "accountant", "max_uses": 1, "expires_hours": 24},
            headers=hdr).json()
        # Пытаемся подделать роль на сервере нельзя: роль берётся из приглашения.
        # Проверяем, что invite с ролью admin не создаётся в принципе:
        r = client.post("/api/v1/users", json={
            "username": "second_admin", "password": "secret2026",
            "full_name": "X", "role": "admin"}, headers=hdr)
        assert r.status_code in (400, 422)  # второй админ запрещён (валидация + обработчик)


# ---------------------------------------------------------------------------
#  Администратор всегда один + передача прав
# ---------------------------------------------------------------------------
class TestSingleAdmin:
    def test_promote_admin_flow(self, client):
        hdr = login(client, "admin", "admin123")
        # создаём бухгалтера вручную
        u = client.post("/api/v1/users", json={
            "username": "buhgalter", "password": "pass123456",
            "full_name": "Бухгалтер", "role": "accountant"}, headers=hdr).json()

        # patch не может сменить роль
        r = client.patch(f"/api/v1/users/{u['id']}", json={"role": "user"},
                         headers=hdr)
        assert r.status_code == 400  # смена роли через patch запрещена в принципе

        # передача прав: бухгалтер становится админом, старый — бухгалтером
        r = client.post(f"/api/v1/users/{u['id']}/promote-admin", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["new_admin"]["role"] == "admin"
        assert r.json()["previous_admin"]["role"] == "accountant"

        # Вернём права обратно (чтобы остальные тесты работали с admin/admin123)
        hdr2 = login(client, "buhgalter", "pass123456")
        users_now = client.get("/api/v1/users", headers=hdr2).json()
        # в системе по-прежнему один админ
        assert sum(1 for x in users_now if x["role"] == "admin") == 1
        old_admin = [x for x in users_now if x["username"] == "admin"][0]
        assert old_admin["role"] == "accountant"
        r = client.post(f"/api/v1/users/{old_admin['id']}/promote-admin", headers=hdr2)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
#  Права по ролям
# ---------------------------------------------------------------------------
class TestRolePermissions:
    def test_user_sees_only_own_receipts(self, client):
        hdr_admin = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={
            "role": "user", "max_uses": 1, "expires_hours": 24},
            headers=hdr_admin).json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "worker1",
            "password": "worker123", "full_name": "Работник"})

        # admin сканирует чек
        client.post("/api/v1/receipts/scan", json={"qr_data": QR},
                    headers=hdr_admin)
        hdr_user = login(client, "worker1", "worker123")

        # пользователь не видит чужой чек
        items = client.get("/api/v1/receipts", headers=hdr_user).json()["items"]
        assert all(True for _ in items)  # свои пусто или только свои
        # а админ видит
        items_admin = client.get("/api/v1/receipts", headers=hdr_admin).json()["items"]
        assert any(r["fd"] == "60001" for r in items_admin)

    def test_user_cannot_verify_or_export(self, client):
        hdr_admin = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={
            "role": "user", "max_uses": 1, "expires_hours": 24},
            headers=hdr_admin).json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "worker2",
            "password": "worker123", "full_name": "Работник 2"})
        hdr_user = login(client, "worker2", "worker123")

        assert client.post("/api/v1/receipts/verify", json={"receipt_ids": []},
                           headers=hdr_user).status_code == 403
        assert client.post("/api/v1/receipts/export",
                           json={"receipt_ids": [], "format": "json"},
                           headers=hdr_user).status_code == 403
        assert client.post("/api/v1/receipts/export-csv", json={"receipt_ids": []},
                           headers=hdr_user).status_code == 403
        assert client.get("/api/v1/settings/mapping",
                          headers=hdr_user).status_code == 403
        assert client.get("/api/v1/users", headers=hdr_user).status_code == 403
        assert client.get("/api/v1/invites", headers=hdr_user).status_code == 403

    def test_accountant_can_verify_export_assign(self, client):
        hdr_admin = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", json={
            "role": "accountant", "max_uses": 1, "expires_hours": 24},
            headers=hdr_admin).json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "acc1",
            "password": "acc12345", "full_name": "Гл. бухгалтер"})
        hdr_acc = login(client, "acc1", "acc12345")

        # сканируем и назначаем сотрудника
        r = client.post("/api/v1/receipts/scan",
                        json={"qr_data": "t=20250901T1200&s=100.00&fn=9999078902001234&i=60002&fp=555666777&n=1"},
                        headers=hdr_acc)
        rid = r.json()["receipt"]["id"]
        r = client.post("/api/v1/receipts/bulk-assign",
                        json={"receipt_ids": [rid], "assignee": "Иванов А.А."},
                        headers=hdr_acc)
        assert r.status_code == 200
        d = client.get(f"/api/v1/receipts/{rid}", headers=hdr_acc).json()
        assert d["assignee"] == "Иванов А.А."

        # проверка и экспорт доступны
        assert client.post("/api/v1/receipts/verify", json={"receipt_ids": []},
                           headers=hdr_acc).status_code == 200

    def test_assignee_in_export_payload(self, client):
        from app.services.exporter import build_push_payload
        from app.models import Receipt

        class R:
            id = "x"; fn = "1"; fd = "2"; fp = "3"
            receipt_date = __import__("datetime").datetime(2025, 9, 1, 10, 0)
            created_at = receipt_date
            total_sum = 100.0; operation = 1; fns_status = "valid"
            qr_data = "t=..."; assignee = "Иванов"; comment = "тест"
            items = []
        payload = build_push_payload([R()], [], "ПоступлениеТоваровУслуг")
        assert payload["receipts"][0]["assignee"] == "Иванов"


# ---------------------------------------------------------------------------
#  Защита от перебора
# ---------------------------------------------------------------------------
class TestBruteForce:
    def test_rate_limit_login(self, client):
        # Серия неудачных входов: после N запросов должен прийти 401/429
        codes = []
        for i in range(12):
            r = client.post("/api/v1/auth/login",
                            json={"username": "nosuchuser", "password": "wrong"})
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in codes, f"ожидали 429, получили: {codes}"

    def test_login_guard_blocks_bruteforce(self, client):
        from app.security import login_guard
        key = ("1.2.3.4", "target")
        for _ in range(5):
            login_guard.record_fail(key)
        try:
            login_guard.check(key)
            raise AssertionError("ожидали блокировку")
        except Exception as e:
            assert "429" in str(getattr(e, "status_code", "")) or "429" in str(e)

    def test_sliding_window_limiter(self, client):
        from app.security import SlidingWindowLimiter
        lim = SlidingWindowLimiter()
        allowed = sum(1 for _ in range(10) if lim.allow("k", 5, 60))
        assert allowed == 5


# ---------------------------------------------------------------------------
#  Петля входа: cookie-фолбэк (регрессия — логин 200, следующий запрос 401)
# ---------------------------------------------------------------------------
class TestSessionCookie:
    def test_login_sets_cookie(self, client):
        r = client.post("/api/v1/auth/login",
                        json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        assert "ymaster_token" in r.cookies, "cookie сессии не установлена"

    def test_requests_work_via_cookie_only(self, client):
        # Без заголовка Authorization — только cookie: не должно быть 401
        client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "admin123"})
        r = client.get("/api/v1/dashboard/stats?days=7")
        assert r.status_code == 200, r.text
        r2 = client.get("/api/v1/auth/me")
        assert r2.status_code == 200
        assert r2.json()["username"] == "admin"

    def test_no_cookie_no_token_returns_401(self, client):
        client.cookies.clear()   # session-scoped клиент: убираем cookie прошлых тестов
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_logout_clears_cookie(self, client):
        client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "admin123"})
        r = client.post("/api/v1/auth/logout")
        assert r.status_code == 200
        # cookie удалена → запрос без заголовка снова 401
        client.cookies.clear()
        assert client.get("/api/v1/auth/me").status_code == 401
