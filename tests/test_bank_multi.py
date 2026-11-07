"""Несколько банков по API у компании (v1.70.102, ADR-32): Т-Банк и Модульбанк (ключ API), Сбербанк
(refresh_token и сертификат клиента), Альфа-Банк (ключ API или токен, сертификат — если выдан).

Банки имитируются (httpx.MockTransport) по форматам их открытых спецификаций; все данные синтетические
(ADR-14): счета, ИНН, суммы, ключи и сертификаты придуманы или выпущены в тесте. В сеть тесты не ходят.
"""
from __future__ import annotations

import base64
import json
import random
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from app.bank import alfa, clientcert, connect, fintech, http, modul, providers, tbank
from app.bank.models import BankConnection, BankOperation, BankStatement
from app.database import SessionLocal
from app.models import Receipt, User, utcnow
from app.primary.models import PrimaryDocument
from app.services import secretbox
from tests import test_primary_upd as _upd
from tests.primary_samples import inn10
from tests.test_bank import acc
from tests.test_v1140 import _mk_user

firms = _upd.firms
TOKEN_T = "t.synthetic-tbank-token-0001"
TOKEN_M = "synthetic-modulbank-token-0001"
ALFA_KEY = "synthetic-alfa-api-key-0001"


# ------------------------------------------------------------------ сертификаты (выпускаются в тесте)
def make_cert(days: int = 365, key=None, cn: str = "synthetic-client") -> tuple[str, str, object, x509.Certificate]:
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    start = now - timedelta(days=2) if days > 0 else now - timedelta(days=30)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start)
            .not_valid_after(now + timedelta(days=days)).sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert_pem, key_pem, key, cert


@pytest.fixture(scope="module")
def cert_pair():
    return make_cert()


# ------------------------------------------------------------------ синтетические банки
def _jwt(payload: dict) -> str:
    part = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJnb3N0In0.{part}.c2lnbmF0dXJl"


class FakeBanks:
    """Т-Банк, Сбербанк, Альфа-Банк и Модульбанк в одном транспорте: маршрут — по адресу запроса."""

    def __init__(self, inn: str):
        self.inn = inn
        self.calls: list[tuple[str, str, str]] = []           # (метод, путь, Authorization)
        self.forms: list[dict] = []
        self.fail: dict[str, int] = {}
        # Т-Банк
        self.t_account, self.t_usd = acc(), "40702840" + acc()[8:]
        self.t_ops: list[dict] = []
        self.t_page = 2                                          # операций в порции (для проверки курсора)
        # Сбер
        self.s_account = acc()
        self.s_inn = inn
        self.s_refresh_n = 0
        self.s_days: dict[str, list[dict]] = {}
        self.s_page = 2
        # Альфа
        self.a_account = acc()
        self.a_inn = inn
        self.a_days: dict[str, list[dict]] = {}
        # Модульбанк
        self.m_account, self.m_id = acc(), str(uuid4())
        self.m_companies: list[dict] | None = None
        self.m_ops: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        auth = request.headers.get("Authorization", "")
        self.calls.append((request.method, path, auth))
        for part, code in self.fail.items():
            if part in path:
                return httpx.Response(code, json={"message": "синтетическая ошибка", "cause": "TEST"})
        if host == "business.tbank.ru":
            return self.tbank(request, path, auth)
        if host == "fintech.sberbank.ru":
            return self.sber(request, path, auth)
        if host == "baas.alfabank.ru":
            return self.alfa(request, path, auth)
        if host == "api.modulbank.ru":
            return self.modul(request, path, auth)
        return httpx.Response(404, json={"message": "not found"})

    # Т-Бизнес
    def tbank(self, request, path, auth):
        if auth != f"Bearer {TOKEN_T}":
            return httpx.Response(401, json={"errorMessage": "Unauthorized"})
        if path == "/openapi/api/v4/bank-accounts":
            return httpx.Response(200, json=[
                {"accountNumber": self.t_account, "name": "Расчётный", "currency": "643", "bankBik": "044525974",
                 "accountType": "Current"},
                {"accountNumber": self.t_usd, "name": "Валютный", "currency": "840", "bankBik": "044525974",
                 "accountType": "Current"}])
        if path == "/openapi/api/v1/statement":
            q = dict(request.url.params)
            assert q["accountNumber"] == self.t_account                     # валютный счёт не загружаем
            lo, hi = date.fromisoformat(q["from"][:10]), date.fromisoformat(q["till"][:10])
            ops = [o for o in self.t_ops if lo <= date.fromisoformat(o["operationDate"][:10]) <= hi]
            start = int(q.get("cursor") or 0)
            chunk = ops[start:start + self.t_page]
            nxt = start + self.t_page
            return httpx.Response(200, json={"operations": chunk, **({"nextCursor": str(nxt)} if nxt < len(ops) else {})})
        return httpx.Response(404, json={"errorMessage": "not found"})

    # Sber API
    def sber(self, request, path, auth):
        if path == "/ic/sso/api/v2/oauth/token":
            form = dict(parse_qsl(request.content.decode()))
            self.forms.append(form)
            if form.get("client_secret") != "sber-secret-synthetic":
                return httpx.Response(400, json={"error": "invalid_client", "error_description": "bad secret"})
            if form.get("refresh_token") != f"s-refresh-{self.s_refresh_n}":
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh revoked"})
            self.s_refresh_n += 1
            return httpx.Response(200, json={"access_token": f"s-acc-{self.s_refresh_n}", "token_type": "Bearer",
                                             "expires_in": 3600, "refresh_token": f"s-refresh-{self.s_refresh_n}",
                                             "scope": "openid GET_STATEMENT_ACCOUNT accounts inn"})
        if auth != f"Bearer s-acc-{self.s_refresh_n}":
            return httpx.Response(401, json={"error": "invalid_token"})
        if path == "/fintech/api/ic/sso/api/v2/oauth/user-info":
            assert request.headers.get("Accept") == "application/jwt"
            return httpx.Response(200, text=json.dumps(_jwt({"inn": self.s_inn, "OrgName": "ООО «Наша»", "accounts": [
                {"accountNumber": self.s_account, "bic": "044525225", "corrAccountNumber": "30101810400000000225"}]})),
                headers={"Content-Type": "application/jwt"})
        if path == "/fintech/api/v2/statement/transactions":
            q = dict(request.url.params)
            assert q["accountNumber"] == self.s_account
            items = self.s_days.get(q["statementDate"])
            if items is None:
                return httpx.Response(404, json={"cause": "DATA_NOT_FOUND", "message": "Выписка не найдена"})
            page = int(q["page"])
            chunk = items[(page - 1) * self.s_page: page * self.s_page]
            links = [{"href": f"/v2/statement/transactions?page={page + 1}", "rel": "next"}] \
                if page * self.s_page < len(items) else []
            return httpx.Response(200, json={"transactions": chunk, "_links": links})
        return httpx.Response(404, json={"message": "not found"})

    # Alfa API
    def alfa(self, request, path, auth):
        if auth not in (f"ApiKey {ALFA_KEY}", "Bearer aaa.bbb.ccc"):
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/api/jp/v2/customer-info":
            return httpx.Response(200, json={"inn": self.a_inn, "shortName": "ООО «Наша»", "accounts": [
                {"number": self.a_account, "currencyCode": "810", "type": "PAYMENT"}]})
        if path == "/api/statement/transactions":
            q = dict(request.url.params)
            items = self.a_days.get(q["statementDate"])
            if items is None:
                return httpx.Response(404, json={"message": "Выписка за дату не найдена"})
            return httpx.Response(200, json={"transactions": items, "_links": []})
        return httpx.Response(404, json={"message": "not found"})

    # Модульбанк
    def modul(self, request, path, auth):
        if auth != f"Bearer {TOKEN_M}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        if path == "/v1/account-info":
            companies = self.m_companies or [{"companyId": "m-c1", "companyName": "ООО «Наша»", "bankAccounts": [
                {"id": self.m_id, "accountName": "Расчётный", "number": self.m_account, "currency": "RUR",
                 "category": "CheckingAccount", "status": "New"},
                {"id": str(uuid4()), "accountName": "Депозит", "number": acc(), "currency": "RUR",
                 "category": "DepositAccount", "status": "New"}]}]
            return httpx.Response(200, json=companies)
        m = re.fullmatch(r"/v1/operation-history/([0-9a-f\-]+)", path)
        if m:
            assert m.group(1) == self.m_id
            body = json.loads(request.content)
            lo, hi = date.fromisoformat(body["from"]), date.fromisoformat(body["till"])
            ops = [o for o in self.m_ops if lo <= date.fromisoformat(o["executed"][:10]) <= hi]
            return httpx.Response(200, json=ops[body["skip"]: body["skip"] + body["records"]])
        return httpx.Response(404, json={"message": "not found"})


def t_op(*, day: date, amount: float, debit: bool = True, inn: str = "", name: str = "", purpose: str = "",
         number: str = "", card: str = "", status: str = "Transaction") -> dict:
    party = {"inn": inn, "name": name, "account": acc() if inn else ""}
    return {"operationId": uuid4().hex, "operationDate": f"{day.isoformat()}T10:15:00+03:00",
            "typeOfOperation": "Debit" if debit else "Credit", "operationStatus": status, "category": "x",
            "accountAmount": amount, "rubleAmount": amount, "payPurpose": purpose, "description": purpose or "Оплата",
            "documentNumber": number, "counterParty": party, **({"cardNumber": card} if card else {})}


def s_txn(*, amount: float, debit: bool = True, inn: str = "", name: str = "", purpose: str, number: str = "",
          kbk: str = "") -> dict:
    other = {"Inn": inn, "Name": name, "Account": acc()}
    rur = {("payee" if debit else "payer") + k: v for k, v in other.items()}
    rur = {k[0].lower() + k[1:]: v for k, v in rur.items()}
    if kbk:
        rur["departmentalInfo"] = {"kbk": kbk, "drawerStatus101": "01"}
    return {"uuid": str(uuid4()), "direction": "DEBIT" if debit else "CREDIT", "number": number,
            "amount": {"amount": amount, "currencyName": "RUB"}, "amountRub": {"amount": amount, "currencyName": "RUB"},
            "operationDate": "2026-01-01T00:00:00Z", "paymentPurpose": purpose, "operationCode": "01",
            "rurTransfer": rur}


def m_op(*, day: date, amount: float, debit: bool = True, inn: str = "", name: str = "", purpose: str,
         number: str = "", status: str | None = None) -> dict:
    return {"id": str(uuid4()), "status": status or ("Executed" if debit else "Received"),
            "category": "Debet" if debit else "Credit", "amount": amount, "currency": "RUR",
            "contragentName": name, "contragentInn": inn, "contragentBankAccountNumber": acc(),
            "paymentPurpose": purpose, "executed": f"{day.isoformat()}T00:00:00", "docNumber": number}


# ------------------------------------------------------------------ фикстуры
@pytest.fixture
def banks(monkeypatch):
    from app.bank import tochka
    # подключения Точки из других тестов в фоновой загрузке — без сети: банк «недоступен»
    monkeypatch.setattr(tochka, "transport", httpx.MockTransport(lambda _r: httpx.Response(503, json={})))
    monkeypatch.setattr(tochka, "sleep", lambda _s: None)

    def make(inn: str) -> FakeBanks:
        fake = FakeBanks(inn)
        monkeypatch.setattr(http, "transport", httpx.MockTransport(fake.handler))
        return fake
    return make


@pytest.fixture
def multi(client, firms, banks):
    """Компания A: сотрудник с картой *4321, его чек на 2 750,00 ₽ и УПД поставщика на 9 150,00 ₽."""
    admin, a, b = firms
    tag = uuid4().hex[:8]
    _mk_user(client, admin, a["id"], f"bmul_emp_{tag}", role="user")
    supplier, other = inn10(), inn10()
    d0 = connect.today_msk() - timedelta(days=8)
    with SessionLocal() as db:
        emp = db.query(User).filter_by(username=f"bmul_emp_{tag}").one()
        rec = Receipt(qr_data="t=synthetic", fn=str(random.randrange(10**15, 10**16)), fd=str(random.randrange(10**6)),
                      fp=str(random.randrange(10**9)), company_id=a["id"], total_sum=2750.0, status="verified",
                      fns_status="valid", created_by=emp.id, ecash_sum=2750.0,
                      receipt_date=datetime.combine(d0 - timedelta(days=1), datetime.min.time()) + timedelta(hours=12))
        upd = PrimaryDocument(company_id=a["id"], kind="УПД", function="СЧФДОП", doc_number="М-77",
                              doc_date=d0 - timedelta(days=2), seller_name="ООО «Кирпич»", seller_inn=supplier,
                              total_with_vat=9150.0, status="ok")
        db.add_all([rec, upd])
        db.commit()
        ids = {"emp": emp.id, "receipt": rec.id, "upd": upd.id}
    res = client.put("/api/v1/bank/settings", headers=a["hdr"],
                     json={"retention_days": 1095, "cards": [{"tail": "4321", "user_id": ids["emp"]}]})
    assert res.status_code == 200, res.text
    fake = banks(a["inn"])
    return {"admin": admin, "a": a, "b": b, "fake": fake, "supplier": supplier, "other": other, "d0": d0, **ids}


def _conn(company_id: str, bank: str) -> BankConnection:
    with SessionLocal() as db:
        row = db.query(BankConnection).filter_by(company_id=company_id, bank=bank).one()
        db.expunge(row)
    return row


def _put(client, hdr, **body):
    return client.put("/api/v1/bank/connection", headers=hdr, json=body)


# ------------------------------------------------------------------ Т-Банк
def test_tbank_token_connect_sync_cursor_and_secrets(client, multi):
    a, fake, d0 = multi["a"], multi["fake"], multi["d0"]
    fake.t_ops = [
        t_op(day=d0, amount=2750.0, purpose="", card="220070******4321"),
        t_op(day=d0 + timedelta(days=1), amount=9150.0, inn=multi["supplier"], name="ООО «Кирпич»", number="77",
             purpose="Оплата по УПД № М-77, в т.ч. НДС 22% 1650.00"),
        t_op(day=d0 + timedelta(days=1), amount=18000.0, inn=multi["other"], name="ООО «Песок»", number="78",
             purpose="Оплата по счёту № 12 за песок"),
        t_op(day=d0 + timedelta(days=2), amount=40000.0, debit=False, inn=inn10(), name="ООО «Заказчик»",
             number="5", purpose="Оплата по договору"),
        t_op(day=d0 + timedelta(days=2), amount=500.0, purpose="Авторизация", card="220070******4321",
             status="Authorization"),
    ]
    empty = client.get("/api/v1/bank/connection?bank=tbank", headers=a["hdr"]).json()
    assert empty["bank"] == "tbank" and empty["status"] == "none" and empty["method"] == "token"
    assert "Выпуск токена" in empty["how"] and empty["cert_policy"] == ""
    assert _put(client, a["hdr"], bank="tbank", method="token").status_code == 422          # ключа нет
    assert _put(client, a["hdr"], bank="tbank", method="token", token="ключ с пробелом").status_code == 422
    res = _put(client, a["hdr"], bank="tbank", method="token", token=f" {TOKEN_T} ",
               start_date=(d0 - timedelta(days=3)).isoformat())
    assert res.status_code == 200, res.text
    assert res.json()["token_set"] and TOKEN_T not in res.text and res.json()["status"] == "new"
    row = _conn(a["id"], "tbank")
    assert row.method == "token" and row.jwt_enc.startswith("enc:v1:") and TOKEN_T not in row.jwt_enc
    ok = client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "tbank"})
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert view["status"] == "active" and view["bank_title"] == "Т-Банк"
    assert view["accounts"] == [{"tail": fake.t_account[-4:], "currency": "RUB", "loaded": True},
                                {"tail": fake.t_usd[-4:], "currency": "USD", "loaded": False}]
    text = json.dumps(view, ensure_ascii=False)
    assert TOKEN_T not in text and fake.t_account not in text
    sync = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "tbank"})
    assert sync.status_code == 200, sync.text
    result = sync.json()["result"]
    # 4 проведённые операции (авторизацию не берём); документы нужны трём списаниям: карта и два поставщика
    assert result["ok"] and result["operations"] == 4 and result["saved"] == 3 and result["matched"] == 2
    statement_calls = [c for c in fake.calls if c[1] == "/openapi/api/v1/statement"]
    assert len(statement_calls) == 3                                        # курсор: порции по 2 операции
    with SessionLocal() as db:
        ops = {op.number or "card": op for op in db.query(BankOperation).filter_by(company_id=a["id"])}
        assert ops["card"].kind == "card" and ops["card"].card_tail == "4321" and ops["card"].match_id == multi["receipt"]
        assert ops["77"].status == "matched" and ops["77"].match_id == multi["upd"]
        assert ops["78"].status == "open"
        sts = db.query(BankStatement).filter_by(company_id=a["id"]).all()
        assert sts and {s.source for s in sts} == {"tbank"}
    again = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "tbank"}).json()["result"]
    assert again["ok"] and again["saved"] == 0                              # повторная загрузка — без дублей
    overview = client.get("/api/v1/bank", headers=a["hdr"]).json()
    assert {s["source"] for s in overview["statements"]} == {"tbank"}


def test_two_banks_in_one_company_list_switch_and_remove(client, multi):
    a, fake = multi["a"], multi["fake"]
    assert _put(client, a["hdr"], bank="tbank", method="token", token=TOKEN_T).status_code == 200
    assert client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "tbank"}).status_code == 200
    assert _put(client, a["hdr"], bank="modul", method="token", token=TOKEN_M).status_code == 200
    assert client.post("/api/v1/bank/connection/jwt", headers=a["hdr"], json={"bank": "modul"}).status_code == 200
    state = client.get("/api/v1/bank/connection", headers=a["hdr"]).json()   # без bank — первый подключённый
    assert state["bank"] == "tbank"
    assert [(c["bank"], c["status"]) for c in state["connections"]] == [("tbank", "active"), ("modul", "active")]
    assert [b["id"] for b in state["banks"]] == ["tochka", "tbank", "sber", "alfa", "modul"]
    assert {b["title"] for b in state["file_banks"]} >= {"ВТБ", "Газпромбанк", "Райффайзенбанк"}
    modul_state = client.get("/api/v1/bank/connection?bank=modul", headers=a["hdr"]).json()
    assert modul_state["bank"] == "modul" and modul_state["accounts"][0]["tail"] == fake.m_account[-4:]
    seen: dict[str, bool] = {}
    with SessionLocal() as db:                                               # фоновая загрузка — оба банка
        for _ in range(8):                                                   # за тик — не больше трёх подключений
            for r in connect.sync_due(db, utcnow()):
                if r["company_id"] == a["id"]:
                    seen[r["bank"]] = r["ok"]
            if len(seen) == 2:
                break
    assert seen == {"tbank": True, "modul": True}
    removed = client.delete("/api/v1/bank/connection?bank=tbank", headers=a["hdr"])
    assert removed.status_code == 200 and removed.json() == {"removed": True}
    with SessionLocal() as db:
        assert [r.bank for r in db.query(BankConnection).filter_by(company_id=a["id"])] == ["modul"]
    assert client.get("/api/v1/bank/connection", headers=a["hdr"]).json()["bank"] == "modul"
    # «Вход через банк» — только у Точки
    assert client.post("/api/v1/bank/connection/authorize", headers=a["hdr"], json={"bank": "modul"}).status_code == 409


def test_outsourcer_dashboard_aggregates_banks(client, multi):
    a, fake = multi["a"], multi["fake"]
    _put(client, a["hdr"], bank="tbank", method="token", token=TOKEN_T)
    client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "tbank"})
    _put(client, a["hdr"], bank="modul", method="token", token=TOKEN_M)
    fake.fail = {"/v1/account-info": 401}
    bad = client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "modul"})
    assert bad.status_code == 502 and "не принял доступ" in bad.json()["detail"]
    assert _conn(a["id"], "modul").status == "reauth"
    board = client.get("/api/v1/clients", headers=a["hdr"])
    assert board.status_code == 200, board.text
    row = next(r for r in board.json()["companies"] if r["id"] == a["id"])
    assert row["bank"]["status"] == "reauth" and row["bank"]["last_error"].startswith("Модульбанк:")
    assert sorted(x["bank"] for x in row["bank"]["banks"]) == ["modul", "tbank"]
    for bank in ("tbank", "modul"):                                          # не оставлять «ждущих» загрузки
        assert client.delete(f"/api/v1/bank/connection?bank={bank}", headers=a["hdr"]).json()["removed"]


# ------------------------------------------------------------------ Сбербанк
def test_sber_requires_cert_refresh_rotation_inn_and_daily_pages(client, multi, cert_pair):
    a, fake, d0 = multi["a"], multi["fake"], multi["d0"]
    cert_pem, key_pem, _key, _cert = cert_pair
    base = {"bank": "sber", "method": "refresh", "client_id": "sberapp01", "client_secret": "sber-secret-synthetic",
            "refresh_token": "s-refresh-0", "start_date": (d0 - timedelta(days=1)).isoformat()}
    no_cert = _put(client, a["hdr"], **base)
    assert no_cert.status_code == 422 and "сертификат" in no_cert.json()["detail"].lower()
    bad = _put(client, a["hdr"], **base, cert="-----BEGIN CERTIFICATE-----\nmusor\n-----END CERTIFICATE-----")
    assert bad.status_code == 422
    only_cert = _put(client, a["hdr"], **base, cert=cert_pem)
    assert only_cert.status_code == 422 and "закрытый ключ" in only_cert.json()["detail"]
    res = _put(client, a["hdr"], **base, cert=cert_pem + key_pem)
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["cert_set"] and view["refresh_set"] and view["cert"]["until"] and len(view["cert"]["fingerprint"]) == 16
    assert "PRIVATE KEY" not in res.text and "s-refresh-0" not in res.text and "sber-secret" not in res.text
    row = _conn(a["id"], "sber")
    assert row.cert_enc.startswith("enc:v1:") and "PRIVATE" not in row.cert_enc
    assert "PRIVATE KEY" in secretbox.unseal(row.cert_enc)

    fake.s_inn = inn10()                                                    # ключи другой компании
    wrong = client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "sber"})
    assert wrong.status_code == 409 and fake.s_inn in wrong.json()["detail"]
    fake.s_inn = a["inn"]
    ok = client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "sber"})
    assert ok.status_code == 200 and ok.json()["status"] == "active", ok.text
    assert fake.forms[0]["grant_type"] == "refresh_token" and fake.forms[0]["client_id"] == "sberapp01"
    row = _conn(a["id"], "sber")
    assert secretbox.unseal(row.refresh_enc) == "s-refresh-1"                  # новая пара сохранена сразу

    day1, day2 = d0, d0 + timedelta(days=1)
    fake.s_days = {
        day1.isoformat(): [s_txn(amount=9150.0, inn=multi["supplier"], name="ООО «Кирпич»", number="77",
                                 purpose="Оплата по УПД № М-77, в т.ч. НДС 22% 1650.00"),
                           s_txn(amount=7300.0, inn=multi["other"], name="ООО «Песок»", number="79",
                                 purpose="Оплата по счёту № 15"),
                           s_txn(amount=12000.0, inn="7727406020", name="Казначейство России", number="80",
                                 purpose="Единый налоговый платёж", kbk="18201061201010000510")],
        day2.isoformat(): [s_txn(amount=60000.0, debit=False, inn=inn10(), name="ООО «Заказчик»", number="3",
                                 purpose="Оплата по договору")],
    }
    with SessionLocal() as db:                                              # токен истёк — продлится сам
        r = db.query(BankConnection).filter_by(company_id=a["id"], bank="sber").one()
        r.token_expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    result = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "sber"}).json()["result"]
    assert result["ok"] and result["operations"] == 4 and result["saved"] == 2 and result["matched"] == 1, result
    assert fake.s_refresh_n == 2
    pages = [c for c in fake.calls if c[1].endswith("/statement/transactions")]
    assert len(pages) >= 3                                                 # день 1 — две страницы
    with SessionLocal() as db:
        ops = {op.number: op for op in db.query(BankOperation).filter_by(company_id=a["id"])}
        assert ops["77"].match_id == multi["upd"] and ops["79"].status == "open" and "80" not in ops
        assert {s.source for s in db.query(BankStatement).filter_by(company_id=a["id"])} == {"sber"}
        r = db.query(BankConnection).filter_by(company_id=a["id"], bank="sber").one()
        r.token_expires_at = utcnow() - timedelta(minutes=1)
        r.refresh_enc = secretbox.seal("s-ref-revoked")
        db.commit()
    res = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "sber"}).json()
    assert not res["result"]["ok"] and res["result"]["kind"] == "auth" and res["connection"]["status"] == "reauth"


def test_sber_daily_window_is_capped():
    conn = BankConnection(bank="sber", start_date=date(2026, 6, 1), synced_until=None)
    lo, hi = connect.window(conn, date(2026, 11, 5))
    assert (lo, hi) == (date(2026, 6, 1), date(2026, 11, 5))
    assert providers.PROVIDERS["sber"]["daily"] and providers.DAILY_MAX_DAYS == 45
    assert fintech.days(date(2026, 10, 30), date(2026, 11, 2)) == [date(2026, 10, 30), date(2026, 10, 31),
                                                                   date(2026, 11, 1), date(2026, 11, 2)]


# ------------------------------------------------------------------ Альфа-Банк
def test_alfa_api_key_or_bearer_inn_and_empty_days(client, multi, cert_pair):
    a, fake, d0 = multi["a"], multi["fake"], multi["d0"]
    assert alfa.auth(ALFA_KEY) == f"ApiKey {ALFA_KEY}" and alfa.auth("aaa.bbb.ccc") == "Bearer aaa.bbb.ccc"
    cert_pem, key_pem, _k, _c = cert_pair
    res = _put(client, a["hdr"], bank="alfa", method="token", token=ALFA_KEY, cert=cert_pem + key_pem,
               start_date=d0.isoformat())
    assert res.status_code == 200 and res.json()["cert_set"], res.text
    fake.a_inn = inn10()
    assert client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "alfa"}).status_code == 409
    assert _conn(a["id"], "alfa").status == "error"
    fake.a_inn = a["inn"]
    assert client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "alfa"}).status_code == 200
    fake.a_days = {d0.isoformat(): [s_txn(amount=9150.0, inn=multi["supplier"], name="ООО «Кирпич»", number="77",
                                          purpose="Оплата по УПД № М-77")]}
    result = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "alfa"}).json()["result"]
    assert result["ok"] and result["saved"] == 1 and result["matched"] == 1
    assert all(c[2] == f"ApiKey {ALFA_KEY}" for c in fake.calls if "/api/" in c[1])
    # сертификат необязателен у Альфы: можно убрать; у Сбера — нельзя
    removed = _put(client, a["hdr"], bank="alfa", method="token", cert_remove=True)
    assert removed.status_code == 200 and not removed.json()["cert_set"] and removed.json()["status"] == "new"


# ------------------------------------------------------------------ Модульбанк
def test_modulbank_pages_statuses_and_many_companies(client, multi):
    a, fake, d0 = multi["a"], multi["fake"], multi["d0"]
    fake.m_ops = [m_op(day=d0, amount=100.0 + i, inn=multi["other"], name="ООО «Песок»", number=str(100 + i),
                       purpose=f"Оплата по счёту № {i}") for i in range(51)]
    fake.m_ops += [m_op(day=d0, amount=9150.0, inn=multi["supplier"], name="ООО «Кирпич»", number="77",
                        purpose="Оплата по УПД № М-77"),
                   m_op(day=d0, amount=5.0, inn=multi["other"], purpose="Ещё не исполнен", number="999",
                        status="SendToBank")]
    assert _put(client, a["hdr"], bank="modul", token=TOKEN_M, start_date=d0.isoformat()).status_code == 200
    assert client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "modul"}).status_code == 200
    accounts = client.get("/api/v1/bank/connection?bank=modul", headers=a["hdr"]).json()["accounts"]
    assert [x["tail"] for x in accounts] == [fake.m_account[-4:]]                # депозит не загружаем
    result = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "modul"}).json()["result"]
    assert result["ok"] and result["operations"] == 52 and result["saved"] == 52 and result["matched"] == 1
    assert len([c for c in fake.calls if "/operation-history/" in c[1]]) == 2   # порции по 50
    fake.m_companies = [{"companyId": "x1", "companyName": "Первая", "bankAccounts": [
        {"id": str(uuid4()), "number": acc(), "currency": "RUR", "category": "CheckingAccount", "status": "New"}]},
        {"companyId": "x2", "companyName": "Вторая", "bankAccounts": [
            {"id": str(uuid4()), "number": acc(), "currency": "RUR", "category": "CheckingAccount", "status": "New"}]}]
    many = client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "modul"})
    assert many.status_code == 409 and "несколько компаний" in many.json()["detail"]


# ------------------------------------------------------------------ ошибки, права, разбор
def test_bank_errors_kinds_and_background_retry(client, multi):
    a, fake = multi["a"], multi["fake"]
    _put(client, a["hdr"], bank="tbank", token=TOKEN_T)
    assert client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "tbank"}).status_code == 200
    for code, kind, status in ((503, "unavailable", "active"), (429, "rate", "active"), (403, "forbidden", "reauth")):
        with SessionLocal() as db:
            r = db.query(BankConnection).filter_by(company_id=a["id"], bank="tbank").one()
            r.status, r.last_error = "active", ""
            db.commit()
        fake.fail = {"/api/v1/statement": code}
        res = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "tbank"}).json()
        assert res["result"]["kind"] == kind and res["connection"]["status"] == status, (code, res)
        assert "Т-Банк" in res["connection"]["last_error"]


def test_access_control_and_validation(client, multi):
    admin, a, b = multi["admin"], multi["a"], multi["b"]
    assert _put(client, a["hdr"], bank="vtb", token=TOKEN_T).status_code == 422          # не по API
    assert client.get("/api/v1/bank/connection?bank=vtb", headers=a["hdr"]).status_code == 422
    assert _put(client, a["hdr"], bank="tbank", token=TOKEN_T).status_code == 200
    assert client.get(f"/api/v1/bank/connection?company_id={a['id']}&bank=tbank", headers=b["hdr"]).status_code == 404
    assert client.delete(f"/api/v1/bank/connection?company_id={a['id']}&bank=tbank", headers=b["hdr"]).status_code == 404
    assert client.get("/api/v1/bank/connection?bank=tbank", headers=admin).status_code == 400   # выберите компанию
    view = client.get(f"/api/v1/bank/connection?company_id={a['id']}&bank=tbank", headers=admin).json()
    assert view["token_set"] and view["status"] == "new"
    assert client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "tbank"}).status_code == 409


def test_file_and_tbank_api_do_not_duplicate(client, multi):
    from tests.test_bank import doc, statement
    a, fake, d0 = multi["a"], multi["fake"], multi["d0"]
    ours = (fake.t_account, a["inn"], "ООО «Наша»")
    purpose = "Оплата по счёту № 12 за песок"
    raw = statement(fake.t_account, [doc(number="78", day=d0.strftime("%d.%m.%Y"), amount="18000.00", payer=ours,
                                         payee=(acc(), multi["other"], "ООО «Песок»"), purpose=[purpose])],
                    start=(d0 - timedelta(days=1)).strftime("%d.%m.%Y"), end=d0.strftime("%d.%m.%Y"))
    up = client.post("/api/v1/bank/statements", headers=a["hdr"], files={"file": ("kl_to_1c.txt", raw, "text/plain")})
    assert up.status_code == 200 and up.json()["saved"] == 1, up.text
    fake.t_ops = [t_op(day=d0, amount=18000.0, inn=multi["other"], name="ООО «Песок»", number="78", purpose=purpose)]
    _put(client, a["hdr"], bank="tbank", token=TOKEN_T, start_date=(d0 - timedelta(days=2)).isoformat())
    client.post("/api/v1/bank/connection/check", headers=a["hdr"], json={"bank": "tbank"})
    result = client.post("/api/v1/bank/connection/sync", headers=a["hdr"], json={"bank": "tbank"}).json()["result"]
    assert result["saved"] == 0 and result["duplicates"] == 1


def test_mapping_tbank_msk_date_card_tail_and_currency():
    me = acc()
    late = t_op(day=date(2026, 10, 5), amount=700.0, card="220070******9876", purpose="")
    late["operationDate"] = "2026-10-05T22:30:00Z"                        # по Москве — уже 6 октября
    late.pop("payPurpose")
    late["description"] = "Оплата в STROYMARKET"
    doc = tbank.transaction_doc(late, me)
    assert doc["ДатаСписано"] == "2026-10-06" and doc["_card"] and "9876" in doc["_purpose"][0]
    assert connect._classify_doc(doc, me, "")["kind"] == "card"
    assert tbank.transaction_doc({**late, "typeOfOperation": "Other"}, me) is None
    assert http.currency("643") == "RUB" and http.currency("RUR") == "RUB" and http.currency("", "40702840" + "0" * 12) == "USD"
    credit = fintech.transaction_doc(s_txn(amount=10.5, debit=False, inn="7701234567", name="ООО «Плательщик»",
                                           purpose="Возврат"), me, date(2026, 10, 7))
    assert credit["ПолучательСчет"] == me and credit["ДатаПоступило"] == "2026-10-07" and credit["Сумма"] == "10.50"
    assert credit["ПлательщикИНН"] == "7701234567"
    assert modul.transaction_doc(m_op(day=date(2026, 10, 8), amount=5.0, purpose="x", status="Canceled"), me) is None
    with pytest.raises(http.BankApiError):
        fintech.safe_account("4070281../../x")


def test_client_certificate_pem_p12_password_mismatch_and_expiry(cert_pair):
    cert_pem, key_pem, key, cert = cert_pair
    pem = clientcert.normalize(cert_pem + key_pem)
    assert "BEGIN CERTIFICATE" in pem and "BEGIN PRIVATE KEY" in pem
    info = clientcert.info(pem)
    assert info["until"] == cert.not_valid_after_utc.date().isoformat() and len(info["fingerprint"]) == 16
    p12 = pkcs12.serialize_key_and_certificates(b"synthetic", key, cert, None,
                                                serialization.BestAvailableEncryption(b"pass-synthetic"))
    b64 = base64.b64encode(p12).decode()
    assert "BEGIN PRIVATE KEY" in clientcert.normalize(p12_b64=b64, password="pass-synthetic")
    with pytest.raises(clientcert.CertError, match="пароль"):
        clientcert.normalize(p12_b64=b64, password="wrong")
    enc_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(b"k-pass")).decode()
    with pytest.raises(clientcert.CertError, match="паролем"):
        clientcert.normalize(cert_pem + enc_key)
    assert "BEGIN PRIVATE KEY" in clientcert.normalize(cert_pem + enc_key, password="k-pass")
    other_cert, _okey, _o, _oc = make_cert()
    with pytest.raises(clientcert.CertError, match="не подходит"):
        clientcert.normalize(other_cert + key_pem)
    old_cert, old_key, _ok, _oc2 = make_cert(days=-1)
    with pytest.raises(clientcert.CertError, match="истёк"):
        clientcert.normalize(old_cert + old_key)
    ctx = http.ssl_context(pem)                                             # сертификат подключается к TLS
    assert ctx is http.ssl_context(pem)                                     # контекст переиспользуется
    with pytest.raises(http.BankApiError) as err:
        http.ssl_context("-----BEGIN CERTIFICATE-----\nbroken\n-----END CERTIFICATE-----\n")
    assert err.value.kind == "tls"


def test_registry_meta_and_read_only_addresses():
    assert list(providers.PROVIDERS) == ["tochka", "tbank", "sber", "alfa", "modul"]
    assert connect.BANKS["sber"] == "Сбербанк" and connect.METHODS["refresh"].startswith("OAuth 2.0")
    for bank in ("tbank", "sber", "alfa", "modul"):
        meta = providers.meta(bank)
        assert meta["how"] and meta["docs"].startswith("https://") and meta["methods"]
    assert providers.PROVIDERS["sber"]["cert"] == "required" and providers.PROVIDERS["alfa"]["cert"] == "optional"
    assert tbank.API_BASE == "https://business.tbank.ru/openapi"
    assert alfa.API_BASE == "https://baas.alfabank.ru/api" and modul.API_BASE == "https://api.modulbank.ru/v1"
    from app.bank import sber
    assert sber.API_BASE == "https://fintech.sberbank.ru:9443/fintech/api"
    assert sber.TOKEN_URL == "https://fintech.sberbank.ru:9443/ic/sso/api/v2/oauth/token"
    for name in ("http", "tbank", "sber", "alfa", "modul", "fintech", "clientcert", "providers"):
        src = Path(f"app/bank/{name}.py").read_text(encoding="utf-8")
        assert "verify=False" not in src and "CERT_NONE" not in src, name
        assert "/payment" not in src and "/transfer" not in src, name             # только чтение: платежей нет
