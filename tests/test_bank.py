"""Банковская выписка (T-45, v1.70.98): разбор формата 1С, виды операций, сопоставление с чеками и УПД,
платежи без документов в «Что нужно от клиента», чеки к возмещению, срок хранения и права доступа.

Только синтетические выписки и данные (ADR-14): счета, ИНН и суммы придуманы в тесте.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

from app.bank import parser
from app.bank import service as bank
from app.bank.models import BankOperation, BankSettings, BankStatement
from app.database import SessionLocal
from app.docreq.models import DocRequest
from app.models import Receipt, User
from app.primary.models import PrimaryDocument
from tests import test_primary_upd as _upd
from tests.primary_samples import inn10
from tests.test_v1140 import _mk_user

firms = _upd.firms
PERIOD = "2026-10"
TREASURY = "03100643000000018500"


def acc() -> str:
    return "40702810" + "".join(random.choice("0123456789") for _ in range(12))


def doc(*, number: str, day: str, amount: str, payer: tuple[str, str, str], payee: tuple[str, str, str],
        purpose: str | list[str], out: bool = True, extra: dict | None = None) -> list[str]:
    """Секция документа: payer/payee — (счёт, ИНН, наименование)."""
    lines = ["СекцияДокумент=Платежное поручение", f"Номер={number}", f"Дата={day}", f"Сумма={amount}",
             f"ПлательщикСчет={payer[0]}", f"Плательщик=ИНН {payer[1]} {payer[2]}", f"ПлательщикИНН={payer[1]}",
             f"Плательщик1={payer[2]}", f"ПлательщикРасчСчет={payer[0]}", f"ПолучательСчет={payee[0]}",
             f"Получатель=ИНН {payee[1]} {payee[2]}", f"ПолучательИНН={payee[1]}", f"Получатель1={payee[2]}",
             f"ПолучательРасчСчет={payee[0]}", f"{'ДатаСписано' if out else 'ДатаПоступило'}={day}"]
    lines += [f"{k}={v}" for k, v in (extra or {}).items()]
    if isinstance(purpose, list):
        lines += ["НазначениеПлатежа=" + purpose[0]] + [f"НазначениеПлатежа{i}={p}" for i, p in enumerate(purpose[1:], 1)]
    else:
        lines.append(f"НазначениеПлатежа={purpose}")
    return lines + ["КонецДокумента"]


def statement(account: str, docs: list[list[str]], *, enc: str = "cp1251", start: str = "01.10.2026",
              end: str = "31.10.2026", created: str = "01.11.2026") -> bytes:
    label = {"cp1251": "Windows", "cp866": "DOS", "utf-8-sig": "Windows"}[enc]
    lines = ["1CClientBankExchange", "ВерсияФормата=1.03", f"Кодировка={label}", "Отправитель=Синтетический банк",
             f"ДатаСоздания={created}", f"ДатаНачала={start}", f"ДатаКонца={end}", f"РасчСчет={account}",
             "СекцияРасчСчет", f"ДатаНачала={start}", f"ДатаКонца={end}", f"РасчСчет={account}",
             "НачальныйОстаток=100000.00", "КонецРасчСчет"]
    for d in docs:
        lines += d
    lines.append("КонецФайла")
    return "\r\n".join(lines).encode(enc)


def _upload(client, hdr, raw: bytes, company_id: str | None = None):
    data = {"company_id": company_id} if company_id else None
    return client.post("/api/v1/bank/statements", headers=hdr, files={"file": ("kl_to_1c.txt", raw, "text/plain")},
                       data=data)


def _receipt(db, company_id: str, when: datetime, total: float, user_id: str | None = None, **kw) -> Receipt:
    rec = Receipt(qr_data="t=synthetic", fn=str(random.randrange(10**15, 10**16)), fd=str(random.randrange(10**6)),
                  fp=str(random.randrange(10**9)), company_id=company_id, receipt_date=when, total_sum=total,
                  status="verified", fns_status="valid", created_by=user_id, **kw)
    db.add(rec)
    db.commit()
    return rec


# ------------------------------------------------------------------ разбор формата
def test_parse_encodings_sections_and_multiline_purpose():
    me, supplier = acc(), acc()
    # в кодировке DOS (cp866) нет кавычек-ёлочек — в таких выписках прямые кавычки
    docs = [doc(number="15", day="05.10.2026", amount="1500.50", payer=(me, "7701234567", 'ООО "Наша"'),
                payee=(supplier, "7702345678", 'ООО "Песок"'),
                purpose=["Оплата по счёту № 45 от 01.10.2026", "за песок, в т.ч. НДС 22% 270.58"])]
    parsed = [parser.parse(statement(me, docs, enc=enc)) for enc in ("cp1251", "cp866", "utf-8-sig")]
    for data in parsed:
        assert data["accounts"] == [me] and data["date_from"] == date(2026, 10, 1)
        assert data["date_to"] == date(2026, 10, 31) and len(data["documents"]) == 1
        op = parser.classify(data["documents"][0], data["accounts"], "7701234567")
        assert op["kind"] == "transfer" and op["direction"] == "out" and op["amount"] == 1500.5
        assert op["purpose"] == "Оплата по счёту № 45 от 01.10.2026 за песок, в т.ч. НДС 22% 270.58"
        assert op["counterparty_name"] == 'ООО "Песок"' and op["counterparty_inn"] == "7702345678"
        assert op["op_date"] == date(2026, 10, 5) and op["account_tail"] == me[-4:]
    assert parsed[0] == parsed[1] == parsed[2]


def test_parse_refuses_what_is_not_a_statement():
    for raw, text in ((b"", "пустой"), ("Привет, это не выписка".encode("cp1251"), "1CClientBankExchange"),
                      (b"1CClientBankExchange\r\nKonecFaila", "нет ни одной операции"),
                      (b"1" * (parser.MAX_BYTES + 1), "5 МБ")):
        with pytest.raises(parser.StatementError, match=text):
            parser.parse(raw)


@pytest.mark.parametrize(("purpose", "payee", "extra", "kind"), [
    ("Оплата по счёту № 7, в т.ч. НДС 22% - 1803.28", None, {}, "transfer"),        # «НДС» — не налог
    ("Оплата за щебень. Без налога (НДС)", None, {}, "transfer"),
    ("Единый налоговый платёж", TREASURY, {}, "tax"),
    ("Оплата по требованию", None, {"СтатусСоставителя": "01"}, "tax"),
    ("Страховые взносы за октябрь", None, {}, "tax"),
    ("Перечисление заработной платы за октябрь 2026 по реестру № 5", None, {}, "salary"),
    ("Списание по операции с использованием карты 220220******1234 LEROY MERLIN", None, {}, "card"),
    ("Оплата товаров по карте *5678 OBI MOSCOW", None, {}, "card"),
    ("Комиссия за обслуживание карты *1234", "70601810000000000001", {}, "fee"),
    ("Выдача наличных по карте *1234", None, {}, "advance"),
    ("Перечисление под отчёт Иванову И. И.", None, {}, "advance"),
    ("Погашение кредита по договору 12", None, {}, "loan"),
])
def test_classify_kinds(purpose, payee, extra, kind):
    me = acc()
    raw = doc(number="1", day="10.10.2026", amount="100.00", payer=(me, "7701234567", "ООО «Наша»"),
              payee=(payee or acc(), "" if payee else "7702345678", "Получатель"), purpose=purpose, extra=extra)
    data = parser.parse(statement(me, [raw]))
    op = parser.classify(data["documents"][0], data["accounts"], "7701234567")
    assert op["kind"] == kind, (purpose, op)
    if kind == "card":
        assert op["card_tail"] in ("1234", "5678")


def test_classify_own_accounts_and_income():
    me, other = acc(), acc()
    own = doc(number="2", day="10.10.2026", amount="5000.00", payer=(me, "7701234567", "ООО «Наша»"),
              payee=(other, "7701234567", "ООО «Наша»"), purpose="Перевод собственных средств")
    income = doc(number="3", day="11.10.2026", amount="9000.00", payer=(other, "7702345678", "ООО «Клиент»"),
                 payee=(me, "7701234567", "ООО «Наша»"), purpose="Оплата по договору", out=False)
    data = parser.parse(statement(me, [own, income]))
    kinds = [parser.classify(d, data["accounts"], "7701234567")["kind"] for d in data["documents"]]
    assert kinds == ["own", "income"]


# ------------------------------------------------------------------ загрузка, сопоставление, запросы
@pytest.fixture
def bank_firm(client, firms):
    """Компания A: сотрудник с картой *1234, его чек на 4 500,00 ₽ и УПД поставщика на 7 930,00 ₽."""
    admin, a, b = firms
    tag = uuid4().hex[:8]
    emp_hdr = _mk_user(client, admin, a["id"], f"bank_emp_{tag}", role="user")
    supplier, other = inn10(), inn10()
    with SessionLocal() as db:
        emp = db.query(User).filter_by(username=f"bank_emp_{tag}").one()
        emp.full_name = "Сотрудник Картов"
        db.commit()
        rec = _receipt(db, a["id"], datetime(2026, 10, 4, 15, 30), 4500.0, emp.id, merchant_name="ООО «Стройдвор»",
                       ecash_sum=4500.0)
        upd = PrimaryDocument(company_id=a["id"], kind="УПД", function="СЧФДОП", doc_number="С-77",
                              doc_date=date(2026, 10, 2), seller_name="ООО «Цемент»", seller_inn=supplier,
                              total_with_vat=7930.0, status="ok")
        db.add(upd)
        db.commit()
        ids = {"emp": emp.id, "receipt": rec.id, "upd": upd.id}
    res = client.put("/api/v1/bank/settings", headers=a["hdr"],
                     json={"retention_days": 1095, "cards": [{"tail": "1234", "user_id": ids["emp"]}]})
    assert res.status_code == 200, res.text
    me = acc()
    a_inn = a["inn"]
    ours = (me, a_inn, "ООО «Наша»")
    docs = [
        doc(number="11", day="05.10.2026", amount="4500.00", payer=ours, payee=(acc(), "", "ПАО Банк"),
            purpose="Списание по операции с использованием карты 220220******1234 STROYDVOR"),
        doc(number="12", day="06.10.2026", amount="1200.00", payer=ours, payee=(acc(), "", "ПАО Банк"),
            purpose="Оплата товаров по карте *1234 KRASKI"),
        doc(number="13", day="10.10.2026", amount="7930.00", payer=ours, payee=(acc(), supplier, "ООО «Цемент»"),
            purpose="Оплата по УПД № С-77 от 02.10.2026, в т.ч. НДС 22% 1430.00"),
        doc(number="14", day="12.10.2026", amount="15000.00", payer=ours, payee=(acc(), other, "ООО «Песок»"),
            purpose="Оплата по счёту № 45 от 11.10.2026 за песок, в т.ч. НДС 22% 2704.92"),
        doc(number="15", day="15.10.2026", amount="30000.00", payer=ours, payee=(TREASURY, "7727406020", "Казначейство России (ФНС России)"),
            purpose="Единый налоговый платёж"),
        doc(number="16", day="20.10.2026", amount="90000.00", payer=ours, payee=(acc(), "7707083893", "ПАО Банк"),
            purpose="Перечисление заработной платы за октябрь 2026"),
        doc(number="17", day="21.10.2026", amount="50000.00", payer=(acc(), inn10(), "ООО «Клиент»"), payee=ours,
            purpose="Оплата по договору поставки", out=False),
    ]
    return {"admin": admin, "a": a, "b": b, "emp_hdr": emp_hdr, "supplier": supplier, "other": other,
            "account": me, "docs": docs, "raw": statement(me, docs), **ids}


def _ops(company_id: str) -> dict[str, BankOperation]:
    with SessionLocal() as db:
        rows = db.query(BankOperation).filter_by(company_id=company_id).all()
        db.expunge_all()
    return {r.number: r for r in rows}


def _payment_requests(company_id: str) -> dict[str, DocRequest]:
    with SessionLocal() as db:
        rows = db.query(DocRequest).filter_by(company_id=company_id, kind="payment").all()
        db.expunge_all()
    return {r.ref_id: r for r in rows}


def test_upload_matches_documents_and_feeds_doc_requests(client, bank_firm):
    a = bank_firm["a"]
    res = _upload(client, a["hdr"], bank_firm["raw"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["operations"], body["saved"], body["matched"], body["open"], body["skipped"]) == (7, 4, 2, 2, 3)
    assert body["kinds"] == {"card": 2, "transfer": 2, "tax": 1, "salary": 1, "income": 1}
    assert body["requests"] == 2 and body["statement"]["account_tail"] == bank_firm["account"][-4:]
    ops = _ops(a["id"])
    assert set(ops) == {"11", "12", "13", "14"}                    # налоги, зарплата и поступления не храним
    assert ops["11"].status == "matched" and ops["11"].match_id == bank_firm["receipt"]
    assert ops["11"].match_note == "чек на 4 500,00 ₽ от 04.10.2026 — ООО «Стройдвор»"
    assert ops["13"].status == "matched" and ops["13"].match_type == "primary" and ops["13"].match_id == bank_firm["upd"]
    assert ops["12"].status == "open" and ops["12"].card_tail == "1234" and ops["14"].status == "open"
    reqs = _payment_requests(a["id"])
    card, transfer = reqs[ops["12"].id], reqs[ops["14"].id]
    assert card.title.startswith("Чек на покупку по карте *1234 на 1 200,00 ₽ от 06.10.2026")
    assert card.assignee_id == bank_firm["emp"] and card.period == PERIOD and card.amount == 1200.0
    assert transfer.title.startswith("Закрывающие документы (УПД, накладная или акт) на оплату 15 000,00 ₽ от "
                                     "12.10.2026 — ООО «Песок» (платёж № 14)")
    assert transfer.counterparty_inn == bank_firm["other"] and transfer.assignee_id is None
    listed = client.get("/api/v1/bank", headers=a["hdr"]).json()
    assert [o["number"] for o in listed["operations"]] == ["14", "12"]
    assert listed["totals"]["open"] == {"count": 2, "amount": 16200.0}
    assert listed["operations"][1]["card_owner"] == "Сотрудник Картов"
    overview = client.get(f"/api/v1/doc-requests?period={PERIOD}", headers=a["hdr"]).json()
    kinds = {i["kind_title"] for i in overview["items"]}
    assert "платёж без документов" in kinds
    # тот же файл ещё раз — отказ; пересекающаяся выписка — без задвоения операций
    again = _upload(client, a["hdr"], bank_firm["raw"])
    assert again.status_code == 409 and "уже загружена" in again.json()["detail"]
    overlap = _upload(client, a["hdr"], statement(bank_firm["account"], bank_firm["docs"][:4], created="02.11.2026",
                                                  start="05.10.2026", end="15.10.2026"))
    assert overlap.status_code == 200 and overlap.json()["duplicates"] == 4 and overlap.json()["saved"] == 0
    assert len(_ops(a["id"])) == 4


def test_arrived_receipt_closes_request_and_ignore_cancels_it(client, bank_firm):
    a = bank_firm["a"]
    assert _upload(client, a["hdr"], bank_firm["raw"]).status_code == 200
    ops = _ops(a["id"])
    with SessionLocal() as db:
        _receipt(db, a["id"], datetime(2026, 10, 6, 11, 0), 1200.0, bank_firm["emp"], merchant_name="ИП Краски")
    client.get(f"/api/v1/doc-requests?period={PERIOD}", headers=a["hdr"])     # обзор закрывает пришедшее
    reqs = _payment_requests(a["id"])
    card = reqs[ops["12"].id]
    assert card.status == "received" and card.close_note == "Закрыт сам: найден чек на 1 200,00 ₽ от 06.10.2026 — ИП Краски"
    assert _ops(a["id"])["12"].status == "matched"
    res = client.post(f"/api/v1/bank/operations/{ops['14'].id}", headers=a["hdr"],
                      json={"action": "ignore", "note": "предоплата по договору, документы в ноябре"})
    assert res.status_code == 200 and res.json()["status"] == "ignored"
    transfer = _payment_requests(a["id"])[ops["14"].id]
    assert transfer.status == "cancelled" and "предоплата по договору" in transfer.close_note
    back = client.post(f"/api/v1/bank/operations/{ops['14'].id}", headers=a["hdr"], json={"action": "open"})
    assert back.json()["status"] == "open"


def test_manual_link_and_rematch(client, bank_firm):
    a, b = bank_firm["a"], bank_firm["b"]
    assert _upload(client, a["hdr"], bank_firm["raw"]).status_code == 200
    ops = _ops(a["id"])
    with SessionLocal() as db:
        late = PrimaryDocument(company_id=a["id"], kind="УПД", function="СЧФДОП", doc_number="45",
                               doc_date=date(2026, 10, 20), seller_name="ООО «Песок»", seller_inn=bank_firm["other"],
                               total_with_vat=9000.0, status="ok")
        foreign = _receipt(db, b["id"], datetime(2026, 10, 6, 10, 0), 1200.0)
        db.add(late)
        db.commit()
        late_id, foreign_id = late.id, foreign.id
    # документ с номером из назначения платежа находится при повторном сопоставлении. v1.70.113 (T-79): УПД № 45 на
    # 9 000 ₽ при оплате 15 000 ₽ закрывает платёж частично — на 6 000 ₽ документов нет, запрос остаётся открытым
    res = client.post("/api/v1/bank/match", headers=a["hdr"])
    assert res.status_code == 200 and res.json()["matched"] == 0
    op14 = _ops(a["id"])["14"]
    assert op14.status == "open" and op14.match_note == ("частично: УПД № 45 от 20.10.2026 — 9 000,00 ₽; "
                                                         "осталось 6 000,00 ₽")
    assert _payment_requests(a["id"])[op14.id].status == "open"
    bad = client.post(f"/api/v1/bank/operations/{ops['12'].id}", headers=a["hdr"],
                      json={"action": "link", "ref_type": "receipt", "ref_id": foreign_id})
    assert bad.status_code == 404                                    # чек чужой компании
    used = client.post(f"/api/v1/bank/operations/{ops['12'].id}", headers=a["hdr"],
                       json={"action": "link", "ref_type": "receipt", "ref_id": bank_firm["receipt"]})
    assert used.status_code == 409 and "уже сопоставлен" in used.json()["detail"]


def test_foreign_statement_and_access_control(client, bank_firm):
    a, b, admin = bank_firm["a"], bank_firm["b"], bank_firm["admin"]
    foreign = statement(acc(), [doc(number="1", day="05.10.2026", amount="10.00", payer=(acc(), b["inn"], "Чужая"),
                                    payee=(acc(), inn10(), "Кто-то"), purpose="Оплата")])
    res = _upload(client, a["hdr"], foreign)
    assert res.status_code == 409 and "другой организации" in res.json()["detail"]
    assert _upload(client, a["hdr"], b"not a statement").status_code == 422
    assert _upload(client, a["hdr"], bank_firm["raw"]).status_code == 200
    op = next(iter(_ops(a["id"]).values()))
    with SessionLocal() as db:
        st = db.query(BankStatement).filter_by(company_id=a["id"]).first()
        st_id = st.id
    # бухгалтер другой компании не видит и не меняет чужое
    assert client.get(f"/api/v1/bank?company_id={a['id']}", headers=b["hdr"]).status_code == 404
    assert client.post(f"/api/v1/bank/operations/{op.id}", headers=b["hdr"], json={"action": "ignore"}).status_code == 404
    assert client.delete(f"/api/v1/bank/statements/{st_id}", headers=b["hdr"]).status_code == 404
    assert client.get("/api/v1/bank", headers=b["hdr"]).json()["operations"] == []
    # сотрудник — нет доступа; администратор — только с выбранной компанией
    assert client.get("/api/v1/bank", headers=bank_firm["emp_hdr"]).status_code == 403
    assert client.get("/api/v1/bank", headers=admin).status_code == 400
    assert len(client.get(f"/api/v1/bank?company_id={a['id']}&status=all", headers=admin).json()["operations"]) == 4


def test_reimbursements_and_csv(client, bank_firm):
    a = bank_firm["a"]
    with SessionLocal() as db:
        own = _receipt(db, a["id"], datetime(2026, 10, 8, 9, 0), 2000.0, bank_firm["emp"], merchant_name="ООО «Гвозди»",
                       cash_sum=2000.0, personal_sum=300.0)
        tail = _receipt(db, a["id"], datetime(2026, 10, 30, 9, 0), 700.0, bank_firm["emp"])   # ждёт следующую выписку
        own_id, tail_id = own.id, tail.id
    empty = client.get("/api/v1/bank/reimburse", headers=a["hdr"]).json()
    assert empty["covered"] is False and empty["employees"] == []
    assert _upload(client, a["hdr"], bank_firm["raw"]).status_code == 200
    data = client.get("/api/v1/bank/reimburse", headers=a["hdr"]).json()
    group = next(g for g in data["employees"] if g["user_id"] == bank_firm["emp"])
    items = {i["receipt_id"]: i for i in group["items"]}
    assert own_id in items and bank_firm["receipt"] not in items and tail_id not in items
    assert items[own_id]["amount"] == 1700.0 and items[own_id]["payment"] == "наличными"
    assert group["name"] == "Сотрудник Картов" and group["pending_total"] == 1700.0
    no_reason = client.post(f"/api/v1/bank/reimburse/{own_id}", headers=a["hdr"], json={"status": "declined"})
    assert no_reason.status_code == 422 and "причину" in no_reason.json()["detail"]
    paid = client.post(f"/api/v1/bank/reimburse/{own_id}", headers=a["hdr"], json={"status": "paid"})
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    data = client.get("/api/v1/bank/reimburse", headers=a["hdr"]).json()
    group = next(g for g in data["employees"] if g["user_id"] == bank_firm["emp"])
    assert group["paid_total"] == 1700.0 and group["pending_total"] == 0
    csv_text = client.get("/api/v1/bank/reimburse.csv", headers=a["hdr"]).content.decode("utf-8")
    assert csv_text.startswith("\ufeffСотрудник;Дата чека;Продавец;Оплата;Сумма чека;К возмещению;Решение;Примечание")
    assert "Сотрудник Картов;2026-10-08;ООО «Гвозди»;наличными;2000,00;1700,00;возмещено;" in csv_text
    back = client.post(f"/api/v1/bank/reimburse/{own_id}", headers=a["hdr"], json={"status": "pending"})
    assert back.json()["status"] == "pending"
    other = client.post(f"/api/v1/bank/reimburse/{own_id}", headers=bank_firm["b"]["hdr"], json={"status": "paid"})
    assert other.status_code == 404


def test_settings_validation(client, bank_firm):
    a, b = bank_firm["a"], bank_firm["b"]
    put = lambda body: client.put("/api/v1/bank/settings", headers=a["hdr"], json=body)  # noqa: E731
    assert put({"retention_days": 10}).status_code == 422
    assert put({"retention_days": 400, "cards": [{"tail": "12a"}]}).status_code == 422
    assert put({"retention_days": 400, "cards": [{"tail": "1111"}, {"tail": "1111"}]}).status_code == 422
    with SessionLocal() as db:
        stranger = db.query(User).filter(User.company_id == b["id"]).first().id
    res = put({"retention_days": 400, "cards": [{"tail": "9999", "user_id": stranger}]})
    assert res.status_code == 422 and "не сотрудник" in res.json()["detail"]
    ok = put({"retention_days": 400, "cards": [{"tail": "1234", "user_id": bank_firm["emp"]}, {"tail": "5555"}]})
    assert ok.status_code == 200
    assert ok.json() == {"retention_days": 400, "cards": [
        {"tail": "1234", "user_id": bank_firm["emp"], "user": "Сотрудник Картов"}, {"tail": "5555", "user_id": "", "user": ""}]}


def test_retention_and_statement_removal_cancel_requests(client, bank_firm):
    a = bank_firm["a"]
    assert _upload(client, a["hdr"], bank_firm["raw"]).status_code == 200
    ops = _ops(a["id"])
    with SessionLocal() as db:                    # срок — прямо в базе: проверка не зависит от сегодняшней даты
        db.merge(BankSettings(company_id=a["id"], retention_days=30, cards_json="[]"))
        db.commit()
        assert bank.purge(db, a["id"], today=date(2026, 11, 8)) == 2     # старше 9 октября — операции 11 и 12
    left = _ops(a["id"])
    assert set(left) == {"13", "14"}
    assert _payment_requests(a["id"])[ops["12"].id].status == "cancelled"
    with SessionLocal() as db:
        st_id = db.query(BankStatement).filter_by(company_id=a["id"]).one().id
    res = client.delete(f"/api/v1/bank/statements/{st_id}", headers=a["hdr"])
    assert res.status_code == 200 and res.json()["deleted"] == 2
    transfer = _payment_requests(a["id"])[ops["14"].id]
    assert transfer.status == "cancelled" and transfer.close_note == "Выписка удалена бухгалтером"
    assert _ops(a["id"]) == {}


def test_shorter_retention_purges_at_once(client, bank_firm):
    """Бухгалтер сократил срок хранения — лишние операции удаляются сразу, не дожидаясь новой выписки."""
    a = bank_firm["a"]
    today = date.today()
    old, fresh = today - timedelta(days=60), today - timedelta(days=5)
    ours = (bank_firm["account"], a["inn"], "ООО «Наша»")
    raw = statement(bank_firm["account"], [
        doc(number="31", day=old.strftime("%d.%m.%Y"), amount="500.00", payer=ours, payee=(acc(), inn10(), "ООО «Старое»"),
            purpose="Оплата по счёту № 1"),
        doc(number="32", day=fresh.strftime("%d.%m.%Y"), amount="700.00", payer=ours, payee=(acc(), inn10(), "ООО «Новое»"),
            purpose="Оплата по счёту № 2")],
        start=old.strftime("%d.%m.%Y"), end=today.strftime("%d.%m.%Y"))
    assert _upload(client, a["hdr"], raw).status_code == 200
    assert set(_ops(a["id"])) == {"31", "32"}
    res = client.put("/api/v1/bank/settings", headers=a["hdr"], json={"retention_days": 30})
    assert res.status_code == 200 and res.json()["retention_days"] == 30
    assert set(_ops(a["id"])) == {"32"}
