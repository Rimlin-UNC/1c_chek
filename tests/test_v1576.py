# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.6 (обновлены в v1.70.70).
# Раньше здесь закреплялся приоритет стороннего сервиса с резервом ФНС.
# Теперь источник один — официальное API ФНС, и проверяется именно это.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
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
    """v1.70.70: очередь источников не зависит от токенов — она одна."""

    def _eng(self):
        from app.services import external
        return external.ExternalFetchEngine()

    def test_no_master_token_still_fns(self, monkeypatch):
        """Нет мастер-ключа — цепочка та же (движок скажет, чего не хватает)."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "fns_master_token": ""})
        assert eng.provider_chain(None) == ["fns_api"]

    def test_master_token_same_chain(self, monkeypatch):
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "fns_master_token": "master"})
        assert eng.provider_chain(None) == ["fns_api"]

    def test_stale_order_setting_ignored(self, monkeypatch):
        """Устаревший external_order в базе больше ни на что не влияет."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "external_order": "proverkacheka,fns_api",
            "proverkacheka_token": "tok", "fns_master_token": "master"})
        assert eng.provider_chain(None) == ["fns_api"]


class TestFetchOrder:
    def test_fns_only_no_fallback(self, monkeypatch):
        """Сбой ФНС → второго источника нет, чек не выдумывается."""
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "fns_master_token": "master"})
        monkeypatch.setattr(eng, "_polite_wait", lambda provider: None)
        calls = []

        def _fns(*a, **k):
            calls.append("fns")
            return False, "ФНС: сеть недоступна", {}

        monkeypatch.setattr(external, "fetch_fns", _fns)
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 807.30, None)
        assert calls == ["fns"], calls
        assert res.ok is False and res.found is False

    def test_no_call_without_master(self, monkeypatch):
        """Нет мастер-ключа → к ФНС не обращаемся вовсе."""
        from app.services import external
        import app.config as cfg_mod
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {})
        monkeypatch.setattr(cfg_mod.settings, "FNS_MASTER_TOKEN", "")
        calls = []
        monkeypatch.setattr(external, "fetch_fns",
                            lambda *a, **k: calls.append("fns") or
                            (False, "нет", {}))
        res = eng.fetch(None, "qr", "fn", "fd", "fp", 807.30, None)
        assert calls == []
        assert res.ok is False and "Мастер-токен" in res.message


class TestSettingsUi:
    def test_order_fixed_by_system(self):
        """v1.70.70: выбора источников в интерфейсе нет — остался лимит."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="ext-order"' not in js        # поле убрано — порядок задаёт система
        assert "добавится первым автоматически" not in js
        assert "1️⃣ proverkacheka.com" not in js
        assert "при мастер-ключе ФНС: 1️⃣ ФНС" not in js
        assert "1000 запросов" in js
        assert "после сброса лимита" in js

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 6)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert tuple(int(x) for x in reg["Обновления"].split(".")) >= (1, 55, 1)              # не задет
