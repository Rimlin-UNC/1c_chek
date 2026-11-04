"""Банк по API (v1.70.99, ADR-29): Точка — OAuth 2.0 и JWT-ключ, выписки по месяцам, продление токенов,
дубли между файлом и API, фоновая загрузка, права доступа и хранение секретов.

Банк имитируется (httpx.MockTransport) по формату открытой спецификации Точки; все данные синтетические
(ADR-14): счета, ИНН, суммы и токены придуманы в тесте. В сеть тесты не ходят.
"""
from __future__ import annotations

import json
import random
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, parse_qsl, urlparse
from uuid import uuid4

import httpx
import pytest

from app.bank import connect, tochka
from app.bank.models import BankConnection, BankOperation, BankStatement
from app.database import SessionLocal
from app.docreq.models import DocRequest
from app.models import Receipt, User, utcnow
from app.primary.models import PrimaryDocument
from tests import test_primary_upd as _upd
from tests.primary_samples import inn10
from tests.test_bank import acc, doc, statement
from tests.test_v1140 import _mk_user

firms = _upd.firms
BIK = "044525104"


def _day(d: date) -> str:
    return d.isoformat()


class FakeTochka:
    """Синтетический банк: токены, разрешение, клиенты, счета и выписки с фильтром по датам."""

    def __init__(self, inn: str):
        self.inn = inn
        self.account = acc()
        self.usd_account = "40702840" + acc()[8:]
        self.transactions: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.forms: list[dict] = []
        self.ready_after = 0               # сколько раз ответить «Processing», прежде чем «Ready»
        self.polls: dict[str, int] = {}
        self.statements: dict[str, dict] = {}
        self.fail: dict[str, int] = {}     # часть пути → код ответа
        self.refresh_n = 0
        self.customers = [{"customerCode": "300000001", "taxCode": inn, "customerType": "Business",
                           "shortName": "ООО «Наша»", "fullName": "ООО «Наша»", "isResident": True}]

    @property
    def account_id(self) -> str:
        return f"{self.account}/{BIK}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        for part, code in self.fail.items():
            if part in path:
                return httpx.Response(code, json={"message": "синтетическая ошибка", "code": str(code)})
        if path.endswith("/connect/token"):
            form = dict(parse_qsl(request.content.decode()))
            self.forms.append(form)
            if form.get("client_secret") != "s3cret-synthetic":
                return httpx.Response(400, json={"error": "invalid_client"})
            grant = form["grant_type"]
            if grant == "client_credentials":
                return httpx.Response(200, json={"access_token": "app-token", "expires_in": 86400})
            if grant == "authorization_code":
                if form.get("code") != "good-code":
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "code expired"})
                return httpx.Response(200, json={"access_token": "acc-1", "refresh_token": "ref-1",
                                                 "expires_in": 86400, "token_type": "bearer"})
            if grant == "refresh_token":
                if form.get("refresh_token") != f"ref-{self.refresh_n + 1}":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refresh_n += 1
                return httpx.Response(200, json={"access_token": f"acc-{self.refresh_n + 1}",
                                                 "refresh_token": f"ref-{self.refresh_n + 1}", "expires_in": 86400})
        if path.endswith("/consent/v1.0/consents"):
            body = json.loads(request.content)
            assert "ReadStatements" in body["Data"]["permissions"]
            assert request.headers["Authorization"] == "Bearer app-token"
            return httpx.Response(200, json={"Data": {"Consent": [{"consentId": "tochka-intent-1",
                                                                    "status": "AwaitingAuthorisation"}]}})
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth == "Bearer app-token":
            return httpx.Response(401, json={"message": "unauthorized"})
        if path.endswith("/open-banking/v1.0/customers"):
            return httpx.Response(200, json={"Data": {"Customer": self.customers}})
        if path.endswith("/open-banking/v1.0/accounts"):
            assert "customerCode" not in request.headers          # заголовка нет в спецификации банка
            return httpx.Response(200, json={"Data": {"Account": [
                {"accountId": self.account_id, "customerCode": "300000001", "currency": "RUB", "status": "Enabled"},
                {"accountId": f"{self.usd_account}/{BIK}", "customerCode": "300000001", "currency": "USD",
                 "status": "Enabled"},
                {"accountId": f"{acc()}/{BIK}", "customerCode": "300000001", "currency": "RUB", "status": "Disabled"},
            ]}})
        if path.endswith("/open-banking/v1.0/statements") and request.method == "POST":
            st = json.loads(request.content)["Data"]["Statement"]
            sid = f"st-{len(self.statements) + 1}"
            self.statements[sid] = st
            return httpx.Response(200, json={"Data": {"Statement": {**st, "statementId": sid, "status": "Created"}}})
        m = re.search(r"/accounts/(.+)/statements/([^/]+)$", path)
        if m:
            sid = m.group(2)
            assert m.group(1) == self.account_id          # «номер/БИК» — в пути как есть
            st = self.statements[sid]
            self.polls[sid] = self.polls.get(sid, 0) + 1
            if self.polls[sid] <= self.ready_after:
                return httpx.Response(200, json={"Data": {"Statement": [{**st, "statementId": sid,
                                                                          "status": "Processing"}]}})
            lo, hi = date.fromisoformat(st["startDateTime"]), date.fromisoformat(st["endDateTime"])
            txns = [t for t in self.transactions if lo <= date.fromisoformat(t["documentProcessDate"]) <= hi]
            return httpx.Response(200, json={"Data": {"Statement": [{**st, "statementId": sid, "status": "Ready",
                                                                      "Transaction": txns}]}})
        return httpx.Response(404, json={"message": "not found"})


def txn(*, number: str, day: date, amount: float, debit: bool = True, inn: str = "", name: str = "",
        description: str, card: bool = False, status: str = "Booked", tax: dict | None = None,
        currency: str = "RUB", amount_nat: float | None = None) -> dict:
    party = {"inn": inn, "name": name}
    other = {"identification": acc(), "schemeName": "RU.CBR.BBAN"}
    out = {"transactionId": uuid4().hex, "paymentId": uuid4().hex, "creditDebitIndicator": "Debit" if debit else "Credit",
           "status": status, "documentNumber": number, "documentProcessDate": _day(day), "description": description,
           "transactionTypeCode": "Банковские карты" if card else "Платежное поручение",
           "Amount": {"amount": amount, "currency": currency, **({"amountNat": amount_nat} if amount_nat else {})}}
    if debit:
        out.update({"CreditorParty": party, "CreditorAccount": other})
    else:
        out.update({"DebtorParty": party, "DebtorAccount": other})
    if tax:
        out["TaxFields"] = tax
    return out


@pytest.fixture
def bank(monkeypatch):
    def make(inn: str) -> FakeTochka:
        fake = FakeTochka(inn)
        monkeypatch.setattr(tochka, "transport", httpx.MockTransport(fake.handler))
        monkeypatch.setattr(tochka, "sleep", lambda _s: None)
        return fake
    monkeypatch.delenv("TOCHKA_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOCHKA_CLIENT_SECRET", raising=False)
    return make


@pytest.fixture
def api_firm(client, firms, bank):
    """Компания A: сотрудник с картой *4321, его чек на 3 300,00 ₽, УПД поставщика на 8 540,00 ₽; банк — синтетический."""
    admin, a, b = firms
    tag = uuid4().hex[:8]
    _mk_user(client, admin, a["id"], f"bapi_emp_{tag}", role="user")
    supplier, other = inn10(), inn10()
    d0 = connect.today_msk() - timedelta(days=9)
    with SessionLocal() as db:
        emp = db.query(User).filter_by(username=f"bapi_emp_{tag}").one()
        rec = Receipt(qr_data="t=synthetic", fn=str(random.randrange(10**15, 10**16)), fd=str(random.randrange(10**6)),
                      fp=str(random.randrange(10**9)), company_id=a["id"], total_sum=3300.0, status="verified",
                      fns_status="valid", created_by=emp.id, ecash_sum=3300.0,
                      receipt_date=datetime.combine(d0 - timedelta(days=1), datetime.min.time()) + timedelta(hours=14))
        upd = PrimaryDocument(company_id=a["id"], kind="УПД", function="СЧФДОП", doc_number="Т-501",
                              doc_date=d0 - timedelta(days=3), seller_name="ООО «Арматура»", seller_inn=supplier,
                              total_with_vat=8540.0, status="ok")
        db.add_all([rec, upd])
        db.commit()
        ids = {"emp": emp.id, "receipt": rec.id, "upd": upd.id}
    res = client.put("/api/v1/bank/settings", headers=a["hdr"],
                     json={"retention_days": 1095, "cards": [{"tail": "4321", "user_id": ids["emp"]}]})
    assert res.status_code == 200, res.text
    fake = bank(a["inn"])
    fake.transactions = [
        txn(number="", day=d0, amount=3300.0, description="Покупка по карте 220015******4321 STROYMARKET", card=True),
        txn(number="501", day=d0 + timedelta(days=1), amount=8540.0, inn=supplier, name="ООО «Арматура»",
            description="Оплата по УПД № Т-501, в т.ч. НДС 22% 1540.00"),
        txn(number="502", day=d0 + timedelta(days=2), amount=21000.0, inn=other, name="ООО «Бетон»",
            description="Оплата по счёту № 77 за бетон, в т.ч. НДС 22% 3786.89"),
        txn(number="503", day=d0 + timedelta(days=2), amount=12000.0, inn="7727406020", name="Казначейство России",
            description="Единый налоговый платёж", tax={"kbk": "18201061201010000510", "originatorStatus": "01"}),
        txn(number="504", day=d0 + timedelta(days=3), amount=55000.0, debit=False, inn=inn10(), name="ООО «Заказчик»",
            description="Оплата по договору"),
        txn(number="505", day=d0 + timedelta(days=3), amount=999.0, inn=other, name="ООО «Бетон»",
            description="Ещё не проведён", status="Pending"),
    ]
    return {"admin": admin, "a": a, "b": b, "fake": fake, "supplier": supplier, "other": other, "d0": d0, **ids}


def _save(client, hdr, **body):
    payload = {"bank": "tochka", "method": "oauth", "client_id": "app-synthetic-1", "client_secret": "s3cret-synthetic"}
    payload.update(body)
    return client.put("/api/v1/bank/connection", headers=hdr, json=payload)


def _authorize(client, hdr, company_id: str | None = None) -> str:
    res = client.post("/api/v1/bank/connection/authorize", headers=hdr,
                      json={"company_id": company_id} if company_id else {})
    assert res.status_code == 200, res.text
    url = urlparse(res.json()["url"])
    query = parse_qs(url.query)
    assert url.netloc == "enter.tochka.com" and url.path == "/connect/authorize"
    assert query["response_type"] == ["code"] and query["client_id"] == ["app-synthetic-1"]
    assert query["consent_id"] == ["tochka-intent-1"]
    assert query["redirect_uri"][0].endswith("/api/v1/bank/tochka/callback")
    assert set(query["scope"][0].split()) == {"accounts", "customers", "statements"}   # только чтение
    return query["state"][0]


def _connect(client, firm) -> None:
    a = firm["a"]
    assert _save(client, a["hdr"], start_date=_day(firm["d0"] - timedelta(days=5))).status_code == 200
    state = _authorize(client, a["hdr"])
    res = client.get("/api/v1/bank/tochka/callback", params={"state": state, "code": "good-code"},
                     follow_redirects=False)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"/?bank=connected&company={a['id']}#/bank"


def _conn(company_id: str) -> BankConnection:
    with SessionLocal() as db:
        row = db.query(BankConnection).filter_by(company_id=company_id).one()
        db.expunge(row)
    return row


# ------------------------------------------------------------------ OAuth и загрузка
def test_oauth_connect_and_first_sync(client, api_firm):
    a, fake = api_firm["a"], api_firm["fake"]
    state = client.get("/api/v1/bank/connection", headers=a["hdr"]).json()
    assert state["status"] == "none" and state["banks"] == [{"id": "tochka", "title": "Точка Банк"}]
    assert state["redirect_uri"].endswith("/api/v1/bank/tochka/callback")
    _connect(client, api_firm)
    row = _conn(a["id"])
    assert row.status == "active" and row.customer_code == "300000001"
    for column in (row.client_secret_enc, row.access_enc, row.refresh_enc, row.accounts_enc):
        assert column.startswith("enc:v1:")                       # секреты и номера счетов — только зашифрованно
    assert "s3cret" not in row.client_secret_enc and fake.account not in row.accounts_enc
    code_form = next(f for f in fake.forms if f["grant_type"] == "authorization_code")
    assert code_form["redirect_uri"].endswith("/api/v1/bank/tochka/callback") and code_form["code"] == "good-code"
    view = client.get("/api/v1/bank/connection", headers=a["hdr"]).json()
    text = json.dumps(view, ensure_ascii=False)
    assert "s3cret" not in text and "acc-1" not in text and "ref-1" not in text and fake.account not in text
    assert view["status"] == "active" and view["secret_set"] and view["client_id"] == "app-synthetic-1"
    assert view["accounts"] == [{"tail": fake.account[-4:], "currency": "RUB", "loaded": True},
                                {"tail": fake.usd_account[-4:], "currency": "USD", "loaded": False}]

    res = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={})
    assert res.status_code == 200, res.text
    result = res.json()["result"]
    # 5 проведённых операций (Pending не берём); документы нужны трём списаниям: карта и два поставщика
    assert result["ok"] and result["operations"] == 5 and result["saved"] == 3
    assert result["matched"] == 2 and result["open"] == 1 and result["requests"] >= 1 and result["pending"] == 0
    with SessionLocal() as db:
        ops = {op.number or "card": op for op in db.query(BankOperation).filter_by(company_id=a["id"])}
        assert ops["card"].kind == "card" and ops["card"].card_tail == "4321"
        assert ops["card"].status == "matched" and ops["card"].match_id == api_firm["receipt"]
        assert ops["501"].status == "matched" and ops["501"].match_id == api_firm["upd"]
        assert ops["502"].status == "open" and "503" not in ops and "504" not in ops and "505" not in ops
        req = db.query(DocRequest).filter_by(company_id=a["id"], kind="payment", ref_id=ops["502"].id).one()
        assert req.status == "open"
        sts = db.query(BankStatement).filter_by(company_id=a["id"]).all()
        assert sts and all(s.source == "tochka" and s.account_tail == fake.account[-4:] for s in sts)
    assert {(m, p.split("/")[-1]) for m, p in fake.calls if "statements" in p and m == "POST"}
    overview = client.get("/api/v1/bank", headers=a["hdr"]).json()
    assert {s["source"] for s in overview["statements"]} == {"tochka"}

    again = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()["result"]
    assert again["ok"] and again["saved"] == 0 and again["duplicates"] == 0   # «уже были» — не дубли
    with SessionLocal() as db:
        assert db.query(BankOperation).filter_by(company_id=a["id"]).count() == 3
        assert db.query(BankStatement).filter_by(company_id=a["id"]).count() == len(sts)


def test_callback_state_is_single_use_and_checked(client, api_firm):
    a = api_firm["a"]
    assert _save(client, a["hdr"]).status_code == 200
    state = _authorize(client, a["hdr"])
    for params in ({"state": "short", "code": "good-code"}, {"state": "x" * 43, "code": "good-code"}):
        res = client.get("/api/v1/bank/tochka/callback", params=params, follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"].startswith("/?bank=error&reason=")
    assert _conn(a["id"]).status == "awaiting"
    bad = client.get("/api/v1/bank/tochka/callback", params={"state": state, "code": "stale-code"},
                     follow_redirects=False)
    assert bad.headers["location"].startswith("/?bank=error&reason=") and f"company={a['id']}" in bad.headers["location"]
    assert _conn(a["id"]).status == "error" and "code expired" in _conn(a["id"]).last_error
    replay = client.get("/api/v1/bank/tochka/callback", params={"state": state, "code": "good-code"},
                        follow_redirects=False)
    assert "bank=error" in replay.headers["location"] and "company=" not in replay.headers["location"]
    state2 = _authorize(client, a["hdr"])
    denied = client.get("/api/v1/bank/tochka/callback", params={"state": state2, "error": "access_denied"},
                        follow_redirects=False)
    assert "bank=error" in denied.headers["location"]
    state3 = _authorize(client, a["hdr"])
    with SessionLocal() as db:                     # подтверждение заняло больше 15 минут
        row = db.query(BankConnection).filter_by(company_id=a["id"]).one()
        row.oauth_state_until = utcnow() - timedelta(minutes=1)
        db.commit()
    late = client.get("/api/v1/bank/tochka/callback", params={"state": state3, "code": "good-code"},
                      follow_redirects=False)
    assert "bank=error" in late.headers["location"] and _conn(a["id"]).oauth_state == ""


def test_refresh_rotates_token_and_reauth_on_failure(client, api_firm):
    a, fake = api_firm["a"], api_firm["fake"]
    _connect(client, api_firm)
    with SessionLocal() as db:
        row = db.query(BankConnection).filter_by(company_id=a["id"]).one()
        row.token_expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()["result"]["ok"]
    assert fake.refresh_n == 1
    with SessionLocal() as db:
        row = db.query(BankConnection).filter_by(company_id=a["id"]).one()
        from app.services import secretbox
        assert secretbox.unseal(row.refresh_enc) == "ref-2" and secretbox.unseal(row.access_enc) == "acc-2"
        row.token_expires_at = utcnow() - timedelta(minutes=1)
        row.refresh_enc = secretbox.seal("ref-revoked")
        db.commit()
    res = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()
    assert not res["result"]["ok"] and res["result"]["kind"] == "auth"
    assert res["connection"]["status"] == "reauth" and res["connection"]["last_error"]
    assert client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).status_code == 409


def test_pending_statement_is_picked_up_later(client, api_firm):
    a, fake = api_firm["a"], api_firm["fake"]
    _connect(client, api_firm)
    fake.ready_after = 100                          # банк долго готовит выписку
    first = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()
    assert first["result"]["ok"] and first["result"]["pending"] >= 1 and first["result"]["saved"] == 0
    row = _conn(a["id"])
    assert row.synced_until is None and json.loads(row.pending_json)
    assert fake.account not in row.pending_json and "account" in json.loads(row.pending_json)[0]
    inits = sum(1 for m, p in fake.calls if m == "POST" and p.endswith("/statements"))
    fake.ready_after = 0
    later = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()["result"]
    assert later["ok"] and later["pending"] == 0 and later["saved"] == 3
    assert sum(1 for m, p in fake.calls if m == "POST" and p.endswith("/statements")) == inits   # не заказывали снова
    assert _conn(a["id"]).synced_until == connect.today_msk()


def test_file_and_api_do_not_duplicate(client, api_firm):
    a, fake = api_firm["a"], api_firm["fake"]
    d = api_firm["d0"] + timedelta(days=2)
    ours = (fake.account, a["inn"], "ООО «Наша»")
    raw = statement(fake.account, [doc(number="502", day=d.strftime("%d.%m.%Y"), amount="21000.00", payer=ours,
                                       payee=(acc(), api_firm["other"], "ООО «Бетон»"),
                                       purpose=["Оплата по счёту № 77 за бетон,", "в т.ч. НДС 22%  3786.89"])],
                    start=(d - timedelta(days=1)).strftime("%d.%m.%Y"), end=d.strftime("%d.%m.%Y"))
    up = client.post("/api/v1/bank/statements", headers=a["hdr"], files={"file": ("kl_to_1c.txt", raw, "text/plain")})
    assert up.status_code == 200 and up.json()["saved"] == 1, up.text
    _connect(client, api_firm)
    result = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()["result"]
    assert result["saved"] == 2 and result["duplicates"] == 1         # № 502 уже пришёл из файла
    with SessionLocal() as db:
        assert db.query(BankOperation).filter_by(company_id=a["id"], number="502").count() == 1


def test_jwt_method_and_wrong_company(client, api_firm):
    a, fake = api_firm["a"], api_firm["fake"]
    bad = client.put("/api/v1/bank/connection", headers=a["hdr"], json={"method": "jwt", "jwt": "not a jwt"})
    assert bad.status_code == 422
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJl"
    res = client.put("/api/v1/bank/connection", headers=a["hdr"],
                     json={"method": "jwt", "jwt": jwt, "start_date": _day(api_firm["d0"] - timedelta(days=3))})
    assert res.status_code == 200 and res.json()["jwt_set"] and jwt not in res.text
    fake.customers = [{"customerCode": "300000001", "taxCode": inn10(), "customerType": "Business",
                       "shortName": "ООО «Чужая»"}]
    wrong = client.post("/api/v1/bank/connection/jwt", headers=a["hdr"], json={})
    assert wrong.status_code == 409 and "ООО «Чужая»" in wrong.json()["detail"]
    fake.customers[0]["taxCode"] = a["inn"]
    ok = client.post("/api/v1/bank/connection/jwt", headers=a["hdr"], json={})
    assert ok.status_code == 200 and ok.json()["status"] == "active", ok.text
    result = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()["result"]
    assert result["ok"] and result["saved"] == 3
    assert not any(f.get("grant_type") for f in fake.forms)              # JWT — без обмена токенов
    # JWT-ключ отозван в банке → «подключить заново»
    fake.fail = {"/open-banking/v1.0/statements": 401}
    with SessionLocal() as db:
        row = db.query(BankConnection).filter_by(company_id=a["id"]).one()
        row.synced_until = None
        db.commit()
    res = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).json()
    assert res["connection"]["status"] == "reauth"


def test_access_control_validation_and_removal(client, api_firm):
    admin, a, b = api_firm["admin"], api_firm["a"], api_firm["b"]
    assert client.get("/api/v1/bank/connection", headers=admin).status_code == 400          # выберите компанию
    assert client.get(f"/api/v1/bank/connection?company_id={a['id']}", headers=b["hdr"]).status_code == 404
    assert _save(client, a["hdr"], client_id="bad id with spaces").status_code == 422
    assert _save(client, a["hdr"], client_secret="").status_code == 422                     # client_id без секрета
    assert _save(client, a["hdr"], client_id="", client_secret="").status_code == 422      # нет приложения сервера
    assert _save(client, a["hdr"], start_date=_day(connect.today_msk() + timedelta(days=1))).status_code == 422
    assert _save(client, a["hdr"], bank="sber").status_code == 422
    assert client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={}).status_code == 409
    assert _save(client, admin, company_id=a["id"]).status_code == 200                      # администратор — в карточке
    state = client.get(f"/api/v1/bank/connection?company_id={a['id']}", headers=admin).json()
    assert state["status"] == "new" and state["secret_set"]
    # секрет не меняется, если поле пустое; смена client_id сбрасывает доступ
    assert _save(client, a["hdr"], client_secret="").status_code == 200
    assert _conn(a["id"]).client_secret_enc
    removed = client.delete("/api/v1/bank/connection", headers=a["hdr"])
    assert removed.status_code == 200 and removed.json() == {"removed": True}
    with SessionLocal() as db:
        assert db.query(BankConnection).filter_by(company_id=a["id"]).count() == 0


def test_server_app_from_env(client, api_firm, monkeypatch):
    a = api_firm["a"]
    monkeypatch.setenv("TOCHKA_CLIENT_ID", "app-synthetic-1")
    monkeypatch.setenv("TOCHKA_CLIENT_SECRET", "s3cret-synthetic")
    res = client.put("/api/v1/bank/connection", headers=a["hdr"], json={"method": "oauth"})
    assert res.status_code == 200 and res.json()["server_app"] and not res.json()["secret_set"]
    state = _authorize(client, a["hdr"])
    done = client.get("/api/v1/bank/tochka/callback", params={"state": state, "code": "good-code"},
                      follow_redirects=False)
    assert done.headers["location"].startswith("/?bank=connected")


def test_background_sync_and_bank_errors(client, api_firm):
    a, fake = api_firm["a"], api_firm["fake"]
    _connect(client, api_firm)
    now = utcnow()
    with SessionLocal() as db:
        out = connect.sync_due(db, now)
        mine = [r for r in out if r["company_id"] == a["id"]]
        assert mine and mine[0]["ok"] and mine[0]["saved"] == 3
        row = db.query(BankConnection).filter_by(company_id=a["id"]).one()
        assert not connect.due(row, now + timedelta(hours=5)) and connect.due(row, now + timedelta(hours=7))
        fake.fail = {"/open-banking/v1.0/statements": 503}
        row.last_sync_at = now - timedelta(hours=7)
        db.commit()
        out = connect.sync_due(db, now)
        mine = [r for r in out if r["company_id"] == a["id"]]
        assert mine and not mine[0]["ok"] and mine[0]["kind"] == "unavailable"
        db.expire_all()
        row = db.query(BankConnection).filter_by(company_id=a["id"]).one()
        assert row.status == "active" and "временно" in row.last_error        # сбой банка — повторим позже
        assert not connect.due(row, row.last_sync_at + timedelta(minutes=20))
        assert connect.due(row, row.last_sync_at + timedelta(minutes=31))


# ------------------------------------------------------------------ разбор транзакций и TLS
def test_transaction_doc_maps_fields_for_classifier():
    me = acc()
    debit = tochka.transaction_doc(txn(number="9", day=date(2026, 10, 5), amount=1500.5, inn="7701234567",
                                       name="ООО «Склад»", description="Оплата по счёту № 9"), me)
    assert debit["ПлательщикСчет"] == me and debit["ДатаСписано"] == "2026-10-05" and debit["Сумма"] == "1500.50"
    assert debit["ПолучательИНН"] == "7701234567" and debit["Получатель1"] == "ООО «Склад»" and not debit["_card"]
    credit = tochka.transaction_doc(txn(number="10", day=date(2026, 10, 6), amount=100.0, debit=False, inn="7709876543",
                                        description="Возврат"), me)
    assert credit["ПолучательСчет"] == me and credit["ДатаПоступило"] == "2026-10-06"
    assert tochka.transaction_doc(txn(number="11", day=date(2026, 10, 6), amount=1.0, description="x",
                                      status="Pending"), me) is None
    usd = tochka.transaction_doc(txn(number="12", day=date(2026, 10, 7), amount=10.0, description="x", currency="USD",
                                     amount_nat=950.0), me)
    assert usd["Сумма"] == "950.00"
    card = txn(number="", day=date(2026, 10, 8), amount=700.0, description="Покупка STROYMARKET", card=True)
    card["CreditorAccount"] = {"identification": "220015******9876", "schemeName": "RU.CBR.PAN"}
    mapped = tochka.transaction_doc(card, me)
    assert mapped["_card"] and mapped["ПолучательСчет"] == ""            # номер карты — не счёт получателя
    op = connect._classify(card, me, "")
    assert op["kind"] == "card" and op["direction"] == "out"
    assert tochka.account_number(f"{me}/{BIK}") == me


def test_tls_trusts_mincifry_root_and_never_skips_verification():
    ctx = tochka.ssl_context()
    names = {dict(x[0] for x in cert["subject"]).get("commonName") for cert in ctx.get_ca_certs()}
    assert "Russian Trusted Root CA" in names
    src = open("app/bank/tochka.py", encoding="utf-8").read()
    assert "verify=False" not in src and "CERT_NONE" not in src


def test_months_and_window():
    assert connect.months(date(2026, 10, 15), date(2026, 12, 2)) == [
        (date(2026, 10, 15), date(2026, 10, 31)), (date(2026, 11, 1), date(2026, 11, 30)),
        (date(2026, 12, 1), date(2026, 12, 2))]
    conn = BankConnection(start_date=date(2026, 9, 20), synced_until=None)
    assert connect.window(conn, date(2026, 11, 4)) == (date(2026, 9, 20), date(2026, 11, 4))
    conn.synced_until = date(2026, 11, 2)            # поздние проводки: с начала месяца «минус 3 дня»
    assert connect.window(conn, date(2026, 11, 4)) == (date(2026, 10, 1), date(2026, 11, 4))
    conn.synced_until = date(2026, 11, 20)
    assert connect.window(conn, date(2026, 11, 21)) == (date(2026, 11, 1), date(2026, 11, 21))
    assert connect.default_start(date(2026, 1, 10)) == date(2025, 12, 1)


def test_mincifry_root_fingerprint_is_pinned():
    """Корневой «Russian Trusted Root CA» в составе программы — тот самый: отпечаток SHA-256 сверен с двумя
    независимыми копиями (Java-клиент банка и сторонний проект на GitHub, 04.11.2026; ADR-29)."""
    import hashlib
    import ssl
    pem = open("app/certs/russian_trusted_ca.pem", encoding="ascii").read()
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S)
    prints = {hashlib.sha256(ssl.PEM_cert_to_DER_cert(b)).hexdigest().upper() for b in blocks}
    assert "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31" in prints
