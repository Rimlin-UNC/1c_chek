"""T-80 (#65, v1.71.0): база контрагентов — tests-first контракт.

Профессиональная логика, закреплённая здесь: поиск строго по нормализованному ИНН,
значения попадают в блоки дословно из структурированной карточки, незнакомые формы не
склоняются автоматически, чужие счета и чужие карточки недоступны, а любая проверка
показывает человеку предупреждения до подписания. Только синтетика (ADR-14).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import inspect

from app.counterparties import blocks, grammar, sources, verify
from app.counterparties.models import Counterparty, CounterpartyAlias, CounterpartyBankAccount
from app.database import Base
from app.services.companies_util import _INN10_WEIGHTS, _inn_checksum
from tests.conftest import login


def _unique_inn(seed: int, shift: int = 0) -> str:
    """Синтетический ИНН юрлица с верной контрольной цифрой и без повторов."""
    base = f"{seed + shift:09d}"
    return base + str(_inn_checksum(base, _INN10_WEIGHTS))


# ---------------------------------------------------------------------------
#  Чистые функции: падежи, блоки, источники, проверка
# ---------------------------------------------------------------------------
def test_grammar_genitive_org_and_unknown_forms():
    assert grammar.genitive_org("ООО «Ромашка-Строй»") == \
        "Общества с ограниченной ответственностью «Ромашка-Строй»"
    assert grammar.genitive_org("АО «Вектор»") == "Акционерного общества «Вектор»"
    assert grammar.genitive_org("ПАО «Север»") == "Публичного акционерного общества «Север»"
    assert grammar.genitive_org("ИП Иванов Иван Иванович") == \
        "Индивидуального предпринимателя Иванова Ивана Ивановича"
    assert grammar.genitive_org("Неизвестная форма «Х»") == "Неизвестная форма «Х»"
    assert grammar.genitive_person("Иван Иванов") == "Ивана Иванова"


def test_blocks_are_verbatim_and_have_checklist():
    card = {
        "name_full": "ООО «Ромашка»", "name_short": "ООО «Ромашка»",
        "inn": "7704123450", "kpp": "770401001", "ogrn": "1027700132195",
        "address": "г. Москва, ул. Ленина, 1", "opf": "ООО",
        "signatory_post": "Директор", "signatory_name": "Иван Иванов",
        "signatory_basis": "Устава", "genitive": "ООО «Ромашка»",
        "accounts": [{"account": "40702810100000000001", "bik": "044525225",
                      "bank_name": "ПАО Сбербанк", "corr_account": "30101810400000000225"}],
        "risk": {"level": "yellow", "reasons": ["Массовый адрес"]},
    }
    own = {"name_full": "ООО «Ямастер»", "inn": "7700000000", "kpp": "770000000"}
    built = blocks.build_blocks(card, own)
    assert list(built) == ["Реквизиты стороны", "Преамбула договора", "Для платёжного поручения"]
    assert "ИНН/КПП: 7704123450/770401001" in built["Реквизиты стороны"]
    assert "БИК: 044525225" in built["Для платёжного поручения"]
    assert "Счёт №: 40702810100000000001" in built["Для платёжного поручения"]
    assert "ООО «Ромашка»" in built["Преамбула договора"]
    assert "проверьте" in built["Преамбула договора"].lower()
    assert blocks.checklist(card) == ["Массовый адрес"]


def test_sources_from_upd_and_bank_are_candidates_only():
    class Doc:
        company_id = "c1"
        seller_name = "ООО «Продавец»"
        seller_inn = "7704123450"
        seller_kpp = "770401001"
        buyer_name = "ООО «Покупатель»"
        buyer_inn = "5001007329"
        buyer_kpp = "500100001"

    class Op:
        company_id = "c1"
        counterparty_name = "ООО «Получатель»"
        counterparty_inn = "7704123450"

    class Income:
        company_id = "c1"
        counterparty_name = "ООО «Плательщик»"
        counterparty_inn = "5001007329"

    found = sources.collect_candidates([Doc()], [Op()], [Income()], company_id="c1")
    assert {x["inn"] for x in found} == {"7704123450", "5001007329"}
    assert {x["name"] for x in found} >= {"ООО «Продавец»", "ООО «Покупатель»",
                                         "ООО «Получатель»", "ООО «Плательщик»"}
    assert all(x["source"] in ("upd", "bank") for x in found)
    assert sources.collect_candidates([], [], [], company_id="c1") == []


def test_verify_by_inn_uses_injected_checko_and_bik_directory():
    seen = {}

    def fake_fetch(kind, raw):
        seen["kind"] = kind
        seen["raw"] = dict(raw)
        return {"status": "Действующее", "address_invalid": True,
                "mass_address_count": 12, "name_full": "ООО «Проверенное»"}

    result = verify.verify_by_inn("7704123450", fetch=fake_fetch)
    assert seen["kind"] == "legal"
    assert result["risk"]["level"] == "red"
    assert any("Недостоверный" in r for r in result["risk"]["reasons"])
    assert verify.lookup_bik("044525225")["bank_name"] == "ПАО Сбербанк"
    assert verify.lookup_bik("999999999") == {}
    assert verify.verify_by_inn("", fetch=lambda *_: {})["risk"]["level"] == "yellow"


# ---------------------------------------------------------------------------
#  Схема, сервис на SQLite/PostgreSQL, изоляция и дубли
# ---------------------------------------------------------------------------
def test_three_tables_registered(store):
    names = set(Base.metadata.tables)
    assert {"counterparties", "counterparty_bank_accounts", "counterparty_aliases"} <= names
    db = store.session()
    try:
        assert set(inspect(db.bind).get_table_names()) >= {
            "counterparties", "counterparty_bank_accounts", "counterparty_aliases"}
    finally:
        db.close()


def test_service_merge_and_isolation(store):
    from app.counterparties import service
    from app.models import User

    db = store.session()
    try:
        admin = User(id="u1", username="admin", role="admin", company_id="c1")
        primary = service.create_card(db, admin, {"inn": _unique_inn(1), "name": "ООО «Первый»",
                                                  "company_id": "c1"})
        duplicate = service.create_card(db, admin, {"inn": _unique_inn(2), "name": "ООО «Второй»",
                                                    "company_id": "c1"})
        service.add_account(db, admin, duplicate["id"], {
            "account": "40702810100000000002", "bik": "044525225", "company_id": "c1"})
        service.add_alias(db, primary["id"], "ООО «Первый»", "upd")
        service.add_alias(db, duplicate["id"], "ООО «Второй»", "bank")
        merged = service.merge_cards(db, admin, primary["id"], duplicate["id"], company_id="c1")
        assert merged["id"] == primary["id"]
        assert {a["name"] for a in merged["aliases"]} >= {"ООО «Первый»", "ООО «Второй»"}
        assert any(a["account"] == "40702810100000000002" for a in merged["accounts"])
        assert db.get(Counterparty, duplicate["id"]) is None

        other = service.create_card(db, admin, {"inn": _unique_inn(3), "name": "ООО «Чужое»",
                                               "company_id": "c2"})
        accountant = User(id="u2", username="acc", role="accountant", company_id="c1")
        assert service.get_card(db, accountant, other["id"]) is None
        assert [x["id"] for x in service.list_cards(db, accountant)] == [primary["id"]]
        assert [x["id"] for x in service.list_cards(db, admin, company_id="c2")] == [other["id"]]
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
#  API: только бухгалтер/админ, изоляция, проверка, счета, кандидаты
# ---------------------------------------------------------------------------
def _company(client, hdr, prefix="Контрагент"):
    return client.post("/api/v1/companies", json={"name": f"ООО {prefix} {uuid4().hex[:8]}"},
                       headers=hdr).json()


def test_api_create_verify_account_merge_candidates_and_isolation(client):
    hdr = login(client, "admin", "admin123")
    c1 = _company(client, hdr, "Первый")
    c2 = _company(client, hdr, "Второй")
    inn1, inn2 = _unique_inn(11), _unique_inn(12)

    first = client.post("/api/v1/counterparties", json={
        "inn": inn1, "name": "ООО «Ромашка»", "company_id": c1["id"]}, headers=hdr)
    assert first.status_code == 201, first.text
    card1 = first.json()
    assert card1["inn"] == inn1 and card1["blocks"]
    assert card1["risk"]["level"] == "none"

    second = client.post("/api/v1/counterparties", json={
        "inn": inn2, "name": "ООО «Василёк»", "company_id": c1["id"]}, headers=hdr)
    assert second.status_code == 201, second.text
    card2 = second.json()

    patched = client.patch(f"/api/v1/counterparties/{card1['id']}", json={
        "signatory_post": "Директор", "signatory_name": "Иван Иванов",
        "signatory_basis": "Устава", "genitive": "ООО «Ромашка»",
        "company_id": c1["id"]}, headers=hdr)
    assert patched.status_code == 200 and patched.json()["signatory_name"] == "Иван Иванов"

    checked = client.post(f"/api/v1/counterparties/{card1['id']}/verify",
                          json={"company_id": c1["id"]}, headers=hdr)
    assert checked.status_code == 200, checked.text
    assert checked.json()["risk"]["level"] in ("green", "yellow", "red")

    acc = client.post(f"/api/v1/counterparties/{card1['id']}/accounts", json={
        "account": "40702810100000000001", "bik": "044525225",
        "company_id": c1["id"]}, headers=hdr)
    assert acc.status_code == 201, acc.text
    assert acc.json()["bank_name"] == "ПАО Сбербанк"

    merged = client.post(f"/api/v1/counterparties/{card1['id']}/merge", json={
        "duplicate_id": card2["id"], "company_id": c1["id"]}, headers=hdr)
    assert merged.status_code == 200, merged.text
    assert "ООО «Василёк»" in [x["name"] for x in merged.json()["aliases"]]

    candidates = client.get("/api/v1/counterparties/candidates",
                            params={"company_id": c1["id"]}, headers=hdr)
    assert candidates.status_code == 200 and isinstance(candidates.json(), list)

    mine = client.get("/api/v1/counterparties", params={"company_id": c1["id"]}, headers=hdr)
    assert mine.status_code == 200 and [x["id"] for x in mine.json()] == [card1["id"]]
    foreign = client.post("/api/v1/counterparties", json={
        "inn": _unique_inn(13), "name": "ООО «Чужое»", "company_id": c2["id"]}, headers=hdr)
    assert foreign.status_code == 201
    foreign_id = foreign.json()["id"]
    assert client.get(f"/api/v1/counterparties/{foreign_id}",
                      params={"company_id": c1["id"]}, headers=hdr).status_code == 404


def test_api_forbidden_for_employee(client):
    hdr = login(client, "admin", "admin123")
    comp = _company(client, hdr, "Сотрудник")
    from tests.test_v1140 import _mk_user
    user = _mk_user(client, hdr, comp["id"], f"cp_user_{uuid4().hex[:6]}", role="user")
    assert client.get("/api/v1/counterparties", headers=user).status_code == 403
