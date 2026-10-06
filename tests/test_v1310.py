# Ямастер Чек — тесты v1.31.0: «Чек-Пул» этап 2 — приём чеков на сайте (без Telegram).
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 2 (docs/dev_plan_checkpool.md):
#  - чек с сайта (строка QR) попадает в пул С ПОЗИЦИЯМИ, источник «web»;
#  - ответ форме быстрый (лёгкие проверки сразу, источники — в фоне);
#  - оферта обязательна и фиксируется в журнале pool_consents (152-ФЗ);
#  - honeypot-бот тихо отбрасывается; rate-limit на IP; лимит суток работает;
#  - анонимная cookie pool_vid: подпись, персистентность баллов, чужой чек не виден;
#  - добавляющая миграция (pool_users.vid, pool_consents) — без разрушений;
#  - ядро не затронуто, версии/WHATS_NEW/CHANGELOG/инструкция синхронны.
import re
import time
from collections import deque
from datetime import datetime

import pytest

from app.database import engine
from app.pool import ingest as pool
from app.pool.models import (PoolConsent, PoolItem, PoolPoint, PoolReceipt,
                             PoolUser, ensure_pool_schema)
from app.services.external import ExternalItem, ExternalResult

ensure_pool_schema(engine)   # добавляющая миграция (idempotent) до тестов

_SEQ = {"n": 8000}


def _qr(sum_rub="1250.00"):
    """Уникальный QR на вызов (тестовая БД персистентна между прогонами)."""
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return (f"t=20260905T1430&s={sum_rub}&fn=9999078903{k:04d}"
            f"&i=65{k:03d}&fp=780{k:03d}&n=1")


_TICKET = ExternalResult(
    ok=True, source="test", found=True, message="OK",
    date_time=datetime(2026, 9, 5, 14, 30),
    total_sum=1250.0, operation=1,
    merchant_name="ООО Ромашка", merchant_inn="7704001275",
    merchant_address="г. Тихвин, ул. Советская 12",
    items=[
        ExternalItem(name="Кофе", quantity=2.0, price=350.0, total=700.0,
                     vat_rate="10", vat_sum=70.0),
        ExternalItem(name="Торт", quantity=1.0, price=800.0, total=800.0,
                     vat_rate="20", vat_sum=160.0),
    ],
    raw={"document": {"receipt": {"totalSum": 125000}}},
)


def _enable(db, on=True):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1" if on else "0")


def _wipe(db):
    """Чистим только СВОИ данные (web + vid + согласия), настройки — в исходное.
    Порядок с учётом FK: позиции → чеки → баллы → согласия → vid-юзеры.
    Ретраи: фоновые записи пула могут держать SQLite на доли секунды."""
    for _ in range(40):
        try:
            web_ids = [r[0] for r in db.query(PoolReceipt.id)
                       .filter_by(source="web").all()]
            vid_ids = [u[0] for u in db.query(PoolUser.id)
                       .filter(PoolUser.vid.isnot(None)).all()]
            all_ids = list({*web_ids, *vid_ids})
            if web_ids:
                db.query(PoolItem).filter(
                    PoolItem.receipt_id.in_(web_ids)).delete(
                    synchronize_session=False)
            if all_ids:
                db.query(PoolReceipt).filter(
                    PoolReceipt.id.in_(all_ids)).delete(
                    synchronize_session=False)
            if vid_ids:
                db.query(PoolPoint).filter(
                    PoolPoint.user_id.in_(vid_ids)).delete(
                    synchronize_session=False)
            db.query(PoolConsent).delete(synchronize_session=False)
            db.query(PoolUser).filter(PoolUser.vid.isnot(None)).delete(
                synchronize_session=False)
            _enable(db, False)
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1310: не удалось очистить web-данные пула")


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _wait_row(fn, timeout=6.0):
    """Ждём фоновую проверку web-чека: (id, статус, число позиций)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        db = _db()
        try:
            r = db.query(PoolReceipt).filter_by(source="web", fn=fn).first()
            if r is not None:
                n = db.query(PoolItem).filter_by(receipt_id=r.id).count()
                return r.id, r.status, n
        finally:
            db.close()
        time.sleep(0.05)
    return None


@pytest.fixture(autouse=True)
def _clean():
    from app.pool import router_public as rp
    rp._IP_HITS.clear()
    db = _db()
    _wipe(db)
    db.close()
    yield
    rp._IP_HITS.clear()
    db = _db()
    _wipe(db)
    db.close()


class TestPublicCheck:
    def test_info_endpoint(self):
        c = _client()
        info = c.get("/api/v1/public/pool/info").json()
        assert info["enabled"] is False
        assert "offerta" in info and info["offerta_version"]
        assert info["daily_limit"] == 50
        db = _db(); _enable(db, True); db.commit(); db.close()
        info = c.get("/api/v1/public/pool/info").json()
        assert info["enabled"] is True
        assert "фискальные" in info["offerta"]

    def test_web_receipt_verified_with_items(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        c = _client()
        qr = _qr()
        t0 = time.monotonic()
        r = c.post("/api/v1/public/pool/check",
                   json={"qr_text": qr, "offerta": True, "hp": "",
                         "form_ms": 5000})
        assert time.monotonic() - t0 < 5, "ответ форме должен быть быстрым"
        body = r.json()
        assert body["ok"] and body["accepted"] and body["fn"]
        row = _wait_row(body["fn"])
        assert row, "фоновая проверка не записала чек"
        rid, status, n_items = row
        assert status == "verified" and n_items == 2, "чек должен попасть в пул С ПОЗИЦИЯМИ"
        db = _db()
        rec = db.get(PoolReceipt, rid)
        assert rec.source == "web"
        u = db.get(PoolUser, rec.pool_user_id)
        assert u.vid, "гость должен быть идентифицирован cookie-vid"
        assert u.points == 1
        cons = db.query(PoolConsent).filter_by(vid=u.vid).all()
        assert len(cons) == 1, "факт согласия с офертой зафиксирован"
        assert cons[0].offerta_version == pool.OFFERTA_VERSION
        assert cons[0].ip_hash and cons[0].form_ms == 5000
        db.close()

    def test_vid_cookie_persists_points(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        c = _client()
        for _ in range(2):
            body = c.post("/api/v1/public/pool/check",
                          json={"qr_text": _qr(), "offerta": True}).json()
            assert body["accepted"]
            assert _wait_row(body["fn"])
        my = c.get("/api/v1/public/pool/my").json()
        assert my["points"] == 2 and my["today"] == 2
        assert len(my["receipts"]) == 2

    def test_duplicate_via_web(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        c = _client()
        qr = _qr()
        b1 = c.post("/api/v1/public/pool/check",
                    json={"qr_text": qr, "offerta": True}).json()
        assert _wait_row(b1["fn"])
        b2 = c.post("/api/v1/public/pool/check",
                    json={"qr_text": qr, "offerta": True}).json()
        assert b2["ok"] and not b2["accepted"] and b2["result"] == "duplicate"
        db = _db()
        assert db.query(PoolReceipt).filter_by(fn=b1["fn"]).count() == 1
        db.close()

    def test_offerta_required(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": False}).json()
        assert not b["ok"] and b["error"] == "offerta_required"
        db = _db()
        assert db.query(PoolReceipt).filter_by(source="web").count() == 0
        assert db.query(PoolConsent).count() == 0, "без согласия нет и записи"
        db.close()

    def test_honeypot_silent_drop(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True,
                         "hp": "сайт-спам"}).json()
        assert b["ok"] and not b["accepted"], "боту отвечаем вежливо"
        db = _db()
        assert db.query(PoolReceipt).filter_by(source="web").count() == 0
        assert db.query(PoolConsent).count() == 0
        db.close()

    def test_bad_qr_via_web(self):
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": "просто слово", "offerta": True}).json()
        assert b["accepted"] is False and b["result"] == "bad_qr"

    def test_disabled_pool(self):
        c = _client()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True}).json()
        assert not b["ok"] and b["error"] == "disabled"

    def test_daily_flood_limit(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        c = _client()
        b0 = c.post("/api/v1/public/pool/check",
                    json={"qr_text": _qr(), "offerta": True}).json()
        assert _wait_row(b0["fn"])                # vid-юзер создан и закоммичен
        vid = c.cookies.get("pool_vid").split(".")[0]
        db = _db()
        u = db.query(PoolUser).filter_by(vid=vid).first()
        assert u is not None
        for i in range(pool.DAILY_LIMIT - 1):     # 1 настоящий + 49 заглушек
            db.add(PoolReceipt(pool_user_id=u.id, source="web",
                               fn=f"9000000000000{i:04d}", fd="1", fp="1"))
        db.commit(); db.close()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True}).json()
        assert b["accepted"] is False and b["result"] == "flood"

    def test_ip_rate_limit(self):
        from app.pool import router_public as rp
        c = _client()
        db = _db(); _enable(db, True); db.commit(); db.close()
        rp._IP_HITS["testclient"] = deque(
            [time.monotonic()] * rp.WEB_IP_HOURLY_LIMIT)
        r = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True})
        assert r.status_code == 429

    def test_receipt_status_ownership(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        c = _client()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr(), "offerta": True}).json()
        rid, _, _ = _wait_row(b["fn"])
        assert c.get(f"/api/v1/public/pool/receipt/{rid}").json()["status"]
        c2 = _client()                            # другой гость (без cookie)
        c2.cookies.clear()
        assert c2.get(f"/api/v1/public/pool/receipt/{rid}").status_code == 404

    def test_pending_anomaly_via_web(self, monkeypatch):
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        c = _client()
        b = c.post("/api/v1/public/pool/check",
                   json={"qr_text": _qr("600000.00"), "offerta": True}).json()
        row = _wait_row(b["fn"])
        assert row and row[1] == "pending", "аномальная сумма → ручная проверка"

    def test_my_without_cookie_is_empty(self):
        c = _client()
        my = c.get("/api/v1/public/pool/my").json()
        assert my["points"] == 0 and my["receipts"] == []


class TestWebIntegration:
    def test_additive_migration(self):
        """Миграция добавляющая: vid у pool_users + таблица pool_consents."""
        import sqlalchemy
        from app.database import engine
        insp = sqlalchemy.inspect(engine)
        assert "vid" in {c["name"] for c in insp.get_columns("pool_users")}
        assert "pool_consents" in insp.get_table_names()

    def test_split_pipeline_same_as_sync(self, monkeypatch):
        """precheck+ingest_parsed дают тот же результат, что sync-путь."""
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.close()
        parsed, err = pool.precheck_ingest(db, _qr(), None)
        assert err is None and parsed.fn
        assert pool.precheck_ingest(db, "мусор", None)[1]["result"] == "bad_qr"
        db2 = _db()
        u = PoolUser(vid="split-test-vid")
        db2.add(u); db2.flush()
        parsed2, err2 = pool.precheck_ingest(db2, _qr(), u)
        res = pool.ingest_parsed(db2, _qr(), parsed2, "web", u)
        assert res["result"] == "verified" and res["points"] == 1
        try:                                      # уборка: сначала позиции (FK)
            db2.query(PoolItem).filter_by(receipt_id=res["receipt_id"]).delete(
                synchronize_session=False)
            db2.query(PoolReceipt).filter_by(id=res["receipt_id"]).delete(
                synchronize_session=False)
            db2.query(PoolPoint).filter_by(user_id=u.id).delete(
                synchronize_session=False)
            db2.delete(u)
            db2.commit()
        finally:
            db2.rollback()
            db2.close()

    def test_bot_channel_still_sync(self, monkeypatch):
        """Регресс: бот-путь (синхронный ingest_receipt) не сломан."""
        from app.services.telegram_bot import process_pool_message
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        db = _db(); _enable(db, True); db.commit(); db.close()
        reply = process_pool_message(db, "chat-1310", "chat-1310",
                                     "tester", _qr())
        assert "✅" in reply and "+1" in reply, reply


class TestVersion1310:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.31.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.31.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.31.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "приём на сайте (v1.31.0)" in manual

    def test_public_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "location.hash.startsWith('#/public')" in js   # гостевая ветка
        assert "public: viewPublic" in js                     # раздел приложения
        for el in ("pub-qr", "pub-photo", "pub-hp", "pub-offerta", "pub-submit"):
            assert f'id="{el}"' in js
        assert "/api/v1/public/pool/info" in js
        assert "/api/v1/public/pool/my" in js
        assert "decodeImageFile" in js                        # jsQR-распознавание
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="public-screen"' in idx
        assert 'id="public-to-login"' in idx
        assert 'href="#/public"' in idx                       # футер входа + меню
        assert 'data-view="public"' in idx
        mn = open("app/main.py", encoding="utf-8").read()
        assert "router_public" in mn

    def test_backend_additive_and_core_untouched(self):
        md = open("app/pool/models.py", encoding="utf-8").read()
        assert "pool_consents" in md and "vid" in md
        assert "ensure_pool_schema" in md
        rp = open("app/pool/router_public.py", encoding="utf-8").read()
        assert "WEB_IP_HOURLY_LIMIT" in rp and "HONEYPOT_MS" in rp
        assert "OFFERTA_VERSION" in open("app/pool/ingest.py", encoding="utf-8").read()
        core = open("app/models.py", encoding="utf-8").read()
        assert "pool_" not in core, "ядро по-прежнему не знает о пуле"

    def test_settings_card_updated(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "(Этап 2 · v1.31.0)" in js
        assert 'id="pool-enabled"' in js and 'id="pool-save"' in js
        assert 'href="#/public"' in js

