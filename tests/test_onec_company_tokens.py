"""Интеграция 1С: токен на компанию (T-57, v1.70.92).

До 1.70.92 один общий токен X-API-Token открывал выгрузку всех компаний: pull без
company_id и EnterpriseData отдавали чеки всех компаний, ack отмечал любые. Токен
компании ограничивает область её чеками; общий токен работает как раньше. Токен
хранится только хешем и показывается один раз. Только синтетические данные (ADR-14).
"""
from __future__ import annotations

import datetime as dt
import json
from uuid import uuid4

from app.database import SessionLocal
from app.models import AppSetting, Company, Receipt
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity, _registry

# фикстура компаний и пользователей — общая с tests/test_unified_receipts.py
companies = _base.companies


def _verified(db, company_id):
    fn, fd, fp = _identity()
    receipt = _registry(db, fn, fd, fp, company=db.get(Company, company_id))
    receipt.status, receipt.exported = "verified", False
    db.flush()
    return receipt.id, fn


def _setup(client, hdr, c1, c2):
    with SessionLocal() as db:
        r1, fn1 = _verified(db, c1)
        r2, fn2 = _verified(db, c2)
        db.commit()
    global_token = "global-" + uuid4().hex
    assert client.put("/api/v1/settings/onec", headers=hdr, json={"api_token": global_token}).status_code == 200
    issued = client.post(f"/api/v1/settings/onec/companies/{c1}/token", headers=hdr).json()
    return {"r1": r1, "fn1": fn1, "r2": r2, "fn2": fn2, "global": global_token, "token": issued["token"],
            "issued": issued}


# чеки соседних тестов в общей базе отсекаются по времени создания
STARTED = (dt.datetime.utcnow() - dt.timedelta(seconds=5)).replace(microsecond=0).isoformat()


def _pull(client, token, **params):
    query = "&".join(f"{k}={v}" for k, v in {"limit": 100, "since": STARTED, **params}.items())
    return client.get(f"/onec/v1/receipts/pull?{query}", headers={"X-API-Token": token})


def test_company_token_scopes_pull_ping_and_enterprise_data(companies, client):
    hdr, c1, c2, _, _ = companies
    s = _setup(client, hdr, c1, c2)
    res = _pull(client, s["token"])
    assert res.status_code == 200, res.text
    body = json.dumps(res.json(), ensure_ascii=False)
    assert s["fn1"] in body and s["fn2"] not in body
    assert _pull(client, s["token"], company_id=c2).status_code == 403
    assert _pull(client, s["token"], company_id=c1).status_code == 200
    ed = client.get("/onec/v1/enterprise-data?format=json", headers={"X-API-Token": s["token"]})
    assert ed.status_code == 200 and s["fn2"] not in ed.text
    ping = client.get("/onec/v1/ping", headers={"X-API-Token": s["token"]}).json()
    assert ping["scope"] == "company"


def test_company_token_acks_only_own_receipts(companies, client):
    hdr, c1, c2, _, _ = companies
    s = _setup(client, hdr, c1, c2)
    res = client.post("/onec/v1/receipts/ack", headers={"X-API-Token": s["token"]}, json={"ids": [s["r1"], s["r2"]]})
    assert res.status_code == 200 and res.json()["acknowledged"] == 1
    with SessionLocal() as db:
        assert db.get(Receipt, s["r1"]).exported is True
        assert db.get(Receipt, s["r2"]).exported is False          # чужой чек не отмечен


def test_global_token_keeps_old_behaviour(companies, client):
    hdr, c1, c2, _, _ = companies
    s = _setup(client, hdr, c1, c2)
    body = json.dumps(_pull(client, s["global"]).json(), ensure_ascii=False)
    assert s["fn1"] in body and s["fn2"] in body                   # общий токен видит все компании
    assert client.get("/onec/v1/ping", headers={"X-API-Token": s["global"]}).json()["scope"] == "all"
    assert client.get("/onec/v1/ping", headers={"X-API-Token": "wrong-token"}).status_code == 401


def test_token_is_stored_as_hash_shown_once_and_revocable(companies, client):
    hdr, c1, c2, _, _ = companies
    s = _setup(client, hdr, c1, c2)
    token = s["token"]
    assert s["issued"]["has_token"] is True and s["issued"]["hint"] == token[-4:]
    with SessionLocal() as db:
        values = [row.value for row in db.query(AppSetting).filter(AppSetting.key.like("onec_token%"))]
        assert values and all(token not in v for v in values)     # в базе — только хеш
    listed = client.get("/api/v1/settings/onec/companies", headers=hdr).json()
    assert token not in json.dumps(listed) and listed["global_token"] is True
    row = next(c for c in listed["companies"] if c["company_id"] == c1)
    assert row["has_token"] is True and "hash" not in row
    reissued = client.post(f"/api/v1/settings/onec/companies/{c1}/token", headers=hdr).json()["token"]
    assert _pull(client, token).status_code == 401                 # перевыпуск отзывает прежний
    assert client.delete(f"/api/v1/settings/onec/companies/{c1}/token", headers=hdr).json()["revoked"] is True
    assert _pull(client, reissued).status_code == 401
    assert client.post(f"/api/v1/settings/onec/companies/{uuid4()}/token", headers=hdr).status_code == 404


def test_company_tokens_are_admin_only(companies, client):
    hdr, c1, c2, a1, _ = companies
    _setup(client, hdr, c1, c2)
    acc = a1                                                       # заголовок бухгалтера компании
    assert client.get("/api/v1/settings/onec/companies", headers=acc).status_code == 403
    assert client.post(f"/api/v1/settings/onec/companies/{c1}/token", headers=acc).status_code == 403


def test_global_token_from_settings_is_sealed_and_still_accepted(companies, client):
    """Токен из «Настроек» хранится зашифрованным (enc:v1:…) и всё равно принимается.

    До 1.70.92 проверка сравнивала токен с зашифрованной строкой: токен, заданный или
    перевыпущенный в «Настройках», 1С не принимала (работал только токен окружения).
    """
    hdr, c1, c2, _, _ = companies
    s = _setup(client, hdr, c1, c2)
    with SessionLocal() as db:
        stored = db.get(AppSetting, "onec_api_token").value
    assert stored != s["global"] and s["global"] not in stored         # в базе — не открытый текст
    assert client.get("/onec/v1/ping", headers={"X-API-Token": s["global"]}).status_code == 200
    assert client.put("/api/v1/settings/onec", headers=hdr, json={"regen_token": True}).status_code == 200
    fresh = client.get("/api/v1/settings/onec/reveal", headers=hdr).json()["api_token"]
    assert client.get("/onec/v1/ping", headers={"X-API-Token": fresh}).json()["scope"] == "all"
    assert client.get("/onec/v1/ping", headers={"X-API-Token": s["global"]}).status_code == 401
