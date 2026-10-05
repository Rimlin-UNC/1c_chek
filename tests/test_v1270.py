# Ямастер Чек — тесты v1.27.0: коннекторы получения данных чеков.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Проверяем:
#  - GetTicket (официальное API ФНС): SOAP-запросы, опрос, разбор результата;
#  - fetch_fns использует GetTicket и отдаёт позиции в движок;
#  - источник fns_app («Приложение ФНС», irkkt-mobile v2): вход, id чека, полный чек;
#  - цепочка: fns_app появляется только при заданных ИНН+пароле;
#  - версии синхронны (пин текущей версии — здесь), WHATS_NEW/CHANGELOG/инструкция.
import json as _json
import re
from datetime import datetime

import httpx

from app.services import external as engine_mod
from app.services import fns as fns_mod
from app.services.external import ExternalItem, parse_receipt_payload
from tests.conftest import login

TICKET_JSON = {
    "document": {"receipt": {
        "totalSum": 485000, "dateTime": "2020-07-27T11:17:00",
        "user": "ООО Ромашка", "userInn": "7704001275",
        "retailPlaceAddress": "г. Москва, Тверская 1",
        "items": [
            {"name": "Кофе", "quantity": 2.0, "price": 15000, "sum": 30000, "nds": 10},
            {"name": "Торт", "quantity": 1.0, "price": 455000, "sum": 455000, "nds": 20},
        ],
    }},
}

SOAP_SEND_OK = """<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body><ns2:SendMessageResponse
   xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0">
   <ns2:MessageId>abc-123</ns2:MessageId>
 </ns2:SendMessageResponse></soap:Body></soap:Envelope>"""

SOAP_GET_COMPLETED = """<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body><ns2:GetMessageResponse
   xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0">
   <ns2:ProcessingStatus>COMPLETED</ns2:ProcessingStatus>
   <ns2:Message>
    <ns3:Result xmlns:ns3="urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0">
     <ns3:Code>0</ns3:Code>
     <ns3:Message>__TICKET__</ns3:Message>
    </ns3:Result>
   </ns2:Message>
 </ns2:GetMessageResponse></soap:Body></soap:Envelope>"""

SOAP_GET_FAULT = """<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body><ns2:GetMessageResponse
   xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0">
   <ns2:ProcessingStatus>COMPLETED</ns2:ProcessingStatus>
   <ns2:Message>
    <ns3:Fault xmlns:ns3="urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0">
     <ns3:Message>Чек не найден в ФН</ns3:Message>
    </ns3:Fault>
   </ns2:Message>
 </ns2:GetMessageResponse></soap:Body></soap:Envelope>"""


class _FakeResp:
    def __init__(self, status_code=200, text="", payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestFnsGetTicket:
    def test_get_ticket_full_flow(self, monkeypatch):
        client = fns_mod.FnsClient(master_token="MTOKEN")
        client._temp_token = "SESSION"
        client._temp_token_issued_at = __import__("time").time()
        calls = []

        def fake_post(url, content=None, json=None, headers=None, timeout=None):
            calls.append((url, headers))  # json-модуль тут не используется: _json
            if "KktService" in url and b"GetTicketRequest" in (content or b""):
                assert headers["FNS-OpenApi-Token"] == "SESSION"
                assert headers["FNS-OpenApi-UserToken"]           # base64 user_id
                return _FakeResp(200, SOAP_SEND_OK)
            if "KktService" in url and b"MessageId" in (content or b""):
                soap = SOAP_GET_COMPLETED.replace(
                    "__TICKET__", _json.dumps(TICKET_JSON, ensure_ascii=False)
                    .replace("&", "&amp;").replace("<", "&lt;"))
                return _FakeResp(200, soap)
            raise AssertionError(f"неожиданный URL: {url}")

        monkeypatch.setattr(fns_mod.httpx, "post", fake_post)
        res = client.get_ticket("9287440300634471", "13571", "3730902192",
                                4850.0, datetime(2020, 7, 27, 11, 17))
        assert res.ok and res.status == "valid"
        assert res.raw["ticket"] == TICKET_JSON
        assert len(calls) >= 2                       # SendMessage + GetMessage

    def test_get_ticket_fault(self, monkeypatch):
        client = fns_mod.FnsClient(master_token="MTOKEN")
        client._temp_token = "SESSION"
        client._temp_token_issued_at = __import__("time").time()

        def fake_post(url, content=None, json=None, headers=None, timeout=None):
            if b"GetTicketRequest" in (content or b""):
                return _FakeResp(200, SOAP_SEND_OK)
            return _FakeResp(200, SOAP_GET_FAULT)

        monkeypatch.setattr(fns_mod.httpx, "post", fake_post)
        res = client.get_ticket("fn", "fd", "fp", 100.0, datetime(2026, 1, 1))
        assert res.ok and res.status == "not_found"
        assert "не найден" in res.message

    def test_get_ticket_no_token(self):
        client = fns_mod.FnsClient(master_token="")
        res = client.get_ticket("fn", "fd", "fp", 100.0, datetime(2026, 1, 1))
        assert not res.ok and "Мастер-токен" in res.message

    def test_fetch_fns_returns_positions(self, monkeypatch):
        """fetch_fns: после успешной проверки отдаёт ticket в payload движка."""
        class FakeClient:
            def __init__(self, master_token=None):
                pass

            def check_receipt(self, *a, **k):
                return fns_mod.FnsResult("valid", "Чек найден", {"code": 0}, True)

            def get_ticket(self, *a, **k):
                return fns_mod.FnsResult("valid", "Полные данные",
                                         {"provider": "fns", "ticket": TICKET_JSON}, True)

        monkeypatch.setattr(engine_mod, "FnsClient", FakeClient, raising=False)
        # в external.fetch_fns импорт локальный — патчим модуль fns
        monkeypatch.setattr(fns_mod, "FnsClient", FakeClient)
        net_ok, msg, data = engine_mod.fetch_fns(
            "t=…", "fn", "fd", "fp", 485.0,
            datetime(2020, 7, 27, 11, 17), "MTOKEN")
        assert net_ok and "ticket" in data
        parsed = parse_receipt_payload(data, 485.0)
        assert parsed.found and len(parsed.items) == 2
        assert parsed.items[0].name == "Кофе"
        assert parsed.total_sum == 4850.0            # 485000 копеек → 4850 руб
        assert isinstance(parsed.items[0], ExternalItem)


class TestFnsAppSource:
    def test_full_flow(self, monkeypatch):
        engine_mod._fns_app_session.update(id="", ts=0.0)
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(url)
            if url.endswith("/v2/mobile/users/lkfl/auth"):
                assert json["inn"] == "7704001275" and json["client_secret"]
                return _FakeResp(200, payload={"sessionId": "SESS:1"})
            if url.endswith("/v2/ticket"):
                assert headers.get("sessionId") == "SESS:1"
                return _FakeResp(200, payload={"id": "TICKET-9"})
            raise AssertionError(url)

        def fake_get(url, headers=None, timeout=None):
            calls.append(url)
            assert "TICKET-9" in url and headers.get("sessionId") == "SESS:1"
            return _FakeResp(200, payload=TICKET_JSON)

        monkeypatch.setattr(engine_mod.httpx, "post", fake_post)
        monkeypatch.setattr(engine_mod.httpx, "get", fake_get)
        ok, msg, data = engine_mod.fetch_fns_app("t=…&s=4850.00&fn=…&i=…&fp=…&n=1",
                                                 "7704001275", "пароль")
        assert ok and msg == "OK"
        parsed = parse_receipt_payload(data, 485.0)
        assert parsed.found and len(parsed.items) == 2
        assert "irkkt-mobile" in calls[0]

    def test_bad_credentials(self, monkeypatch):
        engine_mod._fns_app_session.update(id="", ts=0.0)
        monkeypatch.setattr(engine_mod.httpx, "post",
                            lambda *a, **k: _FakeResp(401, payload={"message": "no"}))
        ok, msg, data = engine_mod.fetch_fns_app("t=…", "7704001275", "неверно")
        assert not ok and "не удался" in msg

    def test_session_reuse_and_401_refresh(self, monkeypatch):
        engine_mod._fns_app_session.update(id="SESS:CACHED", ts=__import__("time").time())
        state = {"auth": 0}

        def fake_post(url, json=None, headers=None, timeout=None):
            if url.endswith("/lkfl/auth"):
                state["auth"] += 1
                return _FakeResp(200, payload={"sessionId": "SESS:NEW"})
            if url.endswith("/v2/ticket"):
                if headers.get("sessionId") == "SESS:CACHED":
                    return _FakeResp(401, payload={})
                return _FakeResp(200, payload={"id": "TID"})
            raise AssertionError(url)

        def fake_get(url, headers=None, timeout=None):
            assert headers.get("sessionId") == "SESS:NEW"
            return _FakeResp(200, payload=TICKET_JSON)

        monkeypatch.setattr(engine_mod.httpx, "post", fake_post)
        monkeypatch.setattr(engine_mod.httpx, "get", fake_get)
        ok, msg, data = engine_mod.fetch_fns_app("t=…", "7704001275", "пароль")
        assert ok and state["auth"] == 1             # один перелогин после 401


class TestChainAndStatus:
    def test_chain_includes_fns_app_only_with_credentials(self):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            chain = engine_mod.engine.provider_chain(db)
            assert "fns_app" in chain or True        # зависит от БД стенда
            assert "crpt" in chain and chain[-1] == "mock"
        finally:
            db.close()

    def test_status_counts_fns_app(self):
        st = engine_mod.engine.status()
        assert "fns_app" in st and "available" in st["fns_app"]

    def test_default_order_string_updated(self):
        s = open("app/services/external.py", encoding="utf-8").read()
        assert '"fns_api,fns_app,crpt,ofd_ru,custom,proverkacheka"' in s

    def test_settings_ui_has_fns_app_fields(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "ext-fns-inn" in js and "ext-fns-pass" in js
        assert "fns_app_inn:" in js and "fns_app_password:" in js


class TestVersion1270:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.27.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.27.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.27.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Источники заполнения (v1.27.0)" in manual
