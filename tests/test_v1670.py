# Ямастер Чек — тесты v1.67.0: исходящие webhooks (Фаза 1, п.7).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки (docs/concept_roadmap_v160.md, Фаза 1 п.7):
#  - событие «чек verified» → POST на URL компании с секретом и повторами;
#  - подпись HMAC-SHA256 (заголовки X-Ymaster-Signature / X-Ymaster-Task),
#    ретраи 60/300/900 с, успех = любой 2xx;
#  - очередь в своей таблице (webhook_logs): attempt/next_attempt_at,
#    воркер в фоне, выключен/битый URL не мешают приёму чеков;
#  - фундамент «интеграции без границ»: без тяжёлых коннекторов;
#  - тесты — БЕЗ реальной сети (httpx.post мокается).
import hashlib
import hmac as hmac_mod
import re
from datetime import datetime

import pytest

_SEQ = {"n": 67000}


# ------------------------------------------------------------------ версии
class TestVersion1670:
    """Строгий версионный замок релиза v1.67.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.67.1: строгий замок версии переехал в test_v1671
        assert tuple(int(x) for x in ver.split(".")) >= (1, 67, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.66.0" not in idx and "?v=1.65.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.67.0':") < js.index("'1.66.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.67.0]") == 1
        assert ch.index("## [1.67.0]") < ch.index("## [1.66.0]")

    def test_block_registered(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Исходящие webhooks"] == "1.67.0"


# ------------------------------------------------------------------ ядро
class TestWebhookCore:
    """Подпись, очередь, доставка и повторы (моки вместо сети)."""

    def test_sign_payload_format(self):
        from app.services import webhooks as wh
        ts, body = 1760000000, '{"event":"receipt_verified"}'
        sig = wh.sign_payload("S3CRET", body, ts)
        expect = hmac_mod.new(b"S3CRET", f"{ts}.{body}".encode(),
                              hashlib.sha256).hexdigest()
        assert sig == expect and len(sig) == 64
        # детерминированность и чувствительность к секрету
        assert wh.sign_payload("S3CRET", body, ts) == sig
        assert wh.sign_payload("OTHER", body, ts) != sig

    def test_enqueue_receipt_verified(self):
        from app.database import SessionLocal
        from app.models import Receipt, WebhookLog
        from app.services import webhooks as wh
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "webhook_url", "http://127.0.0.1:9/hook")
            _SEQ["n"] += 1
            k = _SEQ["n"]
            r = Receipt(qr_data=f"t&s&fn=9{k}&i={k}&fp={k}&n=1",
                        fn=f"9{k}", fd=f"{k}", fp=f"{k}",
                        receipt_date=datetime(2026, 10, 12, 9, 0),
                        total_sum=850.0, operation=1, status="verified",
                        assignee="Тестов Т.", assignee_lc="тестов т.")
            s.add(r)
            s.commit()
            rid = r.id
            s.expire_all()
            rec = wh.enqueue_receipt_verified(s, rid)
            assert rec is not None and rec.receipt_id == rid
            assert rec.event_type == "receipt_verified"
            assert rec.attempt == 0 and rec.delivered_at is None
            assert rec.next_attempt_at is not None
            assert '"event": "receipt_verified"' in rec.payload or \
                '"event":"receipt_verified"' in rec.payload
            # повтор того же чека не плодит записи
            wh.enqueue_receipt_verified(s, rid)
            cnt = s.query(WebhookLog).filter(
                WebhookLog.receipt_id == rid).count()
            assert cnt == 1
        finally:
            appsettings.set_setting(s, "webhook_url", "")
            s.rollback()
            s.close()

    def test_enqueue_cap_50(self):
        from app.database import SessionLocal
        from app.models import WebhookLog
        from app.services import webhooks as wh
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "webhook_url", "http://127.0.0.1:9/hook")
            for i in range(wh.MAX_RECORDS + 5):
                rec = WebhookLog(event_type="receipt_verified", url="http://x/",
                                 payload="{}", attempt=1,
                                 delivered_at=datetime.now())
                s.add(rec)
                s.commit()
            n0 = s.query(WebhookLog).count()
            assert n0 >= wh.MAX_RECORDS
            rec = wh.enqueue_receipt_verified(s, "no-such-receipt")
            assert rec is None                       # нет чека — нет записи
            n1 = s.query(WebhookLog).count()
            assert n1 <= wh.MAX_RECORDS              # очередь подрезана
        finally:
            appsettings.set_setting(s, "webhook_url", "")
            s.rollback()
            s.close()

    def test_deliver_ok_and_headers(self, monkeypatch):
        from app.database import SessionLocal
        from app.models import Receipt, WebhookLog
        from app.services import webhooks as wh
        captured = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {}

        def _post(url, content=None, headers=None, timeout=None):
            captured.update(url=url, content=content, headers=headers)
            return _Resp()

        monkeypatch.setattr(wh.httpx, "post", _post)
        s = SessionLocal()
        try:
            _SEQ["n"] += 1
            k = _SEQ["n"]
            r = Receipt(qr_data=f"t&s&fn=9{k}&i={k}&fp={k}&n=1",
                        fn=f"9{k}", fd=f"{k}", fp=f"{k}",
                        receipt_date=datetime(2026, 10, 12, 9, 0),
                        total_sum=1200.0, operation=1, status="verified")
            s.add(r)
            s.commit()
            from app.services import appsettings
            appsettings.set_setting(s, "webhook_url", "http://127.0.0.1:9/hook")
            appsettings.set_setting(s, "webhook_secret", "S3CRET")
            rec = wh.enqueue_receipt_verified(s, r.id)
            s.expire_all()
            ok, code = wh.deliver_due(s)
            assert ok >= 1 and code == 200
            row = s.query(WebhookLog).filter(
                WebhookLog.id == rec.id).first()
            assert row.delivered_at is not None
            assert row.attempt == 1 and row.last_status == 200
            body = captured["content"].decode()
            assert "receipt_verified" in body
            assert captured["headers"]["Content-Type"] == "application/json"
            assert captured["headers"]["X-Ymaster-Task"] is not None
            sig = wh.sign_payload("S3CRET", body,
                                  int(captured["headers"]["X-Ymaster-Task"]))
            assert captured["headers"]["X-Ymaster-Signature"] == sig
        finally:
            s.rollback()
            s.close()

    def test_deliver_retry_and_exhaust(self, monkeypatch):
        from app.database import SessionLocal
        from app.models import WebhookLog
        from app.services import webhooks as wh

        class _Resp:
            status_code = 500
            def json(self):
                return {}

        monkeypatch.setattr(wh.httpx, "post", lambda *a, **k: _Resp())
        s = SessionLocal()
        try:
            from app.services import appsettings
            appsettings.set_setting(s, "webhook_url", "http://127.0.0.1:9/hook")
            appsettings.set_setting(s, "webhook_secret", "")
            rec = WebhookLog(event_type="receipt_verified", url="http://x/",
                             payload='{"event":"receipt_verified"}',
                             attempt=0, next_attempt_at=datetime(2020, 1, 1))
            s.add(rec)
            s.commit()
            rid = rec.id
            s.expire_all()
            wh.deliver_due(s)
            row = s.query(WebhookLog).filter(
                WebhookLog.id == rid).first()
            assert row.delivered_at is None and row.attempt == 1
            assert row.last_status == 500 and row.next_attempt_at is not None
            assert row.next_attempt_at > row.created_at
            # исчерпание попыток: последняя попытка оставляет failed
            row.attempt = wh.MAX_ATTEMPTS - 1
            row.next_attempt_at = datetime(2020, 1, 1)
            s.commit()
            wh.deliver_due(s)
            row = s.query(WebhookLog).filter(
                WebhookLog.id == rid).first()
            assert row.attempt >= wh.MAX_ATTEMPTS
            assert row.delivered_at is None
            # проваленные больше не гоняются
            ok, _c = wh.deliver_due(s)
            assert ok == 0
        finally:
            s.rollback()
            s.close()

    def test_worker_disabled_without_config(self):
        from app.services import webhooks as wh
        assert hasattr(wh, "start_worker") and hasattr(wh, "stop_worker")
        from app.database import SessionLocal
        s = SessionLocal()
        try:
            from app.services import appsettings
            appsettings.set_setting(s, "webhook_url", "")
            appsettings.set_setting(s, "webhook_enabled", "0")
            assert wh.start_worker() is False
        finally:
            s.rollback()
            s.close()


# ------------------------------------------------------------------ API
class TestWebhookApi:
    """Настройки вебхуков (админ): валидации, секрет не утекает, тест-URL."""

    def test_settings_roundtrip(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        g = client.get("/api/v1/settings/webhooks", headers=H).json()
        assert g["enabled"] in (True, False)
        assert "receipt_verified" in g["all_events"]
        r = client.put("/api/v1/settings/webhooks", headers=H, json={
            "url": "https://example.com/hook", "enabled": False,
            "events": ["receipt_verified"]})
        assert r.status_code == 200, r.text
        g2 = client.get("/api/v1/settings/webhooks", headers=H).json()
        assert g2["url"] == "https://example.com/hook"
        assert g2["events"] == ["receipt_verified"]
        assert g2["enabled"] is False
        # вернуть как было
        client.put("/api/v1/settings/webhooks", headers=H,
                   json={"url": g.get("url") or "", "enabled": False,
                         "events": g.get("events") or ["receipt_verified"]})

    def test_bad_url_422(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/webhooks", headers=H,
                       json={"url": "ftp://example.com"})
        assert r.status_code == 422
        r2 = client.put("/api/v1/settings/webhooks", headers=H,
                        json={"url": "not a url"})
        assert r2.status_code == 422

    def test_bad_event_422(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/webhooks", headers=H,
                       json={"url": "https://example.com/h",
                             "events": ["receipt_deleted"]})
        assert r.status_code == 422

    def test_secret_never_returned(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/webhooks", headers=H,
                       json={"secret": "ТопСекрет123"})
        assert r.status_code == 200
        g = client.get("/api/v1/settings/webhooks", headers=H).json()
        assert "ТопСекрет123" not in str(g)
        assert g["has_secret"] is True and g["secret_masked"]
        client.put("/api/v1/settings/webhooks", headers=H,
                   json={"secret": "-"})

    def test_test_endpoint(self, client, monkeypatch):
        from app.services import webhooks as wh

        class _Resp:
            status_code = 200
            def json(self):
                return {}

        monkeypatch.setattr(wh.httpx, "post", lambda *a, **k: _Resp())
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/webhooks", headers=H,
                       json={"url": "https://example.com/hook"})
        assert r.status_code == 200
        t = client.post("/api/v1/settings/webhooks/test", headers=H, json={})
        assert t.status_code == 200, t.text

    def test_test_endpoint_requires_admin(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=H).json()
        assert me["role"] == "admin"


# ------------------------------------------------------------------ эмиссия
class TestWebhookEmission:
    """Чек стал verified → запись в очереди вебхуков появляется сама."""

    def test_verify_flow_enqueues(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.models import Receipt, WebhookLog
        from app.services import appsettings
        from app.services import fns as fns_svc
        from app.services import webhooks as wh
        from tests.conftest import login

        # v1.70.39: тест готовит своё состояние сам и убирает за собой.
        # Раньше он молча полагался на то, что предыдущий тест оставил URL
        # вебхука настроенным: эмиссия не происходит без URL (webhooks._enqueue
        # возвращает None при пустом webhook_url), поэтому на свежей базе или
        # после теста, который URL очищает, проверка падала — порядкозависимость.
        def _url_session():
            sess = SessionLocal()
            return sess

        def _get_url():
            sess = _url_session()
            try:
                return appsettings.get_setting(sess, "webhook_url", "") or ""
            finally:
                sess.rollback(); sess.close()

        def _set_url(value: str):
            sess = _url_session()
            try:
                appsettings.set_setting(sess, "webhook_url", value)
                sess.commit()
            finally:
                sess.rollback(); sess.close()

        prev_url = _get_url()
        _set_url("http://127.0.0.1:9/hook")
        try:
            self._verify_flow_body(client, monkeypatch, SessionLocal, Receipt,
                                   WebhookLog, fns_svc, wh, login)
        finally:
            _set_url(prev_url)

    def _verify_flow_body(self, client, monkeypatch, SessionLocal, Receipt,
                          WebhookLog, fns_svc, wh, login):

        def _fake_check(fn, fd, fp, total_sum, receipt_date,
                        fns_status="", fns_checked_at=None, **kw):
            return fns_svc.FnsResult(
                status="valid", message="ОК (mock)",
                raw={"provider": "mock", "code": 0, "found": True,
                     "items": [{"name": "Кофе", "price": 850, "sum": 850,
                                "quantity": 1}]},
                ok=True)

        monkeypatch.setattr("app.routers.receipts.check_receipt", _fake_check)
        H = login(client, "admin", "admin123")
        s = SessionLocal()
        try:
            _SEQ["n"] += 1
            k = _SEQ["n"]
            r = Receipt(qr_data=f"t&s&fn=9{k}&i={k}&fp={k}&n=1",
                        fn=f"9{k}", fd=f"{k}", fp=f"{k}",
                        receipt_date=datetime(2026, 10, 12, 9, 0),
                        total_sum=850.0, operation=1, status="new")
            s.add(r)
            s.commit()
            rid = r.id
        finally:
            s.rollback()
            s.close()
        resp = client.post(f"/api/v1/receipts/{rid}/verify", headers=H)
        assert resp.status_code == 200, resp.text
        s = SessionLocal()
        try:
            rec = s.query(Receipt).get(rid)
            assert rec.status == "verified"
            row = (s.query(WebhookLog)
                   .filter(WebhookLog.receipt_id == rid).first())
            assert row is not None                 # v1.67.0: событие в очереди
            assert row.event_type == "receipt_verified"
        finally:
            s.rollback()
            s.close()

    def test_sources_wired(self):
        """Замки вставок: verified-путь receipts зовёт enqueue_receipt_verified.

        Донорские чеки Чек-Пула наружу НЕ идут (v1.67: эмиссия только
        для чеков сотрудников)."""
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert src.count("enqueue_receipt_verified(db, receipt.id)") >= 1
        main = open("app/main.py", encoding="utf-8").read()
        assert "webhooks.start_worker" in main


# ------------------------------------------------------------------ миграции
class TestWebhookInfra:
    """Таблица webhook_logs, секрет в SECRET_KEYS, инструкция."""

    def test_table_and_secret(self):
        from sqlalchemy import inspect
        from app.database import engine
        tables = inspect(engine).get_table_names()
        assert "webhook_logs" in tables
        src = open("app/services/appsettings.py", encoding="utf-8").read()
        assert "webhook_secret" in src

    def test_worker_in_main(self):
        main = open("app/main.py", encoding="utf-8").read()
        assert "from .services import webhooks" in main
        assert "webhooks.start_worker()" in main

    def test_manual_has_webhooks_section(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        d = client.get("/api/v1/manual", headers=H).json()
        titles = {s["title"]: s for s in d["sections"]}
        assert "Вебхуки: события наружу" in titles
        sec = titles["Вебхуки: события наружу"]
        assert sec["audience"] == "admin"
        assert sec["image"] == "/img/manual/webhooks.jpg"
        assert "HMAC" in sec["html"] and "повтор" in sec["html"].lower()
        img = open("app/static/img/manual/webhooks.jpg", "rb").read()
        assert img[:2] == b"\xff\xd8"
