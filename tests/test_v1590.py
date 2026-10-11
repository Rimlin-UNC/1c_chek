# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.0: приём чеков Чек-Пула включается при
# обновлении (одноразовая миграция по решению владельца 11.10.2026),
# сданный донором чек попадает в базу пула и остаётся доступным
# (модерация → баллы → «Подбор для компании»).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import time as _t

import pytest


def _setting(key: str) -> str:
    from app.database import SessionLocal
    from app.services import appsettings
    db = SessionLocal()
    try:
        return appsettings.get_setting(db, key)
    finally:
        db.close()


def _set(key: str, val: str) -> None:
    from app.database import SessionLocal
    from app.services import appsettings
    db = SessionLocal()
    try:
        appsettings.set_setting(db, key, val)
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _pool_on():
    """Тесты файла требуют включённого приёма (как после миграции v1.59.0)."""
    _set("pool_enabled", "1")
    yield


class TestPoolAutoset:
    """Миграция v1.59.0: включение приёма чеков при обновлении."""

    def test_autoset_enabled_once(self):
        """Обновление включает приём (даже если был '0'), ставит маркер
        и больше никогда не трогает настройку: выключенное владельцем
        остаётся выключенным."""
        from app.database import init_db
        _set("pool_enabled_autoset_v1590", "")
        _set("pool_enabled", "0")
        init_db()
        assert _setting("pool_enabled") == "1"
        assert _setting("pool_enabled_autoset_v1590") == "1"
        # владелец выключил после обновления — приложение не включает обратно
        _set("pool_enabled", "0")
        init_db()
        assert _setting("pool_enabled") == "0"
        # возвращаем рабочее состояние другим тестам воркера
        _set("pool_enabled", "1")

    def test_info_enabled_for_donors(self, client):
        """Лендинг и форма получают честное «приём включён»."""
        info = client.get("/api/v1/public/pool/info").json()
        assert info["enabled"] is True
        assert "offerta" in info and info["offerta_version"]

    def test_donor_check_accepted_and_stored(self, client, monkeypatch):
        """Сданный чек попадает в базу пула (qr_data + реквизиты + статус),
        откуда используется: модерация, баллы, «Подбор для компании»."""
        from app.database import SessionLocal
        from app.pool import ingest as pool
        from app.pool.models import PoolReceipt
        from tests.test_v1310 import _TICKET
        monkeypatch.setattr(pool, "_fetch_details", lambda *a, **k: _TICKET)
        r = client.post("/api/v1/public/pool/check", json={
            "qr_text": "t=20260905T1430&s=1250.00&fn=9280440300528078"
                       "&i=12346&fp=67891&n=1",
            "offerta": True, "hp": "", "form_ms": 9000}).json()
        assert r["ok"] is True and r["accepted"] is True
        assert r["fn"] == "9280440300528078"
        row = None
        for _ in range(40):                     # фоновый поток пишёт чек
            db = SessionLocal()
            try:
                row = db.query(PoolReceipt).filter_by(
                    fn="9280440300528078").first()
            finally:
                db.close()
            if row is not None:
                break
            _t.sleep(0.25)
        assert row is not None, "чека донора нет в базе пула"
        assert row.qr_data.startswith("t=20260905T1430")
        assert row.total_sum == 1250.0
        assert row.status == "verified"
        assert row.pool_user_id                 # чек привязан к участнику

    def test_landing_honest_when_disabled(self, client):
        """Приём выключен → форма получает ok:false/disabled с внятным
        текстом (баннер «на паузе» рисует фронт, бэкенд не молчит)."""
        _set("pool_enabled", "0")
        try:
            info = client.get("/api/v1/public/pool/info").json()
            assert info["enabled"] is False
            b = client.post("/api/v1/public/pool/check", json={
                "qr_text": "t=20260905T1430&s=1250.00&fn=9280440300528079"
                           "&i=12347&fp=67892&n=1",
                "offerta": True, "hp": ""}).json()
            assert b["ok"] is False and b["error"] == "disabled"
            assert "выключен" in b["message"]
        finally:
            _set("pool_enabled", "1")


class TestVersion1590:
    """Строгий версионный замок релиза v1.59.0 (7 мест + CHANGELOG)."""

    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.59.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.58.4" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.0':") < js.index("'1.58.4':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.0]") == 1
        assert ch.index("## [1.59.0]") < ch.index("## [1.58.4]")


class TestSyncThreeWays:
    """Три пути сдачи чека (камера / вставка строки / вручную) сходятся
    в одну и ту же строку QR — её парсит один и тот же серверный парсер."""

    def test_manual_qr_string_parses_server_side(self):
        """Строка, собранная фронтендом из реквизитов (buildManualQr),
        обязана разбираться серверным parse_qr в те же реквизиты."""
        from app.services.qr import parse_qr
        # ровно то, что собирает buildManualQr('22.09.2025 14:30', 1500.5, ...)
        qr = ("t=20250922T1430&s=1500.50&fn=9999078902001234"
              "&i=12345&fp=1234567890&n=1")
        parsed = parse_qr(qr)
        assert parsed.fn == "9999078902001234"
        assert parsed.fd == "12345"
        assert parsed.fp == "1234567890"
        assert parsed.total_sum == 1500.5
        assert parsed.operation == 1
        assert parsed.date_time is not None
