# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.9 (обновлены в v1.70.70): ошибки источника
# дают внятную причину (раньше — JSON-тела стороннего сервиса, теперь —
# SOAP-ошибки официального API ФНС по «Перечню ошибок»).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import logging
import re


class _Resp:
    def __init__(self, obj, text=None):
        self._obj = obj
        self.status_code = 200
        self.text = text if text is not None else str(obj)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._obj


def _engine(monkeypatch, payload):
    """Движок с подменённым HTTP: тело payload при HTTP 200 (form)."""
    from app.services import external
    eng = external.ExternalFetchEngine()
    monkeypatch.setattr(eng, "_settings", lambda db: {
        "proverkacheka_token": "55035.TEMP", "fns_master_token": ""})
    monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 2)
    monkeypatch.setattr(external.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(payload)

    monkeypatch.setattr(external.httpx, "post", fake_post)
    return eng, calls


class TestVersion1579:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 9)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.8" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.9':") < js.index("'1.57.8':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.9]") == 1
        assert ch.index("## [1.57.9]") < ch.index("## [1.57.8]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 9)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert tuple(int(x) for x in reg["Обновления"].split(".")) >= (1, 55, 1)              # не задет

    def test_settings_hint_fns(self):
        """v1.70.70: в настройках только мастер-токен и AppId ФНС."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'placeholder="вставьте мастер-токен ФНС"' in js
        assert 'id="fns-appid"' in js
        assert "55035.хххххх" not in js
        assert 'placeholder="55035.xxxxxxxxxxx"' not in js


def _reasons(res):
    """Причина неудачи — из текста результата."""
    return res.message or ""


class TestBodyErrors:
    """Ошибки API ФНС: каждая распознаётся и объясняется по-русски."""

    AUTH = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            'envelope/"><soap:Body><ns2:GetMessageResponse xmlns:ns2="urn://'
            'x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService'
            '/types/1.0"><ns2:Message><ns2:Result><ns2:Token>T</ns2:Token>'
            '</ns2:Result></ns2:Message></ns2:GetMessageResponse>'
            '</soap:Body></soap:Envelope>')

    def _client(self, monkeypatch, responder):
        from app.services import fns
        monkeypatch.setattr(fns, "_db_setting", lambda key: "")
        monkeypatch.setattr(fns.time, "sleep", lambda s: None)
        monkeypatch.setattr(fns.httpx, "post", responder)
        return fns.FnsClient(master_token="MASTER")

    def _resp(self, text, status=200):
        from app.services import fns
        return fns.httpx.Response(
            status, text=text,
            request=fns.httpx.Request("POST", "https://openapi.nalog.ru:8090"))

    def _fault(self, text: str):
        return ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                'envelope/"><soap:Body><soap:Fault><faultstring>'
                f"{text}</faultstring></soap:Fault></soap:Body>"
                "</soap:Envelope>")

    def test_token_rejected_is_named(self, monkeypatch, caplog):
        """Отказ по токену: причина названа, токен будет получен заново."""
        caplog.set_level(logging.INFO)
        state = {"n": 0}

        def responder(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return self._resp(self.AUTH)
            return self._resp(self._fault(
                "Отказ в доступе: не верное значение token"), 500)

        client = self._client(monkeypatch, responder)
        res = client.get_ticket("1", "2", "3", 1.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert res.ok is False
        assert "токен" in _reasons(res).lower()

    def test_daily_limit_is_quota(self, monkeypatch):
        """«Превышен общий дневной лимит» → статус quota (чек в очередь)."""
        state = {"n": 0}

        def responder(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return self._resp(self.AUTH)
            return self._resp(self._fault(
                "Превышен общий дневной лимит запросов, приложением "
                "YmasterAPI"), 500)

        client = self._client(monkeypatch, responder)
        res = client.get_ticket("1", "2", "3", 1.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert res.status == "quota"
        assert "лимит" in res.message.lower()

    def test_message_not_found(self, monkeypatch):
        """Сообщение с результатом не найдено — понятная причина."""
        state = {"n": 0}

        def responder(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return self._resp(self.AUTH)
            if state["n"] == 2:
                return self._resp(
                    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/'
                    'soap/envelope/"><soap:Body><ns2:SendMessageResponse '
                    'xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/'
                    'OpenApiAsyncMessageConsumerService/types/1.0">'
                    "<ns2:MessageId>m1</ns2:MessageId></ns2:SendMessageResponse>"
                    "</soap:Body></soap:Envelope>")
            return self._resp(self._fault(
                "MessageNotFoundException: Сообщение не найдено"), 500)

        client = self._client(monkeypatch, responder)
        res = client.get_ticket("1", "2", "3", 1.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert res.status == "not_found"
        assert "не найдено" in res.message.lower()

    def test_contract_error_explained(self, monkeypatch):
        """Ошибка валидации по схеме XSD объясняется как «не прошёл схему»."""
        state = {"n": 0}

        def responder(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return self._resp(self.AUTH)
            return self._resp(self._fault(
                "UnmarshallingFailureException: unexpected element"), 500)

        client = self._client(monkeypatch, responder)
        res = client.get_ticket("1", "2", "3", 1.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert "схем" in res.message

    def test_success_with_code_200(self, monkeypatch):
        """Code 200 + Ticket — успешный разбор чека."""
        ticket = ('{"id":"t","ofdId":"ofd","receipt":{"totalSum":89000,'
                  '"dateTime":"2026-10-01T09:08:00","user":"ООО Тест",'
                  '"userInn":"5000000000","items":[{"name":"Товар",'
                  '"price":89000,"sum":89000,"quantity":1}]}}')
        result = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                  'envelope/"><soap:Body><ns2:GetMessageResponse xmlns:ns2="'
                  'urn://x-artefacts-gnivc-ru/inplat/servin/'
                  'OpenApiAsyncMessageConsumerService/types/1.0"><ns2:Message>'
                  "<ns2:ProcessingStatus>COMPLETED</ns2:ProcessingStatus>"
                  "<ns2:Result><ns3:Code xmlns:ns3=\"urn://x-artefacts-gnivc-"
                  'ru/ais3/kkt/KktTicketService/types/1.0">200</ns3:Code>'
                  "<ns3:Ticket xmlns:ns3=\"urn://x-artefacts-gnivc-ru/ais3/"
                  f'kkt/KktTicketService/types/1.0">{ticket}</ns3:Ticket>'
                  "</ns2:Result></ns2:Message></ns2:GetMessageResponse>"
                  "</soap:Body></soap:Envelope>")
        state = {"n": 0}

        def responder(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return self._resp(self.AUTH)
            if state["n"] == 2:
                return self._resp(
                    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/'
                    'soap/envelope/"><soap:Body><ns2:SendMessageResponse '
                    'xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/'
                    'OpenApiAsyncMessageConsumerService/types/1.0">'
                    "<ns2:MessageId>m1</ns2:MessageId></ns2:SendMessageResponse>"
                    "</soap:Body></soap:Envelope>")
            return self._resp(result)

        client = self._client(monkeypatch, responder)
        res = client.get_ticket("1", "2", "3", 890.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert res.ok and res.status == "valid"
        assert res.raw["ticket"]["ofdId"] == "ofd"
