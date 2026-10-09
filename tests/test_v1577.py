# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.7: итоговая логика приоритета источников
# (мастер-ключ ФНС → ФНС первой; без ключа → сразу proverkacheka.com)
# + подробный журнал загрузки данных чека («ДАННЫЕ ЧЕКА»).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt
import logging
import re

# QR чека владельца (диагностика загрузки данных)
QR_OWNER = ("t=20260829T1421&s=890.00&fn=7380440902170632"
            "&i=11100&fp=2060442762&n=1")


class TestVersion1577:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.57.7"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.6" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.7':") < js.index("'1.57.6':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.7]") == 1
        assert ch.index("## [1.57.7]") < ch.index("## [1.57.6]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Проверка чеков (ФНС и источники)"] == "1.57.7"
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Обновления"] == "1.55.1"              # не задет


class TestFinalChain:
    """Итоговая логика владельца: смотрим мастер-ключ ФНС."""

    def _eng(self):
        from app.services import external
        return external.ExternalFetchEngine()

    def test_no_master_direct_pke(self, monkeypatch):
        """НЕТ мастер-ключа → запрос СРАЗУ напрямую в proverkacheka.com."""
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": ""})
        monkeypatch.setattr(
            "app.services.external.settings.FNS_MASTER_TOKEN", "",
            raising=False)
        assert eng.provider_chain(None) == ["proverkacheka", "mock"]

    def test_master_fns_first(self, monkeypatch):
        """ЕСТЬ мастер-ключ → ПЕРВЫЙ запрос в ФНС, pke — запасной."""
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


class TestFetchLogging:
    """Журнал «ДАННЫЕ ЧЕКА»: видно всё, что происходит при запросе."""

    def _eng(self):
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkey = None
        return eng

    def _run(self, monkeypatch, master, pke_fail, fns_fail):
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": master})
        monkeypatch.setattr(external, "MAX_SAME_PROVIDER_RETRIES", 0)
        monkeypatch.setattr(external.time, "sleep", lambda s: None)
        calls = []

        def _pke(qr, token):
            calls.append("pke")
            if pke_fail:
                return False, "HTTP 500 от proverkacheka", {}
            return True, "Данные получены", {"document": {"receipt": {}}}

        def _fns(qr, fn, fd, fp, total, dtm, token):
            calls.append("fns")
            if fns_fail:
                return False, "HTTP 500 от ФНС", {}
            return True, "Чек найден", {"provider": "fns_api",
                                        "document": {"receipt": {}}}

        monkeypatch.setattr(external, "fetch_proverkacheka", _pke)
        monkeypatch.setattr(external, "fetch_fns", _fns)
        monkeypatch.setattr(
            external, "parse_receipt_payload",
            lambda data, known: external.ExternalResult(
                ok=True, source="", found=True, message="ok",
                total_sum=known, items=[], raw=data))
        return eng, calls

    def test_log_no_master_direct_pke(self, monkeypatch, caplog):
        """Без мастер-ключа: ФНС не вызывается, сразу pke; QR в журнале."""
        caplog.set_level(logging.INFO)
        eng, calls = self._run(monkeypatch, master="", pke_fail=False,
                               fns_fail=False)
        res = eng.fetch(None, QR_OWNER, "7380440902170632", "11100",
                        "2060442762", 890.00, None)
        assert calls == ["pke"], calls              # напрямую, ФНС не вызывалась
        assert res.ok and res.source == "proverkacheka"
        txt = caplog.text
        assert "ДАННЫЕ ЧЕКА [fn=7380440902170632 i=11100 fp=2060442762]" in txt
        assert "890.0" in txt                       # сумма из QR
        assert "мастер-ключ ФНС: НЕТ" in txt
        assert "ОЧЕРЕДЬ ИСТОЧНИКОВ: proverkacheka → mock" in txt
        assert "✅ proverkacheka: ДАННЫЕ ЧЕКА ПОЛУЧЕНЫ" in txt

    def test_log_master_fns_first_then_pke(self, monkeypatch, caplog):
        """С мастер-ключом: первый запрос в ФНС; сбой ФНС → pke; всё в журнале."""
        caplog.set_level(logging.INFO)
        eng, calls = self._run(monkeypatch, master="sekret", pke_fail=False,
                               fns_fail=True)
        res = eng.fetch(None, QR_OWNER, "7380440902170632", "11100",
                        "2060442762", 890.00, None)
        assert calls == ["fns", "pke"], calls
        assert res.ok and res.source == "proverkacheka"
        txt = caplog.text
        assert "мастер-ключ ФНС: есть (…" in txt    # ключ маскирован
        assert "ОЧЕРЕДЬ ИСТОЧНИКОВ: fns_api → proverkacheka → mock" in txt
        assert "❌ fns_api: сбой (попытка 1/1): HTTP 500 от ФНС" in txt
        assert "✅ proverkacheka: ДАННЫЕ ЧЕКА ПОЛУЧЕНЫ" in txt

    def test_log_all_sources_down(self, monkeypatch, caplog):
        """Оба источника упали → демо-источник, в журнале «ДАННЫЕ НЕ
        ПОЛУЧЕНЫ» с причинами по каждому."""
        caplog.set_level(logging.INFO)
        eng, calls = self._run(monkeypatch, master="sekret", pke_fail=True,
                               fns_fail=True)
        res = eng.fetch(None, QR_OWNER, "7380440902170632", "11100",
                        "2060442762", 890.00, None)
        assert calls == ["fns", "pke"]
        assert res.source == "mock" and res.found is False   # демо, не данные
        txt = caplog.text
        assert "🚫 ДАННЫЕ НЕ ПОЛУЧЕНЫ [fn=7380440902170632" in txt
        assert "реальные источники не ответили" in txt
        assert "HTTP 500 от ФНС" in txt and "HTTP 500 от proverkacheka" in txt


class TestWorkerLogging:
    """Журнал воркера: QR чека, результат, источник, объём записанного."""

    def test_worker_logs_receipt(self, client, monkeypatch, caplog):
        import uuid
        from tests.conftest import login
        from app.database import SessionLocal
        from app.models import Receipt
        rid = str(uuid.uuid4())
        db = SessionLocal()
        db.add(Receipt(id=rid, qr_data=QR_OWNER,
                       fn="7380440902170632", fd="11100", fp="2060442762",
                       total_sum=890.00,
                       receipt_date=dt.datetime(2026, 8, 29, 14, 21),
                       full_data=False))
        db.commit()
        db.close()
        try:
            from app.services.external import ExternalResult
            monkeypatch.setattr(
                "app.routers.receipts.external_engine.fetch",
                lambda db, qr, fn, fd, fp, total, dtm: ExternalResult(
                    ok=True, source="proverkacheka", found=True,
                    message="Данные получены", total_sum=890.00, operation=1,
                    items=[], raw={},
                    extra={"properties": [{"name": "Номер заказа",
                                           "value": "151939ПРИ"}]}))
            caplog.set_level(logging.INFO)
            hdr = login(client, "admin", "admin123")
            r = client.post(f"/api/v1/receipts/{rid}/fetch-details?force=1",
                            headers=hdr)
            assert r.status_code == 200, r.text
            txt = caplog.text
            assert "📥 ЗАГРУЗКА ДАННЫХ: чек(ов) в очереди: 1" in txt
            assert "🔎 Чек" in txt and "t=20260829T1421" in txt
            assert "💾 Чек" in txt and "источник=proverkacheka" in txt
            assert "результат=ok" in txt
            # данные действительно записаны в чек
            db = SessionLocal()
            try:
                rec = db.get(Receipt, rid)
                assert rec.details_source == "proverkacheka"
                assert "151939ПРИ" in (rec.ext_json or "")
            finally:
                db.close()
        finally:
            db = SessionLocal()
            obj = db.get(Receipt, rid)
            if obj:
                db.delete(obj)
                db.commit()
            db.close()
