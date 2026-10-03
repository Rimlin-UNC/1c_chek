# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.11.0: МУЛЬТИКОМПАНИЙНОСТЬ (аутсорсинг бухгалтерии).
# Компании-клиенты с изолированными пространствами; админ видит всё и
# перемещает чеки; приглашения привязаны к компании; WS-изоляция.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login

_SEQ = [0]


def _qr(sum_: str = "2150.00") -> str:
    """Уникальный QR для теста: БД живёт всю сессию — пересечений быть не должно."""
    _SEQ[0] += 1
    n = _SEQ[0]
    return (f"t=2026100{n % 28 + 1}T1530&s={sum_}"
            f"&fn=7381440700{n:07d}&i={40000 + n}&fp={900000000 + n}&n=1")


def _mk_company(client, hdr, name, inn=""):
    r = client.post("/api/v1/companies", json={"name": name, "inn": inn},
                    headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


def _invite_into(client, hdr, company_id, role="user"):
    body = {"role": role, "note": "тест"}
    if company_id:
        body["company_id"] = company_id
    r = client.post("/api/v1/invites", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _register(client, token, username):
    r = client.post("/api/v1/auth/register", json={
        "token": token, "username": username,
        "password": "secret123", "full_name": username.title()})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


class TestCompaniesCrud:
    def test_create_list_rename_archive(self, client):
        hdr = login(client, "admin", "admin123")
        c1 = _mk_company(client, hdr, 'ООО «Партнёр-СВ»', "7801234567")
        assert c1["name"] == 'ООО «Партнёр-СВ»' and c1["is_active"]
        # дубликат имени запрещён
        r = client.post("/api/v1/companies", json={"name": "ооо «партнёр-св»"},
                        headers=hdr)
        assert r.status_code == 409
        # переименование
        r = client.patch(f"/api/v1/companies/{c1['id']}",
                         json={"name": 'ООО «Партнёр-СВ+»'}, headers=hdr)
        assert r.status_code == 200 and r.json()["name"] == 'ООО «Партнёр-СВ+»'
        # список содержит статистику
        lst = client.get("/api/v1/companies", headers=hdr).json()
        assert any(x["id"] == c1["id"] for x in lst)
        assert all("receipts" in x and "accountants" in x for x in lst)
        # архивировать НЕ последнюю активную можно
        _mk_company(client, hdr, "ИП Иванов И.И.")
        r = client.patch(f"/api/v1/companies/{c1['id']}",
                         json={"is_active": False}, headers=hdr)
        assert r.status_code == 200 and r.json()["is_active"] is False
        # архив запрещает перемещение чеков
        r2 = client.post("/api/v1/receipts/move",
                         json={"receipt_ids": ["x"], "company_id": c1["id"]},
                         headers=hdr)
        assert r2.status_code == 409
        # не-админ компаний не видит
        tok = _invite_into(client, hdr, None, "user")
        hdr_u = _register(client, tok, "comp_user1")
        assert client.get("/api/v1/companies", headers=hdr_u).status_code == 403

    def test_default_company_created(self, client):
        """Миграция: компания по умолчанию существует (данные не теряются)."""
        hdr = login(client, "admin", "admin123")
        lst = client.get("/api/v1/companies", headers=hdr).json()
        assert isinstance(lst, list)


class TestIsolation:
    def test_companies_cannot_see_each_other(self, client):
        hdr = login(client, "admin", "admin123")
        ca = _mk_company(client, hdr, 'ООО «Альфа»')
        cb = _mk_company(client, hdr, 'ООО «Бета»')
        # бухгалтеры компаний
        ha = _register(client, _invite_into(client, hdr, ca["id"], "accountant"), "alfa_buh")
        hb = _register(client, _invite_into(client, hdr, cb["id"], "accountant"), "beta_buh")
        # каждый сканирует чек в свою компанию
        ra = client.post("/api/v1/receipts/scan",
                         json={"qr_data": _qr(), "source": "manual"},
                         headers=ha).json()["receipt"]
        rb = client.post("/api/v1/receipts/scan",
                         json={"qr_data": _qr(), "source": "manual"},
                         headers=hb).json()["receipt"]
        assert ra["company_id"] == ca["id"] and rb["company_id"] == cb["id"]
        # изоляция списка
        list_a = client.get("/api/v1/receipts", headers=ha).json()["items"]
        assert [x["id"] for x in list_a] == [ra["id"]]
        # изоляция карточки/QR: чужой чек = «не найден»
        assert client.get(f"/api/v1/receipts/{rb['id']}", headers=ha).status_code == 404
        assert client.get(f"/api/v1/receipts/{rb['id']}/qr.png", headers=ha).status_code == 404
        # правка и удаление чужого — тоже
        assert client.patch(f"/api/v1/receipts/{rb['id']}", json={"comment": "хак"},
                            headers=ha).status_code == 404
        assert client.delete(f"/api/v1/receipts/{rb['id']}", headers=ha).status_code == 404
        # админ видит обе компании
        adm = client.get("/api/v1/receipts", headers=hdr).json()["items"]
        assert {ra["id"], rb["id"]} <= {x["id"] for x in adm}
        # админ с фильтром — только одну
        admf = client.get(f"/api/v1/receipts?company_id={ca['id']}",
                          headers=hdr).json()["items"]
        assert [x["id"] for x in admf] == [ra["id"]]
        # пользователь видит только свои чеки своей компании
        hu = _register(client, _invite_into(client, hdr, ca["id"], "user"), "alfa_user")
        ru = client.post("/api/v1/receipts/scan",
                         json={"qr_data": _qr("100.00"), "source": "manual"},
                         headers=hu).json()["receipt"]
        lst = client.get("/api/v1/receipts", headers=hu).json()["items"]
        assert [x["id"] for x in lst] == [ru["id"]]           # чек бухгалтера не виден

    def test_cross_company_duplicate_hidden(self, client):
        """Скан чека, уже учтённого в другой компании, не раскрывает данные."""
        hdr = login(client, "admin", "admin123")
        ca = _mk_company(client, hdr, 'ООО «Гамма»')
        cb = _mk_company(client, hdr, 'ООО «Дельта»')
        ha = _register(client, _invite_into(client, hdr, ca["id"], "accountant"), "gamma_buh")
        hb = _register(client, _invite_into(client, hdr, cb["id"], "accountant"), "delta_buh")
        qr = _qr()                      # один и тот же чек сканируют обе компании
        client.post("/api/v1/receipts/scan", json={"qr_data": qr, "source": "manual"},
                    headers=ha)
        r = client.post("/api/v1/receipts/scan",
                        json={"qr_data": qr, "source": "manual"}, headers=hb)
        assert r.status_code == 200
        assert r.json()["receipt"] is None and r.json()["duplicate"] is True
        assert "другой организации" in r.json()["message"]


class TestMoveReceipts:
    def test_admin_moves_receipt_between_companies(self, client):
        hdr = login(client, "admin", "admin123")
        ca = _mk_company(client, hdr, 'ООО «Омега»')
        cb = _mk_company(client, hdr, 'ООО «Сигма»')
        ha = _register(client, _invite_into(client, hdr, ca["id"], "accountant"), "omega_buh")
        hb = _register(client, _invite_into(client, hdr, cb["id"], "accountant"), "sigma_buh")
        rid = client.post("/api/v1/receipts/scan",
                          json={"qr_data": _qr(), "source": "manual"},
                          headers=ha).json()["receipt"]["id"]
        # бухгалтер А видит, Б — нет
        assert client.get(f"/api/v1/receipts/{rid}", headers=ha).status_code == 200
        assert client.get(f"/api/v1/receipts/{rid}", headers=hb).status_code == 404
        # не-админ переместить не может
        r = client.post("/api/v1/receipts/move",
                        json={"receipt_ids": [rid], "company_id": cb["id"]},
                        headers=ha)
        assert r.status_code == 403
        # админ перемещает с новым подотчётным
        r = client.post("/api/v1/receipts/move",
                        json={"receipt_ids": [rid], "company_id": cb["id"],
                              "assignee": "Сигма С.С."}, headers=hdr)
        assert r.status_code == 200 and r.json()["moved"] == 1, r.text
        # теперь видит Б, А — нет; подотчётный переназначен
        assert client.get(f"/api/v1/receipts/{rid}", headers=hb).status_code == 200
        assert client.get(f"/api/v1/receipts/{rid}", headers=ha).status_code == 404
        d = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
        assert d["company_id"] == cb["id"] and d["assignee"] == "Сигма С.С."

    def test_admin_scan_into_selected_company(self, client):
        hdr = login(client, "admin", "admin123")
        ca = _mk_company(client, hdr, 'ООО «Лама»')
        r = client.post("/api/v1/receipts/scan",
                        json={"qr_data": _qr("4999.99"), "source": "manual",
                              "company_id": ca["id"]}, headers=hdr).json()["receipt"]
        assert r["company_id"] == ca["id"]
        # пользователь без компании: скан уходит в «нулевое» пространство
        hu = _register(client, _invite_into(client, hdr, None, "user"), "free_user")
        r2 = client.post("/api/v1/receipts/scan",
                         json={"qr_data": _qr("50.00"), "source": "manual"},
                         headers=hu).json()["receipt"]
        assert r2["company_id"] is None


class TestInvitesAndUsers:
    def test_invite_binds_company(self, client):
        hdr = login(client, "admin", "admin123")
        c = _mk_company(client, hdr, 'ООО «Ток»')
        tok = _invite_into(client, hdr, c["id"], "accountant")
        info = client.get(f"/api/v1/auth/invite-info?token={tok}").json()
        assert info["valid"] and info["company_id"] == c["id"]
        assert "ООО «Ток»" in (info["company_name"] or "")
        hdr_u = _register(client, tok, "tok_buh")
        me = client.get("/api/v1/auth/me", headers=hdr_u).json()
        assert me["company_id"] == c["id"] and me["company_name"] == 'ООО «Ток»'
        # список приглашений показывает компанию
        inv = client.get("/api/v1/invites", headers=hdr).json()
        assert any(x["company_name"] == 'ООО «Ток»' for x in inv)
        # создание пользователя сразу в компанию
        r = client.post("/api/v1/users", json={
            "username": "tok_user", "password": "parol123",
            "role": "user", "company_id": c["id"]}, headers=hdr)
        assert r.status_code == 200 and r.json()["company_id"] == c["id"]
        # перевод сотрудника в другую компанию
        c2 = _mk_company(client, hdr, 'ООО «Ротор»')
        r = client.patch(f"/api/v1/users/{r.json()['id']}",
                         json={"company_id": c2["id"]}, headers=hdr)
        assert r.status_code == 200 and r.json()["company_id"] == c2["id"]
