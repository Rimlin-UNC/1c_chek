"""«Единая база», фаза 3 (ADR-20, #24, T-35): число организаций для компаний.

Администратор видит число организаций у чека всегда. Компании — только с его
разрешения (Q-20): режим «выключено» (по умолчанию) · «всем компаниям» ·
«выбранным компаниям» (флаг companies.show_companies_count), роли — бухгалтер
и руководитель (Q-21). Только число, только по своим чекам, без названий других
организаций; сотрудник, донор Чек-Пула и платный API (B2B) — никогда.
"""
from __future__ import annotations

import ast
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import Company, User
from app.services import appsettings
from app.unified import links, service, visibility
from app.unified.models import (
    UnifiedCompanyLink,
    UnifiedDocument,
    UnifiedOrigin,
    UnifiedOwnership,
)
from tests.conftest import login
from tests.test_v1140 import _mk_user

ROOT = Path(__file__).resolve().parent.parent


def _triple() -> tuple[str, str, str]:
    return "7" + str(int(uuid4().hex, 16) % 10**15).zfill(15), "3467", "2882574203"


def _qr(fn: str, fd: str, fp: str, total: str = "100.00") -> str:
    return f"t=20260922T1556&s={total}&fn={fn}&i={fd}&fp={fp}&n=1"


def _scan(client, head, qr):
    return client.post("/api/v1/receipts/scan", headers=head, json={"qr_data": qr})


def _company(client, hdr, prefix: str) -> str:
    resp = client.post("/api/v1/companies", headers=hdr,
                       json={"name": f"{prefix} {uuid4().hex[:8]}"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _visibility_snapshot() -> dict[str, str]:
    """Настройки показа — общие для базы тестов: снимок, чтобы вернуть как было."""
    with SessionLocal() as db:
        return {key: appsettings.get_setting(db, key, "")
                for key in (visibility.MODE_KEY, visibility.ROLES_KEY)}


def _visibility_restore(saved: dict[str, str]) -> None:
    with SessionLocal() as db:
        for key, value in saved.items():
            appsettings.set_setting(db, key, value)
        db.commit()


@pytest.fixture
def case(client, monkeypatch):
    """Альфа владеет чеками, Бета претендует; Гамма — компания без флага.
    Настройки показа после теста возвращаются как были."""
    saved = _visibility_snapshot()
    monkeypatch.setattr("app.routers.receipts._maybe_auto_verify", lambda *a: False)
    monkeypatch.setattr("app.routers.receipts._maybe_auto_fetch", lambda *a: False)
    hdr = login(client, "admin", "admin123")
    ca, cb, cc = (_company(client, hdr, name) for name in
                  ("Показ Альфа", "Показ Бета", "Показ Гамма"))
    roles = {"a_user": ("user", ca), "a_acc": ("accountant", ca), "a_man": ("manager", ca),
             "b_user": ("user", cb), "b_acc": ("accountant", cb), "c_acc": ("accountant", cc)}
    heads = {key: _mk_user(client, hdr, comp, f"vis_{key}_{uuid4().hex[:8]}", role=role)
             for key, (role, comp) in roles.items()}
    heads["admin"] = hdr
    client.cookies.clear()
    yield SimpleNamespace(h=heads, ca=ca, cb=cb, cc=cc)
    client.cookies.clear()
    _visibility_restore(saved)


def _receipt(client, case, head_key: str = "a_user") -> SimpleNamespace:
    """Чек компании сотрудника (скан QR — запись в едином реестре создаётся сразу)."""
    qr = _qr(*_triple())
    resp = _scan(client, case.h[head_key], qr)
    assert resp.status_code == 200 and resp.json()["receipt"], resp.text
    rid = resp.json()["receipt"]["id"]
    with SessionLocal() as db:
        doc = service.document_for_source(db, service.REGISTRY, rid)
        assert doc is not None
        return SimpleNamespace(id=rid, doc=doc.id, qr=qr)


def _claim(client, case, receipt, head_key: str = "b_user") -> None:
    """Сотрудник другой компании сканирует тот же чек — отказ и заявка."""
    resp = _scan(client, case.h[head_key], receipt.qr)
    assert links.CLAIM_NOTE in resp.text, resp.text


def _set_visibility(client, case, mode: str, roles=("accountant", "manager"),
                    company_ids=None) -> dict:
    body = {"mode": mode, "roles": list(roles)}
    if company_ids is not None:
        body["company_ids"] = list(company_ids)
    resp = client.put("/api/v1/unified/visibility", headers=case.h["admin"], json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _items(client, head, **params) -> dict:
    resp = client.get("/api/v1/receipts", headers=head, params={"page_size": 200, **params})
    assert resp.status_code == 200, resp.text
    return {item["id"]: item for item in resp.json()["items"]}


def _card(client, head, receipt_id: str):
    return client.get(f"/api/v1/receipts/{receipt_id}", headers=head)


def _admin_count(client, case, doc_id: str) -> int:
    resp = client.get(f"/api/v1/unified/documents/{doc_id}/companies", headers=case.h["admin"])
    assert resp.status_code == 200, resp.text
    return resp.json()["count"]


def _decide(client, case, receipt, company_id: str, action: str) -> None:
    payload = client.get(f"/api/v1/unified/documents/{receipt.doc}/companies",
                         headers=case.h["admin"]).json()
    link = next(c for c in payload["companies"] if c["company_id"] == company_id and c["link_id"])
    resp = client.post(f"/api/v1/unified/links/{link['link_id']}/{action}", headers=case.h["admin"],
                       json={"expected_version": payload["version"], "reason": "Решение администратора"})
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------
#  Кто видит число
# --------------------------------------------------------------------------
def test_default_off_admin_sees_staff_does_not(client, case):
    """По умолчанию показ выключен: администратор видит число, компания — нет."""
    _set_visibility(client, case, "off")
    receipt = _receipt(client, case)
    _claim(client, case, receipt)
    assert _items(client, case.h["admin"])[receipt.id]["companies_count"] == 2
    assert _card(client, case.h["admin"], receipt.id).json()["companies_count"] == 2
    for key in ("a_acc", "a_man", "a_user"):
        assert "companies_count" not in _items(client, case.h[key])[receipt.id], key
        assert "companies_count" not in _card(client, case.h[key], receipt.id).json(), key


def test_mode_all_shows_number_only_to_allowed_roles(client, case):
    """«Всем компаниям»: бухгалтер и руководитель — да, сотрудник — никогда."""
    receipt = _receipt(client, case)
    _claim(client, case, receipt)
    _set_visibility(client, case, "all")
    for key in ("a_acc", "a_man"):
        assert _items(client, case.h[key])[receipt.id]["companies_count"] == 2, key
        assert _card(client, case.h[key], receipt.id).json()["companies_count"] == 2, key
    assert "companies_count" not in _items(client, case.h["a_user"])[receipt.id]
    assert "companies_count" not in _card(client, case.h["a_user"], receipt.id).json()
    # чужой чек компания не видит вовсе — ни строки, ни числа
    assert receipt.id not in _items(client, case.h["b_acc"])


def test_mode_selected_uses_company_flag(client, case):
    """«Выбранным компаниям»: только компаниям с отметкой."""
    receipt = _receipt(client, case)
    _claim(client, case, receipt)
    own_beta = _receipt(client, case, "b_user")
    payload = _set_visibility(client, case, "selected", company_ids=[case.cb])
    assert {c["id"] for c in payload["companies"] if c["enabled"]} == {case.cb}
    assert "companies_count" not in _items(client, case.h["a_acc"])[receipt.id]
    assert _items(client, case.h["b_acc"])[own_beta.id]["companies_count"] == 1
    assert all("companies_count" not in item for item in _items(client, case.h["c_acc"]).values())
    _set_visibility(client, case, "selected", company_ids=[case.ca])
    assert _items(client, case.h["a_acc"])[receipt.id]["companies_count"] == 2
    assert "companies_count" not in _items(client, case.h["b_acc"])[own_beta.id]


def test_roles_setting_limits_who_sees(client, case):
    """Роли из настройки; «ни одной роли» сохраняется и читается как есть."""
    receipt = _receipt(client, case)
    _set_visibility(client, case, "all", roles=["accountant"])
    assert _items(client, case.h["a_acc"])[receipt.id]["companies_count"] == 1
    assert "companies_count" not in _items(client, case.h["a_man"])[receipt.id]
    payload = _set_visibility(client, case, "all", roles=[])
    assert payload["roles"] == []
    assert client.get("/api/v1/unified/visibility", headers=case.h["admin"]).json()["roles"] == []
    with SessionLocal() as db:
        assert appsettings.get_setting(db, visibility.ROLES_KEY) == visibility.NO_ROLES
    for key in ("a_acc", "a_man"):
        assert "companies_count" not in _items(client, case.h[key])[receipt.id], key


def test_number_only_for_own_receipts(client, case):
    """Число — только по своим чекам и без названий других организаций."""
    receipt = _receipt(client, case)
    _claim(client, case, receipt)
    _set_visibility(client, case, "all")
    foreign = _card(client, case.h["b_acc"], receipt.id)
    assert foreign.status_code in (403, 404) and "companies_count" not in foreign.text
    with SessionLocal() as db:
        beta_name = db.get(Company, case.cb).name
    own = _card(client, case.h["a_acc"], receipt.id)
    assert own.json()["companies_count"] == 2
    assert beta_name not in own.text
    assert beta_name not in client.get("/api/v1/receipts", headers=case.h["a_acc"],
                                       params={"page_size": 200}).text


# --------------------------------------------------------------------------
#  Как считается
# --------------------------------------------------------------------------
def test_counting_rules_match_admin_list(client, case):
    """Реестр считает так же, как список администратора (п. 10.1 проекта)."""
    _set_visibility(client, case, "all")
    declined, voided, given, crowded = (_receipt(client, case) for _ in range(4))
    for receipt in (declined, voided, given, crowded):
        _claim(client, case, receipt)
    _claim(client, case, crowded, "c_acc")
    _decide(client, case, declined, case.cb, "decline")      # «отклонена» — в числе
    _decide(client, case, voided, case.cb, "void")           # «не учитывать» — нет
    _decide(client, case, given, case.cb, "transfer")        # прежний владелец — нет
    expected = {declined.id: 2, voided.id: 1, given.id: 1, crowded.id: 3}
    admin = _items(client, case.h["admin"])
    with SessionLocal() as db:
        counts = links.companies_counts(db, list(expected))
    for receipt in (declined, voided, given, crowded):
        assert admin[receipt.id]["companies_count"] == expected[receipt.id]
        assert counts[receipt.id] == expected[receipt.id]
        assert _admin_count(client, case, receipt.doc) == expected[receipt.id]
    # переданный чек теперь у Беты: её бухгалтер видит 1, Альфа его не видит
    assert _items(client, case.h["b_acc"])[given.id]["companies_count"] == 1
    assert given.id not in _items(client, case.h["a_acc"])


@pytest.mark.usefixtures("client")
def test_receipt_outside_unified_layer_counts_as_one():
    """Чек архива без единой записи учитывает только своя компания."""
    with SessionLocal() as db:
        assert links.companies_counts(db, ["нет-такого-чека"]) == {"нет-такого-чека": 1}
        assert links.companies_counts(db, []) == {}


def test_admin_filter_multi_company(client, case):
    """Фильтр «Организаций: две и больше» — только у администратора."""
    multi, single = _receipt(client, case), _receipt(client, case)
    _claim(client, case, multi)
    found = _items(client, case.h["admin"], multi_company="true")
    assert multi.id in found and single.id not in found
    assert all(item["companies_count"] >= 2 for item in found.values())
    staff = _items(client, case.h["a_acc"], multi_company="true")
    assert {multi.id, single.id} <= set(staff)               # штатной роли фильтр не применяется


def test_visibility_api_is_admin_only_and_validated(client, case):
    assert client.get("/api/v1/unified/visibility", headers=case.h["a_acc"]).status_code == 403
    assert client.put("/api/v1/unified/visibility", headers=case.h["a_acc"],
                      json={"mode": "all"}).status_code == 403
    assert client.put("/api/v1/unified/visibility", headers=case.h["admin"],
                      json={"mode": "everyone"}).status_code == 422
    assert client.put("/api/v1/unified/visibility", headers=case.h["admin"],
                      json={"mode": "all", "roles": ["user"]}).status_code == 422
    payload = _set_visibility(client, case, "selected", roles=["accountant"], company_ids=[case.cb])
    assert (payload["mode"], payload["roles"]) == ("selected", ["accountant"])
    got = client.get("/api/v1/unified/visibility", headers=case.h["admin"]).json()
    assert {c["id"] for c in got["companies"] if c["enabled"]} >= {case.cb}
    assert case.ca not in {c["id"] for c in got["companies"] if c["enabled"]}
    feed = client.get("/api/v1/dashboard/recent", headers=case.h["a_acc"], params={"limit": 100}).json()
    assert not any(item["action"] == "unified_visibility_updated" for item in feed)
    admin_feed = client.get("/api/v1/dashboard/recent", headers=case.h["admin"],
                            params={"limit": 100}).json()
    assert any(item["action"] == "unified_visibility_updated" for item in admin_feed)


def test_list_counts_do_not_grow_with_page(client, case):
    """Число считается одним запросом на страницу — без N+1."""
    _set_visibility(client, case, "all")
    seen: list[str] = []

    def listener(_conn, _cursor, statement, *_args):
        if "unified_company_links" in statement:
            seen.append(statement)

    sizes = {}
    for batch in (2, 10):
        for _ in range(batch):
            _claim(client, case, _receipt(client, case))
        event.listen(engine, "before_cursor_execute", listener)
        try:
            items = _items(client, case.h["a_acc"])
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        sizes[len(items)] = len(seen)
        seen.clear()
    assert list(sizes.values()) == [1, 1], sizes


def test_b2b_never_gets_the_number():
    """Платный API — отдельный контур: поле не появляется ни в одном его ответе."""
    sources = list((ROOT / "app" / "b2b").rglob("*.py"))
    assert sources and not any("companies_count" in p.read_text(encoding="utf-8") for p in sources)
    produced = [p.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*.py")
                if '"companies_count"' in p.read_text(encoding="utf-8")]
    assert produced == ["app/routers/receipts.py"]


def test_ui_wiring_in_registry_and_move_dialog():
    """Интерфейс: значок, строка карточки, фильтр администратора, отказ перемещения."""
    import re
    app = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    imported = re.search(r"import \{([^}]*)\} from './unified_review\.js';", app)
    assert imported and {"blockedMoveSummary", "orgCountChip", "orgCountRow"} <= {
        name.strip() for name in imported.group(1).split(",")}
    assert app.count("orgCountChip(r.companies_count, isAdmin())") == 2     # строка и карточка
    assert app.count("orgCountRow(r.companies_count, isAdmin())") == 1
    # фильтр «Организаций» — только администратору, переживает перестроение вида
    assert re.search(r"\$\{isAdmin\(\) \? `<label class=\"field\"><span>Организаций 🏢</span>", app)
    assert app.count("['#f-multi', 'multi_company']") == 2 and "multi_company: ''," in app
    move = app[app.index("function openMoveDialog()"):app.index("function bindShell()")]
    assert "skip_blocked: true" in move and "blockedMoveSummary(e.data && e.data.detail)" in move
    assert 'href="#/conflicts"' in move and 'id="mv-rest"' in move
    review = (ROOT / "app" / "static" / "js" / "unified_review.js").read_text(encoding="utf-8")
    assert "'/api/v1/unified/visibility'" in review and 'id="ur-tab-visibility"' in review
    bundle = (ROOT / "tests" / "test_v1121.py").read_text(encoding="utf-8")
    assert '"unified_review.js", "app.js"' in bundle          # стенду DOM нужен модуль значка


def test_browser_scenario_covers_phase3():
    """Playwright: администратор разрешает показ, бухгалтер видит только число (дизайн, §7)."""
    spec = (ROOT / "tests" / "e2e" / "app.spec.js").read_text(encoding="utf-8")
    block = spec.split("test('число организаций:", 1)[1]
    for needle in ("#f-multi", "#ur-tab-visibility", "#ur-vis-mode-selected", "e2e_acc_alpha",
                   "toHaveText('🏢 2')", "#rd-org-count", "not.toContainText('E2E Бета')"):
        assert needle in block, needle
    seed = (ROOT / "qa" / "playwright_seed.py").read_text(encoding="utf-8")
    # заявка «отклонена»: в числе организаций, но не в значке меню сценария разбора
    assert "links.LINK_CLAIM, 'declined'" in seed and "seed_if_needed()" in seed


# --------------------------------------------------------------------------
#  Схема: колонка со значением по умолчанию в базе (откат на 1.70.80–1.70.85)
# --------------------------------------------------------------------------
def _migrations_for(table: str) -> list[tuple[str, str]]:
    """Команды _ensure_schema для таблицы — из исходника (их выполняет старт)."""
    tree = ast.parse((ROOT / "app" / "database.py").read_text(encoding="utf-8"))
    func = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_ensure_schema")
    mapping = next(node.value for node in ast.walk(func)
                   if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                   and any(isinstance(t, ast.Name) and t.id == "migrations" for t in node.targets))
    for key, value in zip(mapping.keys, mapping.values):
        if isinstance(key, ast.Constant) and key.value == table:
            return ast.literal_eval(value)
    return []


def _new_column_ddl() -> str:
    return dict(_migrations_for("companies"))["show_companies_count"]


def test_new_column_default_works_for_old_inserts(tmp_path):
    """Старая версия (без колонки) создаёт компанию — флаг «нет» из базы."""
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE companies (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))")
        conn.exec_driver_sql(_new_column_ddl())
        conn.exec_driver_sql("INSERT INTO companies (id, name) VALUES ('old', 'Создана старой версией')")
        assert conn.exec_driver_sql("SELECT show_companies_count FROM companies").scalar() in (0, False)
    old.dispose()
    # новая установка: то же значение по умолчанию задаёт сама модель
    column = Company.__table__.c.show_companies_count
    assert column.server_default is not None and not column.nullable


@pytest.mark.skipif(not os.getenv("UNIFIED_POSTGRES_TEST_URL"),
                    reason="не задан UNIFIED_POSTGRES_TEST_URL")
def test_postgres_counts_and_filter():
    """Подсчёт, фильтр и команда добавления колонки — так же на PostgreSQL."""
    pg = create_engine(os.environ["UNIFIED_POSTGRES_TEST_URL"], pool_size=2, max_overflow=0)
    if pg.dialect.name != "postgresql":
        pg.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен указывать на PostgreSQL")
    schema = "qa_phase3_" + uuid4().hex
    tables = [Company.__table__, UnifiedDocument.__table__, UnifiedOwnership.__table__,
              UnifiedOrigin.__table__, UnifiedCompanyLink.__table__]
    try:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            Base.metadata.create_all(bind=conn, tables=tables)
            # база старой версии: таблица компаний без колонки, затем _ensure_schema
            conn.exec_driver_sql("CREATE TABLE old_companies (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))")
            conn.exec_driver_sql(_new_column_ddl().replace("TABLE companies", "TABLE old_companies"))
            conn.exec_driver_sql("INSERT INTO old_companies (id, name) VALUES ('old', 'Старая')")
            assert conn.exec_driver_sql("SELECT show_companies_count FROM old_companies").scalar() is False
        with Session(pg) as db:
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            a, b, c = Company(name="PG Альфа"), Company(name="PG Бета"), Company(name="PG Гамма")
            db.add_all([a, b, c])
            db.flush()
            docs = []
            for k in range(3):
                doc = UnifiedDocument(identity_key=uuid4().hex, fn=str(k), fd="2", fp="3")
                db.add(doc)
                db.flush()
                docs.append(doc)
                db.add(UnifiedOwnership(document_id=doc.id, company_id=a.id))
                db.add(UnifiedOrigin(document_id=doc.id, source_kind="registry", source_id=f"r{k}"))
            now = datetime(2026, 11, 3)
            links._upsert(db, [
                links._row(docs[0].id, b.id, "claim", "open", "scan_qr", attempts=1, first_at=now, last_at=now),
                links._row(docs[0].id, c.id, "claim", "declined", "manual", attempts=1, first_at=now, last_at=now),
                links._row(docs[1].id, b.id, "claim", "void", "scan_qr", attempts=1, first_at=now, last_at=now),
                links._row(docs[2].id, c.id, "former_owner", "open", "move", attempts=0, first_at=now, last_at=now),
            ], None)
            db.flush()
            assert links.companies_counts(db, ["r0", "r1", "r2", "r-archive"]) == {
                "r0": 3, "r1": 1, "r2": 1, "r-archive": 1}
            assert sorted(db.execute(links.multi_company_receipts()).scalars()) == ["r0"]
            assert db.execute(select(Company.show_companies_count).where(Company.id == a.id)).scalar() is False
            db.rollback()
    finally:
        with pg.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.dispose()


def test_user_model_unchanged():
    """Роли пользователей не расширялись: показ — настройка, а не новая роль."""
    assert "show_companies_count" not in User.__table__.c
