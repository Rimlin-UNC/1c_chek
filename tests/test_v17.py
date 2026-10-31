# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.7.0: темы, печать PDF (раскрой A4),
# мок-тест источников, пакетная выдача чеков, SSL-обслуживание.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import os

from tests.conftest import login

QR = ("t=20260927T1529&s=2150.00&fn=7381440700130934"
      "&i=33079&fp=3673437411&n=1")


class TestExternalCheck:
    def test_not_found_is_ok(self, client, monkeypatch):
        """v1.70.70: «чек не найден в ФНС» — нормальный ответ, а не сбой.

        Демо-источника больше нет: тест связи обращается к ФНС, и
        отсутствие чека в базе не должно выглядеть как ошибка связи.
        """
        from app.services import external
        from app.services.external import ExternalResult

        def fake_fetch(self, *a, **kw):
            return ExternalResult(ok=True, source="fns_api",
                                  message="Чек не найден в базе ФНС",
                                  found=False)

        monkeypatch.setattr(external.ExternalFetchEngine, "fetch", fake_fetch)
        monkeypatch.setattr(external.ExternalFetchEngine, "status",
                            lambda self: {"fns_api": {"available": True}})
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/settings/external/test", headers=hdr,
                        json={"qrraw": QR})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["found"] is False
        assert d["source"] == "fns_api"
        assert "ФНС" in d["message"]


class TestReceiptsIds:
    def test_ids_return_items(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/scan", json={"qr_data": QR, "source": "manual"},
                        headers=hdr)
        assert r.status_code == 200, r.text
        rid = r.json()["receipt"]["id"]
        lst = client.get(f"/api/v1/receipts?ids={rid}", headers=hdr).json()
        assert lst["total"] == 1
        assert "items" in lst["items"][0]          # позиции включены
        other = client.get("/api/v1/receipts", headers=hdr).json()
        assert all("items" not in x for x in other["items"])   # без ids — как раньше


class TestThemesAndPrint:
    def test_theme_files(self):
        # v1.25.0: тема одна — светлая (переключатель удалён)
        h = open("app/static/index.html", encoding="utf-8").read()
        css = open("app/static/css/app.css", encoding="utf-8").read()
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "color-scheme: light" in css
        assert "--bg: #f5f5f5" in css and "--text: #333333" in css
        assert "data-theme" not in h and "applyTheme" not in js

    def test_print_pack_files(self):
        assert os.path.exists("app/static/js/printpack.js")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "printReceiptsPDF" in js and "btn-print-pdf" in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@page" in css and "#print-root" in css
        assert "size: A4 portrait" in css

    def test_ssl_auto_renew_in_deploy(self):
        d = open("deploy.sh", encoding="utf-8").read()
        # v1.8.3: таймер systemd или snap; продление через CERTBOT_BIN
        assert "renew --cert-name" in d
        assert "certbot.timer" in d or "snap.certbot.renew.timer" in d
        assert "--fix-ssl" in d                       # режим починки SSL

    def test_no_blinking_indicator(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "animation: pulse 2.2s infinite" not in css   # точка больше не мигает
