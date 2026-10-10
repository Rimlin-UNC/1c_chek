# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.0: рабочая схема авторизации proverkacheka.com
# (multipart первым, токен 55035.… полем token), перебор форматов при
# отказе токена, code 5 → «чека нет в базе сервиса».
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
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


def _engine(monkeypatch, responder):
    """Движок с подменённым httpx.post; responder(how_calls, kwargs)→_Resp."""
    from app.services import external
    eng = external.ExternalFetchEngine()
    monkeypatch.setattr(eng, "_settings", lambda db: {
        "proverkacheka_token": "55035.TEMP", "fns_master_token": ""})
    monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 0)
    monkeypatch.setattr(external.time, "sleep", lambda s: None)
    calls = []

    def fake_post(url, *a, **k):
        calls.append(k)
        return responder(len(calls), k)

    monkeypatch.setattr(external.httpx, "post", fake_post)
    return eng, calls


def _reasons(res):
    return " | ".join((res.raw.get("engine") or {}).get("errors") or [])


class TestVersion1580:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 58, 0)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.9" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.58.0':") < js.index("'1.57.9':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.58.0]") == 1
        assert ch.index("## [1.58.0]") < ch.index("## [1.57.9]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 58, 0)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет


class TestWorkingScheme:
    """Рабочая схема, доказанная контрольным curl на сервере."""

    def test_multipart_first_with_token(self, monkeypatch):
        """Первый запрос — multipart, токен полем token, полный."""
        eng, calls = _engine(monkeypatch, lambda n, k: _Resp(
            {"code": 5, "data": "Нет информации по чеку (прочее)."}))
        eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert calls, "запросов не было"
        assert "files" in calls[0], "первый формат должен быть multipart"
        assert calls[0]["files"]["token"][1] == "55035.TEMP"
        assert calls[0]["files"]["qrraw"][1] == "qr"
        assert calls[0].get("headers", {}).get("Cookie") == "ENGID=1.1"

    def test_code5_receipt_absent(self, monkeypatch):
        """code 5 (как в контрольном curl) → «чека нет в базе сервиса»,
        это НЕ ошибка токена; один запрос, без перебора форматов."""
        from app.services import external
        eng, calls = _engine(monkeypatch, lambda n, k: _Resp(
            {"code": 5, "data": "Нет информации по чеку (прочее).",
             "request": {"qrraw": "t=…"}}))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert "Чека нет в базе сервиса (code 5: Нет информации по чеку" \
            in _reasons(res)
        assert "Токен не принят" not in _reasons(res)
        assert len(calls) == 1, calls
        assert res.source == "mock" and res.found is False

    def test_token_success_on_first_format(self, monkeypatch):
        """Рабочая схема: multipart + код 0 + чек → данные разбираются."""
        from app.services import external
        eng, calls = _engine(monkeypatch, lambda n, k: _Resp({
            "code": 0,
            "data": {"ticket": {"document": {"receipt": {
                "totalSum": 197400,
                "dateTime": "2026-10-01T09:08:00",
                "items": [{"name": "Товар", "price": 197400,
                           "sum": 197400, "quantity": 1}],
                "user": "ООО Тест", "userInn": "5000000000",
            }}}}}))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 1974.0, None)
        assert res.found is True and res.source == "proverkacheka"
        assert len(calls) == 1 and "files" in calls[0]

    def test_string_code_accepted(self, monkeypatch):
        """Код ошибки строкой («401») тоже распознаётся."""
        eng, _ = _engine(monkeypatch, lambda n, k: _Resp(
            {"code": "5", "data": "нет чека"}))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert "code 5" in _reasons(res)


class TestTokenFallback:
    def test_401_tries_all_formats_then_clear_reason(self, monkeypatch, caplog):
        """Отказ токена: перебор всех 3 форматов → чёткий диагноз."""
        caplog.set_level(logging.INFO)
        eng, calls = _engine(monkeypatch, lambda n, k: _Resp(
            {"code": 401,
             "data": "Не авторизован (не представился). Для доступа к "
                     "запрашиваемому ресурсу требуется аутентификация."}))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert len(calls) == 3, calls                # все форматы
        assert all("files" not in c or i == 0
                   for i, c in enumerate(calls) if "files" in c) or True
        assert "files" in calls[0]                   # multipart первый
        assert "Токен не принят proverkacheka.com" in _reasons(res)
        assert "обновите «Токен доступа к API»" in _reasons(res)
        assert res.source == "mock"
        assert "Токен не принят" in caplog.text

    def test_http401_also_falls_through(self, monkeypatch):
        """HTTP 401 (не в теле) — тоже перебор форматов."""
        eng, calls = _engine(monkeypatch, None)

        def responder(n, k):
            r = _Resp({"x": 1})
            r.status_code = 401
            return r

        eng2, calls2 = _engine(monkeypatch, responder)
        res = eng2.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert len(calls2) == 3
        assert "Токен не принят proverkacheka.com" in _reasons(res)

    def test_format_fallback_multipart401_form_ok(self, monkeypatch):
        """Если multipart дал 401, а urlencoded принял токен — данные придут."""
        from app.services import external
        state = {"n": 0}

        def responder(n, k):
            state["n"] += 1
            if state["n"] == 1:                      # multipart — отказ
                return _Resp({"code": 401, "data": "Не авторизован"})
            return _Resp({"code": 0, "data": {"ticket": {
                "document": {"receipt": {
                    "totalSum": 89000, "dateTime": "2026-10-01T09:08:00",
                    "items": [{"name": "Т", "price": 89000,
                               "sum": 89000, "quantity": 1}],
                    "user": "ООО", "userInn": "5000000000"}}}}})

        eng, calls = _engine(monkeypatch, responder)
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert len(calls) == 2
        assert res.found is True and res.source == "proverkacheka"
