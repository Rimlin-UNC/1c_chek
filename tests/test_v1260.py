# ======================================================================
# Ямастер Чек — тесты v1.26.0: источники заполнения чеков.
# Новый анонимный источник «Честный Знак» (mobile.api.crpt.ru), порядок
# цепочки (proverkacheka — последним, беречь квоту), честная полнота
# («ответ без позиций» ≠ заполнение → движок идёт к следующему источнику).
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import json
import re
from unittest.mock import patch

import httpx

from tests.conftest import login, client  # noqa: F401


class TestCrptSource:
    def test_fetch_crpt_parses_receipt(self):
        """fetch_crpt: ответ ЧЗ с данными чека разбирается парсером."""
        from app.services import external as ex

        payload = {"document": {"receipt": {
            "totalSum": 80000, "dateTime": "2026-10-02T12:00:00",
            "user": "ООО Лента", "userInn": "780100000090",
            "retailPlaceAddress": "СПб, Ленина 1",
            "items": [{"name": "Молоко", "quantity": 2, "price": 40,
                       "sum": 80, "vat_rate": "none", "vat_sum": 0}]}}}

        class R:
            status_code = 200
            def json(self):
                return payload

        with patch.object(ex.httpx, "post", return_value=R()) as m:
            net_ok, msg, data = ex.fetch_crpt("t=20261002T1200&s=800.00&fn=1&i=2&fp=3&n=1")
        assert net_ok is True and msg == "OK"
        parsed = ex.parse_receipt_payload(data, 800.0)
        assert parsed.found is True and parsed.items
        assert parsed.merchant_name == "ООО Лента"
        # запрос — ровно как в приложении (подход nechestniy_znak)
        args, kwargs = m.call_args
        assert args[0] == ex.CRPT_URL
        assert kwargs["json"]["codeType"] == "qr"

    def test_fetch_crpt_network_error_and_limits(self):
        from app.services import external as ex

        def boom(url, **k):
            raise httpx.ConnectError("нет сети")

        with patch.object(ex.httpx, "post", side_effect=boom):
            net_ok, msg, _ = ex.fetch_crpt("t=…")
        assert net_ok is False and "Сеть" in msg

        class Lim:
            status_code = 429
            def json(self):
                return {}

        with patch.object(ex.httpx, "post", return_value=Lim()):
            net_ok, msg, _ = ex.fetch_crpt("t=…")
        assert net_ok is False and "429" in msg

    def test_crpt_in_chain_without_token(self, client):
        """crpt доступен анонимно; proverkacheka без токена — нет."""
        from app.services.external import engine
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            chain = engine.provider_chain(db)
        finally:
            db.close()
        assert "crpt" in chain
        assert chain[-1] == "mock"

    def test_default_order_keeps_proverkacheka_last(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "fns_api,crpt,ofd_ru,custom,proverkacheka" in js
        src = open("app/services/external.py", encoding="utf-8").read()
        assert '"fns_api,crpt,ofd_ru,custom,proverkacheka"' in src
        # Честный Знак описан в настройках
        assert "Честный Знак" in js

    def test_external_status_reports_crpt(self, client):
        adm = login(client, "admin", "admin123")
        d = client.get("/api/v1/receipts/external/status", headers=adm).json()
        assert d["configured"]["crpt"] is True
        assert "crpt" in d["chain"]


class TestHonestCompleteness:
    def test_source_without_items_moves_to_next(self, client, monkeypatch):
        """Источник ответил, но позиций нет → движок идёт дальше, не выдаёт
        «пустое заполнение»; итог: found=False."""
        from app.services import external as ex

        def fake_crpt(qr):
            return True, "OK", {"answer": "нет данных чека"}   # ответ без чека

        monkeypatch.setattr(ex, "fetch_crpt", fake_crpt)
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            res = ex.engine.fetch(db, "t=…", "fn", "fd", "fp", 100.0, None)
        finally:
            db.close()
        assert res.found is False
        assert any("crpt" in e for e in
                   (res.raw.get("engine", {}).get("errors") or []))

    def test_full_data_requires_positions(self, client):
        """full_data ставится только при позициях; ответ без позиций не
        помечает чек полными данными."""
        import app.routers.receipts as rr
        adm, comp, U = None, None, None
        from tests.test_v2520 import _mk, _scan, _set_db  # переиспользуем хелперы
        adm, comp, U = _mk(client, prefix="h26")
        r1 = _scan(client, U)
        _set_db(r1["id"], full_data=False)
        res = rr.ExternalResult(ok=True, source="x", found=True)   # без позиций
        db = rr.SessionLocal() if hasattr(rr, "SessionLocal") else None
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            rec = db.get(Receipt, r1["id"])
            rr._apply_external_result(db, rec, res)
            assert rec.full_data is False
        finally:
            db.close()

    def test_full_data_with_positions_set(self, client):
        """Ответ с позициями (найден) → чек помечается полными данными."""
        import app.routers.receipts as rr
        from tests.test_v2520 import _mk, _scan
        from app.database import SessionLocal
        from app.models import Receipt
        adm, comp, U = _mk(client, prefix="p26")
        r1 = _scan(client, U)
        from app.services.external import ExternalItem
        items = [ExternalItem(name="Хлеб", quantity=1, price=45,
                                 total=45, vat_rate="none", vat_sum=0)]
        res = rr.ExternalResult(ok=True, source="crpt", found=True,
                                total_sum=45.0, items=items)
        db = SessionLocal()
        try:
            rec = db.get(Receipt, r1["id"])
            rr._apply_external_result(db, rec, res)
            assert rec.full_data is True
        finally:
            db.close()


class TestVersion1260:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # Пин конкретной версии перенесён в tests/test_v1261.py (тест текущей версии)
        # v1.26.0: assert ver == "1.26.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.26.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.26.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Источники заполнения" in manual
