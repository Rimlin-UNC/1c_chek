# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.6: proverkacheka.com — всегда первый источник,
# ФНС — только резерв при мастер-ключе ФНС.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1576:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 6)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.5" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.6':") < js.index("'1.57.5':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.6]") == 1
        assert ch.index("## [1.57.6]") < ch.index("## [1.57.5]")


class TestChainPriority:
    """Решение владельца: proverkacheka.com — приоритет №1, ФНС — резерв."""

    def _eng(self):
        from app.services import external
        return external.ExternalFetchEngine()

    def test_no_master_direct_pke(self, monkeypatch):
        """Нет мастер-ключа ФНС → запрос напрямую в proverkacheka.com."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": ""})
        assert eng.provider_chain(None) == ["proverkacheka", "mock"]

    def test_master_fns_reserve_after_pke(self, monkeypatch):
        """v1.57.7 (итоговая логика владельца): мастер-ключ задан →
        ПЕРВЫЙ запрос в ФНС, proverkacheka.com — запасной после него."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": "master"})
        assert eng.provider_chain(None) == ["fns_api", "proverkacheka", "mock"]

    def test_master_without_pke_token(self, monkeypatch):
        """Мастер-ключ есть, токена pke нет → очередь [fns_api, mock]."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "", "fns_master_token": "master"})
        assert eng.provider_chain(None) == ["fns_api", "mock"]

    def test_stale_order_setting_ignored(self, monkeypatch):
        """Устаревший external_order «fns_api,…» больше не ставит ФНС первой."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "external_order": "fns_api,proverkacheka",
            "proverkacheka_token": "tok", "fns_master_token": "master"})
        chain = eng.provider_chain(None)
        assert chain == ["fns_api", "proverkacheka", "mock"]   # external_order игнорируется


class TestFetchOrder:
    def test_pke_first_then_fns_reserve(self, monkeypatch):
        """v1.57.7: ФНС первая; сбой ФНС → запрос уходит в proverkacheka."""
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": "master"})
        monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 0)
        monkeypatch.setattr(external.time, "sleep", lambda s: None)
        calls = []

        def _pke(qr, token):
            calls.append("pke")
            return True, "Данные получены", {"document": {"receipt": {}}}

        def _fns(qr, fn, fd, fp, total, dtm, token):
            calls.append("fns")
            return False, "сбой сети ФНС", {}

        monkeypatch.setattr(external, "fetch_proverkacheka", _pke)
        monkeypatch.setattr(external, "fetch_fns", _fns)
        monkeypatch.setattr(
            external, "parse_receipt_payload",
            lambda data, known: external.ExternalResult(
                ok=True, source="", found=True, message="ok",
                total_sum=known, items=[], raw=data))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 807.30, None)
        assert calls == ["fns", "pke"], calls
        assert res.ok and res.source == "proverkacheka"

    def test_no_fns_call_without_master(self, monkeypatch):
        """Нет мастер-ключа → fetch_fns вообще не вызывается."""
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": ""})
        monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 0)
        monkeypatch.setattr(external.time, "sleep", lambda s: None)
        calls = []

        def _pke(qr, token):
            calls.append("pke")
            return True, "Данные получены", {"document": {"receipt": {}}}

        def _fns(*a):
            calls.append("fns")
            return True, "ok", {}

        monkeypatch.setattr(external, "fetch_proverkacheka", _pke)
        monkeypatch.setattr(external, "fetch_fns", _fns)
        monkeypatch.setattr(
            external, "parse_receipt_payload",
            lambda data, known: external.ExternalResult(
                ok=True, source="", found=True, message="ok",
                total_sum=known, items=[], raw=data))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 807.30, None)
        assert calls == ["pke"], calls          # напрямую, без ФНС
        assert res.ok and res.source == "proverkacheka"


class TestSettingsUi:
    def test_order_fixed_by_system(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="ext-order"' not in js        # поле убрано — порядок задаёт система
        assert "добавится первым автоматически" not in js
        assert "1️⃣ proverkacheka.com" in js            # без ключа — первый
        assert "при мастер-ключе ФНС: 1️⃣ ФНС" in js    # с ключом — ФНС первый
        assert "первый запрос в ФНС" in js

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 6)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет
