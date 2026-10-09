# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.8: сырой ответ proverkacheka.com в журнале.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
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
        assert reg["Обновления"] == "1.55.1"              # не задет


class TestRawResponseLogging:
    def test_http_status_logged(self, monkeypatch, caplog):
        """Каждый HTTP-ответ пишется в журнал с кодом и размером."""
        from app.services import external
        caplog.set_level(logging.INFO)

        def fake_post(*a, **k):
            return _FakeResp(200, '{"user": "ООО", "totalSum": 80730}',
                             json_obj={"user": "ООО", "totalSum": 80730})

        monkeypatch.setattr(external.httpx, "post", fake_post)
        ok, msg, data = external.fetch_proverkacheka("t=…&s=…", "tok")
        assert ok and data["totalSum"] == 80730
        txt = caplog.text
        assert "📩 proverkacheka [multipart]: HTTP 200" in txt   # v1.58.0
        assert "ответ:" in txt and '"totalSum": 80730' in txt.replace(" ", " ") \
            or "📩" in txt and "totalSum" in txt

    def test_body_snippet_in_log(self, monkeypatch, caplog):
        """Тело ответа доступно в журнале (до 700 символов)."""
        from app.services import external
        caplog.set_level(logging.INFO)
        body = '{"error": "ticket not found", "detail": "нет чека"}'

        def fake_post(*a, **k):
            return _FakeResp(200, body, json_obj={"error": "ticket not found",
                                                  "detail": "нет чека"})

        monkeypatch.setattr(external.httpx, "post", fake_post)
        ok, msg, data = external.fetch_proverkacheka("qr", "tok")
        assert ok
        assert "ticket not found" in caplog.text

    def test_parse_message_lists_keys(self, monkeypatch, caplog):
        """«Данных чека нет» дополнено ключами присланного JSON."""
        from app.services import external
        res = external.parse_receipt_payload(
            {"status": "error", "message": "чек не найден"}, None)
        assert res.found is False
        assert "ключи ответа: status, message" in res.message
        # и эта строка попадает в ⚠️-журнал движка при загрузке
        caplog.set_level(logging.INFO)
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": ""})
        monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 0)
        monkeypatch.setattr(external.time, "sleep", lambda s: None)

        def fake_post(*a, **k):
            return _FakeResp(200, '{"status": "error"}',
                             json_obj={"status": "error"})

        monkeypatch.setattr(external.httpx, "post", fake_post)
        out = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert out.source == "mock"                  # реальные не дали данных
        assert "ключи ответа: status" in caplog.text
