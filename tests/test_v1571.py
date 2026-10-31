# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.1: блок proverkacheka — устойчивый запрос
# (3 формата), ошибки с причиной, «Полные данные чека» в API и UI.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
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
    """v1.70.70: устойчивость оставшегося единственного источника — ФНС.

    Раньше здесь проверялся перебор форматов стороннего сервиса. Теперь
    проверяем то, что действительно важно для ФНС: сетевой сбой, отказ
    по токену с повтором, исчерпанный лимит и понятная причина в тексте.
    """

    def _fake(self, monkeypatch, responses):
        from app.services import fns

        calls = []
        it = iter(responses)

        def fake_post(url, content=None, headers=None, timeout=None, **kw):
            calls.append({"url": url, "headers": headers or {},
                          "body": content or b""})
            return next(it)

        monkeypatch.setattr(fns.httpx, "post", fake_post)
        monkeypatch.setattr(fns.time, "sleep", lambda s: None)
        monkeypatch.setattr(fns, "_db_setting", lambda key: "")
        return fns.FnsClient(master_token="MASTER"), calls

    def _resp(self, text, status=200):
        from app.services import fns
        return fns.httpx.Response(
            status, text=text,
            request=fns.httpx.Request("POST", "https://openapi.nalog.ru:8090"))

    def test_network_failure_reason(self, monkeypatch):
        from app.services import fns
        client = fns.FnsClient(master_token="MASTER")

        def boom(*a, **k):
            raise fns.httpx.ConnectError("network down")

        monkeypatch.setattr(fns.httpx, "post", boom)
        res = client.get_ticket("1", "2", "3", 1.0,
                                dt.datetime(2026, 1, 1))
        assert res.ok is False and "сеть недоступна" in res.message.lower()

    def test_server_fault_reason_visible(self, monkeypatch):
        auth = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                'envelope/"><soap:Body><ns2:GetMessageResponse xmlns:ns2="urn://'
                'x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService'
                '/types/1.0"><ns2:Message><ns2:Result><ns2:Token>T1</ns2:Token>'
                '</ns2:Result></ns2:Message></ns2:GetMessageResponse>'
                '</soap:Body></soap:Envelope>')
        fault = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                 'envelope/"><soap:Body><soap:Fault><faultstring>'
                 "UnmarshallingFailureException: unexpected element Fn"
                 "</faultstring></soap:Fault></soap:Body></soap:Envelope>")
        client, _ = self._fake(monkeypatch, [
            self._resp(auth), self._resp(fault, 500)])
        res = client.get_ticket("1", "2", "3", 1.0, dt.datetime(2026, 1, 1))
        assert res.ok is False
        assert "схем" in res.message, res.message

    def test_token_refused_once_then_refreshed(self, monkeypatch):
        auth = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                'envelope/"><soap:Body><ns2:GetMessageResponse xmlns:ns2="urn://'
                'x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService'
                '/types/1.0"><ns2:Message><ns2:Result><ns2:Token>T1</ns2:Token>'
                '</ns2:Result></ns2:Message></ns2:GetMessageResponse>'
                '</soap:Body></soap:Envelope>')
        send = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                'envelope/"><soap:Body><ns2:SendMessageResponse xmlns:ns2="urn://'
                'x-artefacts-gnivc-ru/inplat/servin/'
                'OpenApiAsyncMessageConsumerService/types/1.0">'
                '<ns2:MessageId>m1</ns2:MessageId></ns2:SendMessageResponse>'
                '</soap:Body></soap:Envelope>')
        client, calls = self._fake(monkeypatch, [
            self._resp(auth), self._resp("denied", 401),
            self._resp(auth), self._resp(send), self._resp("bad", 500)])
        client.get_ticket("1", "2", "3", 1.0, dt.datetime(2026, 1, 1))
        # после 401 токен запрошен заново (auth-запрос второй раз)
        assert sum(1 for c in calls if "AuthService" in c["url"]) == 2

    def test_quota_exhausted_before_request(self, monkeypatch):
        from app.services import fns, fns_quota
        monkeypatch.setattr(fns_quota, "spend", lambda amount=1, db=None: False)
        monkeypatch.setattr(fns, "_db_setting", lambda key: "")
        client = fns.FnsClient(master_token="MASTER")
        res = client.get_ticket("1", "2", "3", 1.0, dt.datetime(2026, 1, 1))
        assert res.status == "quota"
        assert "лимит" in res.message.lower()


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
        assert tuple(int(x) for x in reg["Сканирование чеков"].split(".")) >= (1, 56, 2)      # не задет
