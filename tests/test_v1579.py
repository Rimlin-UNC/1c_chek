# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.9: тела-ошибки proverkacheka.com при HTTP 200
# ({"code":401,"data":"Не авторизован (не представился)…"}) → внятная
# причина, без бессмысленных повторов.
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
        assert ver == "1.57.9"
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
        assert reg["Проверка чеков (ФНС и источники)"] == "1.57.9"
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет

    def test_token_hint_new_format(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "формат 55035.хххххх)" in js
        assert 'placeholder="55035.xxxxxxxxxxx"' in js
        assert "Справка → API)" not in js.split("📥 Источники данных чека")[1][:2000]


def _reasons(res):
    """Причины по каждому источнику — из raw.engine.errors."""
    return " | ".join((res.raw.get("engine") or {}).get("errors") or [])


class TestBodyErrors:
    """HTTP 200 с кодом ошибки в теле — то, что было на проде."""

    def test_401_body_token_rejected(self, monkeypatch, caplog):
        """Ровно прод-кейс: code 401 «Не авторизован (не представился)»."""
        from app.services import external
        caplog.set_level(logging.INFO)
        eng, calls = _engine(monkeypatch, {
            "code": 401,
            "data": "Не авторизован (не представился). Для доступа к "
                    "запрашиваемому ресурсу требуется аутентификация."})
        res = eng.fetch(None, "t=…&s=890.00&fn=…&i=…&fp=…&n=1",
                        "fn", "11100", "fp", 890.0, None)
        # причина названа прямо, а не «данных чека нет»
        assert "Токен не принят proverkacheka.com" in _reasons(res)
        assert "обновите «Токен доступа к API»" in _reasons(res)
        # повторов НЕТ: один HTTP-вызов — и переход дальше (mock)
        assert calls["n"] == 1, calls
        assert res.source == "mock" and res.found is False
        assert "Токен не принят" in caplog.text

    def test_402_body_quota(self, monkeypatch):
        from app.services import external
        eng, calls = _engine(monkeypatch, {"code": 402, "data": "квота"})
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert "Квота/тариф API исчерпаны (code 402" in _reasons(res)
        assert calls["n"] == 1                      # без повторов
        assert res.source == "mock"

    def test_404_body_not_found(self, monkeypatch):
        from app.services import external
        eng, _ = _engine(monkeypatch, {"code": 404, "data": "чек не найден"})
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert "Чека нет в базе сервиса (code 404" in _reasons(res)

    def test_429_body_limit(self, monkeypatch):
        from app.services import external
        eng, _ = _engine(monkeypatch, {"code": 429, "data": "слишком много"})
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert "Лимит/блокировка proverkacheka.com (code 429" in _reasons(res)

    def test_success_with_code_zero(self, monkeypatch):
        """code:0 с чеком (позиции непустые) — обычный успешный разбор."""
        from app.services import external
        eng, _ = _engine(monkeypatch, {
            "code": 0, "data": {"ticket": {"document": {"receipt": {
                "totalSum": 89000,
                "dateTime": "2026-10-01T09:08:00",
                "items": [{"name": "Товар", "price": 89000,
                           "sum": 89000, "quantity": 1}],
                "user": "ООО Тест", "userInn": "5000000000",
            }}}}})
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 890.0, None)
        assert res.found is True and res.source == "proverkacheka"

    def test_field_auth_sends_token(self, monkeypatch):
        """Токен уходит в поле token (как в проверенной интеграции)."""
        from app.services import external
        seen = {}

        def fake_post(url, *a, **k):
            seen.update(k)
            return _Resp({"code": 401, "data": "нет"})

        monkeypatch.setattr(external.httpx, "post", fake_post)
        external.fetch_proverkacheka("qr", "55035.TEMP")
        assert seen.get("data", {}).get("token") == "55035.TEMP"
