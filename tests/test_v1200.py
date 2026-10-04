# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.20.0: фишки стиля «Ямастер» — умный фокус,
# тепловая карта сроков, настройка оформления, спокойный час, иллюстрации.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import datetime as dt

from tests.conftest import login


def _mk_user(client, role, username):
    hdr = login(client, "admin", "admin123")
    inv = client.post("/api/v1/invites", json={"role": role}, headers=hdr).json()
    r = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": f"Тест {username}"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


class TestHeatMap:
    def test_stats_has_late_soon(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/dashboard/stats", headers=hdr)
        assert r.status_code == 200, r.text
        rows = r.json().get("by_assignee", [])
        assert isinstance(rows, list)
        for a in rows:
            assert "late" in a and "soon" in a

    def test_overdue_receipt_marks_late(self, client):
        ah = login(client, "admin", "admin123")
        # срок 1 день; автопроверку выключаем ДО скана — иначе фоновая
        # проверка успеет перевести чек в verified и он не попадёт в «открытые»
        rr = client.put("/api/v1/settings/app", headers=ah,
                        json={"advance_deadline_days": 1, "auto_verify": False})
        assert rr.status_code == 200, rr.text
        u = _mk_user(client, "user", "heat_usr")
        r = client.post("/api/v1/receipts/scan", headers=u, json={
            "qr_data": "t=20250101T1200&s=550.00&fn=9999078902001299&i=60003&fp=777888001&n=1",
            "source": "manual", "verify": False})
        assert r.status_code == 200, r.text
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            old = dt.datetime.utcnow() - dt.timedelta(days=5)
            db.query(Receipt).update({"created_at": old, "status": "new"})
            db.commit()
        finally:
            db.close()
        st = client.get("/api/v1/dashboard/stats", headers=ah).json()
        rows = [a for a in st["by_assignee"] if a["late"] > 0]
        assert rows, st["by_assignee"]
        assert all(a["soon"] == 0 for a in rows)

    def test_ui_heat_map(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("heat-chip", "heat-late", "heat-soon", "heat-ok",
                       "heat-legend", "тепловая", "близко к сроку"):
            assert needle in js, needle


class TestSmartFocus:
    def test_ui_smart_focus(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("btn-focus", "bindSmartFocus", "setSmartFocus",
                       "ym-focus", "ym-focus-target", "Умный фокус"):
            assert needle in js, needle
        # Esc выходит из фокуса
        assert "if (e.key === 'Escape') setSmartFocus(false)" in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "body.ym-focus .content .card" in css
        assert ".dash-toolbar" in css

    def test_attention_kpi_is_focus_target(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "kpi-attention', '⏳ Требуют внимания', 'не обработаны > 3 дней').replace(" in js
        assert "card-assignee" in js


class TestAppearance:
    def test_settings_card(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("🎨 Оформление", "accent-row", "density-seg",
                       "btn-quiet", "quiet-dur", "bindAppearance",
                       "applyAccent", "applyDensity", "renderAppearanceState",
                       "Спокойный час"):
            assert needle in js, needle

    def test_early_apply_no_flash(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "ymaster-accent" in idx and "ymaster-density" in idx
        assert "data-accent" in idx

    def test_css_accent_and_density(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert 'html[data-accent="indigo"]' in css
        assert 'html[data-accent="sea"]' in css
        assert 'html[data-accent="sky"]' in css
        assert 'html[data-density="compact"]' in css
        assert ".swatch" in css and ".appearance-label" in css

    def test_quiet_hours_in_toast(self):
        u = open("app/static/js/ui.js", encoding="utf-8").read()
        assert "ymaster-quiet-until" in u
        assert "kind !== 'err'" in u            # ошибки показываются всегда


class TestEmptyIllustrations:
    def test_empty_state_svg(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "empty-illo" in js and "<svg" in js
        assert "ym-float" in open("app/static/css/app.css",
                                  encoding="utf-8").read()
        # фирменные цвета в иллюстрации
        for hexc in ("#FF7A00", "#4B0082", "#2E8B57"):
            assert hexc in js


class TestVersion1200:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.20.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert f"'{ver}':" in js
