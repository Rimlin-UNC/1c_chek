# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.7: собственная камера в мини-приложении
# (getUserMedia + jsQR, как на сайте) и резервный путь регистрации
# (код боту сообщением — кнопка копирования в окне входа).
# ======================================================================
import re

import pytest


class TestCameraScanner1707:
    def test_maxapp_has_jsqr_and_camera_button(self):
        html = open("app/static/maxapp/index.html", encoding="utf-8").read()
        assert 'src="/maxapp/jsQR.js"' in html
        assert 'id="btn-cam"' in html
        assert open("app/static/maxapp/jsQR.js", "rb").read(50)

    def test_camera_engine_in_js(self):
        js = open("app/static/maxapp/maxapp.js", encoding="utf-8").read()
        assert "getUserMedia" in js and "jsQR(" in js
        assert "scanCamera" in js and "stopCam" in js
        # openCodeReader — первый выбор; камера — надёжный запасной
        assert js.index("openCodeReader") < js.index("scanCamera")
        # showService умеет обратный вызов (остановить камеру по «Назад»)
        assert "window._svcBack" in js

    def test_scan_fallback_is_camera(self):
        js = open("app/static/maxapp/maxapp.js", encoding="utf-8").read()
        m = re.search(r"if \(!app \|\| !app\.openCodeReader\)[^}]*}",
                      js, re.S)
        assert m and "scanCamera" in m.group(0)


class TestRegistrationFallback1707:
    def test_login_modal_copy_button(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "mxl-copy" in js
        # в окне входа объяснён путь «код боту сообщением»
        assert "сообщением" in js

    def test_session_failures_logged(self):
        src = open("app/routers/maxapp.py", encoding="utf-8").read()
        assert src.count("maxapp session") >= 2   # отказ + успех в журнале


class TestVersions1707:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.8: строгий замок версии переехал в test_v1708
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 7)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and "?v=1.70.6" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "1.70.6" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
        mapp = open("app/static/maxapp/index.html", encoding="utf-8").read()
        assert f"/maxapp/maxapp.js?v={ver}" in mapp

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.7]") == 1
        assert ch.index("## [1.70.7]") < ch.index("## [1.70.6]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.7':") < js.index("'1.70.6':")


class TestNoRegression1707:
    def test_login_flow_alive(self, client):
        from app.database import SessionLocal
        from app.services import appsettings, max_login
        max_login._ip_times.clear()   # лимит кодов глобальный — изолируем
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
        finally:
            s.rollback(); s.close()
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_photo_scan_alive(self, client, monkeypatch):
        import hashlib, hmac, io, json as js_, time, urllib.parse
        from datetime import datetime as _dt
        from app.database import SessionLocal
        from app.pool import ingest
        from app.services.external import ExternalResult
        from app.services import appsettings
        monkeypatch.setattr(
            ingest, "_fetch_details",
            lambda *a, **k: ExternalResult(
                ok=True, source="test", found=True, message="OK",
                date_time=_dt(2026, 10, 13, 12, 0), total_sum=100.0,
                operation=1, merchant_name="M", merchant_inn="1",
                items=[], raw={}))
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", "700001.ABCDEF")
        finally:
            s.rollback(); s.close()
        token = "700001.ABCDEF"
        ad = int(time.time())
        raw = [("auth_date", str(ad)), ("query_id", "q1"),
               ("user", js_.dumps({"id": 88001, "username": "cam"}))]
        launch = "\n".join(f"{k}={v}" for k, v in sorted(raw))
        sec = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        sig = hmac.new(sec, launch.encode(), hashlib.sha256).hexdigest()
        pairs = [(k, urllib.parse.quote(v, safe="")) for k, v in raw]
        pairs.append(("hash", sig))
        init_data = "&".join(f"{k}={v}" for k, v in pairs)
        sess = client.post("/api/v1/maxapp/session",
                           json={"init_data": init_data}).json()
        H = {"Authorization": "Bearer " + sess["access_token"]}
        import qrcode
        buf = io.BytesIO()
        qrcode.make("t=20261013T120000&s=100&fn=9900000000080881"
                    "&i=80881&fp=808810&n=1").save(buf, format="PNG")
        r = client.post("/api/v1/maxapp/scan-image", headers=H,
                        files={"file": ("q.png", buf.getvalue(),
                                        "image/png")})
        assert r.status_code == 200 and r.json()["accepted"] is True
