# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.5: блок загрузки данных чека из v1.25.1 —
# запрос уходит всегда, значок «📥✓» в списке.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import re

_N = {"k": 0}


def _login(client):
    from tests.conftest import login
    return login(client, "admin", "admin123")


def _mk_receipt(full_data: bool):
    """Прямая фикстура чека; fn/fd/fp уникальны (UNIQUE на тройку)."""
    import uuid
    from app.database import SessionLocal
    from app.models import Receipt
    _N["k"] += 1
    k = _N["k"]
    db = SessionLocal()
    rid = str(uuid.uuid4())
    db.add(Receipt(id=rid,
                   qr_data=(f"t=20261008T1305&s=807.30&fn=81230{k:03d}"
                            f"&i=98{k:03d}&fp=3304159{k:03d}&n=1"),
                   fn=f"81230{k:03d}", fd=f"98{k:03d}", fp=f"3304159{k:03d}",
                   total_sum=807.30,
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


class TestVersion1575:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 5)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.4" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.5':") < js.index("'1.57.4':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.5]") == 1
        assert ch.index("## [1.57.5]") < ch.index("## [1.57.4]")


class TestV1251Block:
    def test_single_always_queued(self, client):
        """Чек с full_data=1 → запрос ВСЕГДА уходит (v1.25.1)."""
        rid = _mk_receipt(full_data=True)
        try:
            hdr = _login(client)
            r = client.post(f"/api/v1/receipts/{rid}/fetch-details", headers=hdr)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["queued"] is True
            assert "skipped" not in body
            assert "2–7 с" in body["message"]        # текст v1.25.1
        finally:
            _del_receipt(rid)

    def test_bulk_always_queued(self, client):
        """Массовый запрос: вся выборка в очередь, без «пропуска полных»."""
        r1 = _mk_receipt(full_data=True)
        r2 = _mk_receipt(full_data=False)
        try:
            hdr = _login(client)
            r = client.post("/api/v1/receipts/fetch-details",
                            json={"receipt_ids": [r1, r2]}, headers=hdr)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["queued"] == 2 and body["skipped"] == 0
            assert "по очереди" in body["message"]   # текст v1.25.1
        finally:
            _del_receipt(r1)
            _del_receipt(r2)

    def test_worker_fetches_everything(self):
        """Воркер v1.25.1: пропуска нет, решение за человеком."""
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        w = src.split("def _run_external_fetch")[1].split("def _maybe_auto_fetch")[0]
        assert "full_data" not in w
        assert "решение о запросе принимает человек" in src

    def test_refusals_removed(self):
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert "повторный запрос не требуется" not in src

    def test_engine_runs_on_button(self, client, monkeypatch):
        """Кнопка → движок реально вызывается → ext_json записан."""
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
                assert rec.full_data is True
            finally:
                db.close()
        finally:
            _del_receipt(rid)


class TestListBadge:
    def test_badge_and_button(self):
        """v1.25.1 в списке: «📥✓» у полученных, «📥» у остальных."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "📥✓" in js
        assert "Обновить данные» — в карточке чека" in js
        assert "Получить полные данные чека из сервиса проверки" in js
        assert "r.full_data ? '' :" not in js


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 5)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет
