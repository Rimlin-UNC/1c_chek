# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.8 (обновлены в v1.70.70): сырой обмен с
# источником виден в журнале. Источник теперь один — Открытые API ФНС.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import logging
import re


class _FakeResp:
    """Подменный ответ httpx для проверки журналирования."""

    def __init__(self, status_code=200, text='{"code": 0, "message": "ok"}',
                 json_obj=None):
        self.status_code = status_code
        self.text = text
        self._json = json_obj if json_obj is not None else {"code": 0}
        self.content = text.encode("utf-8")

    def json(self):
        return self._json


class TestVersion1578:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 8)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.7" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.8':") < js.index("'1.57.7':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.8]") == 1
        assert ch.index("## [1.57.8]") < ch.index("## [1.57.7]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 8)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert tuple(int(x) for x in reg["Обновления"].split(".")) >= (1, 55, 1)              # не задет


class TestRawResponseLogging:
    """v1.70.70: каждый обмен с ФНС виден в журнале."""

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

    AUTH = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            'envelope/"><soap:Body><ns2:GetMessageResponse xmlns:ns2="urn://'
            'x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService'
            '/types/1.0"><ns2:Message><ns2:Result><ns2:Token>T</ns2:Token>'
            '</ns2:Result></ns2:Message></ns2:GetMessageResponse>'
            '</soap:Body></soap:Envelope>')

    def test_http_status_logged(self, monkeypatch, caplog):
        """Ответ ФНС пишется в журнал с адресом, кодом и размером."""
        caplog.set_level(logging.INFO)
        client = self._client(monkeypatch,
                              lambda *a, **k: self._resp(self.AUTH))
        assert client.token() == "T"
        txt = caplog.text
        assert "📩 ФНС /open-api/AuthService/0.1: HTTP 200" in txt
        assert "байт" in txt

    def test_error_body_snippet_in_log(self, monkeypatch, caplog):
        """Тело ошибки доступно в журнале (фрагмент до 400 символов)."""
        caplog.set_level(logging.INFO)
        fault = ("AccessDeniedException: Доступ к сервису для переданного "
                 "IP, запрещен")
        client = self._client(monkeypatch,
                              lambda *a, **k: self._resp(fault, 500))
        ok, msg = client.probe()
        assert ok is False
        assert "AccessDenied" in caplog.text
        assert "IP" in msg

    def test_parse_message_lists_keys(self, monkeypatch, caplog):
        """«Данных чека нет» дополнено ключами присланного ответа."""
        from app.services import external
        res = external.parse_receipt_payload(
            {"status": "error", "message": "чек не найден"}, None)
        assert res.found is False
        assert "ключи ответа: status, message" in res.message
