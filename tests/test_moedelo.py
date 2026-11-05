"""«Моё дело» по API (T-71, v1.70.107, ADR-37): ключ в карточке компании и акты покупок из первички.

В сеть тесты не ходят: «Моё дело» подменено httpx.MockTransport (client.transport), компании, документы и
ключ синтетические (ADR-14). Проверяется: ключ хранится только зашифрованным и не возвращается; проверка —
запрос на чтение, 401 и 403 — понятные ошибки и статус «ошибка»; бухгалтер видит только свою компанию;
разбор документа (услуги, товары, смешанный) и причины, по которым документ не уходит; акт уходит с явными
суммами строк, поставщик находится или добавляется по ИНН; повтор не создаёт второй акт ни у нас, ни в
«Моём деле»; расхождение суммы отмечается; пакет за период отправляет только услуги и останавливается на
ошибке ключа; в журнал действий ключ не попадает.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.database import SessionLocal
from app.models import AuditLog
from app.moedelo import client as md_client
from app.moedelo import service as md
from app.moedelo.models import MoedeloConnection, MoedeloLink
from tests.test_primary_upd import firms, inn10  # noqa: F401 — фикстура компаний и бухгалтеров

KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"       # синтетический ключ вида GUID
DAY = "2026-10-15"


class FakeMoedelo:
    """«Моё дело» в памяти: контрагенты, акты покупок и журнал запросов."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.kontragents: list[dict] = []
        self.acts: list[dict] = []
        self.status: dict[str, int] = {}
        self.sum_shift = 0.0
        self.inn_endpoint = True
        self.next_id = 7000

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method, params = request.url.path, request.method, dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, params, body, request.headers.get("md-api-key")))
        forced = self.status.get(path) or self.status.get("*")
        if forced:
            return httpx.Response(forced, json={"Message": "синтетическая ошибка"})
        if path == md.KONTRAGENTS_PATH and method == "GET":
            rows = [k for k in self.kontragents if not params.get("inn") or k["Inn"] == params["inn"]]
            return httpx.Response(200, json={"ResourceList": rows, "TotalCount": len(rows)})
        if path == md.KONTRAGENT_BY_INN_PATH and method == "POST":
            if not self.inn_endpoint:
                return httpx.Response(404)
            row = {"Id": self._id(), "Inn": body["Inn"], "Kpp": "", "Name": "по ИНН", "IsArchived": False}
            self.kontragents.append(row)
            return httpx.Response(201, json=row)
        if path == md.KONTRAGENTS_PATH and method == "POST":
            row = {"Id": self._id(), "Inn": body["Inn"], "Kpp": body.get("Kpp", ""), "Name": body["Name"]}
            self.kontragents.append(row)
            return httpx.Response(201, json=row)
        if path == md.ACT_PATH and method == "GET":
            rows = [a for a in self.acts if a["Number"] == params.get("number")
                    and str(a["KontragentId"]) == params.get("kontragentId")]
            return httpx.Response(200, json={"ResourceList": rows})
        if path == md.ACT_PATH and method == "POST":
            total = round(sum(i["SumWithNds"] for i in body["Items"]) + self.sum_shift, 2)
            act = {"Id": self._id(), "Number": body["Number"], "DocDate": body["DocDate"] + "T00:00:00+03:00",
                   "KontragentId": body["KontragentId"], "Sum": total, "Items": body["Items"]}
            self.acts.append(act)
            return httpx.Response(201, json=act)
        return httpx.Response(404)

    def posted_acts(self) -> list[dict]:
        return [c[3] for c in self.calls if c[0] == "POST" and c[1] == md.ACT_PATH]


@pytest.fixture
def fake(monkeypatch):
    state = FakeMoedelo()
    monkeypatch.setattr(md_client, "transport", httpx.MockTransport(state.handler))
    return state


def line(name: str, qty: float | None, price: float | None, rate: str = "22", unit: str = "усл. ед.",
         code: str = "876", base: float | None = None) -> dict:
    base = round((qty or 0) * (price or 0), 2) if base is None else base
    vat = 0.0 if rate == "none" else round(base * int(rate) / 100, 2)
    return {"name": name, "unit_code": code, "unit_name": unit, "quantity": qty, "price": price,
            "sum_without_vat": base, "vat_rate": rate, "vat_sum": vat, "sum_with_vat": round(base + vat, 2)}


SERVICES = [line("Доставка бетона на объект", 2, 4500), line("Аренда опалубки, сутки", 3, 1200, unit="сут", code="359")]
GOODS = [line("Цемент М500 Д0, мешок 50 кг", 40, 520, unit="шт", code="796"),
         line("Пена монтажная 750 мл", 12, 410, unit="шт", code="796")]


def make_doc(client, firm, lines: list[dict], *, number: str | None = None, day: str = DAY,
             function: str = "СЧФДОП", seller_inn: str | None = None) -> dict:
    payload = {"source": "manual", "draft": {
        "function": function, "number": number or f"MD-{uuid4().hex[:8]}", "date": day,
        "seller": {"name": "ООО «Стройсервис»", "inn": seller_inn or inn10(), "kpp": "770101001"},
        "buyer": {"name": "ООО «Покупатель»", "inn": firm["inn"], "kpp": ""},
        "totals": {"without_vat": round(sum(x["sum_without_vat"] for x in lines), 2),
                   "vat": round(sum(x["vat_sum"] for x in lines), 2),
                   "with_vat": round(sum(x["sum_with_vat"] for x in lines), 2)},
        "lines": lines}}
    res = client.post("/api/v1/primary/confirm", headers=firm["hdr"], json=payload)
    assert res.status_code == 200, res.text
    doc = res.json()["document"]
    assert doc["status"] != "errors", doc
    return doc


def connect(client, firm) -> dict:
    saved = client.put("/api/v1/moedelo/connection", headers=firm["hdr"], json={"key": KEY})
    assert saved.status_code == 200, saved.text
    checked = client.post("/api/v1/moedelo/connection/check", headers=firm["hdr"], json={})
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "active"
    return checked.json()


# ------------------------------------------------------------------ ключ
def test_key_is_sealed_never_returned_and_checked_by_reading(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    saved = client.put("/api/v1/moedelo/connection", headers=a["hdr"], json={"key": f"  {KEY} "})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["status"] == "new" and body["key_set"] is True and body["key_tail"] == KEY[-4:]
    assert KEY not in saved.text and "restapi.moedelo.org/docs" in body["docs_url"]
    with SessionLocal() as db:
        row = db.query(MoedeloConnection).filter(MoedeloConnection.company_id == a["id"]).one()
        assert row.key_enc.startswith("enc:v1:") and KEY not in row.key_enc
    checked = client.post("/api/v1/moedelo/connection/check", headers=a["hdr"], json={})
    assert checked.status_code == 200 and checked.json()["status"] == "active" and checked.json()["last_ok_at"]
    method, path, params, _, header = fake.calls[-1]
    assert (method, path, params, header) == ("GET", md.KONTRAGENTS_PATH, {"pageSize": "1"}, KEY)
    assert KEY not in client.get("/api/v1/moedelo/connection", headers=a["hdr"]).text


@pytest.mark.parametrize("code,needle", [(401, "не приняло ключ"), (403, "не открывает этот раздел")])
def test_check_errors_are_clear_and_saved(client, firms, fake, code, needle):  # noqa: F811
    _, a, _ = firms
    client.put("/api/v1/moedelo/connection", headers=a["hdr"], json={"key": KEY})
    fake.status["*"] = code
    res = client.post("/api/v1/moedelo/connection/check", headers=a["hdr"], json={})
    assert res.status_code == 422 and needle in res.json()["detail"]
    state = client.get("/api/v1/moedelo/connection", headers=a["hdr"]).json()
    assert state["status"] == "error" and needle in state["last_error"]


def test_network_failure_is_reported(client, firms, monkeypatch):  # noqa: F811
    _, a, _ = firms

    def down(_request):
        raise httpx.ConnectError("нет сети")

    monkeypatch.setattr(md_client, "transport", httpx.MockTransport(down))
    client.put("/api/v1/moedelo/connection", headers=a["hdr"], json={"key": KEY})
    res = client.post("/api/v1/moedelo/connection/check", headers=a["hdr"], json={})
    assert res.status_code == 502 and "не отвечает" in res.json()["detail"]


@pytest.mark.parametrize("bad", ["short-key", "0f8fad5b d9cb 469f a165 70867728950e", "ключ-по-русски-1234567"])
def test_bad_key_format_is_rejected(client, firms, bad):  # noqa: F811
    _, a, _ = firms
    res = client.put("/api/v1/moedelo/connection", headers=a["hdr"], json={"key": bad})
    assert res.status_code == 422 and "Интеграция с партнёрами" in res.json()["detail"]


@pytest.mark.usefixtures("fake")
def test_companies_are_isolated(client, firms):  # noqa: F811
    admin, a, b = firms
    connect(client, a)
    doc = make_doc(client, a, SERVICES)
    assert client.get(f"/api/v1/moedelo/connection?company_id={a['id']}", headers=b["hdr"]).status_code == 404
    assert client.get(f"/api/v1/moedelo/documents/{doc['id']}", headers=b["hdr"]).status_code == 404
    assert client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=b["hdr"], json={}).status_code == 404
    assert client.get("/api/v1/moedelo/connection", headers=admin).status_code == 400
    seen = client.get(f"/api/v1/moedelo/connection?company_id={a['id']}", headers=admin).json()
    assert seen["status"] == "active" and seen["key_tail"] == KEY[-4:]
    assert client.get("/api/v1/moedelo/connection", headers=b["hdr"]).json()["status"] == "none"


# ------------------------------------------------------------------ разбор документа
def _ln(name: str, unit: str = "шт", code: str = "796", rate: str = "22", **kw) -> SimpleNamespace:
    data = {"name": name, "unit_name": unit, "unit_code": code, "vat_rate": rate, "quantity": 1, "price": 100.0,
            "sum_without_vat": 100.0, "vat_sum": 0 if rate == "none" else 22.0,
            "sum_with_vat": 100.0 if rate == "none" else 122.0}
    data.update(kw)
    return SimpleNamespace(**data)


@pytest.mark.parametrize("name,unit,code,expected", [
    ("Доставка цемента", "шт", "796", True), ("Монтаж окон ПВХ", "шт", "796", True),
    ("Аренда опалубки", "шт", "796", True), ("Работы по устройству стяжки", "м2", "055", True),
    ("Пена монтажная 750 мл", "шт", "796", False), ("Ремонтный состав, 25 кг", "шт", "796", False),
    ("Металлопрокат лист 2 мм", "т", "168", False), ("Цемент М500", "т", "168", False),
    ("Экскаватор-погрузчик", "маш.-ч", "", True), ("Абонентская плата", "усл. ед.", "876", True)])
def test_service_hint_is_careful(name, unit, code, expected):
    assert md.is_service_line(_ln(name, unit, code)) is expected


def test_classify_documents():
    assert md.classify([_ln("Доставка"), _ln("Погрузка и разгрузка")]) == "services"
    assert md.classify([_ln("Цемент"), _ln("Доставка")]) == "mixed"
    assert md.classify([_ln("Цемент"), _ln("Песок")]) == "goods"


def _doc(**kw) -> SimpleNamespace:
    data = {"company_id": "c1", "status": "ok", "function": "СЧФДОП", "correction_number": "", "doc_number": "15",
            "doc_date": date(2026, 10, 15), "seller_inn": "7701234567", "seller_kpp": "", "seller_name": "ООО «А»",
            "currency": "643", "lines": [_ln("Доставка")], "total_with_vat": 122.0}
    data.update(kw)
    return SimpleNamespace(**data)


@pytest.mark.parametrize("change,needle", [
    ({"status": "errors"}, "ошибки проверки"), ({"function": "СЧФ"}, "счёт-фактура без передаточной части"),
    ({"correction_number": "1"}, "Исправленный документ"), ({"doc_number": " "}, "нет номера"),
    ({"doc_date": None}, "нет даты"), ({"seller_inn": ""}, "нет ИНН"), ({"currency": "840"}, "не в рублях"),
    ({"lines": [_ln("Доставка", rate="agent")]}, "НДС исчисляет налоговый агент"),
    ({"total_with_vat": 500.0}, "не совпадает с итогом"), ({"lines": []}, "нет строк"),
    ({"company_id": None}, "не привязан к компании")])
def test_blockers_explain_why(change, needle):
    info = md.plan(_doc(**change))
    assert not info["can_send"] and any(needle in b for b in info["blockers"]), info["blockers"]


def test_plan_ok_and_unmeasurable_service():
    assert md.plan(_doc())["can_send"] is True
    item = md.act_item(_ln("Консультация юриста", unit="", code="", quantity=None, price=None,
                           sum_without_vat=5000.0, vat_sum=1100.0, sum_with_vat=6100.0))
    assert item == {"Name": "Консультация юриста", "Unit": "усл. ед.", "NdsType": 22, "SumWithoutNds": 5000.0,
                    "NdsSum": 1100.0, "SumWithNds": 6100.0, "Count": 1, "Price": 5000.0, "IsUnmeasurable": True}
    free = md.act_item(_ln("Доставка", rate="none"))
    assert free["NdsType"] == -1 and free["NdsSum"] == 0.0 and free["SumWithNds"] == 100.0


# ------------------------------------------------------------------ отправка
def test_service_document_goes_as_act_once(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    doc = make_doc(client, a, SERVICES)
    state = client.get(f"/api/v1/moedelo/documents/{doc['id']}", headers=a["hdr"]).json()
    assert state["plan"]["kind"] == "services" and state["plan"]["can_send"] and state["link"] is None
    assert state["connection"]["status"] == "active"
    res = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert res.status_code == 200, res.text
    link = res.json()["link"]
    assert link["status"] == "sent" and link["md_number"] == doc["doc_number"] and link["kontragent_new"] is True
    assert res.json()["state"]["link"]["md_id"] == link["md_id"]
    by_inn = [c for c in fake.calls if c[1] == md.KONTRAGENT_BY_INN_PATH]
    assert len(by_inn) == 1 and by_inn[0][3] == {"Inn": doc["seller"]["inn"]}
    (act,) = fake.posted_acts()
    assert act["Number"] == doc["doc_number"] and act["DocDate"] == DAY and act["NdsPositionType"] == 2
    assert isinstance(act["KontragentId"], int)
    assert [i["Name"] for i in act["Items"]] == [x["name"] for x in SERVICES]
    first = act["Items"][0]
    assert (first["Count"], first["Price"], first["Unit"], first["NdsType"]) == (2, 4500, "усл. ед.", 22)
    assert (first["SumWithoutNds"], first["NdsSum"], first["SumWithNds"]) == (9000.0, 1980.0, 10980.0)
    assert round(sum(i["SumWithNds"] for i in act["Items"]), 2) == doc["total_with_vat"]
    again = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert again.status_code == 409 and "уже в «Моём деле»" in again.json()["detail"]
    assert len(fake.posted_acts()) == 1


def test_existing_supplier_and_act_in_moedelo_are_reused(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    seller = inn10()
    fake.kontragents += [{"Id": 11, "Inn": seller, "Kpp": "770101001", "Name": "старый", "IsArchived": True},
                         {"Id": 12, "Inn": seller, "Kpp": "770101001", "Name": "рабочий", "IsArchived": False}]
    number = f"MD-{uuid4().hex[:8]}"
    fake.acts.append({"Id": 555, "Number": number, "DocDate": f"{DAY}T00:00:00+03:00", "KontragentId": 12,
                      "Sum": round(sum(x["sum_with_vat"] for x in SERVICES), 2)})
    doc = make_doc(client, a, SERVICES, number=number, seller_inn=seller)
    res = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert res.status_code == 200, res.text
    link = res.json()["link"]
    assert (link["status"], link["md_id"], link["kontragent_new"]) == ("found", "555", False)
    assert fake.posted_acts() == [] and not [c for c in fake.calls if c[0] == "POST"]


def test_supplier_fallback_when_inn_method_is_missing(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    fake.inn_endpoint = False
    doc = make_doc(client, a, SERVICES)
    assert client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={}).status_code == 200
    made = [c[3] for c in fake.calls if c[0] == "POST" and c[1] == md.KONTRAGENTS_PATH]
    assert made and made[0]["Inn"] == doc["seller"]["inn"] and made[0]["Form"] == "UL"
    assert made[0]["Name"] == "ООО «Стройсервис»"


def test_sum_mismatch_is_flagged(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    fake.sum_shift = 1.5
    doc = make_doc(client, a, SERVICES)
    link = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={}).json()["link"]
    assert link["status"] == "mismatch" and "проверьте акт там" in link["message"]


def test_goods_wait_for_next_stage_unless_accountant_confirms_services(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    doc = make_doc(client, a, GOODS)
    state = client.get(f"/api/v1/moedelo/documents/{doc['id']}", headers=a["hdr"]).json()
    assert state["plan"]["kind"] == "goods" and "следующим этапом" in state["plan"]["kind_title"]
    res = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert res.status_code == 422 and "Похоже на товары" in res.json()["detail"] and fake.posted_acts() == []
    forced = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={"as_services": True})
    assert forced.status_code == 200 and len(fake.posted_acts()) == 1


def test_send_needs_checked_key_and_key_failure_stops(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    doc = make_doc(client, a, SERVICES)
    res = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert res.status_code == 409 and "Подключите «Моё дело»" in res.json()["detail"]
    connect(client, a)
    fake.status["*"] = 401
    res = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert res.status_code == 422 and "не приняло ключ" in res.json()["detail"]
    assert client.get("/api/v1/moedelo/connection", headers=a["hdr"]).json()["status"] == "error"
    fake.status.clear()
    res = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert res.status_code == 409 and "не проверен или не работает" in res.json()["detail"]


def test_forget_link_then_resend_finds_act_in_moedelo(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    doc = make_doc(client, a, SERVICES)
    first = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={}).json()["link"]
    removed = client.delete(f"/api/v1/moedelo/documents/{doc['id']}/link", headers=a["hdr"])
    assert removed.status_code == 200 and removed.json()["removed"] is True
    again = client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={}).json()["link"]
    assert again["status"] == "found" and again["md_id"] == first["md_id"] and len(fake.posted_acts()) == 1


@pytest.mark.usefixtures("fake")
def test_disconnect_keeps_sent_marks(client, firms):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    doc = make_doc(client, a, SERVICES)
    client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    assert client.delete("/api/v1/moedelo/connection", headers=a["hdr"]).json() == {"removed": True}
    assert client.get("/api/v1/moedelo/connection", headers=a["hdr"]).json()["status"] == "none"
    with SessionLocal() as db:
        assert db.query(MoedeloLink).filter(MoedeloLink.document_id == doc["id"]).count() == 1


# ------------------------------------------------------------------ пакет
def test_batch_sends_services_only_and_stops_on_key_error(client, firms, fake):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    day = "2026-09-0{}"
    docs = [make_doc(client, a, SERVICES, day=day.format(i)) for i in (1, 2)]
    goods = make_doc(client, a, GOODS, day=day.format(3))
    period = {"date_from": "2026-09-01", "date_to": "2026-09-09"}
    check = client.post("/api/v1/moedelo/send/check", headers=a["hdr"], json=period).json()
    assert check["count"] == 2 and check["goods"] == 1 and check["already"] == 0
    assert {r["id"] for r in check["ready"]} == {d["id"] for d in docs} and fake.posted_acts() == []
    assert check["skipped"][0]["id"] == goods["id"] and "следующим этапом" in check["skipped"][0]["reason"]
    sent = client.post("/api/v1/moedelo/send", headers=a["hdr"], json=period).json()
    assert (sent["sent"], sent["found"], sent["mismatch"], sent["failed"], sent["skipped"]) == (2, 0, 0, [], 1)
    again = client.post("/api/v1/moedelo/send/check", headers=a["hdr"], json=period).json()
    assert again["count"] == 0 and again["already"] == 2
    make_doc(client, a, SERVICES, day=day.format(4))
    make_doc(client, a, SERVICES, day=day.format(5))
    fake.status["*"] = 429
    stopped = client.post("/api/v1/moedelo/send", headers=a["hdr"], json=period).json()
    assert stopped["sent"] == 0 and len(stopped["failed"]) == 1 and "частоту запросов" in stopped["stopped"]


@pytest.mark.usefixtures("fake")
def test_batch_for_admin_needs_company(client, firms):  # noqa: F811
    admin, a, _ = firms
    connect(client, a)
    assert client.post("/api/v1/moedelo/send/check", headers=admin, json={}).status_code == 400
    doc = make_doc(client, a, SERVICES, day="2026-08-03")
    check = client.post("/api/v1/moedelo/send/check", headers=admin,
                        json={"company_id": a["id"], "date_from": "2026-08-01", "date_to": "2026-08-31"}).json()
    assert [r["id"] for r in check["ready"]] == [doc["id"]]


@pytest.mark.usefixtures("fake")
def test_audit_never_holds_the_key(client, firms):  # noqa: F811
    _, a, _ = firms
    connect(client, a)
    doc = make_doc(client, a, SERVICES)
    client.post(f"/api/v1/moedelo/documents/{doc['id']}/send", headers=a["hdr"], json={})
    with SessionLocal() as db:
        rows = db.query(AuditLog).filter(AuditLog.action.like("moedelo_%")).all()
    actions = {r.action for r in rows}
    assert {"moedelo_key_saved", "moedelo_connected", "moedelo_document_sent"} <= actions
    assert all(KEY not in (r.details or "") and KEY[-12:] not in (r.details or "") for r in rows)
