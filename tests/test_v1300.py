# Ямастер Чек — тесты v1.30.0: «Чек-Пул» этап 1 (приём чеков через бота).
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки Этапа 1 (docs/dev_plan_checkpool.md):
#  - чек из Telegram попадает в pool_receipts С ПОЗИЦИЯМИ;
#  - дубликат не создаётся и не приносит баллов;
#  - аномальная сумма → pending; не найден → rejected; лимит суток работает;
#  - ядро не затронуто: существующие тесты проходят БЕЗ правок;
#  - маршруты админки, оферта, версии.
import re
from datetime import datetime

from app.pool import ingest as pool
from app.pool.models import PoolItem, PoolPoint, PoolReceipt, PoolUser
from app.services.external import ExternalItem, ExternalResult
from app.services.qr import QRParseError
from tests.conftest import login

_SEQ = {"n": 6000}


def _qr(sum_rub="1500.00"):
    """Уникальный QR на вызов: тестовая БД персистентна между тестами."""
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return (f"t=20260901T1200&s={sum_rub}&fn=9999078902{k:04d}"
            f"&i=64{k:03d}&fp=779{k:03d}&n=1")

_TICKET = ExternalResult(
    ok=True, source="test", found=True, message="OK",
    date_time=datetime(2026, 9, 1, 12, 0),
    total_sum=1500.0, operation=1,
    merchant_name="ООО Ромашка", merchant_inn="7704001275",
    merchant_address="г. Тихвин, ул. Советская 12",
    cashier="Иванова И.",
    items=[
        ExternalItem(name="Кофе", quantity=2.0, price=350.0, total=700.0,
                     vat_rate="10", vat_sum=70.0),
        ExternalItem(name="Торт", quantity=1.0, price=800.0, total=800.0,
                     vat_rate="20", vat_sum=160.0),
    ],
    raw={"document": {"receipt": {"totalSum": 150000}}},
)


def _enable(db, on=True):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1" if on else "0")


_USEQ = {"n": 9000}


def _mk_user(db):
    _USEQ["n"] += 1
    return pool.get_or_create_user(db, tg_user_id=str(_USEQ["n"]),
                                   tg_username="hunter")


class TestIngestPipeline:
    def test_verified_receipt_with_items(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
            u = _mk_user(db)
            res = pool.ingest_receipt(db, _qr(), "telegram_bot", u)
            assert res["result"] == "verified" and res["points"] == 1
            assert res["user_points"] == 1
            r = db.get(PoolReceipt, res["receipt_id"])
            assert r is not None
            assert r.status == "verified" and r.full_data is True
            assert r.merchant_inn == "7704001275"
            assert r.total_sum == 1500.0
            items = db.query(PoolItem).filter_by(receipt_id=r.id).all()
            assert len(items) == 2 and items[0].name == "Кофе"
            led = db.query(PoolPoint).filter_by(user_id=u.id).all()
            assert len(led) == 1 and led[0].delta == 1 and led[0].reason == "receipt"
        finally:
            db.close()

    def test_duplicate_no_second_row_no_points(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
            u = _mk_user(db)
            qr = _qr()
            r1 = pool.ingest_receipt(db, qr, "telegram_bot", u)
            assert r1["result"] == "verified"
            r2 = pool.ingest_receipt(db, qr, "telegram_bot", u)
            assert r2["result"] == "duplicate" and r2["points"] == 0
            assert db.query(PoolReceipt).filter_by(
                id=r1["receipt_id"]).count() == 1
            assert u.points == 1                     # дубль не приносит баллов
            assert db.query(PoolPoint).filter_by(user_id=u.id).count() == 1
        finally:
            db.close()

    def test_bad_qr(self):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            u = _mk_user(db)
            res = pool.ingest_receipt(db, "привет как дела", "telegram_bot", u)
            assert res["result"] == "bad_qr"
        finally:
            db.close()

    def test_anomaly_sum_goes_pending(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
            u = _mk_user(db)
            big = ("t=20260901T1200&s=600000.00&fn=9999078902006000"
                   "&i=64222&fp=7799902&n=1")
            res = pool.ingest_receipt(db, big, "telegram_bot", u)
            assert res["result"] == "pending" and res["points"] == 0
            r = db.query(PoolReceipt).filter_by(fn="9999078902006000").one()
            assert r.status == "pending" and r.points_awarded == 0
        finally:
            db.close()

    def test_not_found_rejected_without_points(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: ExternalResult(
                ok=True, source="mock", found=False, message="Источник не нашёл чек"))
            u = _mk_user(db)
            qr = ("t=20260901T1200&s=100.00&fn=9999078902006111"
                  "&i=64333&fp=7799903&n=1")
            res = pool.ingest_receipt(db, qr, "telegram_bot", u)
            assert res["result"] == "rejected" and res["points"] == 0
        finally:
            db.close()

    def test_daily_limit_flood(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
            u = _mk_user(db)
            # 50 чеков за сегодня — лимит исчерпан
            for i in range(pool.DAILY_LIMIT):
                db.add(PoolReceipt(
                    pool_user_id=u.id, source="telegram_bot", qr_data=f"q{i}",
                    fn=f"9999078902{i:05d}", fd=f"1{i}", fp=f"7{i}",
                    total_sum=10.0, status="verified"))
            db.commit()
            qr = ("t=20260901T1200&s=100.00&fn=9999078902006222"
                  "&i=64444&fp=7799904&n=1")
            res = pool.ingest_receipt(db, qr, "telegram_bot", u)
            assert res["result"] == "flood"
        finally:
            db.close()

    def test_disabled_flag(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db, on=False)
            u = _mk_user(db)
            res = pool.ingest_receipt(db, _qr(), "telegram_bot", u)
            assert res["result"] == "disabled"
        finally:
            db.close()


class TestBotIntegration:
    def test_process_pool_message_full_flow(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
            qr = _qr()
            reply = tg_process(db, qr, "555", "ivan_p")
            assert reply and "✅" in reply and "+1" in reply
            # дубликат — вежливый ответ без баллов
            reply2 = tg_process(db, qr, "555", "ivan_p")
            assert "уже в пуле" in reply2
            # не-QR текст → None (пойдёт в обычные команды)
            assert tg_process(db, "как дела?", "555", "ivan_p") is None
        finally:
            db.close()

    def test_balance_command(self, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db)
            monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
            tg_process(db, _qr(), "777", "anna")
            from app.services.telegram_bot import _pool_balance_text
            txt = _pool_balance_text(db, "777", "777")
            assert "баланс: 1" in txt.lower()
            txt2 = _pool_balance_text(db, "800", "800")      # неизвестный
            assert "пока нет чеков" in txt2
        finally:
            db.close()

    def test_start_without_code_shows_offerta(self, client, monkeypatch):
        from app.database import SessionLocal
        from app.services import telegram_bot as tg
        db = SessionLocal()
        try:
            _enable(db)
            sent = []
            monkeypatch.setattr(tg, "send_message",
                                lambda t, c, text, proxy="": sent.append(text) or True)
            monkeypatch.setattr(tg, "get_token", lambda db: "T")
            monkeypatch.setattr(tg, "get_proxy", lambda db: "")
            tg.handle_update(db, {"message": {"chat": {"id": "1"}, "text": "/start"}})
            assert sent and "Чек-Пул" in sent[0] and "фискальные" in sent[0]
        finally:
            db.close()


class TestAdminRoutes:
    def test_overview_and_settings(self, client, monkeypatch):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            _enable(db, on=False)
        finally:
            db.close()
        adm = login(client, "admin", "admin123")
        d = client.get("/api/v1/pool-admin/overview", headers=adm).json()
        assert {"enabled", "receipts_total", "verified", "pending",
                "users_total", "points_total", "offerta"} <= set(d)
        r = client.put("/api/v1/pool-admin/settings", headers=adm,
                       json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        d = client.get("/api/v1/pool-admin/overview", headers=adm).json()
        assert d["enabled"] is True
        r = client.put("/api/v1/pool-admin/settings", headers=adm,
                       json={"enabled": False})
        assert r.json()["enabled"] is False

    def test_receipts_list(self, client):
        adm = login(client, "admin", "admin123")
        d = client.get("/api/v1/pool-admin/receipts", headers=adm).json()
        assert {"total", "page", "items"} <= set(d)
        assert isinstance(d["items"], list)

    def test_forbidden_for_non_admin(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies", headers=hdr,
                           json={"name": f"ООО Пул {hdr['Authorization'][-6:]}"}).json()
        inv = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "user", "company_id": comp["id"]}).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"poolu_{comp['id'][:6]}",
            "password": "parol123", "full_name": "Пуль Пульков"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        assert client.get("/api/v1/pool-admin/overview", headers=hu).status_code == 403


def tg_process(db, text, chat_id, username):
    from app.services.telegram_bot import process_pool_message
    return process_pool_message(db, chat_id, chat_id, username, text)


class TestVersion1300:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.30.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.30.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.30.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Чек-Пул: этап 1 (v1.30.0)" in manual

    def test_settings_ui_card(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="pool-enabled"' in js and 'id="pool-save"' in js
        assert "/api/v1/pool-admin/overview" in js

    def test_core_untouched_no_pool_tables_in_core(self):
        """Ядро: таблица receipts не знает о пуле (нет FK/полей пула)."""
        s = open("app/models.py", encoding="utf-8").read()
        assert "pool_" not in s
