# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.7: итоговая логика приоритета источников
# (мастер-ключ ФНС → ФНС первой; без ключа → сразу proverkacheka.com)
# + подробный журнал загрузки данных чека («ДАННЫЕ ЧЕКА»).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
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
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 7)  # структурный
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
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 7)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert tuple(int(x) for x in reg["Обновления"].split(".")) >= (1, 55, 1)              # не задет


class TestFinalChain:
    """v1.70.70: очередь одна — официальное API ФНС."""

    def _eng(self):
        from app.services import external
        return external.ExternalFetchEngine()

    def test_no_master_chain_same(self, monkeypatch):
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "fns_master_token": ""})
        monkeypatch.setattr(
            "app.services.external.settings.FNS_MASTER_TOKEN", "",
            raising=False)
        assert eng.provider_chain(None) == ["fns_api"]

    def test_master_chain_same(self, monkeypatch):
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "fns_master_token": "master"})
        assert eng.provider_chain(None) == ["fns_api"]

    def test_no_third_party_provider_left(self, monkeypatch):
        eng = self._eng()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "proverkacheka_token": "tok", "fns_master_token": "master"})
        chain = eng.provider_chain(None)
        assert "proverkacheka" not in chain and "mock" not in chain


class TestFetchLogging:
    """Журнал «ДАННЫЕ ЧЕКА»: видно, что происходит при запросе в ФНС."""

    def _run(self, monkeypatch, master, fns_fail):
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "fns_master_token": master})
        monkeypatch.setattr(eng, "_polite_wait", lambda provider: None)
        calls = []

        def _fns(qr, fn, fd, fp, total, dtm, token):
            calls.append("fns")
            if fns_fail:
                return False, "ФНС: сеть недоступна", {}
            return True, "Чек найден в ФНС", {
                "provider": "fns", "status": "valid",
                "document": {"receipt": {
                    "totalSum": 89000, "items": [
                        {"name": "Т", "price": 89000, "sum": 89000,
                         "quantity": 1}]}}}

        monkeypatch.setattr(external, "fetch_fns", _fns)
        return eng, calls

    def test_log_no_master_named_reason(self, monkeypatch, caplog):
        """Без мастер-ключа — понятная причина и QR в журнале."""
        caplog.set_level(logging.INFO)
        eng, calls = self._run(monkeypatch, master="", fns_fail=False)
        res = eng.fetch(None, QR_OWNER, "7380440902170632", "11100",
                        "2060442762", 890.00, None)
        assert calls == [], "без токена к ФНС не обращаемся"
        assert res.ok is False and "Мастер-токен" in res.message
        txt = caplog.text
        assert "ДАННЫЕ ЧЕКА [fn=7380440902170632 i=11100 fp=2060442762]" in txt
        assert "890.0" in txt

    def test_log_success_from_fns(self, monkeypatch, caplog):
        """Успешная проверка: ключ маскирован, факт получения в журнале."""
        caplog.set_level(logging.INFO)
        eng, calls = self._run(monkeypatch, master="sekret", fns_fail=False)
        res = eng.fetch(None, QR_OWNER, "7380440902170632", "11100",
                        "2060442762", 890.00, None)
        assert calls == ["fns"]
        assert res.ok and res.source == "fns_api"
        txt = caplog.text
        assert "Мастер-токен ФНС: есть (…" in txt
        assert "sekret" not in txt
        assert "✅ ФНС: ДАННЫЕ ЧЕКА ПОЛУЧЕНЫ" in txt

    def test_log_failure_is_explicit(self, monkeypatch, caplog):
        """Сбой ФНС — в журнале «ДАННЫЕ НЕ ПОЛУЧЕНЫ» с причиной."""
        caplog.set_level(logging.INFO)
        eng, calls = self._run(monkeypatch, master="sekret", fns_fail=True)
        res = eng.fetch(None, QR_OWNER, "7380440902170632", "11100",
                        "2060442762", 890.00, None)
        assert calls == ["fns"]
        assert res.ok is False and res.found is False
        txt = caplog.text
        assert "❌ ФНС: сбой" in txt and "сеть недоступна" in txt
        assert "proverkacheka" not in txt.lower()


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
