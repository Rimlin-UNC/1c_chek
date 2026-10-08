# ======================================================================
# Ямастер Чек — тесты v1.25.2: жизненный цикл «полных данных» чека.
# Проверенный ФНС чек с позициями = полные данные: без повторных
# запросов (идемпотентность), backfill старых чеков, кнопки строк.
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

from tests.conftest import login, client  # noqa: F401

_INN = {"n": 0}


def _next_inn() -> str:
    """Уникальный валидный ИНН на каждый тест (контрольная сумма 10 зн.)."""
    def ok10(s):
        c = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(c, map(int, s[:9]))) % 11 % 10 == int(s[9])
    while True:
        _INN["n"] += 1
        cand = f"5002{_INN['n'] * 7 + 11:06d}"
        if ok10(cand):
            return cand


def _mk(client, role="user", prefix="fdx"):
    adm = login(client, "admin", "admin123")
    inn = _next_inn()
    comp = client.post("/api/v1/companies", json={
        "name": f"ООО Полные-{inn[-4:]}", "inn": inn}, headers=adm).json()
    assert "id" in comp, comp
    inv = client.post("/api/v1/invites", json={
        "role": role, "company_id": comp["id"]}, headers=adm).json()
    u = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": f"{prefix}_{comp['id'][:6]}",
        "password": "parol123", "full_name": "Сотрудник Полный"}).json()
    U = {"Authorization": "Bearer " + u["access_token"]}
    return adm, comp, U


_QR = {"n": 0}


def _scan(client, U):
    _QR["n"] += 1
    k = _QR["n"]
    qr = (f"t=2026100{k % 9 + 1}T1200&s=1{k}0.00&fn=9999078902005{k:02d}"
          f"&i=64{k:02d}&fp=779{k:02d}01&n=1")
    r = client.post("/api/v1/receipts/scan", headers=U, json={
        "qr_data": qr, "source": "manual", "verify": False})
    assert r.status_code == 200, r.text
    return r.json()["receipt"]


def _set_db(rid, **fields):
    from app.database import SessionLocal
    from app.models import Receipt
    db = SessionLocal()
    try:
        db.query(Receipt).filter(Receipt.id == rid).update(fields)
        db.commit()
    finally:
        db.close()


class TestFullDataLifecycle:
    def test_verify_with_items_sets_full_data(self, client, monkeypatch):
        """Проверка ФНС (valid) вернувшая позиции → full_data = True."""
        import app.routers.receipts as rr

        class Res:
            ok = True
            status = "valid"
            message = "Найден"
            raw = {"provider": "mock", "items": [
                {"name": "Позиция 1", "quantity": 1, "price": 100, "sum": 100,
                 "vat_rate": "none", "vat_sum": 0}]}

        monkeypatch.setattr(rr, "check_receipt",
                            lambda *a, **k: Res())
        adm, comp, U = _mk(client, prefix="fdv")
        r1 = _scan(client, U)
        assert r1["full_data"] is False
        v = client.post("/api/v1/receipts/verify", headers=adm,
                        json={"receipt_ids": [r1["id"]]})
        assert v.status_code == 200, v.text
        import time
        d = {}
        for _ in range(200):                 # фон-поток в нагруженном прогоне
            d = client.get(f"/api/v1/receipts/{r1['id']}", headers=adm).json()
            if d.get("full_data"):
                break
            time.sleep(0.1)
        assert d.get("full_data") is True, (
            "проверен ФНС + позиции = полные данные; "
            f"status={d.get('status')} fns={d.get('fns_status')} msg={d.get('fns_message')}")
        assert d.get("fns_status") == "valid"

    def test_verify_without_items_keeps_full_data_false(self, client, monkeypatch):
        """Проверка без позиций: данные не полные — запрос остаётся."""
        import app.routers.receipts as rr

        class Res:
            ok = True
            status = "valid"
            message = "Найден"
            raw = {"provider": "mock"}      # позиций нет

        monkeypatch.setattr(rr, "check_receipt", lambda *a, **k: Res())
        adm, comp, U = _mk(client, prefix="fdn")
        r1 = _scan(client, U)
        client.post("/api/v1/receipts/verify", headers=adm,
                    json={"receipt_ids": [r1["id"]]})
        import time
        d = {}
        for _ in range(200):
            d = client.get(f"/api/v1/receipts/{r1['id']}", headers=adm).json()
            if d.get("fns_status") == "valid":
                break
            time.sleep(0.1)
        assert d.get("full_data") is False, "без позиций полными данные не считаются"

    def test_fetch_details_idempotent(self, client, monkeypatch):
        """v1.57.5: блок v1.25.1 возвращён — запрос уходит ВСЕГДА,
        даже для чека с флагом full_data (у старых чеков флаг стоит,
        а расширенных данных в базе нет)."""
        import app.routers.receipts as rr
        calls = []
        monkeypatch.setattr(rr, "_run_external_fetch",
                            lambda ids, force=False: calls.append(ids))
        adm, comp, U = _mk(client, prefix="fdi")
        r1 = _scan(client, U)
        _set_db(r1["id"], full_data=True)
        calls.clear()          # авто-загрузка скана тоже писала в calls
        r = client.post(f"/api/v1/receipts/{r1['id']}/fetch-details", headers=adm)
        assert r.status_code == 200, r.text
        assert r.json()["queued"] is True
        assert calls and calls[0] == [r1["id"]], "запрос должен ставиться в очередь"

    def test_fetch_bulk_skips_full(self, client, monkeypatch):
        """v1.57.5: блок v1.25.1 — массовый запрос ставит в очередь ВСЮ
        выборку (без «пропуска полных»); квоту бережёт сам человек."""
        import app.routers.receipts as rr
        calls = []
        monkeypatch.setattr(rr, "_run_external_fetch",
                            lambda ids, force=False: calls.append(ids))
        adm, comp, U = _mk(client, prefix="fdb")
        r1 = _scan(client, U)
        r2 = _scan(client, U)
        _set_db(r1["id"], full_data=True)
        calls.clear()
        r = client.post("/api/v1/receipts/fetch-details", headers=adm,
                        json={"receipt_ids": [r1["id"], r2["id"]]})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["queued"] == 2
        assert calls and sorted(calls[0]) == sorted([r1["id"], r2["id"]])
        # повторный вызов — тоже уходит в очередь (v1.25.1)
        r = client.post("/api/v1/receipts/fetch-details", headers=adm,
                        json={"receipt_ids": [r1["id"]]})
        assert r.json()["queued"] == 1

    def test_filter_full_data_after_backfill(self, client):
        """Backfill: проверенные чеки с позициями = полные; фильтр их видит."""
        from app.database import SessionLocal, _backfill_full_data
        from app.models import Receipt, ReceiptItem
        adm, comp, U = _mk(client, prefix="fdt")
        r1 = _scan(client, U)
        C = comp["id"]
        # «старый» чек: проверен ФНС, позиции есть, флаг не выставлен
        _set_db(r1["id"], fns_status="valid", status="verified", full_data=False)
        db = SessionLocal()
        try:
            db.add(ReceiptItem(receipt_id=r1["id"], name="Позиция", quantity=1,
                               price=100, total=100, vat_rate="none",
                               vat_sum=0, position=0))
            db.commit()
        finally:
            db.close()
        _backfill_full_data()
        db = SessionLocal()
        try:
            flag = db.query(Receipt).filter(Receipt.id == r1["id"]).first().full_data
        finally:
            db.close()
        assert flag is True
        got = client.get(f"/api/v1/receipts?full_data=true&company_id={C}",
                         headers=adm).json()["items"]
        assert r1["id"] in [x["id"] for x in got]

    def test_auto_fetch_skips_full(self, client):
        """Автозагрузка после скана не трогает чеки с полными данными."""
        import app.routers.receipts as rr
        from app.database import SessionLocal
        from app.models import Receipt
        adm, comp, U = _mk(client, prefix="fda")
        r1 = _scan(client, U)
        _set_db(r1["id"], full_data=True, status="new")
        db = SessionLocal()
        try:
            rec = db.get(Receipt, r1["id"])
            assert rr._maybe_auto_fetch(None, db, rec) is False
        finally:
            db.close()


class TestUIRules:
    def test_row_only_edit_when_full(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # v1.57.5 (блок v1.25.1): у полного чека — значок «📥✓»,
        # у неполного — живая кнопка «📥»
        assert "📥✓" in js
        assert "r.full_data\n             ? '<span" in js or "r.full_data ? '<span" in js
        assert 'Получить полные данные чека из сервиса проверки' in js

    def test_filter_labels(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "📥 проверены + полные данные" in js
        assert "⏳ данных не хватает — можно запросить" in js


class TestVersion1252:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # актуальный пин — в тесте текущей версии (v1.26.0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.25.2':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.25.2]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Полные данные чека" in manual
