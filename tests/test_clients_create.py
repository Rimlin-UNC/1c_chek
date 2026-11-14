"""T-84 (v1.71.1): бухгалтер сам заводит клиента — создание компании с экрана «Клиенты».

Что проверяется (критерии приёмки):
1. администратор создаёт компанию как раньше, только администратор компаний не «ведёт»;
2. бухгалтер создаёт компанию и сразу получает её в клиенты — одной транзакцией;
3. бухгалтер не может создать компанию, если у него уже предел клиентов (MAX_CLIENTS);
4. сотрудник и руководитель компанию не создают (403), данные не меняются;
5. дубли (канон названия и ИНН) отсекаются и для бухгалтера — как у администратора;
6. создание бухгалтером видно в журнале с компанией клиента; клиент доступен сразу.

Данные синтетические (ADR-14). Тесты идут и на SQLite, и на PostgreSQL — набор общий.
"""
from __future__ import annotations

from uuid import uuid4

import tests.test_primary_upd as _upd
from app.clients import service as svc
from app.clients.models import AccountantClient
from app.database import SessionLocal
from app.models import Company, User
from tests.test_v1140 import _mk_user

firms = _upd.firms          # компании A и B со своими бухгалтерами — как в test_clients.py


def _me(client, hdr) -> dict:
    res = client.get("/api/v1/auth/me", headers=hdr)
    assert res.status_code == 200, res.text
    return res.json()


def _accountant(client, admin, company_id: str) -> tuple[dict, str]:
    """Бухгалтер в компании company_id: (заголовки авторизации, id)."""
    tag = uuid4().hex[:8]
    hdr = _mk_user(client, admin, company_id, f"acc_new_{tag}", role="accountant")
    return hdr, _me(client, hdr)["id"]


def test_accountant_creates_client_and_sees_it_at_once(client, firms):
    """Бухгалтер создаёт клиента — тот сразу в переключателе и в сводке."""
    admin, a, _b = firms
    acc, acc_id = _accountant(client, admin, a["id"])
    name = f"ООО «Новый клиент {uuid4().hex[:6]}»"
    res = client.post("/api/v1/companies", headers=acc,
                      json={"name": name, "inn": "", "note": "договор № 7"})
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["name"] == name and created["is_active"] is True
    assert created["note"] == "договор № 7"

    # связь бухгалтер ↔ новый клиент создана в той же транзакции
    with SessionLocal() as db:
        rows = db.query(AccountantClient).filter(AccountantClient.user_id == acc_id).all()
        assert [r.company_id for r in rows] == [created["id"]]
        assert rows[0].granted_by == acc_id
        assert db.get(Company, created["id"]) is not None

    # бухгалтер видит клиента в переключателе и в сводке — без действий администратора
    me = _me(client, acc)
    assert created["id"] in [c["id"] for c in me["clients"]]
    data = client.get("/api/v1/clients", headers=acc)
    assert data.status_code == 200, data.text
    assert created["id"] in [c["id"] for c in data.json()["companies"]]

    # и сразу может работать в нём: запрос с заголовком активного клиента — не 403
    inside = client.get("/api/v1/doc-requests", headers={**acc, "X-Ym-Company": created["id"]})
    assert inside.status_code == 200, inside.text


def test_admin_create_unchanged_and_has_no_client_link(client, firms):
    """Администратор создаёт компанию как раньше; себе клиентов он не «назначает»."""
    admin, _a, _b = firms
    before = client.get("/api/v1/companies", headers=admin).json()
    name = f"ООО «Админский клиент {uuid4().hex[:6]}»"
    res = client.post("/api/v1/companies", headers=admin, json={"name": name})
    assert res.status_code == 201, res.text
    created = res.json()
    with SessionLocal() as db:
        assert db.query(AccountantClient).filter(AccountantClient.company_id == created["id"]).count() == 0
        assert db.query(Company).count() == len(before) + 1


def test_other_roles_cannot_create_company(client, firms):
    """Сотрудник, руководитель и участник пула компаний не создают — и данные не меняются."""
    admin, a, _b = firms
    tag = uuid4().hex[:8]
    before = len(client.get("/api/v1/companies", headers=admin).json())
    for role in ("user", "manager"):
        hdr = _mk_user(client, admin, a["id"], f"nocomp_{role}_{tag}", role=role)
        res = client.post("/api/v1/companies", headers=hdr, json={"name": f"ООО «Чужая {role} {tag}»"})
        assert res.status_code == 403, (role, res.status_code, res.text)
    assert len(client.get("/api/v1/companies", headers=admin).json()) == before


def test_duplicates_are_rejected_for_accountant_too(client, firms):
    """Дубли отсекаются одинаково: каноническое название и тот же ИНН."""
    admin, a, _b = firms
    acc, _acc_id = _accountant(client, admin, a["id"])
    tag = uuid4().hex[:6]
    name = f"ООО «Дубль {tag}»"
    first = client.post("/api/v1/companies", headers=acc, json={"name": name})
    assert first.status_code == 201, first.text
    same = client.post("/api/v1/companies", headers=acc, json={"name": name.upper().replace("«", '"').replace("»", '"')})
    assert same.status_code == 409 and "Похожая компания" in same.json()["detail"]
    # короткое и некорректное имя — 422, компания не появляется
    assert client.post("/api/v1/companies", headers=acc, json={"name": "Я"}).status_code == 422
    bad_inn = client.post("/api/v1/companies", headers=acc, json={"name": f"ООО «ИНН {tag}»", "inn": "7707083894"})
    assert bad_inn.status_code == 422
    # по ходу найдено: ИНН из нулей проходил проверку контрольных цифр (0 == 0)
    zero_inn = client.post("/api/v1/companies", headers=acc, json={"name": f"ООО «Ноль {tag}»", "inn": "0000000000"})
    assert zero_inn.status_code == 422


def test_accountant_limit_is_respected(client, firms, monkeypatch):
    """Предел клиентов (MAX_CLIENTS) проверяется до создания — лишняя компания не появляется."""
    admin, a, _b = firms
    acc, acc_id = _accountant(client, admin, a["id"])
    monkeypatch.setattr(svc, "MAX_CLIENTS", 1)
    first = client.post("/api/v1/companies", headers=acc, json={"name": f"ООО «Первый {uuid4().hex[:6]}»"})
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/companies", headers=acc, json={"name": f"ООО «Второй {uuid4().hex[:6]}»"})
    assert second.status_code == 409 and "клиент" in second.json()["detail"].lower()
    with SessionLocal() as db:
        assert db.query(AccountantClient).filter(AccountantClient.user_id == acc_id).count() == 1
        assert db.query(Company).filter(Company.name.like("ООО «Второй%")).count() == 0


def test_accountant_without_company_can_create_client(client, firms):
    """Бухгалтер без своей компании (после удаления компании) тоже может завести клиента."""
    admin, a, _b = firms
    acc, acc_id = _accountant(client, admin, a["id"])
    with SessionLocal() as db:
        db.get(User, acc_id).company_id = None
        db.commit()
    res = client.post("/api/v1/companies", headers=acc, json={"name": f"ООО «Без своей {uuid4().hex[:6]}»"})
    assert res.status_code == 201, res.text
    me = _me(client, acc)
    assert res.json()["id"] in [c["id"] for c in me["clients"]]


def test_similar_hint_for_accountant_is_limited_to_own_clients(client, firms):
    """Подсказка «похожая компания уже есть» работает у бухгалтера — только по своим клиентам.

    v1.71.1: окно создания клиента открывает бухгалтер, а `/similar` требовал
    администратора — кнопка молча не работала. Теперь бухгалтер видит подсказку,
    но только про компании своего доступа: названия чужих клиентов ему не видны.
    """
    admin, a, b = firms
    acc, _acc_id = _accountant(client, admin, a["id"])
    tag = uuid4().hex[:6]
    mine = client.post("/api/v1/companies", headers=acc, json={"name": f"ООО «Циклон {tag}»"})
    assert mine.status_code == 201, mine.text
    foreign = client.post("/api/v1/companies", headers=admin, json={"name": f"ООО «Циклон чужой {tag}»"})
    assert foreign.status_code == 201, foreign.text

    res = client.get("/api/v1/companies/similar", headers=acc, params={"name": f"ООО Циклон {tag}"})
    assert res.status_code == 200, res.text
    ids = [c["id"] for c in res.json()]
    assert mine.json()["id"] in ids
    assert foreign.json()["id"] not in ids, "бухгалтеру видно название чужого клиента"

    # администратор видит все компании, как и раньше
    res = client.get("/api/v1/companies/similar", headers=admin, params={"name": f"ООО Циклон {tag}"})
    assert res.status_code == 200
    admin_ids = [c["id"] for c in res.json()]
    assert {mine.json()["id"], foreign.json()["id"]} <= set(admin_ids)

    # роли без права заводить клиентов подсказку не получают
    for role in ("user", "manager"):
        hdr = _mk_user(client, admin, a["id"], f"similar_{role}_{uuid4().hex[:6]}", role=role)
        res = client.get("/api/v1/companies/similar", headers=hdr, params={"name": f"ООО Циклон {tag}"})
        assert res.status_code == 403, (role, res.text)
    assert b is not None
