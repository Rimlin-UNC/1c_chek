"""Контрактные тесты «интерфейс ↔ API» (T-41, v1.70.110).

Урок 1.70.89/1.70.90: тест правки чека слал минимальное тело, а форма правки шлёт все
поля сразу, — потерю данных Чек-Пула тесты не заметили до 1.70.90. Теперь тела берутся
из самого интерфейса: tests/ui_api_calls.mjs разбирает app/static/js/*.js парсером acorn
и выдаёт каждый вызов API — метод, путь, поля тела с условиями и поля позиций.

Здесь проверяется:
1. каждый вызов интерфейса попадает в маршрут сервера — нет 404 и 405;
2. каждое поле тела сервер принимает: оно объявлено в модели Pydantic эндпоинта
   (лишнее поле Pydantic молча отбрасывает) или, если тело — словарь, читается в коде
   эндпоинта; поля позиций сверяются с моделью позиции;
3. PATCH-правка не шлёт стёртое поле как null — для сервера это «не менять»;
4. каждое поле модели правки чека обрабатывает PATCH;
5. сквозные прогоны форм чека с телом, которое собирает интерфейс (пустые поля — пустой
   строкой), через API на SQLite и PostgreSQL: правка, выгруженный чек, уведомление
   сотрудника, карточка, ручной ввод — единая запись и Чек-Пул ничего не теряют;
6. самопроверка: с правилом очистки 1.70.89 прогон с телом из app.js находит потерю,
   а минимальное тело — нет (ровно урок 1.70.90).

Значения, которые форма кладёт в поля (дату — в часовом поясе браузера), проверяет
tests/js/edit_form_contract.test.js в поясе Челябинска (UTC+5). Только синтетические
данные (ADR-14).
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import typing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth import create_access_token, hash_password
from app.database import Base, SessionLocal, get_db
from app.main import app
from app.models import Company, Receipt, User
from app.pool.models import PoolItem, PoolReceipt
from app.routers import receipts as receipts_router
from app.schemas import ReceiptPatch
from app.services import appsettings
from app.unified import service
from app.unified.models import UnifiedItem
from tests import test_unified_receipts as _base
from tests.test_unified_receipts import _identity, _pool
from tests.test_v1140 import _mk_user

# фикстура компаний и пользователей — общая с tests/test_unified_receipts.py
companies = _base.companies

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "ui_api_calls.mjs"
HTTP = ("get", "post", "put", "patch", "delete")
ADDRESS, CASHIER = "г. Контракт, ул. Формы, 7", "Кассир Контрактов"
SHOP, INN = "Магазин", "4703148343"           # продавец из данных ФНС в Чек-Пуле (_pool)

# PATCH-поля, где null — осознанное «не менять» (ключ: метод, маршрут, поле — причина)
NULL_MEANS_KEEP = {
    ("PATCH", "/api/v1/users/{user_id}", "company_id"):
        "перевод в другую компанию, а не отвязка (v1.11.0): «без компании» — не переводить",
}


# --------------------------------------------------------------------------
#  Вызовы интерфейса и маршруты сервера
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ui_calls() -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен: контракт проверяется в CI (Node 22) и после qa/setup_dev.sh")
    if not (ROOT / "node_modules" / "acorn").is_dir():
        pytest.skip("нет node_modules/acorn: выполните npm ci")
    done = subprocess.run([node, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, f"tests/ui_api_calls.mjs упал:\n{done.stderr}"
    return json.loads(done.stdout)


def _api_routes(routes, prefix: str = ""):
    """Маршруты API в порядке регистрации.

    FastAPI 0.16x хранит подключённый роутер узлом (_IncludedRouter) — раскрываем его;
    прежние версии отдают маршруты плоским списком. Полноту обхода сверяет
    test_route_list_matches_openapi: если устройство роутера снова изменится, тест
    упадёт с понятным сообщением, а не сузит контракт молча.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from _api_routes(inner.routes, prefix + (getattr(context, "prefix", "") or ""))


ROUTES = [(path, frozenset(route.methods), route) for path, route in _api_routes(app.routes)]


def _template_rx(template: str) -> re.Pattern:
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile("".join(
        (".+" if p.endswith(":path}") else "[^/]+") if p.startswith("{") else re.escape(p)
        for p in parts))


def _sample(template: str) -> str:
    """Путь маршрута с образцами параметров: «/receipts/{receipt_id}» → «/receipts/x1»."""
    return re.sub(r"\{[^}]+\}", lambda m: "x1/x2" if m.group(0).endswith(":path}") else "x1", template)


def _pieces(url: str) -> list[str]:
    return url.split("?", 1)[0].split("{}")


def _whole(pieces: list[str], i: int) -> bool:
    """«{}» занимает сегмент целиком («/receipts/{}/card»), а не хвост («/receipts{}»)."""
    nxt = pieces[i + 1]
    return pieces[i].endswith("/") and (nxt == "" or nxt.startswith("/"))


def _url_rx(url: str) -> re.Pattern:
    """Путь вызова → выражение: целый сегмент — один и более символов, хвост сегмента
    (строка запроса «'/receipts' + qs», действие «'/pool-company/' + path + qs») — любые."""
    pieces = _pieces(url)
    out = []
    for i, piece in enumerate(pieces):
        out.append(re.escape(piece))
        if i < len(pieces) - 1:
            out.append("[^/]+" if _whole(pieces, i) else "[^/]*")
    return re.compile("".join(out))


def _concrete(url: str) -> str:
    pieces = _pieces(url)
    return "".join(p + ("x1" if i < len(pieces) - 1 and _whole(pieces, i) else "")
                   for i, p in enumerate(pieces))


def _candidates(method: str, url: str):
    """Маршруты, куда может попасть вызов: (совпал путь, совпали путь и метод)."""
    url_rx, concrete = _url_rx(url), _concrete(url)
    by_path = [(path, methods, route) for path, methods, route in ROUTES
               if url_rx.fullmatch(_sample(path)) or _template_rx(path).fullmatch(concrete)]
    return by_path, [hit for hit in by_path if method in hit[1]]


def _model(annotation) -> type[BaseModel] | None:
    """Модель Pydantic из аннотации поля (Optional[...], list[...] раскрываются)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _model(arg)
        if found is not None:
            return found
    return None


def _names(model: type[BaseModel]) -> set[str]:
    names = set()
    for name, field in model.model_fields.items():
        names.add(name)
        if isinstance(field.alias, str):
            names.add(field.alias)
    return names


def _body_model(route: APIRoute):
    """Как эндпоинт принимает JSON-тело: ('model', модель) | ('fields', имена) |
    ('dict', None) — словарь или чтение из Request | ('none', None)."""
    dependant = route.dependant
    params = dependant.body_params
    if not params:
        return ("dict", None) if dependant.request_param_name else ("none", None)
    if len(params) > 1 or getattr(params[0].field_info, "embed", False):
        return "fields", {p.alias for p in params}
    model = _model(params[0].field_info.annotation)
    if model is None or model.model_config.get("extra") == "allow":
        return "dict", None
    return "model", model


def _reads(route: APIRoute, key: str) -> bool:
    """Эндпоинт со словарём в теле читает поле: строка «key» есть в его коде или модуле."""
    pattern = re.compile(r"""["']%s["']""" % re.escape(key))
    if pattern.search(inspect.getsource(route.endpoint)):
        return True
    module = inspect.getmodule(route.endpoint)
    return bool(module is not None and pattern.search(inspect.getsource(module)))


def _body_problem(route: APIRoute, body: dict) -> str:
    """Пустая строка — эндпоинт принимает тело целиком, иначе — что не так."""
    kind, info = _body_model(route)
    keys = body["keys"]
    if kind == "none":
        return f"эндпоинт тело не читает, а форма шлёт поля {_quoted(keys)}"
    if kind == "dict":
        lost = [k for k in keys if not _reads(route, k)]
        return (f"тело — словарь, а поля {_quoted(lost)} в коде эндпоинта "
                f"{route.endpoint.__name__} не встречаются — сервер их не прочитает") if lost else ""
    names = info if kind == "fields" else _names(info)
    title = "эндпоинт" if kind == "fields" else f"модель {info.__name__}"
    lost = [k for k in keys if k not in names]
    if lost:
        return (f"поля {_quoted(lost)} не принимает {title} — Pydantic молча отбросит их, "
                f"значение пропадёт")
    if kind == "model":
        for key, inner in body["nested"].items():
            sub = info.model_fields.get(key)
            sub_model = _model(sub.annotation) if sub is not None else None
            if sub_model is None:
                continue
            lost = [k for k in inner["keys"] if k not in _names(sub_model)]
            if lost:
                return (f"поля {_quoted(lost)} в «{key}» не принимает модель {sub_model.__name__} — "
                        f"Pydantic молча отбросит их")
    return ""


def _quoted(keys) -> str:
    return ", ".join(f"«{k}»" for k in keys)


def _where(call: dict, url: str) -> str:
    return f'{call["file"]}:{call["line"]} {call["method"]} {url} ({call["func"] or "—"})'


# --------------------------------------------------------------------------
#  1–4. Статический контракт: маршруты, поля, null, обработка полей
# --------------------------------------------------------------------------
def test_route_list_matches_openapi():
    """Обход маршрутов видит всё, что публикует схема OpenAPI."""
    known = {(path, method) for path, methods, _ in ROUTES for method in methods}
    missing = [f"{m.upper()} {p}" for p, item in app.openapi()["paths"].items()
               for m in item if m in HTTP and (p, m.upper()) not in known]
    assert len(ROUTES) >= 300 and not missing, (
        "Обход маршрутов FastAPI пропустил операции схемы OpenAPI — изменилось устройство "
        "роутера, поправьте _api_routes:\n" + "\n".join(missing[:20]))


def test_ui_calls_are_extracted(ui_calls):
    """Разбор интерфейса жив: вызовов сотни, путей без значения в коде — единицы."""
    assert len(ui_calls) >= 300, len(ui_calls)
    assert sum(1 for c in ui_calls if c["body"]["kind"] == "json" and c["body"]["keys"]) >= 100
    dynamic = [f'{c["file"]}:{c["line"]}' for c in ui_calls if c["dynamic"]]
    assert len(dynamic) <= 8, "Слишком много вызовов с путём без значения в коде: " + ", ".join(dynamic)
    edit = _call(ui_calls, "openEditReceipt", "PATCH", "/api/v1/receipts/{}")
    assert {"merchant_name", "merchant_address", "cashier", "comment", "items"} <= set(edit["body"]["keys"])
    assert edit["body"]["nested"]["items"]["kind"] == "array"


def test_every_ui_call_hits_a_route(ui_calls):
    problems = []
    for call in ui_calls:
        for url in call["urls"]:
            by_path, hits = _candidates(call["method"], url)
            if not hits:
                why = "метод не поддерживается (405)" if by_path else "такого маршрута нет (404)"
                problems.append(f"{_where(call, url)} — {why}")
    assert not problems, "Интерфейс вызывает API, которого нет на сервере:\n" + "\n".join(problems)


def test_every_ui_body_field_is_accepted_by_the_server(ui_calls):
    """Новое поле формы без обработки на сервере — падение с понятным сообщением."""
    problems = []
    for call in ui_calls:
        body = call["body"]
        if body["kind"] != "json" or not body["keys"]:
            continue
        for url in call["urls"]:
            _, hits = _candidates(call["method"], url)
            if not hits:
                continue                      # о маршруте сообщит соседний тест
            verdicts = [_body_problem(route, body) for _, _, route in hits]
            if all(verdicts):                 # ни один подходящий маршрут не принимает тело
                problems.append(f"{_where(call, url)}: {min(verdicts, key=len)}")
    assert not problems, (
        "Форма шлёт поля, которые сервер не обработает. Добавьте поле в модель и обработку "
        "на сервере или уберите его из формы:\n" + "\n".join(problems))


def test_patch_never_sends_a_cleared_field_as_null(ui_calls):
    """«x || null» в PATCH: для сервера null — «не менять», стереть значение нельзя."""
    problems = []
    for call in ui_calls:
        if call["method"] != "PATCH":
            continue
        for url in call["urls"]:
            _, hits = _candidates("PATCH", url)
            for key in call["body"]["nullish"]:
                if not any((("PATCH", path, key) in NULL_MEANS_KEEP) for path, _, _ in hits):
                    problems.append(f"{_where(call, url)}: поле «{key}» уходит как null, когда его стёрли")
    assert not problems, (
        "Стёртое поле должно уходить пустой строкой, иначе «Сохранено», а значение осталось:\n"
        + "\n".join(problems))


def test_receipt_patch_handles_every_model_field():
    """Объявленное, но забытое в PATCH поле сервер принял бы и молча потерял."""
    source = inspect.getsource(receipts_router.patch_receipt)
    missed = [f for f in ReceiptPatch.model_fields if f"body.{f}" not in source and f'"{f}"' not in source]
    assert not missed, f"PATCH /api/v1/receipts/{{id}} не обрабатывает поля модели: {_quoted(missed)}"


# --------------------------------------------------------------------------
#  5. Сквозные прогоны форм чека через API (SQLite и PostgreSQL)
# --------------------------------------------------------------------------
@dataclass
class Backend:
    """База для прогона: сессии, бухгалтер компании, сотрудник, компания, администратор."""
    name: str
    session: Callable[[], Session]
    staff: dict
    employee: dict
    employee_id: str
    company_id: str
    admin: dict


def _call(ui_calls, func: str, method: str, url: str) -> dict:
    found = [c for c in ui_calls if c["func"] == func and c["method"] == method and url in c["urls"]]
    assert len(found) == 1, f"в {func} ожидался один вызов {method} {url}, найдено {len(found)}"
    return found[0]


def _calls(ui_calls, func: str, method: str, url: str) -> list[dict]:
    return [c for c in ui_calls if c["func"] == func and c["method"] == method and url in c["urls"]]


def _scanned_after_pool(db: Session, company_id: str, created_by: str | None = None,
                        *, with_pool: bool = True) -> tuple[str, str | None]:
    """Чек сначала в Чек-Пуле (данные ФНС: продавец, адрес, кассир, позиция), затем
    компания сканирует QR: у копии реестра нет ни продавца, ни позиций — как в 1.70.89."""
    fn, fd, fp = _identity()
    pool_id = None
    if with_pool:
        pool = _pool(db, fn, fd, fp)
        pool.merchant_address, pool.cashier = ADDRESS, CASHIER
        pool.verified_at = datetime(2026, 9, 22, 16, 0)     # проверен ФНС в день покупки
        db.flush()
        service.register_source(db, service.POOL, pool, None, strict=True)
        pool_id = pool.id
    copy = Receipt(fn=fn, fd=fd, fp=fp, qr_data=f"t=20260922T1556&s=100.00&fn={fn}&i={fd}&fp={fp}&n=1",
                   receipt_date=datetime(2026, 9, 22, 15, 56), total_sum=100.0, operation=1,
                   company_id=company_id, created_by=created_by)
    db.add(copy)
    db.flush()
    service.register_source(db, service.REGISTRY, copy, None, strict=True)
    db.commit()
    return copy.id, pool_id


COPY_FIELDS = ("merchant_name", "merchant_inn", "merchant_address", "cashier", "receipt_date",
               "total_sum", "operation", "fn", "fd", "fp", "assignee", "category", "comment",
               "notified", "personal_sum")
SHARED_FIELDS = ("merchant_name", "merchant_inn", "merchant_address", "cashier", "receipt_date",
                 "total_sum", "operation")


def _plain(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, float):
        return round(value, 2)
    return "" if value is None else value


def _snapshot(backend: Backend, receipt_id: str, pool_id: str | None) -> dict[str, object]:
    """Всё, что форма могла бы потерять: копия чека, единая запись, Чек-Пул."""
    with backend.session() as db:
        receipt = db.get(Receipt, receipt_id)
        doc = service.document_for_source(db, service.REGISTRY, receipt_id)
        snap = {f"копия чека: {f}": _plain(getattr(receipt, f)) for f in COPY_FIELDS}
        snap["копия чека: позиций"] = len(receipt.items)
        snap.update({f"единая запись: {f}": _plain(getattr(doc, f)) for f in SHARED_FIELDS})
        snap["единая запись: позиций"] = db.query(UnifiedItem).filter(
            UnifiedItem.document_id == doc.id).count()
        if pool_id:
            pool = db.get(PoolReceipt, pool_id)
            snap.update({f"Чек-Пул: {f}": _plain(getattr(pool, f)) for f in SHARED_FIELDS})
            snap["Чек-Пул: позиций"] = db.query(PoolItem).filter(PoolItem.receipt_id == pool_id).count()
    return snap


def _losses(before: dict, after: dict, expected: dict | None = None) -> list[str]:
    """Расхождения «до/после»: ожидаемые изменения сверяются, остальное — потеря."""
    expected = expected or {}
    out = []
    for key, old in before.items():
        new = after[key]
        if key in expected:
            if new != expected[key]:
                out.append(f"{key}: ожидалось «{expected[key]}», стало «{new}»")
        elif new != old:
            out.append(f"{key}: было «{old}», стало «{new}»")
    return out


# Условия, под которыми форма правки (openEditReceipt) кладёт поле в тело. Новое условие
# в форме — тест падает и просит описать его здесь: изменение формы не пройдёт мимо контракта.
def _edit_conditions(r: dict, *, admin: bool, date_changed: bool, items_dirty: bool) -> dict[str, bool]:
    return {
        "!coreLocked": not (r["exported"] and not admin),
        "dateEl.value && dateEl.value !== dateShown": date_changed,
        "itemsDirty": items_dirty,
    }


def _edit_value(key: str, r: dict, nested: dict):
    """Значение поля так, как форма правки заполняет его из чека r (пустое — пустой строкой)."""
    if key == "items":
        keys = nested["items"]["keys"]
        defaults = {"vat_rate": "none", "vat_sum": 0, "quantity": 1, "price": 0, "total": 0}
        return [{k: it[k] if it.get(k) is not None else defaults.get(k, "") for k in keys}
                for it in r["items"]]
    if key == "receipt_date":
        return (r["receipt_date"] or "")[:16]      # datetime-local: ГГГГ-ММ-ДДTЧЧ:ММ
    if key in ("total_sum", "personal_sum"):
        return float(r.get(key) or 0)
    if key == "operation":
        return int(r.get("operation") or 1)
    if key == "notified":
        return bool(r.get("notified"))
    value = r.get(key)
    if value is None or isinstance(value, str):
        return (value or "").strip()
    raise AssertionError(f"для поля «{key}» формы правки нет правила заполнения — опишите его в _edit_value")


def _edit_body(call: dict, r: dict, *, admin: bool = False, date_changed: bool = False,
               items_dirty: bool = False) -> dict:
    """Тело формы правки чека r — набор полей и условия берутся из app.js."""
    known = _edit_conditions(r, admin=admin, date_changed=date_changed, items_dirty=items_dirty)
    body = {}
    for key in call["body"]["keys"]:
        guards = call["body"]["conditions"].get(key, [])
        unknown = [g for g in guards if g not in known]
        assert not unknown, (f"форма правки кладёт «{key}» в тело при новом условии {unknown} — "
                             f"опишите его в _edit_conditions")
        if all(known[g] for g in guards):
            body[key] = _edit_value(key, r, call["body"]["nested"])
    return body


def _sqlite_backend(client, companies_fixture) -> Backend:
    hdr, c1, _, a1, _ = companies_fixture
    employee = _mk_user(client, hdr, c1, "contract_emp_" + uuid4().hex[:10], role="user")
    me = client.get("/api/v1/auth/me", headers=employee).json()
    return Backend("SQLite", SessionLocal, a1, employee, me["id"], c1, hdr)


@pytest.fixture
def sqlite_backend(client, companies):
    return _sqlite_backend(client, companies)


@pytest.fixture
def pg_backend():
    """Та же программа на PostgreSQL: одноразовая схема, get_db — сессии этой схемы."""
    url = os.getenv("UNIFIED_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("PostgreSQL-вариант: нужен UNIFIED_POSTGRES_TEST_URL (CI, qa/pg_local.py)")
    schema = "qa_contract_" + uuid4().hex
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("UNIFIED_POSTGRES_TEST_URL должен вести в PostgreSQL")
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    pg_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def pg_db():
        db = pg_session()
        try:
            yield db
        finally:
            db.close()

    try:
        Base.metadata.create_all(bind=engine)
        with pg_session() as db:
            company = Company(name="Контракт PG " + uuid4().hex[:8])
            db.add(company)
            db.flush()
            staff = User(username="pg_acc_" + uuid4().hex[:8], password_hash=hash_password("parol123"),
                         role="accountant", company_id=company.id)
            employee = User(username="pg_emp_" + uuid4().hex[:8], password_hash=hash_password("parol123"),
                            role="user", company_id=company.id)
            admin = User(username="pg_adm_" + uuid4().hex[:8], password_hash=hash_password("parol123"),
                         role="admin")
            db.add_all([staff, employee, admin])
            db.commit()
            appsettings.set_setting(db, "pool_enabled", "1")
            backend = Backend("PostgreSQL", pg_session,
                              {"Authorization": "Bearer " + create_access_token(staff)},
                              {"Authorization": "Bearer " + create_access_token(employee)},
                              employee.id, company.id,
                              {"Authorization": "Bearer " + create_access_token(admin)})
        app.dependency_overrides[get_db] = pg_db
        yield backend
    finally:
        app.dependency_overrides.pop(get_db, None)
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def backend(request):
    return request.getfixturevalue(f"{'sqlite' if request.param == 'sqlite' else 'pg'}_backend")


def _replay_edit(client, backend: Backend, call: dict, receipt_id: str, pool_id: str | None,
                 *, changes: dict | None = None, expected: dict | None = None,
                 **form) -> list[str]:
    """Открыть форму правки на чеке, изменить поля, сохранить — что потерялось."""
    r = client.get(f"/api/v1/receipts/{receipt_id}", headers=backend.staff).json()
    body = _edit_body(call, r, **form)
    body.update(changes or {})
    before = _snapshot(backend, receipt_id, pool_id)
    res = client.patch(f"/api/v1/receipts/{receipt_id}", headers=backend.staff, json=body)
    assert res.status_code == 200, f"{backend.name}: {res.status_code} {res.text}"
    return _losses(before, _snapshot(backend, receipt_id, pool_id), expected)


def test_edit_form_save_loses_nothing(client, ui_calls, backend):
    """Главный сценарий 1.70.89: чек по QR после Чек-Пула, правка одного комментария."""
    call = _call(ui_calls, "openEditReceipt", "PATCH", "/api/v1/receipts/{}")
    with backend.session() as db:
        rid, pid = _scanned_after_pool(db, backend.company_id)
    losses = _replay_edit(client, backend, call, rid, pid,
                          changes={"comment": "правка через форму"},
                          expected={"копия чека: comment": "правка через форму"})
    assert not losses, f"{backend.name}: сохранение формы правки потеряло данные:\n" + "\n".join(losses)
    with backend.session() as db:
        doc = service.document_for_source(db, service.REGISTRY, rid)
        assert (doc.merchant_name, doc.merchant_inn, doc.merchant_address, doc.cashier) == (
            SHOP, INN, ADDRESS, CASHIER)


def test_edit_form_with_changed_date_and_items(client, ui_calls, backend):
    """Изменённая дата уходит местным временем кассы, позиции — после правки позиций."""
    call = _call(ui_calls, "openEditReceipt", "PATCH", "/api/v1/receipts/{}")
    with backend.session() as db:
        rid, pid = _scanned_after_pool(db, backend.company_id)
    r = client.get(f"/api/v1/receipts/{rid}", headers=backend.staff).json()
    body = _edit_body(call, r, date_changed=True, items_dirty=True)
    assert body["receipt_date"] == "2026-09-22T15:56" and body["items"] == []
    body["receipt_date"] = "2026-09-22T16:10"
    body["items"] = [{k: v for k, v in {"name": "Цемент М500", "quantity": 2, "price": 50, "total": 100,
                                        "vat_rate": "none", "vat_sum": 0}.items()
                      if k in call["body"]["nested"]["items"]["keys"]}]
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff, json=body)
    assert res.status_code == 200, res.text
    saved = client.get(f"/api/v1/receipts/{rid}", headers=backend.staff).json()
    assert saved["receipt_date"] == "2026-09-22T16:10:00", backend.name
    assert [it["name"] for it in saved["items"]] == ["Цемент М500"]
    # время с часовым поясом (старая форма, сторонний клиент) — без пересчёта: 16:20 — это 16:20
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff,
                       json={"receipt_date": "2026-09-22T16:20:00+05:00"})
    assert res.status_code == 200, res.text
    saved = client.get(f"/api/v1/receipts/{rid}", headers=backend.staff).json()
    assert saved["receipt_date"] == "2026-09-22T16:20:00", f"{backend.name}: время сдвинулось"
    assert res.json()["receipt_date"] == "2026-09-22T16:20:00"
    with backend.session() as db:
        doc = service.document_for_source(db, service.REGISTRY, rid)
        assert (doc.merchant_address, doc.cashier) == (ADDRESS, CASHIER)


def test_exported_receipt_form_keeps_core_and_data(client, ui_calls, backend):
    """Выгруженный чек у бухгалтера: форма не шлёт реквизиты, сохранение проходит."""
    call = _call(ui_calls, "openEditReceipt", "PATCH", "/api/v1/receipts/{}")
    with backend.session() as db:
        rid, pid = _scanned_after_pool(db, backend.company_id)
        db.get(Receipt, rid).exported = True
        db.commit()
    r = client.get(f"/api/v1/receipts/{rid}", headers=backend.staff).json()
    body = _edit_body(call, r)
    assert not {"fn", "fd", "fp", "total_sum", "receipt_date", "items"} & set(body), body
    losses = _replay_edit(client, backend, call, rid, pid, changes={"assignee": "Иванов А.А."},
                          expected={"копия чека: assignee": "Иванов А.А."})
    assert not losses, f"{backend.name}:\n" + "\n".join(losses)


def test_employee_notify_dialog(client, ui_calls, backend):
    """Сотрудник уведомляет бухгалтерию о своём чеке: только галочка и комментарий."""
    call = _call(ui_calls, "openNotifyDialog", "PATCH", "/api/v1/receipts/{}")
    values = {"notified": True, "comment": "товар вернули"}
    assert set(call["body"]["keys"]) <= set(values), call["body"]["keys"]
    with backend.session() as db:
        rid, pid = _scanned_after_pool(db, backend.company_id, created_by=backend.employee_id)
    before = _snapshot(backend, rid, pid)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.employee,
                       json={k: values[k] for k in call["body"]["keys"]})
    assert res.status_code == 200, f"{backend.name}: {res.text}"
    losses = _losses(before, _snapshot(backend, rid, pid),
                     {"копия чека: notified": True, "копия чека: comment": "товар вернули"})
    assert not losses, f"{backend.name}:\n" + "\n".join(losses)


def test_drawer_save_clears_and_applies_article(client, ui_calls, backend):
    """Карточка чека: стёртые сотрудник и комментарий очищаются, статья подставляется."""
    calls = _calls(ui_calls, "receiptDrawer", "PATCH", "/api/v1/receipts/{}")
    save = [c for c in calls if {"assignee", "comment"} <= set(c["body"]["keys"])]
    article = [c for c in calls if c["body"]["keys"] == ["category"]]
    assert len(save) == 1 and len(article) == 1, calls
    assert not save[0]["body"]["nullish"], "карточка шлёт стёртое поле как null"
    with backend.session() as db:
        rid, pid = _scanned_after_pool(db, backend.company_id)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff,
                       json={"assignee": "Петров П.П.", "comment": "старый комментарий"})
    assert res.status_code == 200, res.text
    before = _snapshot(backend, rid, pid)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff,
                       json=dict.fromkeys(save[0]["body"]["keys"], ""))
    assert res.status_code == 200, res.text
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff, json={"category": "Стройматериалы"})
    assert res.status_code == 200, res.text
    losses = _losses(before, _snapshot(backend, rid, pid),
                     {"копия чека: assignee": "", "копия чека: comment": "",
                      "копия чека: category": "Стройматериалы"})
    assert not losses, f"{backend.name}:\n" + "\n".join(losses)


def test_manual_entry_form(client, ui_calls, backend):
    """Ручной ввод: тело формы как его собирает manualDialog — чек в реестре и единой записи."""
    call = _call(ui_calls, "manualDialog", "POST", "/api/v1/receipts/manual")
    fn, fd, fp = _identity()
    values = {"date_time": "22.09.2026 15:56", "total_sum": 1234.5, "fn": fn, "fd": fd, "fp": fp,
              "operation": 1, "company_id": backend.company_id}
    unknown = [k for k in call["body"]["keys"] if k not in values]
    assert not unknown, f"для полей ручного ввода {_quoted(unknown)} нет значения в тесте"
    payload = {k: values[k] for k in call["body"]["keys"]}
    res = client.post("/api/v1/receipts/manual", headers=backend.staff, json=payload)
    assert res.status_code == 200, f"{backend.name}: {res.text}"
    data = res.json()
    assert data["duplicate"] is False and data["receipt"]["receipt_date"] == "2026-09-22T15:56:00"
    with backend.session() as db:
        doc = service.document_for_source(db, service.REGISTRY, data["receipt"]["id"])
        assert doc is not None and (doc.fn, doc.fd, doc.fp) == (fn, fd, fp)
        assert _plain(doc.receipt_date) == "2026-09-22T15:56:00" and round(doc.total_sum, 2) == 1234.5
    again = client.post("/api/v1/receipts/manual", headers=backend.staff, json=payload)
    assert again.status_code == 200 and again.json()["duplicate"] is True


# --------------------------------------------------------------------------
#  6. Самопроверка: тест ловит ошибку 1.70.89
# --------------------------------------------------------------------------
def test_self_check_catches_the_1_70_89_rule(client, ui_calls, sqlite_backend, monkeypatch):
    """С правилом 1.70.89 («любое пустое поле тела — очистка») прогон с телом из app.js
    находит потерю данных, а минимальное тело — нет: ровно поэтому ошибка дожила до 1.70.90."""
    call = _call(ui_calls, "openEditReceipt", "PATCH", "/api/v1/receipts/{}")
    real = service.sync_registry
    sent: dict = {}

    def rule_1_70_89(db, receipt, user, **options):
        options.pop("cleared", None)
        emptied = frozenset(f for f in receipts_router._CLEARABLE_FIELDS
                            if f in sent and not str(sent[f]).strip())
        return real(db, receipt, user, cleared=emptied, **options)

    monkeypatch.setattr(service, "sync_registry", rule_1_70_89)
    backend = sqlite_backend
    with backend.session() as db:
        rid, pid = _scanned_after_pool(db, backend.company_id)
    sent.update({"comment": "только комментарий"})
    before = _snapshot(backend, rid, pid)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff, json=dict(sent))
    assert res.status_code == 200, res.text
    assert not _losses(before, _snapshot(backend, rid, pid),
                       {"копия чека: comment": "только комментарий"}), "минимальное тело не должно ничего терять"

    r = client.get(f"/api/v1/receipts/{rid}", headers=backend.staff).json()
    sent.clear()
    sent.update(_edit_body(call, r))
    sent["comment"] = "правка через форму"
    before = _snapshot(backend, rid, pid)
    res = client.patch(f"/api/v1/receipts/{rid}", headers=backend.staff, json=dict(sent))
    assert res.status_code == 200, res.text
    losses = _losses(before, _snapshot(backend, rid, pid), {"копия чека: comment": "правка через форму"})
    assert any(x.startswith("единая запись: merchant_address") for x in losses), losses
    assert any(x.startswith("Чек-Пул: cashier") for x in losses), losses
