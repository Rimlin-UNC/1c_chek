"""Закрытие выписки документами и папка с УПД (T-79, #63, v1.70.113, ADR-43).

Подбор «платёж ↔ УПД» проверяется по шагам на чистых данных и сквозь API: папка с УПД расходится по компаниям
по ИНН (расход, продажа, документ между своими — в обе), поступления выписки сохраняются, отчёт показывает, что
закрыто, частично и без документов, ручная привязка и отвязка, права и изоляция компаний, удаление выписки.
SQLite и PostgreSQL (фикстура store); только синтетические данные (ADR-14).
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.auth import create_access_token, hash_password
from app.bank import closing
from app.bank.models import BankIncome, BankLink, BankOperation, BankStatement
from app.docreq.models import DocRequest
from app.models import Company, User
from app.primary.models import PrimaryDocument
from tests.primary_samples import inn10, upd_xml
from tests.test_bank import acc, doc, statement

D0 = date(2026, 10, 1)
PASSWORD_HASH = hash_password("parol123")


# ------------------------------------------------------------------ подбор по шагам (без базы)
def _p(i: int, day: date, amount: float, purpose: str = "") -> closing.Pay:
    return closing.Pay(f"p{i}", day, amount, "7700000001", "Поставщик", str(i), purpose)


def _d(i: int, day: date, amount: float, number: str = "") -> closing.Doc:
    return closing.Doc(f"d{i}", day, amount, "7700000001", "Поставщик", number or str(i), f"УПД № {number or i}")


def test_exact_number_and_partial():
    pays, docs = [_p(1, D0, 1000)], [_d(1, D0 + timedelta(days=3), 1000)]
    closing.allocate(pays, docs)
    assert pays[0].links == [("d1", 1000.0, "sum")] and pays[0].rest == 0 and docs[0].rest == 0
    # номер из назначения: оплата 15 000 при УПД № 45 на 9 000 — закрыто частично, на 6 000 документов нет
    pays, docs = [_p(1, D0, 15000, "Оплата по УПД № 45 от 20.10.2026")], [_d(1, D0, 9000, "45")]
    closing.allocate(pays, docs)
    assert pays[0].links == [("d1", 9000.0, "number")] and pays[0].rest == 6000
    assert closing._status(15000, pays[0].rest) == "partial"


def test_sets_both_ways():
    pays = [_p(1, D0, 100000)]
    docs = [_d(1, D0, 60000), _d(2, D0 + timedelta(days=5), 40000), _d(3, D0, 7000)]
    closing.allocate(pays, docs)
    assert sorted(x[0] for x in pays[0].links) == ["d1", "d2"] and {h for _, _, h in pays[0].links} == {"set"}
    assert {d.id: d for d in docs}["d3"].rest == 7000                    # лишний документ остаётся неоплаченным
    pays = [_p(1, D0, 50000), _p(2, D0 + timedelta(days=10), 50000)]
    docs = [_d(1, D0 + timedelta(days=12), 100000)]
    closing.allocate(pays, docs)
    assert [x[2] for x in docs[0].links] == ["set", "set"] and docs[0].rest == 0


def test_ambiguous_set_goes_fifo_and_fifo_closes_rest():
    pays = [_p(1, D0, 300)]
    docs = [_d(1, D0, 100), _d(2, D0 + timedelta(days=1), 200), _d(3, D0 + timedelta(days=2), 100),
            _d(4, D0 + timedelta(days=3), 200)]                          # 100+200 — два одинаковых набора
    closing.allocate(pays, docs)
    assert [(d, a, h) for d, a, h in pays[0].links] == [("d1", 100.0, "fifo"), ("d2", 200.0, "fifo")]
    pays = [_p(1, D0, 50000), _p(2, D0 + timedelta(days=20), 70000)]
    docs = [_d(1, D0 + timedelta(days=2), 40000), _d(2, D0 + timedelta(days=25), 80000)]
    closing.allocate(pays, docs)
    assert pays[0].links == [("d1", 40000.0, "fifo"), ("d2", 10000.0, "fifo")]
    assert pays[1].links == [("d2", 70000.0, "fifo")] and docs[1].rest == 0


def test_manual_pin_first_and_window():
    pays = [_p(1, D0, 1000), _p(2, D0 + timedelta(days=1), 1000)]
    docs = [_d(1, D0, 1000)]
    closing.allocate(pays, docs, pins=[("p2", "d1", None)])
    assert pays[1].links == [("d1", 1000.0, "manual")] and pays[0].rest == 1000
    # та же сумма, но документ за 200 дней до оплаты — не «та же сумма», а только очередь дат
    pays, docs = [_p(1, D0, 500)], [_d(1, D0 - timedelta(days=200), 500)]
    closing.allocate(pays, docs)
    assert pays[0].links == [("d1", 500.0, "fifo")]


# ------------------------------------------------------------------ сквозь API
def _user(db, company_id: str | None, role: str) -> User:
    user = User(username=f"cl_{role}_{uuid4().hex[:10]}", full_name=f"Тест {role}", password_hash=PASSWORD_HASH,
                role=role, company_id=company_id)
    db.add(user)
    db.flush()
    return user


def _hdr(user: User) -> dict:
    return {"Authorization": "Bearer " + create_access_token(user)}


def _upd(seller: tuple, buyer: tuple, number: str, day: str, base: int) -> bytes:
    """Документ на одну строку: НДС 22 % от целой базы — без копеечных расхождений в арифметике."""
    vat = base * 22 // 100
    line = {"name": "Материалы", "unit": "796", "unit_name": "шт", "qty": "1", "price": f"{base:.2f}",
            "base": f"{base:.2f}", "rate": "22%", "vat": f"{vat:.2f}", "total": f"{base + vat:.2f}", "code": ""}
    return upd_xml(seller=seller, buyer=buyer, number=number, day=day, lines=[line])


@pytest.fixture
def org(client, store):
    """Компании A и B (своя группа), поставщики S1–S3 и покупатель K1 — чужие ИНН. Бухгалтеры A и B, аутсорсер
    с клиентом B (своя — A), администратор (в SQLite — прежний, в PostgreSQL — свой)."""
    from app.clients.models import AccountantClient
    out = {"store": store, "inn": {k: inn10() for k in ("A", "B", "S1", "S2", "S3", "K1", "X", "Y")}}
    with store.session() as db:
        for letter in "AB":
            company = Company(name=f"Сверка {letter} {uuid4().hex[:6]}", inn=out["inn"][letter])
            db.add(company)
            db.flush()
            out[letter] = company.id
            out[f"acc_{letter.lower()}"] = _hdr(_user(db, company.id, "accountant"))
        outsourcer = _user(db, out["A"], "accountant")
        db.add(AccountantClient(user_id=outsourcer.id, company_id=out["B"]))
        out["outsourcer"] = _hdr(outsourcer)
        admin = (db.query(User).filter(User.username == "admin", User.role == "admin").one() if store.name == "SQLite"
                 else _user(db, None, "admin"))
        out["admin"] = _hdr(admin)
        db.commit()
    yield out
    client.cookies.clear()


def _party(org: dict, key: str, name: str | None = None) -> tuple:
    return (name or f"ООО «{key}»", org["inn"][key], "770101001")


def _folder(client, hdr, files: dict[str, bytes]):
    payload = [("files", (name, body, "application/xml")) for name, body in files.items()]
    return client.post("/api/v1/primary/upload", headers=hdr, files=payload, data={"route": "auto"})


def _statement_a(org: dict) -> bytes:
    ours = (org["account_a"], org["inn"]["A"], "ООО «A»")
    return statement(org["account_a"], [
        doc(number="1", day="10.10.2026", amount="97600.00", payer=ours, payee=(acc(), org["inn"]["S1"], "ООО «S1»"),
            purpose="Оплата по счёту 12 за материалы"),
        doc(number="2", day="12.10.2026", amount="15000.00", payer=ours, payee=(acc(), org["inn"]["S2"], "ООО «S2»"),
            purpose="Оплата по УПД № 45 от 08.10.2026"),
        doc(number="3", day="14.10.2026", amount="7000.00", payer=ours, payee=(acc(), org["inn"]["S3"], "ООО «S3»"),
            purpose="Оплата за услуги"),
        doc(number="4", day="16.10.2026", amount="48800.00", payer=(acc(), org["inn"]["K1"], "ООО «K1»"),
            payee=ours, purpose="Оплата по договору поставки", out=False),
        doc(number="5", day="18.10.2026", amount="3000.00", payer=(acc(), "", "Иванов Иван"), payee=ours,
            purpose="Возврат подотчёта", out=False),
    ])


@pytest.fixture
def filled(org):
    """Папка с УПД (расходы, продажа, чужой документ, документ внутри группы) и выписка компании A."""
    org["account_a"] = acc()
    files = {                                         # итоги с НДС: 61 000, 36 600, 9 150, 48 800, 1 220, 12 200
        "2026-10/S1/ON_NSCHFDOPPR_s1_60.xml": _upd(_party(org, "S1"), _party(org, "A"), "101", "05.10.2026", 50000),
        "2026-10/S1/ON_NSCHFDOPPR_s1_40.xml": _upd(_party(org, "S1"), _party(org, "A"), "102", "15.10.2026", 30000),
        "2026-10/S2/ON_NSCHFDOPPR_s2_45.xml": _upd(_party(org, "S2"), _party(org, "A"), "45", "08.10.2026", 7500),
        "продажи/ON_NSCHFDOPPR_k1.xml": _upd(_party(org, "A"), _party(org, "K1"), "П-7", "15.10.2026", 40000),
        "чужое/ON_NSCHFDOPPR_xy.xml": _upd(_party(org, "X"), _party(org, "Y"), "9", "01.10.2026", 1000),
        "группа/ON_NSCHFDOPPR_ab.xml": _upd(_party(org, "A"), _party(org, "B"), "Г-1", "11.10.2026", 10000),
    }
    org["files"] = files
    return org


def test_folder_routes_by_inn_and_rights(client, filled):
    org = filled
    res = _folder(client, org["acc_a"], org["files"])
    assert res.status_code == 200, res.text
    body = res.json()
    by_file = {}
    for r in body["results"]:
        by_file.setdefault(r["file"].rsplit("/", 1)[-1], []).append(r)
    assert by_file["ON_NSCHFDOPPR_xy.xml"][0]["result"] == "rejected"
    assert "не ваши компании" in by_file["ON_NSCHFDOPPR_xy.xml"][0]["message"]
    # обычный бухгалтер A — документ группы ложится только в A (продажа); B ему недоступна
    assert [(r["company_id"], r["direction"]) for r in by_file["ON_NSCHFDOPPR_ab.xml"]] == [(org["A"], "out")]
    assert by_file["ON_NSCHFDOPPR_k1.xml"][0]["direction"] == "out"
    assert by_file["ON_NSCHFDOPPR_s1_60.xml"][0]["direction"] == "in" and body["created"] == 5
    assert body["companies"] == [{"company_id": org["A"], "name": body["companies"][0]["name"], "in": 3, "out": 2,
                                  "duplicates": 0}]
    # аутсорсер с клиентом B — тот же файл группы ложится и в B (расход); в A он уже есть — дубль
    again = _folder(client, org["outsourcer"], {"ab.xml": org["files"]["группа/ON_NSCHFDOPPR_ab.xml"]}).json()
    assert sorted((r["company_id"], r["direction"], r["result"]) for r in again["results"]) == sorted(
        [(org["A"], "out", "duplicate"), (org["B"], "in", "created")])
    # что уже загружено — по SHA-256 в доступных компаниях
    hashes = [hashlib.sha256(b).hexdigest() for b in org["files"].values()]
    known_a = client.post("/api/v1/primary/known", headers=org["acc_a"], json={"hashes": hashes}).json()["known"]
    assert len(known_a) == 5 and hashlib.sha256(org["files"]["чужое/ON_NSCHFDOPPR_xy.xml"]).hexdigest() not in known_a
    known_b = client.post("/api/v1/primary/known", headers=org["acc_b"], json={"hashes": hashes}).json()["known"]
    assert known_b == [hashlib.sha256(org["files"]["группа/ON_NSCHFDOPPR_ab.xml"]).hexdigest()]
    # в списке — направление; в 1С продажа как поступление не выгружается
    items = client.get("/api/v1/primary?limit=50", headers=org["acc_a"]).json()["items"]
    assert sorted(i["direction"] for i in items) == ["in", "in", "in", "out", "out"]
    check = client.post("/api/v1/primary/export/check", headers=org["acc_a"],
                        json={"format": "xml", "include_errors": True}).json()
    assert check["count"] == 3                                            # две продажи не выгружаются


def test_statement_incomes_and_closing_report(client, filled):
    org = filled
    assert _folder(client, org["acc_a"], org["files"]).status_code == 200
    res = client.post("/api/v1/bank/statements", headers=org["acc_a"],
                      files={"file": ("kl_to_1c.txt", _statement_a(org), "text/plain")})
    assert res.status_code == 200, res.text
    assert res.json()["incomes"] == 1                                    # поступление без ИНН не хранится
    report = client.get("/api/v1/bank/closing?date_from=2026-10-01&date_to=2026-10-31", headers=org["acc_a"]).json()
    rows = {r["number"]: r for r in report["expense"]["rows"]}
    assert rows["1"]["status"] == "closed" and {ln["how"] for ln in rows["1"]["links"]} == {"set"}
    assert rows["2"]["status"] == "partial" and rows["2"]["rest"] == 5850 and rows["2"]["links"][0]["how"] == "number"
    assert rows["3"]["status"] == "open" and rows["3"]["links"] == []
    s = report["expense"]["summary"]
    assert (s["amount"], s["closed_amount"], s["open_amount"]) == (119600.0, 106750.0, 12850.0)
    assert (s["closed_count"], s["partial_count"], s["open_count"]) == (1, 1, 1) and s["percent"] == 89.3
    inc = report["income"]
    assert [(r["number"], r["status"], r["links"][0]["how"]) for r in inc["rows"]] == [("4", "closed", "sum")]
    assert [d["title"] for d in inc["unpaid"]] == ["УПД № Г-1 от 11.10.2026"]             # продажа в B без оплаты
    cps = {c["inn"]: c for c in report["expense"]["counterparties"]}
    assert cps[org["inn"]["S2"]]["balance"] == 5850 and cps[org["inn"]["S1"]]["balance"] == 0
    # статусы списаний сведены: закрытое — «документ найден», частичное — запрос «без документов» остаётся
    with org["store"].session() as db:
        ops = {o.number: o for o in db.query(BankOperation).filter(BankOperation.company_id == org["A"])}
        assert ops["1"].status == "matched" and ops["1"].match_note.startswith("закрыт документами: ")
        assert ops["2"].status == "open" and ops["2"].match_note.startswith("частично: УПД № 45")
        reqs = {r.ref_id: r for r in db.query(DocRequest).filter(DocRequest.company_id == org["A"],
                                                                  DocRequest.kind == "payment")}
        assert ops["1"].id not in reqs or reqs[ops["1"].id].status != "open"
        assert reqs[ops["2"].id].status == "open" and reqs[ops["3"].id].status == "open"
    csv = client.get("/api/v1/bank/closing.csv?side=expense", headers=org["acc_a"])
    assert csv.status_code == 200 and csv.content.startswith("\ufeff".encode())
    assert "закрыт частично" in csv.content.decode("utf-8-sig")


def test_reupload_adds_incomes_link_unlink_and_isolation(client, filled):
    org = filled
    raw = _statement_a(org)
    assert _folder(client, org["acc_a"], org["files"]).status_code == 200
    assert client.post("/api/v1/bank/statements", headers=org["acc_a"],
                       files={"file": ("kl.txt", raw, "text/plain")}).status_code == 200
    with org["store"].session() as db:                                   # как у выписки, загруженной до 1.70.113
        db.query(BankIncome).filter(BankIncome.company_id == org["A"]).delete()
        db.commit()
    again = client.post("/api/v1/bank/statements", headers=org["acc_a"], files={"file": ("kl.txt", raw, "text/plain")})
    assert again.status_code == 200 and again.json()["incomes"] == 1 and again.json()["incomes_only"] is True
    assert client.post("/api/v1/bank/statements", headers=org["acc_a"],
                       files={"file": ("kl.txt", raw, "text/plain")}).status_code == 409
    report = client.get("/api/v1/bank/closing", headers=org["acc_a"]).json()
    op3 = next(r for r in report["expense"]["rows"] if r["number"] == "3")
    # ручная привязка: к платежу S3 — документ другого контрагента (S2, № 45), чужая компания — 404
    cand = client.get(f"/api/v1/bank/closing/candidates?operation_id={op3['id']}", headers=org["acc_a"]).json()
    assert cand["operation"]["amount"] == 7000.0 and cand["items"] == []          # у S3 документов нет
    with org["store"].session() as db:
        doc45 = db.query(PrimaryDocument).filter(PrimaryDocument.company_id == org["A"],
                                                 PrimaryDocument.doc_number == "45").one().id
    body = {"operation_id": op3["id"], "document_ids": [doc45]}
    assert client.post("/api/v1/bank/closing/link", headers=org["acc_b"], json=body).status_code == 404
    linked = client.post("/api/v1/bank/closing/link", headers=org["acc_a"], json=body)
    assert linked.status_code == 200 and linked.json()["linked"] == 1
    rows = {r["number"]: r for r in client.get("/api/v1/bank/closing", headers=org["acc_a"]).json()["expense"]["rows"]}
    assert rows["3"]["status"] == "closed" and rows["3"]["manual"] is True and rows["3"]["links"][0]["how"] == "manual"
    # № 45 (9 150) сначала отдаёт 7 000 ручной привязке, остаток 2 150 — платежу по номеру
    assert rows["2"]["status"] == "partial" and rows["2"]["closed"] == 2150.0 and rows["2"]["links"][0]["how"] == "number"
    assert client.post("/api/v1/bank/closing/unlink", headers=org["acc_a"],
                       json={"operation_id": op3["id"]}).json()["unlinked"] == 1
    rows = {r["number"]: r for r in client.get("/api/v1/bank/closing", headers=org["acc_a"]).json()["expense"]["rows"]}
    assert rows["3"]["status"] == "open" and rows["2"]["closed"] == 9150.0
    # чужой бухгалтер отчёт компании A не видит; администратор — по выбранной компании
    assert client.get(f"/api/v1/bank/closing?company_id={org['A']}", headers=org["acc_b"]).status_code == 404
    assert client.get(f"/api/v1/bank/closing?company_id={org['A']}", headers=org["admin"]).status_code == 200
    settle = client.post("/api/v1/bank/closing/settle", headers=org["acc_b"],
                         json={"company_ids": [org["A"], org["B"]]}).json()
    assert [c["company_id"] for c in settle["companies"]] == [org["B"]]           # A — не её компания


def test_delete_statement_removes_incomes_and_links(client, filled):
    org = filled
    assert _folder(client, org["acc_a"], org["files"]).status_code == 200
    res = client.post("/api/v1/bank/statements", headers=org["acc_a"],
                      files={"file": ("kl.txt", _statement_a(org), "text/plain")}).json()
    report = client.get("/api/v1/bank/closing", headers=org["acc_a"]).json()
    income = report["income"]["rows"][0]
    with org["store"].session() as db:
        sale = db.query(PrimaryDocument).filter(PrimaryDocument.company_id == org["A"],
                                                PrimaryDocument.doc_number == "П-7").one().id
    assert client.post("/api/v1/bank/closing/link", headers=org["acc_a"],
                       json={"side": "income", "operation_id": income["id"], "document_ids": [sale]}).status_code == 200
    assert client.delete(f"/api/v1/bank/statements/{res['statement']['id']}", headers=org["acc_a"]).status_code == 200
    with org["store"].session() as db:
        assert db.query(BankIncome).filter(BankIncome.company_id == org["A"]).count() == 0
        assert db.query(BankLink).filter(BankLink.company_id == org["A"]).count() == 0
        assert db.query(BankStatement).filter(BankStatement.company_id == org["A"]).count() == 0
