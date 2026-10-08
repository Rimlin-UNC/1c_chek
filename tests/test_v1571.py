# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.1: блок proverkacheka — устойчивый запрос
# (3 формата), ошибки с причиной, «Полные данные чека» в API и UI.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import re
from unittest.mock import patch as mock_patch

import httpx


class TestVersion1571:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.57.1 перенесён в tests/test_v1572.py (версия ушла вперёд)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.1':") < js.index("'1.57.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.1]") == 1
        assert ch.index("## [1.57.1]") < ch.index("## [1.57.0]")


class TestFetchRobust:
    def _fake_post(self, calls, responses):
        """responses: список (status, text/json) — по одному на вызов."""
        it = iter(responses)

        def _post(url, **kwargs):
            calls.append(kwargs)
            status, body = next(it)
            if isinstance(body, dict):
                return httpx.Response(status, json=body, request=httpx.Request("POST", url))
            return httpx.Response(status, text=body, request=httpx.Request("POST", url))
        return _post

    def test_form_ok_first_try(self):
        from app.services.external import fetch_proverkacheka
        calls = []
        payload = {"totalSum": 10000, "items": [{"name": "x", "price": 10000,
                                                 "sum": 10000, "quantity": 1}]}
        with mock_patch("httpx.post", self._fake_post(calls, [(200, payload)])):
            ok, msg, data = fetch_proverkacheka("t=…&s=100.00&fn=1&i=2&fp=3&n=1", "tok")
        assert ok and msg == "OK" and data["totalSum"] == 10000
        assert len(calls) == 1 and "data" in calls[0]     # urlencoded первым

    def test_fallback_form_json_multipart(self):
        from app.services.external import fetch_proverkacheka
        calls = []
        payload = {"totalSum": 10000, "items": []}
        with mock_patch("httpx.post", self._fake_post(
                calls, [(400, "bad format"), (422, "bad format"), (200, payload)])):
            ok, msg, data = fetch_proverkacheka("t=…", "tok")
        assert ok and data["totalSum"] == 10000
        assert len(calls) == 3
        assert "data" in calls[0] and "json" in calls[1] and "files" in calls[2]

    def test_all_formats_rejected_message(self):
        from app.services.external import fetch_proverkacheka
        calls = []
        with mock_patch("httpx.post", self._fake_post(
                calls, [(400, "need qr param"), (422, "no"), (400, "still no")])):
            ok, msg, data = fetch_proverkacheka("t=…", "tok")
        assert not ok and data == {}
        assert "Формат запроса не принят" in msg
        assert "still no" in msg                          # фрагмент ответа виден

    def test_token_and_quota_errors(self):
        from app.services.external import fetch_proverkacheka
        calls = []
        with mock_patch("httpx.post", self._fake_post(calls, [(401, "{}")])):
            ok, msg, _ = fetch_proverkacheka("t=…", "bad")
        assert not ok and "токен не принят" in msg
        with mock_patch("httpx.post", self._fake_post(calls, [(402, "{}")])):
            ok, msg, _ = fetch_proverkacheka("t=…", "tok")
        assert not ok and "квота" in msg

    def test_server_error_shows_snippet(self):
        from app.services.external import fetch_proverkacheka
        calls = []
        with mock_patch("httpx.post", self._fake_post(
                calls, [(500, "upstream exploded")])):
            ok, msg, _ = fetch_proverkacheka("t=…", "tok")
        assert not ok and "upstream exploded" in msg


class TestExtDataVisible:
    def test_to_dict_includes_ext(self):
        import uuid
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            rid = str(uuid.uuid4())
            db.add(Receipt(id=rid, qr_data="t=…", fn="77", fd="78", fp="79", total_sum=100.0,
                           receipt_date=dt.datetime(2026, 10, 5, 12, 30),
                           ext_json='{"taxation": "УСН (доходы)", "shift_number": 50}'))
            db.commit()
            rc = db.get(Receipt, rid)
            d = rc.to_dict()
            assert d["ext"]["taxation"] == "УСН (доходы)"
            assert d["ext"]["shift_number"] == 50
            db.delete(rc)
            db.commit()
        finally:
            db.close()

    def test_to_dict_broken_ext_is_none(self):
        import uuid
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            rid = str(uuid.uuid4())
            db.add(Receipt(id=rid, qr_data="t=…", fn="87", fd="88", fp="89", total_sum=1.0,
                           receipt_date=dt.datetime(2026, 10, 5, 12, 30),
                           ext_json="{broken json"))
            db.commit()
            rc = db.get(Receipt, rid)
            assert rc.to_dict()["ext"] is None
            db.delete(rc)
            db.commit()
        finally:
            db.close()

    def test_api_returns_ext(self, client):
        import uuid
        hdr = login_hdr(client)
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            rid = str(uuid.uuid4())
            db.add(Receipt(id=rid, qr_data="t=…", fn="67", fd="68", fp="69", total_sum=232.20,
                           receipt_date=dt.datetime(2026, 10, 5, 12, 30),
                           ext_json='{"retail_place": "Магазин \\"ТД\\""}'))
            db.commit()
        finally:
            db.close()
        r = client.get(f"/api/v1/receipts/{rid}", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["ext"]["retail_place"] == 'Магазин "ТД"'

    def test_ui_block_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function extBlockHTML(ext)" in js
        assert "${extBlockHTML(r.ext)" in js
        for label in ("Место расчётов", "ККТ (рег. номер)", "Смена",
                      "Налогообложение", "Формат ФФД", "Итоги НДС",
                      "Свойства заказа", "Признаки позиций"):
            assert label in js, label
        # суммы НДС из копеек
        assert "Number(n.ndsSum || 0) / 100" in js


def login_hdr(client):
    from tests.conftest import login
    return login(client, "admin", "admin123")


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        # точный пин 1.57.1 перенесён в tests/test_v1572.py
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 1)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Сканирование чеков"] == "1.56.2"      # не задет
