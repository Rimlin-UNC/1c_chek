# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.12.3: автор и компания на чеках; вёрстка
# окна «Что нового» (карточки вместо сетки .kv).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import itertools

from tests.conftest import login

_SEQ = itertools.count(500)


def _qr(sum_: str = "250.00") -> str:
    n = next(_SEQ)
    return (f"t=20261005T1530&s={sum_}&fn=7381440700{n:07d}"
            f"&i={n}&fp={700000000 + n}&n=1")


class TestAuthorAndCompanyOnReceipts:
    def test_list_and_card_show_author_and_company(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": 'ООО «Вектор»'}, headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"],
            "note": "Виктория — ООО «Вектор»"}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "vector_u",
            "password": "parol123", "full_name": "Виктория Векторная"})
        hdr_u = {"Authorization": "Bearer " + reg.json()["access_token"]}
        rid = client.post("/api/v1/receipts/scan",
                          json={"qr_data": _qr(), "source": "web"},
                          headers=hdr_u).json()["receipt"]["id"]
        # список (админ): автор и компания на месте
        item = next(x for x in client.get("/api/v1/receipts",
                                          headers=hdr).json()["items"]
                    if x["id"] == rid)
        assert item["created_by_name"] == "Виктория Векторная"
        assert item["created_by"] == "vector_u"
        assert item["company_name"] == 'ООО «Вектор»'
        # карточка
        d = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
        assert d["created_by_name"] == "Виктория Векторная"
        assert d["company_name"] == 'ООО «Вектор»'
        # скан-ответ тоже несёт название компании
        r2 = client.post("/api/v1/receipts/scan",
                         json={"qr_data": _qr(), "source": "web"},
                         headers=hdr_u).json()["receipt"]
        assert r2["company_name"] == 'ООО «Вектор»'

    def test_csv_has_author_and_company_columns(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": 'ООО «Горизонт»'}, headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "gorizont_u",
            "password": "parol123", "full_name": "Григорий Горизонтов"})
        hdr_u = {"Authorization": "Bearer " + reg.json()["access_token"]}
        client.post("/api/v1/receipts/scan",
                    json={"qr_data": _qr("777.00"), "source": "web"},
                    headers=hdr_u)
        r = client.post("/api/v1/receipts/export-csv",
                        json={"receipt_ids": []}, headers=hdr)
        assert r.status_code == 200
        text = r.content.decode("utf-8-sig")
        assert "Кто добавил" in text and "Компания" in text
        assert "Григорий Горизонтов" in text and "ООО «Горизонт»" in text


class TestWhatsNewLayout:
    def test_cards_markup_not_kv_grid(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'class="whats-new"' in js and "wn-item" in js
        # старая сломанная сетка не должна вернуться
        assert "grid-template-columns:auto 1fr" not in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".wn-item {" in css and ".wn-text {" in css
        assert "overflow-wrap: anywhere" in css

    def test_ui_strings_for_author_and_selection(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "rcpt-added" in js                      # строка автора в таблице
        assert "· добавили: " in js                    # сводка выбора
        assert "· компании: " in js
        assert "<dt>Кто добавил</dt>" in js            # карточка чека
        assert "<dt>Компания</dt>" in js
