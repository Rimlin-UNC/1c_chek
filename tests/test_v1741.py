"""Тесты v1.74.1: изоляция и отказоустойчивость «Настроек» (viewSettings),
мультикомпанийные/внешние бухгалтеры, точность пакетной проверки ФНС и вебхуков."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.clients.models import AccountantClient
from app.config import settings
from app.database import SessionLocal
from app.goods.models import GoodsMemory
from app.models import AppSetting, MerchantRule, Receipt, User, WebhookLog
from app.routers import receipts as receipts_router
from app.services import verify_jobs
from tests.conftest import login

ROOT = Path(__file__).resolve().parent.parent


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _unique_inn10() -> str:
    """Уникальный валидный 10-значный ИНН для параллельного прогона pytest-xdist без коллизий."""
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    body = f"77{uuid.uuid4().int % 10_000_000:07d}"
    ctrl = sum(int(d) * w for d, w in zip(body, weights)) % 11 % 10
    return f"{body}{ctrl}"


def _register_role(
    client, h_adm: dict, role: str, company_id: str | None, tag: str
) -> tuple[dict, str]:
    inv = client.post(
        "/api/v1/invites",
        headers=h_adm,
        json={"role": role, "company_id": company_id, "max_uses": 1},
    ).json()
    uname = f"{role[:3]}_{tag}_{uuid.uuid4().hex[:4]}"
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "token": inv["token"],
            "username": uname,
            "password": "Password123!",
            "full_name": f"Тест {uname}",
        },
    )
    assert reg.status_code == 200, reg.text
    uid = reg.json()["user"]["id"]
    return login(client, uname, "Password123!"), uid


def test_version_1741_and_static_locks():
    """Версия 1.74.1 и все фронтенд-защиты раздела «Настройки» синхронизированы."""
    ver = settings.APP_VERSION
    assert tuple(int(x) for x in ver.split(".")) >= (1, 74, 1)

    sw = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
    assert f"ymaster-check-v{ver}" in sw
    assert sw.count(f"?v={ver}") == 5

    idx = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert f"app.css?v={ver}" in idx and f"home.css?v={ver}" in idx and f"app.js?v={ver}" in idx

    b2b = (ROOT / "app/static/b2b/index.html").read_text(encoding="utf-8")
    assert f"b2b.css?v={ver}" in b2b and f"b2b.js?v={ver}" in b2b

    mx = (ROOT / "app/static/maxapp/index.html").read_text(encoding="utf-8")
    assert f"maxapp.js?v={ver}" in mx

    manifest = json.loads((ROOT / "app/static/manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["version"] == ver

    app_js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    billing_js = (ROOT / "app/static/js/billing.js").read_text(encoding="utf-8")
    api_js = (ROOT / "app/static/js/api.js").read_text(encoding="utf-8")
    goods_js = (ROOT / "app/static/js/goods.js").read_text(encoding="utf-8")

    assert "'1.74.1':" in app_js
    assert "## [1.74.1]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.74.1 —" in (ROOT / "docs/ui_journal.md").read_text(encoding="utf-8")

    # 1. Нет коллизии id="cs-save" между #st-consent в app.js и модалкой в billing.js
    assert 'id="cs-save"' in app_js
    assert 'id="cs-save"' not in billing_js
    assert 'id="csub-save"' in billing_js

    # 2. Во всём JS нет вызовов toast(..., ..., число) с перепутанным 3-м аргументом (title)
    for js_file in (ROOT / "app/static/js").glob("*.js"):
        txt = js_file.read_text(encoding="utf-8")
        bad = re.findall(r"\btoast\([^;\n]+,\s*['\"][a-z]+['\"]\s*,\s*\d+\s*\)", txt)
        assert not bad, f"{js_file.name}: числовой 3-й аргумент toast вместо заголовка: {bad}"

    # 3. Защита localStorage в api.js и AbortController + _settingsPolicyCompany в viewSettings
    assert "try { return localStorage.getItem(TOKEN_KEY)" in api_js
    assert "_settingsNavAbort" in app_js
    assert "_settingsPolicyCompany" in app_js
    assert "isAdmin() && checkoSet" in app_js
    assert "isAdmin() && onec" in app_js
    assert "isAdmin() && maxSet" in app_js
    assert "isAdmin() && whSet" in app_js
    assert "isAdmin() && consentSet" in app_js
    assert "api.post('/api/v1/admin/backups/import', fd)" in app_js
    assert "?company_id=${encodeURIComponent(companyId)}" in goods_js


def test_outsource_accountant_counterparties_isolation(client):
    """Внешний бухгалтер без user.company_id видит ТОЛЬКО назначенные компании в /counterparties*."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()

    c1 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Клиент Аутсорс А {tag}", "inn": _unique_inn10()},
    ).json()
    c2 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Чужая Фирма Б {tag}", "inn": _unique_inn10()},
    ).json()

    inn_c1 = _unique_inn10()
    inn_c2 = _unique_inn10()
    card_c1 = client.post(
        "/api/v1/counterparties",
        headers=h_adm,
        json={"inn": inn_c1, "name": f"Поставщик Свой {tag}", "company_id": c1["id"]},
    )
    assert card_c1.status_code == 201, card_c1.text
    card_c2 = client.post(
        "/api/v1/counterparties",
        headers=h_adm,
        json={"inn": inn_c2, "name": f"Поставщик Чужой {tag}", "company_id": c2["id"]},
    )
    assert card_c2.status_code == 201, card_c2.text

    # 1) Бухгалтер вообще без компании и без назначенных клиентов -> видит строго пустой контур
    h_bare, bare_uid = _register_role(client, h_adm, "accountant", None, f"bare_{tag}")
    with SessionLocal() as db:
        u = db.get(User, bare_uid)
        assert u is not None and u.company_id is None

    r_bare_list = client.get("/api/v1/counterparties", headers=h_bare)
    assert r_bare_list.status_code == 200
    bare_ids = {it["id"] for it in r_bare_list.json()}
    assert card_c1.json()["id"] not in bare_ids
    assert card_c2.json()["id"] not in bare_ids

    assert client.get(f"/api/v1/counterparties/{card_c1.json()['id']}", headers=h_bare).status_code == 404
    assert client.get(f"/api/v1/counterparties/{card_c2.json()['id']}", headers=h_bare).status_code == 404
    assert (
        client.post(
            "/api/v1/counterparties",
            headers=h_bare,
            json={"inn": _unique_inn10(), "name": "Попытка", "company_id": c2["id"]},
        ).status_code
        == 422
    )

    # 2) Внешний бухгалтер с company_id=None, которому назначена ТОЛЬКО компания c1
    h_out, out_uid = _register_role(client, h_adm, "accountant", None, f"out_{tag}")
    with SessionLocal() as db:
        db.add(AccountantClient(user_id=out_uid, company_id=c1["id"]))
        db.commit()

    r_out_list = client.get("/api/v1/counterparties", headers=h_out)
    assert r_out_list.status_code == 200
    out_ids = {it["id"] for it in r_out_list.json()}
    assert card_c1.json()["id"] in out_ids
    assert card_c2.json()["id"] not in out_ids

    assert client.get(f"/api/v1/counterparties/{card_c1.json()['id']}", headers=h_out).status_code == 200
    assert client.get(f"/api/v1/counterparties/{card_c2.json()['id']}", headers=h_out).status_code == 404
    assert (
        client.get(
            "/api/v1/counterparties",
            headers={**h_out, "X-Ym-Company": c2["id"]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/counterparties",
            headers=h_out,
            json={"inn": _unique_inn10(), "name": "Чужая карточка", "company_id": c2["id"]},
        ).status_code
        == 422
    )
    ok_create = client.post(
        "/api/v1/counterparties",
        headers=h_out,
        json={"inn": _unique_inn10(), "name": f"Новая карточка Своя {tag}", "company_id": c1["id"]},
    )
    assert ok_create.status_code == 201, ok_create.text


def test_outsource_and_multicompany_settings_policy_goods_mrules(client):
    """Переключение company_id в /settings/policy, /goods/memory и /receipts/merchant-rules
    работает для администратора и мультикомпанийного/внешнего бухгалтера и блокирует чужие компании."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()

    c1 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Политика 1 {tag}", "inn": _unique_inn10()},
    ).json()
    c2 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Политика 2 {tag}", "inn": _unique_inn10()},
    ).json()
    c3 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Политика Чужая {tag}", "inn": _unique_inn10()},
    ).json()

    # Сохраняем разные лимиты для c1 и c2
    assert (
        client.put(
            "/api/v1/settings/policy",
            headers=h_adm,
            json={"company_id": c1["id"], "limit_per_check": 500, "limit_per_month": 15000},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/v1/settings/policy",
            headers=h_adm,
            json={"company_id": c2["id"], "limit_per_check": 1200, "limit_per_month": 42000},
        ).status_code
        == 200
    )

    inn_r1 = _unique_inn10()
    inn_r2 = _unique_inn10()
    with SessionLocal() as db:
        gm1 = GoodsMemory(
            company_id=c1["id"],
            name_key=f"товар_а_{tag}",
            category="Топливо и ГСМ",
            sample_name="Бензин АИ-95",
        )
        gm2 = GoodsMemory(
            company_id=c2["id"],
            name_key=f"товар_б_{tag}",
            category="Канцтовары",
            sample_name="Бумага А4",
        )
        mr1 = MerchantRule(
            company_id=c1["id"],
            merchant_inn=inn_r1,
            merchant_name=f"АЗС Своя {tag}",
            field="category",
            value="Топливо и ГСМ",
        )
        mr2 = MerchantRule(
            company_id=c2["id"],
            merchant_inn=inn_r2,
            merchant_name=f"Комус Второй {tag}",
            field="category",
            value="Канцтовары",
        )
        mr3 = MerchantRule(
            company_id=c3["id"],
            merchant_inn=_unique_inn10(),
            merchant_name=f"Чужой Магазин {tag}",
            field="category",
            value="Прочее",
        )
        db.add_all([gm1, gm2, mr1, mr2, mr3])
        db.commit()
        gm1_id = gm1.id
        mr1_id, mr3_id = mr1.id, mr3.id

    # Администратор без ?company_id= больше не падает с 400 на /goods/memory, а с ?company_id= фильтрует
    r_adm_gm_default = client.get("/api/v1/goods/memory", headers=h_adm)
    assert r_adm_gm_default.status_code == 200, r_adm_gm_default.text
    r_adm_mr_c2 = client.get(f"/api/v1/receipts/merchant-rules?company_id={c2['id']}", headers=h_adm)
    assert r_adm_mr_c2.status_code == 200
    adm_mr_rows = r_adm_mr_c2.json()
    assert isinstance(adm_mr_rows, list) and any(r["merchant_inn"] == inn_r2 for r in adm_mr_rows)
    assert {r["company_id"] for r in adm_mr_rows} <= {c2["id"]}

    # Внешний бухгалтер (company_id=None), которому назначены c1 и c2 (но НЕ c3)
    h_out, out_uid = _register_role(client, h_adm, "accountant", None, f"multi_{tag}")
    with SessionLocal() as db:
        db.add_all(
            [
                AccountantClient(user_id=out_uid, company_id=c1["id"]),
                AccountantClient(user_id=out_uid, company_id=c2["id"]),
            ]
        )
        db.commit()

    # Политика по c1 и c2 читается, по чужой c3 -> 403
    p1 = client.get(f"/api/v1/settings/policy?company_id={c1['id']}", headers=h_out)
    assert p1.status_code == 200 and p1.json()["limit_per_check"] == 500
    p2 = client.get(f"/api/v1/settings/policy?company_id={c2['id']}", headers=h_out)
    assert p2.status_code == 200 and p2.json()["limit_per_check"] == 1200
    assert client.get(f"/api/v1/settings/policy?company_id={c3['id']}", headers=h_out).status_code == 403

    # Память товаров для внешнего бухгалтера работает без 400 и переключается по ?company_id=
    g_def = client.get("/api/v1/goods/memory", headers=h_out)
    assert g_def.status_code == 200
    g2 = client.get(f"/api/v1/goods/memory?company_id={c2['id']}", headers=h_out)
    assert g2.status_code == 200
    assert any(it["name_key"] == f"товар_б_{tag}" for it in g2.json()["items"])
    assert client.get(f"/api/v1/goods/memory?company_id={c3['id']}", headers=h_out).status_code == 403

    # Удаление правила памяти товаров по назначенной c1 с ?company_id= проходит
    del_gm = client.delete(f"/api/v1/goods/memory/{gm1_id}?company_id={c1['id']}", headers=h_out)
    assert del_gm.status_code == 200 and del_gm.json()["ok"] is True

    # Правила поставщиков: внешний бухгалтер видит c1 и c2, фильтрует c2, но НЕ видит и не удаляет c3
    mr_all = client.get("/api/v1/receipts/merchant-rules", headers=h_out).json()
    mr_ids = {r["id"] for r in mr_all}
    assert mr1_id in mr_ids
    assert mr3_id not in mr_ids
    assert (
        client.get(f"/api/v1/receipts/merchant-rules?company_id={c3['id']}", headers=h_out).status_code
        == 403
    )
    assert client.delete(f"/api/v1/receipts/merchant-rules/{mr3_id}", headers=h_out).status_code == 404
    assert (
        client.post(f"/api/v1/receipts/merchant-rules/{mr1_id}/backfill", headers=h_out).status_code
        == 200
    )
    assert client.delete(f"/api/v1/receipts/merchant-rules/{mr1_id}", headers=h_out).status_code == 200

    # Защита /api/v1/settings/app/summary от нечислового мусора в AppSetting
    with SessionLocal() as db:
        row = db.get(AppSetting, "advance_deadline_days")
        prev_val = row.value if row else None
        if row:
            row.value = "corrupted_not_int"
        else:
            db.add(AppSetting(key="advance_deadline_days", value="corrupted_not_int"))
        db.commit()
    try:
        summ = client.get("/api/v1/settings/app/summary", headers=h_out)
        assert summ.status_code == 200
        assert summ.json()["advance_deadline_days"] == 10
    finally:
        with SessionLocal() as db:
            row = db.get(AppSetting, "advance_deadline_days")
            if row:
                if prev_val is None:
                    db.delete(row)
                else:
                    row.value = prev_val
                db.commit()


def test_verify_jobs_missing_receipt_and_owner_isolation(client):
    """Удалённый до фоновой проверки чек завершает счётчик verify_jobs (не вешает прогресс),
    а чужой пользователь не может прочитать прогресс задания по job_id."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    c1 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Задания А {tag}", "inn": _unique_inn10()},
    ).json()
    c2 = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Задания Б {tag}", "inn": _unique_inn10()},
    ).json()
    h_acc1, uid1 = _register_role(client, h_adm, "accountant", c1["id"], f"vj1_{tag}")
    h_acc2, _ = _register_role(client, h_adm, "accountant", c2["id"], f"vj2_{tag}")

    job = verify_jobs.create(2, uid1)
    jid = job["job_id"]

    # Оба чека уже отсутствуют в БД — задание обязано дойти до finished=True, а не зависнуть на 0/2
    receipts_router._verify_one_sync(f"missing-1-{tag}", jid)
    receipts_router._verify_one_sync(f"missing-2-{tag}", jid)

    r_own = client.get(f"/api/v1/receipts/verify/{jid}", headers=h_acc1)
    assert r_own.status_code == 200
    snap = r_own.json()["job"]
    assert snap["done"] == 2
    assert snap["total"] == 2
    assert snap["finished"] is True
    assert snap["counters"]["unknown"] == 2

    # Администратор видит задание, а бухгалтер другой компании получает 404
    assert client.get(f"/api/v1/receipts/verify/{jid}", headers=h_adm).status_code == 200
    assert client.get(f"/api/v1/receipts/verify/{jid}", headers=h_acc2).status_code == 404


def test_webhook_enqueued_only_for_verified_receipts(client, monkeypatch):
    """Исходящий вебхук receipt_verified ставится в очередь строго при status == 'verified'."""
    from app.services import appsettings
    from app.services import fns as fns_svc

    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    comp = client.post(
        "/api/v1/companies",
        headers=h_adm,
        json={"name": f"ООО Вебхук Точность {tag}", "inn": _unique_inn10()},
    ).json()

    # Включаем исходящий вебхук
    with SessionLocal() as db:
        appsettings.set_setting(db, "webhook_url", "http://127.0.0.1:9/hook")
        appsettings.set_setting(db, "webhook_secret", "sec-1741")
        db.commit()

    def _make_receipt(idx: int) -> str:
        fn = f"99990789{uuid.uuid4().int % 100_000_000:08d}"
        fd = str(10_000 + idx + (uuid.uuid4().int % 80_000))
        fp = str(100_000_000 + (uuid.uuid4().int % 800_000_000))
        with SessionLocal() as db:
            r = Receipt(
                qr_data=f"t=20261127T1200&s=350.00&fn={fn}&i={fd}&fp={fp}&n=1",
                fn=fn,
                fd=fd,
                fp=fp,
                company_id=comp["id"],
                receipt_date=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
                total_sum=350.0,
                operation=1,
                status="new",
                fns_status="unknown",
            )
            db.add(r)
            db.commit()
            return r.id

    r_quota = _make_receipt(1)
    r_invalid = _make_receipt(2)
    r_valid = _make_receipt(3)

    try:
        # 1) Ответ ФНС 'quota' -> чек остаётся 'new', вебхук НЕ ставится в очередь
        monkeypatch.setattr(
            receipts_router,
            "check_receipt",
            lambda *a, **kw: fns_svc.FnsResult(
                status="quota",
                message="Суточный лимит исчерпан",
                raw={"provider": "mock", "code": 429, "found": False},
                ok=False,
            ),
        )
        receipts_router._verify_one_sync(r_quota)

        # 2) Ответ ФНС 'invalid' -> чек становится 'failed', вебхук НЕ ставится в очередь
        monkeypatch.setattr(
            receipts_router,
            "check_receipt",
            lambda *a, **kw: fns_svc.FnsResult(
                status="invalid",
                message="Чек не найден в ФНС",
                raw={"provider": "mock", "code": 404, "found": False},
                ok=False,
            ),
        )
        receipts_router._verify_one_sync(r_invalid)

        # 3) Ответ ФНС 'valid' -> чек становится 'verified', вебхук СТАВИТСЯ в очередь
        monkeypatch.setattr(
            receipts_router,
            "check_receipt",
            lambda *a, **kw: fns_svc.FnsResult(
                status="valid",
                message="Чек подтверждён ФНС",
                raw={
                    "provider": "mock",
                    "code": 0,
                    "found": True,
                    "items": [{"name": "Кофе зерновой", "price": 350, "sum": 350, "quantity": 1}],
                },
                ok=True,
            ),
        )
        receipts_router._verify_one_sync(r_valid)

        with SessionLocal() as db:
            assert db.get(Receipt, r_quota).status != "verified"
            assert db.get(Receipt, r_invalid).status == "failed"
            assert db.get(Receipt, r_valid).status == "verified"
            q_logs = db.query(WebhookLog).filter(WebhookLog.receipt_id == r_quota).count()
            inv_logs = db.query(WebhookLog).filter(WebhookLog.receipt_id == r_invalid).count()
            ok_logs = db.query(WebhookLog).filter(WebhookLog.receipt_id == r_valid).count()
            assert q_logs == 0
            assert inv_logs == 0
            assert ok_logs == 1
    finally:
        with SessionLocal() as db:
            appsettings.set_setting(db, "webhook_url", "")
            appsettings.set_setting(db, "webhook_secret", "")
            db.commit()
