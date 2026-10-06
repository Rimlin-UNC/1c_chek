# Ямастер Чек — тесты v1.38.0: «Чек-Пул» этап 9 «Рост» — граф связей
# антифрода и партнёрский кэшбэк с QR «на кассе».
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 9 (docs/dev_plan_checkpool.md, выполненная часть):
#  - граф-аналитика: /pool-fraud/graph отдаёт кластеры участников с рёбрами
#    «общее устройство / общая подсеть / реферальная пара»; панель #/fraud
#    рисует SVG-граф, клик по узлу — карточка участника;
#  - партнёрский кэшбэк: чек с ИНН партнёра → баллы = % от суммы (кап 100),
#    дедуп по чеку, карантин не платит, не-партнёр не платит;
#  - QR «на кассе»: SVG по коду партнёра, только короткие ссылки пула;
#  - ML отложен по плану (корпус < 5 тыс.); платное API — v1.39.0.
import time
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal, engine
from app.pool import partners
from app.pool.models import (PoolAchievement, PoolConsent, PoolFingerprint,
                             PoolItem, PoolIpLog, PoolPoint, PoolReceipt,
                             PoolReferral, PoolSignal, PoolToken, PoolUser,
                             PoolWithdrawal, ensure_pool_schema)

ensure_pool_schema(engine)

_C = {"n": 0}


def _db():
    return SessionLocal()


def _wipe():
    db = _db()
    for _ in range(40):
        try:
            for t in (PoolItem, PoolReceipt, PoolPoint, PoolSignal, PoolIpLog,
                      PoolFingerprint, PoolAchievement, PoolWithdrawal,
                      PoolReferral, PoolToken, PoolConsent, PoolUser):
                db.query(t).delete(synchronize_session=False)
            db.commit()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.25)
    raise RuntimeError("test_v1380: не удалось очистить пул")


@pytest.fixture(autouse=True)
def _clean(client):
    from app.pool import router_auth
    router_auth._HITS.clear()
    _wipe()
    db = _db()
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1")
    db.commit()
    db.close()
    yield
    router_auth._HITS.clear()
    _wipe()


def _mkuser(db, vid=None, email=None, verified=False):
    u = PoolUser(vid=vid, email=email or "", email_verified=verified)
    db.add(u)
    db.flush()
    return u


def _seed(receipt_inn="", total=500.0, fn=None):
    _C["n"] += 1
    db = _db()
    r = PoolReceipt(source="web", fn=str(fn or 800000 + _C["n"]),
                    fd="1", fp=str(_C["n"]), total_sum=total,
                    merchant_inn=receipt_inn, status="verified")
    db.add(r)
    db.commit()
    rid = r.id
    db.close()
    return rid


class TestPartnerCashback:
    def test_inn_lookup_and_public_list(self, client):
        assert partners.find_by_inn("7810010017")["name"] == "Пекарня «Колос»"
        assert partners.find_by_inn("7700000000") is None
        assert partners.find_by_code("part-kolos")["pct"] == 10
        d = client.get("/api/v1/public/pool/partners").json()
        assert len(d["items"]) >= 4
        assert d["cap"] == partners.CASHBACK_CAP
        assert all("inn" not in i for i in d["items"]), "ИНН наружу не отдаём"

    def test_cashback_paid_dedup_cap_quarantine(self, client):
        # чеки сеём ДО открытия write-сессии (SQLite: один писатель)
        rid = _seed(receipt_inn="7810010017", total=500.0)    # 10% → 50
        rid2 = _seed(receipt_inn="7810020022", total=3000.0)  # 8% → 240 → кап
        rid3 = _seed(receipt_inn="7810010017", total=1000.0)  # для карантина
        rid4 = _seed(receipt_inn="7700000000", total=1000.0)  # не партнёр
        db = _db()
        try:
            u = _mkuser(db, vid="part-cb-1")
            r, r2 = db.get(PoolReceipt, rid), db.get(PoolReceipt, rid2)
            assert partners.on_verified_receipt(db, u, r) == 50
            assert partners.on_verified_receipt(db, u, r) == 0  # дедуп по чеку
            db.commit()
            rows = (db.query(PoolPoint)
                    .filter_by(user_id=u.id, reason="partner_cashback").all())
            assert len(rows) == 1 and rows[0].delta == 50
            # кап: 8% от 3000 = 240 → 100
            assert partners.on_verified_receipt(
                db, u, r2) == partners.CASHBACK_CAP
            # карантин не платит
            u.quarantined_at = datetime.utcnow()
            r3 = db.get(PoolReceipt, rid3)
            assert partners.on_verified_receipt(db, u, r3) == 0
            # не партнёр
            r4 = db.get(PoolReceipt, rid4)
            assert partners.on_verified_receipt(db, u, r4) == 0
            db.commit()
        finally:
            db.close()

    def test_ingest_hook_via_public_form(self, client, monkeypatch):
        """Хук v1.38.0 в ingest: чек с ИНН партнёра → кэшбэк участнику."""
        from app.services.external import ExternalItem, ExternalResult
        from app.pool import ingest as pool_ingest
        monkeypatch.setattr(pool_ingest, "_fetch_details",
                            lambda *a, **k: ExternalResult(
                                ok=True, source="test", found=True, message="OK",
                                date_time=datetime(2026, 10, 6, 12, 0),
                                total_sum=400.0, operation=1,
                                merchant_name="Пекарня Колос",
                                merchant_inn="7810010017",
                                merchant_address="г Санкт-Петербург, Невский 1",
                                items=[ExternalItem(name="Хлеб", quantity=2.0,
                                                    price=200.0, total=400.0)]))
        db = _db()
        from app.services import appsettings
        appsettings.set_setting(db, "pool_enabled", "1")
        db.commit()
        u = PoolUser(vid="part-hook-vid")
        db.add(u)
        db.commit()
        uid = u.id
        db.close()
        db = _db()
        u = db.get(PoolUser, uid)          # attached: баллы обновятся в БД
        res = pool_ingest.ingest_receipt(
            db, "t=20261006T1200&s=400.00&fn=88000001&i=1&fp=1&n=1", "web", u)
        assert res["result"] == "verified"
        db.commit()
        db.close()
        db = _db()
        pts = (db.query(PoolPoint)
               .filter_by(user_id=uid, reason="partner_cashback").all())
        u = db.get(PoolUser, uid)
        db.close()
        assert len(pts) == 1 and pts[0].delta == 40    # 10% от 400
        assert u.points == 41                          # 1 за чек + 40 кэшбэк


class TestPartnerQr:
    def test_svg_by_code(self, client):
        r = client.get("/api/v1/public/pool/partners/qr.svg",
                       params={"code": "PART-KOLOS"})
        assert r.status_code == 200
        assert "image/svg" in r.headers["content-type"]
        body = r.text
        assert body.startswith("<svg") and "<path" in body
        # содержимое — ссылка на страницу сдачи (проверка декодированием
        # матрицы не нужна: длину и структуру проверяем)
        assert "viewBox" in body

    def test_unknown_code_404(self, client):
        assert client.get("/api/v1/public/pool/partners/qr.svg",
                          params={"code": "PART-NOPE"}).status_code == 404

    def test_long_text_rejected(self):
        import pytest as _pytest
        with _pytest.raises(ValueError):
            partners.qr_svg("x" * 200)

    def test_qr_roundtrip_jsqr(self):
        """Сквозная проверка: SVG → матрица → jsQR → исходная ссылка."""
        import subprocess
        import sys
        r = subprocess.run(
            ["node", "-e", """
const { execSync } = require('child_process');
const svg = execSync(`python3 -c "
from app.pool import partners
print(partners.qr_svg('https://chek.ymaster.ru/#/pub'))"`).toString();
const n = +svg.match(/viewBox="0 0 (\d+) (\d+)"/)[1];
const rects = [...svg.matchAll(/M(\d+) (\d+)h1v1h-1z/g)].map(r2 => [+r2[1], +r2[2]]);
const size = n * 8;
const data = new Uint8ClampedArray(size * size * 4).fill(255);
for (const [x, y] of rects)
  for (let dy = 0; dy < 8; dy++) for (let dx = 0; dx < 8; dx++) {
    const px = ((y * 8 + dy) * size + (x * 8 + dx)) * 4;
    data[px] = data[px+1] = data[px+2] = 0;
  }
const jsQR = require('./app/static/js/vendor/jsQR.js');
const res = jsQR(data, size, size);
process.exit(res && res.data === 'https://chek.ymaster.ru/#/pub' ? 0 : 1);
"""], capture_output=True, text=True, cwd=".")
        assert r.returncode == 0, f"QR не декодировался: {r.stderr[:200]}"


class TestFraudGraph:
    def _mk_cluster(self, db):
        """Ферма: 2 аккаунта с общим устройством + реферальная пара,
        и один независимый участник."""
        a = _mkuser(db, vid="farm-a", email="farm-a@x.ru")
        b = _mkuser(db, vid="farm-b", email="farm-b@x.ru")
        c = _mkuser(db, vid="solo-c", email="solo-c@x.ru")
        vh = "farm-device-hash-1"
        db.add(PoolFingerprint(user_id=a.id, visitor_hash=vh))
        db.add(PoolFingerprint(user_id=b.id, visitor_hash=vh))
        db.add(PoolReferral(referrer_id=a.id, referred_id=b.id, code_used="X"))
        for ip in ("91.44.1.7", "91.44.1.9"):
            db.add(PoolIpLog(user_id=b.id, ip=ip, ip24="91.44.1"))
        db.add(PoolIpLog(user_id=c.id, ip="10.0.0.1", ip24="10.0.0"))
        db.commit()
        return a, b, c

    def test_graph_nodes_and_edges(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        db = _db()
        a, b, c = self._mk_cluster(db)
        ids = {a.id, b.id, c.id}
        db.close()
        d = client.get("/api/v1/pool-fraud/graph?days=30", headers=hdr).json()
        node_ids = {n["id"] for n in d["nodes"]}
        # соло-участник без связей может не попасть в узлы (кластеры ≥2)
        assert {a.id, b.id} <= node_ids
        kinds = {(e["a"], e["b"], e["kind"]) for e in d["edges"]}
        pairs = {frozenset((e["a"], e["b"])) for e in d["edges"]}
        assert frozenset((a.id, b.id)) in pairs
        kinds_found = {e["kind"] for e in d["edges"]}
        assert "device" in kinds_found and "referral" in kinds_found
        assert d["clusters"] >= 1
        # узлы несут метку/риск
        n = next(n for n in d["nodes"] if n["id"] == a.id)
        assert "farm-a@x.ru" in n["label"] and n["risk"] == 0

    def test_graph_requires_admin(self):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)
        assert c.get("/api/v1/pool-fraud/graph").status_code == 401

    def test_empty_graph(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        d = client.get("/api/v1/pool-fraud/graph", headers=hdr).json()
        assert d["nodes"] == [] and d["edges"] == []


class TestVersion1380:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.38.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.38.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.38.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "партнёрский кэшбэк (v1.38.0)" in manual

    def test_ui_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "viewPartners" in js and "partners/qr.svg" in js
        assert "fraudLoadGraph" in js and "pool-fraud/graph" in js
        assert "(Этап 9 · v1.38.0)" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'data-view="partners"' in idx

    def test_vendor_and_partners_json(self):
        import os
        assert os.path.exists("app/vendor/qrcode/main.py")
        meta = open("app/vendor/qrcode/METADATA.txt", encoding="utf-8").read()
        assert "BSD" in meta
        pj = open("app/pool/geo/data/partners.json", encoding="utf-8").read()
        assert "7810010017" in pj
        ing = open("app/pool/ingest.py", encoding="utf-8").read()
        assert "partners.on_verified_receipt" in ing
