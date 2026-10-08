# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.4: force-запрос данных — чеки с full_data=1
# (старые, без ext_json) снова загружаются.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import re
from unittest.mock import patch as mock_patch


def _login(client):
    from tests.conftest import login
    return login(client, "admin", "admin123")


def _mk_receipt(full_data: bool):
    import uuid
    from app.database import SessionLocal
    from app.models import Receipt
    db = SessionLocal()
    rid = str(uuid.uuid4())
    db.add(Receipt(id=rid, qr_data="t=20261008T1305&s=807.30&fn=738&i=9895&fp=3304159072&n=1",
                   fn="738", fd="9895", fp="3304159072", total_sum=807.30,
                   receipt_date=dt.datetime(2026, 10, 8, 13, 5),
                   full_data=full_data))
    db.commit()
    db.close()
    return rid


def _del_receipt(rid):
    from app.database import SessionLocal
    from app.models import Receipt
    db = SessionLocal()
    obj = db.get(Receipt, rid)
    if obj:
        db.delete(obj)
        db.commit()
    db.close()


class TestVersion1574:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.57.4"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.3" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.4':") < js.index("'1.57.3':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.4]") == 1
        assert ch.index("## [1.57.4]") < ch.index("## [1.57.3]")


class TestForceFetch:
    def test_full_data_without_force_skipped(self, client):
        rid = _mk_receipt(full_data=True)
        try:
            hdr = _login(client)
            r = client.post(f"/api/v1/receipts/{rid}/fetch-details", headers=hdr)
            assert r.status_code == 200
            assert r.json()["skipped"] is True       # старое поведение сохранено
        finally:
            _del_receipt(rid)

    def test_full_data_with_force_queued(self, client):
        rid = _mk_receipt(full_data=True)
        try:
            hdr = _login(client)
            r = client.post(f"/api/v1/receipts/{rid}/fetch-details?force=1",
                            headers=hdr)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["queued"] is True and "skipped" not in body
        finally:
            _del_receipt(rid)

    def test_new_receipt_always_queued(self, client):
        rid = _mk_receipt(full_data=False)
        try:
            hdr = _login(client)
            r = client.post(f"/api/v1/receipts/{rid}/fetch-details", headers=hdr)
            assert r.status_code == 200 and r.json()["queued"] is True
        finally:
            _del_receipt(rid)

    def test_bulk_force(self, client):
        rid = _mk_receipt(full_data=True)
        try:
            hdr = _login(client)
            r = client.post("/api/v1/receipts/fetch-details",
                            json={"receipt_ids": [rid], "force": True},
                            headers=hdr)
            assert r.status_code == 200, r.text
            assert r.json()["queued"] == 1           # force прошёл фильтр full_data
        finally:
            _del_receipt(rid)

    def test_bg_worker_respects_force_flag(self):
        """Воркер пропускает full_data только без force."""
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert "def _run_external_fetch(receipt_ids: list[str], force: bool = False)" in src
        assert "if receipt.full_data and not force:" in src
        assert "background.add_task(_run_external_fetch, [receipt.id], True)" in src

    def test_engine_actually_runs_on_force(self, client, monkeypatch):
        """force → движок реально вызывается и пишет ext_json."""
        rid = _mk_receipt(full_data=True)
        try:
            hdr = _login(client)
            from app.services.external import ExternalResult
            called = {}

            def fake_fetch(db, qr, fn, fd, fp, total, dtm):
                called["ok"] = True
                return ExternalResult(
                    ok=True, source="proverkacheka", found=True,
                    message="Данные получены", total_sum=807.30, operation=1,
                    merchant_name='ООО "СТРОЙТОРГОВЛЯ"', merchant_inn="4703148343",
                    items=[], raw={}, extra={"properties": [
                        {"name": "Номер заказа", "value": "151939ПРИ"}]})

            monkeypatch.setattr(
                "app.routers.receipts.external_engine.fetch", fake_fetch)
            r = client.post(f"/api/v1/receipts/{rid}/fetch-details?force=1",
                            headers=hdr)
            assert r.status_code == 200 and called.get("ok")
            from app.database import SessionLocal
            from app.models import Receipt
            db = SessionLocal()
            try:
                rec = db.get(Receipt, rid)
                assert rec.details_source == "proverkacheka"
                assert "151939ПРИ" in (rec.ext_json or "")
            finally:
                db.close()
        finally:
            _del_receipt(rid)


class TestUiForce:
    def test_card_button_uses_force(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "fetch-details?force=1" in js

    def test_schema_has_force(self):
        sc = open("app/schemas.py", encoding="utf-8").read()
        blk = sc.split("class FetchDetailsRequest")[1][:400]
        assert "force: bool = False" in blk


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Проверка чеков (ФНС и источники)"] == "1.57.4"
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет
