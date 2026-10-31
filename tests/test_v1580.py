# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты обмена с Открытыми API ФНС.
#
# v1.70.70: файл переписан. Раньше здесь закреплялась «рабочая схема»
# стороннего сервиса проверки чеков (multipart, перебор форматов, code 5).
# Сторонний сервис из программы удалён, поэтому те же сценарии проверяются
# на официальном SOAP API ФНС: отказ токена, повтор с новым токеном,
# состояния обработки, код 200 с чеком и отсутствие чека в базе.
#
# Сеть не используется: httpx.post подменяется ответами по документации
# «API 2.0.9 от 10.12.2024» (префиксы ns2:, вложенные Result/Ticket).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re

import pytest

AUTH_OK = (
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body><ns2:GetMessageResponse "
    'xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/'
    'OpenApiMessageConsumerService/types/1.0">'
    "<ns2:Message><ns2:Result><ns2:Token>TOKEN-777</ns2:Token>"
    "<ns2:ExpireTime>2026-10-31T18:40:00+03:00</ns2:ExpireTime>"
    "</ns2:Result></ns2:Message></ns2:GetMessageResponse></soap:Body>"
    "</soap:Envelope>"
)

SEND_OK = (
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body><ns2:SendMessageResponse "
    'xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/'
    'OpenApiAsyncMessageConsumerService/types/1.0">'
    "<ns2:MessageId>msg-42</ns2:MessageId>"
    "</ns2:SendMessageResponse></soap:Body></soap:Envelope>"
)

TICKET_JSON = ('{"id":"t-1","ofdId":"ofd-1","receipt":{"totalSum":89000,'
               '"dateTime":"2026-10-01T09:08:00","user":"ООО Тест",'
               '"userInn":"5000000000","items":[{"name":"Товар",'
               '"price":89000,"sum":89000,"quantity":1}]}}')


def _result_xml(status: str = "COMPLETED", code: str = "200",
                message: str = "", ticket: str = TICKET_JSON) -> str:
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><ns2:GetMessageResponse "
        'xmlns:ns2="urn://x-artefacts-gnivc-ru/inplat/servin/'
        'OpenApiAsyncMessageConsumerService/types/1.0">'
        "<ns2:Message>"
        f"<ns2:ProcessingStatus>{status}</ns2:ProcessingStatus>"
        "<ns2:Result>"
        f"<ns3:Code xmlns:ns3=\"urn://x-artefacts-gnivc-ru/ais3/kkt/"
        f"KktTicketService/types/1.0\">{code}</ns3:Code>"
        + (f"<ns3:Message xmlns:ns3=\"urn://x-artefacts-gnivc-ru/ais3/kkt/"
                        f"KktTicketService/types/1.0\">{message}</ns3:Message>"
           if message else "")
        + (f"<ns3:Ticket xmlns:ns3=\"urn://x-artefacts-gnivc-ru/ais3/kkt/"
                        f"KktTicketService/types/1.0\">{ticket}</ns3:Ticket>"
           if ticket else "")
        + "</ns2:Result></ns2:Message></ns2:GetMessageResponse>"
        "</soap:Body></soap:Envelope>")


class _Resp:
    """Минимальный ответ httpx для подмены."""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "text/xml; charset=utf-8"}

    def json(self):
        raise ValueError("not json")


def _fake_api(monkeypatch, responses):
    """Подменить httpx.post в клиенте ФНС очередью ответов.

    responses — список (как отвечать) или функция (номер запроса, тело,
    заголовки) → _Resp. Возвращает список сделанных запросов.
    """
    from app.services import fns

    calls = []
    it = iter(responses) if isinstance(responses, list) else None

    def fake_post(url, content=None, headers=None, timeout=None, **kw):
        calls.append({"url": url, "body": content or b"",
                      "headers": headers or {}})
        if it is not None:
            return next(it)
        return responses(len(calls), content or b"", headers or {})

    monkeypatch.setattr(fns.httpx, "post", fake_post)
    monkeypatch.setattr(fns.time, "sleep", lambda s: None)
    return calls


def _client(monkeypatch, **kw):
    from app.services import fns
    monkeypatch.setattr(fns, "_db_setting", lambda key: "")
    return fns.FnsClient(master_token="MASTER-TOKEN", **kw)


class TestAuthContract:
    """Аутентификация: адрес, пространство имён, разбор токена."""

    def test_auth_request_matches_documentation(self, monkeypatch):
        calls = _fake_api(monkeypatch, [_Resp(AUTH_OK)])
        client = _client(monkeypatch)
        assert client.token() == "TOKEN-777"
        assert len(calls) == 1
        req = calls[0]
        assert req["url"].endswith("/open-api/AuthService/0.1")
        body = req["body"].decode("utf-8")
        # тело — AuthRequest в пространстве имён сервиса аутентификации
        assert "urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0" in body
        assert "<tns:MasterToken>MASTER-TOKEN</tns:MasterToken>" in body
        assert req["headers"]["SOAPAction"] == '"urn:GetMessageRequest"'

    def test_token_cached_forty_minutes(self, monkeypatch):
        """Документация: токен кэшируется 40 минут — второй запрос без auth."""
        from app.services import fns
        calls = _fake_api(monkeypatch, [_Resp(AUTH_OK), _Resp(SEND_OK),
                                       _Resp(_result_xml())])
        client = _client(monkeypatch)
        client.token()
        client.token()
        assert fns.TOKEN_TTL_S == 40 * 60
        assert len(calls) == 1, "повторный токен не запрашивается"

    def test_bad_master_token_reason(self, monkeypatch):
        fault = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                 'envelope/"><soap:Body><soap:Fault><faultstring>Отказ в '
                 "доступе: не верное значение master-токена</faultstring>"
                 "</soap:Fault></soap:Body></soap:Envelope>")
        _fake_api(monkeypatch, [_Resp(fault, 500)])
        client = _client(monkeypatch)
        ok, msg = client.probe()
        assert ok is False and "токен" in msg.lower()


class TestTicketContract:
    """Проверка чека: SendMessage → GetMessage → Code 200 + Ticket."""

    def _run(self, monkeypatch, responses):
        calls = _fake_api(monkeypatch, responses)
        client = _client(monkeypatch)
        res = client.get_ticket("7381440700130934", "33079", "3673437411",
                                890.0, __import__("datetime").datetime(2026, 10, 1, 9, 8))
        return res, calls

    def test_send_message_headers_and_namespaces(self, monkeypatch):
        res, calls = self._run(monkeypatch, [
            _Resp(AUTH_OK), _Resp(SEND_OK), _Resp(_result_xml())])
        assert res.status == "valid"
        send = calls[1]
        assert send["url"].endswith("/open-api/ais3/KktService/0.1")
        assert send["headers"]["SOAPAction"] == '"urn:SendMessageRequest"'
        assert send["headers"]["FNS-OpenApi-Token"] == "TOKEN-777"
        assert "FNS-OpenApi-UserToken" in send["headers"]
        body = send["body"].decode("utf-8")
        assert "OpenApiAsyncMessageConsumerService/types/1.0" in body
        assert "urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0" in body
        for tag in ("Sum", "Date", "Fn", "TypeOperation", "FiscalDocumentId",
                    "FiscalSign", "RawData"):
            assert f"<tns:{tag}>" in body
        assert "<tns:Sum>89000</tns:Sum>" in body      # копейки
        assert "<tns:RawData>true</tns:RawData>" in body

    def test_completed_with_code_200_is_valid(self, monkeypatch):
        res, calls = self._run(monkeypatch, [
            _Resp(AUTH_OK), _Resp(SEND_OK), _Resp(_result_xml())])
        assert res.ok and res.status == "valid"
        assert res.raw["code"] == "200"
        assert res.raw["ticket"]["ofdId"] == "ofd-1"
        poll = calls[2]
        assert poll["headers"]["SOAPAction"] == '"urn:GetMessageRequest"'
        assert b"msg-42" in poll["body"]

    def test_processing_then_completed(self, monkeypatch):
        res, calls = self._run(monkeypatch, [
            _Resp(AUTH_OK), _Resp(SEND_OK),
            _Resp(_result_xml(status="PROCESSING", code="", ticket="")),
            _Resp(_result_xml(status="PROCESSING", code="", ticket="")),
            _Resp(_result_xml())])
        assert res.status == "valid"
        assert len(calls) == 5, "два опроса до готовности"

    def test_receipt_absent_is_not_found(self, monkeypatch):
        res, _ = self._run(monkeypatch, [
            _Resp(AUTH_OK), _Resp(SEND_OK),
            _Resp(_result_xml(code="404", message="Чек не найден",
                              ticket=""))])
        assert res.ok is True
        assert res.status == "not_found"

    def test_no_master_token_refused_early(self, monkeypatch):
        from app.services import fns
        monkeypatch.setattr(fns, "_db_setting", lambda key: "")
        client = fns.FnsClient(master_token="")
        res = client.get_ticket("1", "2", "3", 1.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert res.ok is False and "Мастер-токен" in res.message


class TestErrors:
    """Ошибки по «Перечню ошибок Открытых SOAP API»."""

    def test_ip_access_denied(self, monkeypatch):
        fault = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                 'envelope/"><soap:Body><soap:Fault><faultstring>'
                 "AccessDeniedException: Доступ к сервису для переданного IP, "
                 "запрещен</faultstring></soap:Fault></soap:Body>"
                 "</soap:Envelope>")
        _fake_api(monkeypatch, [_Resp(fault, 500)])
        client = _client(monkeypatch)
        ok, msg = client.probe()
        assert ok is False
        assert "IP" in msg and "разрешённых" in msg

    def test_daily_limit_reported_as_quota(self, monkeypatch):
        from app.services import fns
        fault = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
                 'envelope/"><soap:Body><soap:Fault><faultstring>Превышен '
                 "общий дневной лимит запросов, приложением YmasterAPI"
                 "</faultstring></soap:Fault></soap:Body></soap:Envelope>")
        _fake_api(monkeypatch, [_Resp(AUTH_OK), _Resp(fault, 500)])
        monkeypatch.setattr(fns, "_db_setting", lambda key: "")
        client = fns.FnsClient(master_token="M")
        res = client.get_ticket("1", "2", "3", 1.0,
                                __import__("datetime").datetime(2026, 1, 1))
        assert res.status == "quota"

    def test_unmarshalling_is_contract_error(self):
        from app.services import fns
        kind, text = fns.classify_error(
            "UnmarshallingFailureException: unexpected element")
        assert kind == "contract" and "схем" in text


class TestVersion1580:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 58, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 58, 0)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
