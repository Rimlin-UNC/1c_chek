"""Тесты v1.74.0: Этап 2 — корпоративные платные подписки, тарифы, помодульные цены,
Grandfathering, пробный и льготный периоды, счета, Read-Only сохранность архива и RBAC."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.clients.models import AccountantClient
from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting
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


def _register_role(client, h_adm: dict, role: str, company_id: str, tag: str) -> tuple[dict, str]:
    inv = client.post("/api/v1/invites", headers=h_adm,
                      json={"role": role, "company_id": company_id, "max_uses": 1}).json()
    uname = f"{role[:3]}_{tag}_{uuid.uuid4().hex[:4]}"
    reg = client.post("/api/v1/auth/register",
                      json={"token": inv["token"], "username": uname,
                            "password": "Password123!", "full_name": f"Тест {uname}"})
    assert reg.status_code == 200, reg.text
    uid = reg.json()["user"]["id"]
    return login(client, uname, "Password123!"), uid


def test_version_1740_synced():
    """Версия 1.74.0 и модуль billing.js синхронны во всех точках кэша, манифеста и журналов."""
    ver = settings.APP_VERSION
    assert tuple(int(x) for x in ver.split(".")) >= (1, 74, 0)
    sw = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
    assert f"ymaster-check-v{ver}" in sw
    assert "'/js/billing.js'" in sw
    idx = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert f"app.css?v={ver}" in idx and f"home.css?v={ver}" in idx and f"app.js?v={ver}" in idx
    b2b = (ROOT / "app/static/b2b/index.html").read_text(encoding="utf-8")
    assert f"b2b.css?v={ver}" in b2b and f"b2b.js?v={ver}" in b2b
    mx = (ROOT / "app/static/maxapp/index.html").read_text(encoding="utf-8")
    assert f"maxapp.js?v={ver}" in mx
    manifest = json.loads((ROOT / "app/static/manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["version"] == ver
    app_js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    assert "'1.74.0':" in app_js
    assert "st-billing" in (ROOT / "app/static/js/billing.js").read_text(encoding="utf-8")
    assert "## [1.74.0]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.74.0 —" in (ROOT / "docs/ui_journal.md").read_text(encoding="utf-8")

    # Проверка, что все именованные ES-импорты в billing.js и app.js реально экспортируются целевыми модулями
    import re
    js_dir = ROOT / "app/static/js"
    for src_name in ("billing.js", "app.js"):
        src = (js_dir / src_name).read_text(encoding="utf-8")
        for m in re.finditer(r"import\s*\{([^}]+)\}\s*from\s*['\"]\./([^'\"?]+)(?:\?[^'\"]*)?['\"]", src):
            names = [part.strip().split(" as ")[0].strip() for part in m.group(1).split(",") if part.strip()]
            target_text = (js_dir / m.group(2)).read_text(encoding="utf-8")
            exported = set(
                re.findall(r"\bexport\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z0-9_$]+)", target_text)
            )
            for blk in re.finditer(r"\bexport\s*\{([^}]+)\}", target_text):
                for item in blk.group(1).split(","):
                    part = item.strip()
                    if part:
                        exported.add(part.split(" as ")[-1].strip())
            for name in names:
                assert name in exported, (
                    f"{src_name} импортирует '{name}' из ./{m.group(2)}, но такого export нет!"
                )


def test_admin_billing_rbac_and_overview(client):
    """Коммерческий центр /api/v1/billing/admin/* открыт строго администратору (401/403 для остальных)."""
    client.cookies.clear()
    assert client.get("/api/v1/billing/admin/overview").status_code == 401

    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    comp = client.post("/api/v1/companies", headers=h_adm,
                       json={"name": f"ООО РБАК Биллинг {tag}", "inn": _unique_inn10()}).json()

    h_acc, _ = _register_role(client, h_adm, "accountant", comp["id"], tag)
    h_mgr, _ = _register_role(client, h_adm, "manager", comp["id"], tag)
    h_usr, _ = _register_role(client, h_adm, "user", comp["id"], tag)

    for hdr in (h_acc, h_mgr, h_usr):
        assert client.get("/api/v1/billing/admin/overview", headers=hdr).status_code == 403
        assert client.put("/api/v1/billing/admin/config", headers=hdr,
                          json={"trial_days": 30}).status_code == 403
        assert client.put("/api/v1/billing/admin/modules/receipts", headers=hdr,
                          json={"monthly_kop": 99_000, "yearly_kop": 990_000}).status_code == 403

    ov = client.get("/api/v1/billing/admin/overview", headers=h_adm)
    assert ov.status_code == 200, ov.text
    data = ov.json()
    assert {"config", "metrics", "module_prices", "plans", "companies", "promos", "invoices", "events"} <= set(data.keys())
    mod_codes = {m["code"] for m in data["module_prices"]}
    assert mod_codes == {"receipts", "upd", "manager_cabinet", "counterparties", "contracts"}


def test_pricing_grandfathering_vs_next_renewal_and_12x_guard(client):
    """Изменение цен администратором: защита от годовой ошибки (>12x) и Grandfathering."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    comp = client.post("/api/v1/companies", headers=h_adm,
                       json={"name": f"ООО Грандфазеринг {tag}", "inn": _unique_inn10()}).json()

    # Создаём выделенный тариф для изолированной проверки версий цены и Grandfathering
    plan_code = f"plan_{tag}"
    cp = client.post("/api/v1/billing/admin/plans", headers=h_adm, json={
        "code": plan_code,
        "title": f"Пакет Проверка {tag}",
        "modules": ["receipts", "upd"],
        "monthly_kop": 250_000,
        "quarterly_kop": 700_000,
        "yearly_kop": 2_500_000,
        "limits": {"receipts_monthly": 300, "upd_monthly": 100},
    })
    assert cp.status_code == 200, cp.text
    assert cp.json()["plan"]["price_version"] == 1

    # Защита от несогласованной годовой цены (> 12 месячных платежей)
    bad = client.put(f"/api/v1/billing/admin/plans/{plan_code}", headers=h_adm, json={
        "code": plan_code,
        "title": f"Пакет Проверка {tag}",
        "modules": ["receipts", "upd"],
        "monthly_kop": 250_000,
        "yearly_kop": 3_500_000,
    })
    assert bad.status_code == 422
    assert "12 месячных" in bad.json()["detail"]

    # Подключаем компании тариф по цене 2 500 ₽/мес с фиксацией цены (price_locked=True)
    end_dt = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sub_r = client.put(f"/api/v1/billing/admin/companies/{comp['id']}/subscription", headers=h_adm, json={
        "status": "active",
        "plan_code": plan_code,
        "billing_cycle": "monthly",
        "period_end": end_dt,
        "price_locked": True,
    })
    assert sub_r.status_code == 200, sub_r.text
    sub1 = sub_r.json()["subscription"]
    assert sub1["locked_price_kop"] == 250_000

    # Администратор поднимает цену тарифа до 3 200 ₽/мес с политикой grandfather
    up1 = client.put(f"/api/v1/billing/admin/plans/{plan_code}", headers=h_adm, json={
        "code": plan_code,
        "title": f"Пакет Проверка {tag}",
        "modules": ["receipts", "upd"],
        "monthly_kop": 320_000,
        "quarterly_kop": 900_000,
        "yearly_kop": 3_200_000,
        "apply_policy": "grandfather",
    })
    assert up1.status_code == 200, up1.text
    assert up1.json()["plan"]["price_version"] == 2

    # У действующей компании сохраняется зафиксированная цена 2 500 ₽/мес (grandfathered=True),
    # а прайс каталога для новых подключений уже равен 3 200 ₽/мес!
    my_adm = client.get(f"/api/v1/billing/my?company_id={comp['id']}", headers=h_adm).json()
    assert my_adm["subscription"]["locked_price_kop"] == 250_000
    assert my_adm["subscription"]["grandfathered"] is True
    q_same = client.post("/api/v1/billing/quote", headers=h_adm,
                         json={"company_id": comp["id"], "plan_code": plan_code, "billing_cycle": "monthly"}).json()
    assert q_same["catalog_kop"] == 320_000
    assert q_same["subtotal_kop"] == 250_000
    assert q_same["grandfathered"] is True

    # Теперь администратор применяет политику next_renewal — замок снимается для следующего продления
    up2 = client.put(f"/api/v1/billing/admin/plans/{plan_code}", headers=h_adm, json={
        "code": plan_code,
        "title": f"Пакет Проверка {tag}",
        "modules": ["receipts", "upd"],
        "monthly_kop": 330_000,
        "quarterly_kop": 920_000,
        "yearly_kop": 3_300_000,
        "apply_policy": "next_renewal",
    })
    assert up2.status_code == 200
    q_renew = client.post("/api/v1/billing/quote", headers=h_adm,
                          json={"company_id": comp["id"], "plan_code": plan_code, "billing_cycle": "monthly"}).json()
    assert q_renew["catalog_kop"] == 330_000
    assert q_renew["subtotal_kop"] == 330_000
    assert q_renew["grandfathered"] is False

    # Включение и возврат модуля будущей «Подготовки договоров» (contracts) администратором
    orig_contracts = next(
        m for m in client.get("/api/v1/billing/admin/overview", headers=h_adm).json()["module_prices"]
        if m["code"] == "contracts"
    )
    try:
        m_up = client.put("/api/v1/billing/admin/modules/contracts", headers=h_adm, json={
            "monthly_kop": 159_000,
            "yearly_kop": 1_590_000,
            "monthly_quota": 50,
            "available": True,
        })
        assert m_up.status_code == 200, m_up.text
        assert m_up.json()["module"]["available"] is True
        assert m_up.json()["module"]["monthly_kop"] == 159_000
    finally:
        client.put("/api/v1/billing/admin/modules/contracts", headers=h_adm, json={
            "monthly_kop": orig_contracts["monthly_kop"],
            "yearly_kop": orig_contracts["yearly_kop"],
            "monthly_quota": orig_contracts["monthly_quota"],
            "available": orig_contracts["available"],
        })


def test_promos_multicompany_invoice_print_and_mark_paid(client):
    """Промокоды, расчёт с НДС, выписка счёта мультикомпанийным бухгалтером, печать по тикету и оплата."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()

    orig_cfg = client.get("/api/v1/billing/admin/overview", headers=h_adm).json()["config"]
    try:
        client.put("/api/v1/billing/admin/config", headers=h_adm, json={
            "vat_mode": "vat5",
            "seller_name": "ООО «Ямастер Тест»",
            "seller_inn": "7707083893",
            "seller_bank": "АО «ТБанк»",
            "seller_bik": "044525974",
            "seller_account": "40702810000000000001",
        })

        c_own = client.post("/api/v1/companies", headers=h_adm,
                            json={"name": f"ООО Своя Бух {tag}", "inn": _unique_inn10()}).json()
        c_cli = client.post("/api/v1/companies", headers=h_adm,
                            json={"name": f"ООО Клиент Аутсорс {tag}", "inn": _unique_inn10()}).json()
        c_alien = client.post("/api/v1/companies", headers=h_adm,
                              json={"name": f"ООО Чужая Фирма {tag}", "inn": _unique_inn10()}).json()

        h_acc, acc_id = _register_role(client, h_adm, "accountant", c_own["id"], tag)
        h_usr, _ = _register_role(client, h_adm, "user", c_own["id"], tag)

        # Закрепляем c_cli за бухгалтером на аутсорсе
        with SessionLocal() as db:
            db.add(AccountantClient(user_id=acc_id, company_id=c_cli["id"]))
            db.commit()

        # Промокод 15%
        pcode = f"PROMO{tag.upper()}"
        pr = client.post("/api/v1/billing/admin/promos", headers=h_adm, json={
            "code": pcode,
            "title": "Акция теста",
            "discount_kind": "percent",
            "discount_value": 15,
            "max_uses": 5,
        })
        assert pr.status_code == 200, pr.text

        # Сотрудник не может выписывать счёт (403)
        assert client.post("/api/v1/billing/request-invoice", headers=h_usr,
                           json={"company_id": c_own["id"], "plan_code": "start_receipts"}).status_code == 403
        # Бухгалтер не может выписать счёт на чужую компанию (403)
        assert client.post("/api/v1/billing/request-invoice", headers=h_acc,
                           json={"company_id": c_alien["id"], "plan_code": "start_receipts"}).status_code == 403

        # Расчёт и выписка счёта бухгалтером для закреплённой клиентской компании на квартал (3 мес.)
        q = client.post("/api/v1/billing/quote", headers=h_acc, json={
            "company_id": c_cli["id"],
            "plan_code": "start_receipts",
            "addon_modules": ["counterparties"],
            "billing_cycle": "quarterly",
            "promo_code": pcode,
        })
        assert q.status_code == 200, q.text
        qd = q.json()
        assert qd["months"] == 3
        assert qd["promo_code"] == pcode
        assert qd["promo_discount_kop"] == (qd["subtotal_kop"] * 15) // 100
        assert qd["discount_kop"] == qd["promo_discount_kop"] > 0
        assert qd["vat_mode"] == "vat5"
        assert qd["total_kop"] > 0 and qd["vat_kop"] > 0
        assert set(qd["modules"]) == {"receipts", "counterparties"}

        inv_r = client.post("/api/v1/billing/request-invoice", headers=h_acc, json={
            "company_id": c_cli["id"],
            "plan_code": "start_receipts",
            "addon_modules": ["counterparties"],
            "billing_cycle": "quarterly",
            "promo_code": pcode,
            "comment": "Просим счёт на квартал",
        })
        assert inv_r.status_code == 200, inv_r.text
        inv = inv_r.json()["invoice"]
        assert inv["status"] == "issued"
        assert inv["total_kop"] == qd["total_kop"]

        # Узкий билет печати счёта: работает без Bearer для своего счёта, но не даёт доступ к сессии или чужому счёту
        tk_r = client.post(f"/api/v1/billing/invoices/{inv['id']}/print-ticket", headers=h_acc)
        assert tk_r.status_code == 200
        ticket = tk_r.json()["ticket"]

        html_r = client.get(f"/api/v1/billing/invoices/{inv['id']}/print?ticket={ticket}")
        assert html_r.status_code == 200
        assert inv["number"] in html_r.text
        assert "ООО «Ямастер Тест»" in html_r.text
        assert c_cli["inn"] in html_r.text
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401

        # Администратор подтверждает оплату -> подписка компании активируется на 3 месяца!
        mp = client.post(f"/api/v1/billing/admin/invoices/{inv['id']}/mark-paid", headers=h_adm,
                         json={"payment_ref": "ПП № 778 от 27.11.2026"})
        assert mp.status_code == 200, mp.text
        paid_sub = mp.json()["subscription"]
        assert paid_sub["status"] == "active"
        assert set(paid_sub["enabled_modules"]) == {"receipts", "counterparties"}

        # Продление на +15 дней с комментарием в аудит
        ext = client.post(f"/api/v1/billing/admin/companies/{c_cli['id']}/extend", headers=h_adm,
                          json={"add_days": 15, "reason": "Бонус за годовой переход"})
        assert ext.status_code == 200, ext.text
        ov = client.get("/api/v1/billing/admin/overview", headers=h_adm).json()
        assert ov["metrics"]["mrr_kop"] >= paid_sub["locked_price_kop"] // 3
        assert ov["metrics"]["arr_kop"] == ov["metrics"]["mrr_kop"] * 12
        assert any(a["action"] == "billing_subscription_extended" for a in ov["events"])
    finally:
        client.put("/api/v1/billing/admin/config", headers=h_adm, json={
            "vat_mode": orig_cfg["vat_mode"],
            "billing_mode": orig_cfg["billing_mode"],
            "seller_name": orig_cfg["seller_name"],
            "seller_inn": orig_cfg["seller_inn"],
            "seller_bank": orig_cfg["seller_bank"],
            "seller_bik": orig_cfg["seller_bik"],
            "seller_account": orig_cfg["seller_account"],
        })


def test_strict_enforcement_quotas_grace_period_and_readonly_archive(client):
    """Боевой режим strict: проверка модулей (402), квот, Grace Period, безлимита admin и сохранности архива."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    orig_cfg = client.get("/api/v1/billing/admin/overview", headers=h_adm).json()["config"]

    comp = client.post("/api/v1/companies", headers=h_adm,
                       json={"name": f"ООО Строгий Тариф {tag}", "inn": _unique_inn10()}).json()
    h_acc, _ = _register_role(client, h_adm, "accountant", comp["id"], tag)
    h_mgr, _ = _register_role(client, h_adm, "manager", comp["id"], tag)

    fn_base = f"99990789{uuid.uuid4().int % 100_000_000:08d}"

    try:
        # 1. Назначаем компании активный тариф ТОЛЬКО на чеки с квотой 1 чек/мес.
        future_end = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
        client.put(f"/api/v1/billing/admin/companies/{comp['id']}/subscription", headers=h_adm, json={
            "status": "active",
            "plan_code": "start_receipts",
            "period_end": future_end,
            "custom_limits": {"receipts_monthly": 1, "upd_monthly": 0, "counterparties_monthly": 0},
        })
        client.put("/api/v1/billing/admin/config", headers=h_adm,
                   json={"billing_mode": "strict", "grace_days": 7})

        # Первый чек в рамках оплаченного модуля receipts проходит (200)
        qr1 = f"t=20261127T1100&s=450.00&fn={fn_base}&i=101&fp=11223344&n=1"
        r_scan1 = client.post("/api/v1/receipts/scan", headers=h_acc, json={"qr_data": qr1})
        assert r_scan1.status_code == 200, r_scan1.text
        rid = r_scan1.json()["receipt"]["id"]

        # Второй чек превышает месячную квоту (receipts_monthly=1) -> 402 quota_exceeded!
        qr2 = f"t=20261127T1105&s=550.00&fn={fn_base}&i=102&fp=22334455&n=1"
        r_scan2 = client.post("/api/v1/receipts/scan", headers=h_acc, json={"qr_data": qr2})
        assert r_scan2.status_code == 402
        assert r_scan2.json()["detail"]["code"] == "quota_exceeded"

        # Администратор (admin) никогда не ограничивается подпиской или квотой компании!
        r_adm_scan = client.post("/api/v1/receipts/scan", headers=h_adm,
                                 json={"qr_data": qr2, "company_id": comp["id"]})
        assert r_adm_scan.status_code == 200, r_adm_scan.text

        # Неоплаченные модули: контрагенты, УПД и аналитика руководителя возвращают 402 module_not_included
        r_cp = client.post("/api/v1/counterparties", headers=h_acc,
                           json={"inn": _unique_inn10(), "name": "ООО Поставщик"})
        assert r_cp.status_code == 402
        assert r_cp.json()["detail"]["code"] == "module_not_included"
        assert r_cp.json()["detail"]["module"] == "counterparties"

        r_upd = client.post("/api/v1/primary/upload", headers=h_acc,
                            files={"files": ("test.xml", "<Файл/>".encode(), "application/xml")})
        assert r_upd.status_code == 402
        assert r_upd.json()["detail"]["module"] == "upd"

        r_mgr = client.get("/api/v1/dashboard/breakdown", headers=h_mgr)
        assert r_mgr.status_code == 402
        assert r_mgr.json()["detail"]["module"] == "manager_cabinet"

        # 2. Льготный период (Grace Period = 7 дней): подписка истекла 2 дня назад -> статус grace, доступ открыт!
        grace_end = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        client.put(f"/api/v1/billing/admin/companies/{comp['id']}/subscription", headers=h_adm, json={
            "status": "active",
            "plan_code": "start_receipts",
            "period_end": grace_end,
            "custom_limits": {"receipts_monthly": 10},
        })
        my_grace = client.get("/api/v1/billing/my", headers=h_acc).json()["subscription"]
        assert my_grace["status"] == "grace"
        assert any("льготн" in w.lower() for w in my_grace["warnings"])
        qr3 = f"t=20261127T1115&s=650.00&fn={fn_base}&i=103&fp=33445566&n=1"
        assert client.post("/api/v1/receipts/scan", headers=h_acc, json={"qr_data": qr3}).status_code == 200

        # 3. Полное истечение подписки (20 дней назад > grace_days=7) -> expired + Read-Only архив!
        expired_end = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        client.put(f"/api/v1/billing/admin/companies/{comp['id']}/subscription", headers=h_adm, json={
            "status": "active",
            "plan_code": "start_receipts",
            "period_end": expired_end,
            "custom_limits": {"receipts_monthly": 10},
        })
        me_exp = client.get("/api/v1/auth/me", headers=h_acc).json()
        assert me_exp["entitlements"]["status"] == "expired"
        assert me_exp["entitlements"]["read_only"] is True

        # Новая запись блокируется с 402 subscription_expired...
        qr4 = f"t=20261127T1120&s=750.00&fn={fn_base}&i=104&fp=44556677&n=1"
        r_exp = client.post("/api/v1/receipts/scan", headers=h_acc, json={"qr_data": qr4})
        assert r_exp.status_code == 402
        assert r_exp.json()["detail"]["code"] == "subscription_expired"

        # ...НО весь ранее накопленный архив чеков и карточка чека доступны для чтения (200 OK)!
        r_list = client.get("/api/v1/receipts", headers=h_acc)
        assert r_list.status_code == 200
        assert any(it["id"] == rid for it in r_list.json()["items"])
        assert client.get(f"/api/v1/receipts/{rid}/card", headers=h_acc).status_code == 200
    finally:
        client.put("/api/v1/billing/admin/config", headers=h_adm, json={
            "billing_mode": orig_cfg["billing_mode"],
            "grace_days": orig_cfg["grace_days"],
        })


def test_company_wipe_cleans_up_billing_records(client):
    """Полное удаление организации очищает её запись подписки и счета без осиротевших ключей."""
    h_adm = login(client, "admin", "admin123")
    tag = _uid()
    # Создаём опорную компанию (удалить последнюю компанию система не позволяет по инварианту 409)
    client.post("/api/v1/companies", headers=h_adm,
                json={"name": f"ООО Опорная {tag}", "inn": _unique_inn10()})
    comp = client.post("/api/v1/companies", headers=h_adm,
                       json={"name": f"ООО На Удаление {tag}", "inn": _unique_inn10()}).json()
    cid = comp["id"]

    client.put(f"/api/v1/billing/admin/companies/{cid}/subscription", headers=h_adm,
               json={"status": "active", "plan_code": "start_receipts"})
    inv = client.post("/api/v1/billing/admin/invoices", headers=h_adm,
                      json={"company_id": cid, "plan_code": "start_receipts"}).json()["invoice"]

    del_r = client.post(f"/api/v1/companies/{cid}/delete", headers=h_adm,
                        json={"mode": "wipe", "confirm": comp["name"]})
    assert del_r.status_code == 200, del_r.text

    with SessionLocal() as db:
        assert db.get(AppSetting, f"billing:sub:{cid}") is None
        idx_row = db.get(AppSetting, "billing:invoices_index")
        invs = json.loads(idx_row.value) if idx_row and idx_row.value else []
        assert all(i.get("id") != inv["id"] and i.get("company_id") != cid for i in invs)
