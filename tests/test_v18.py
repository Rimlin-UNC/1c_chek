# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.8.0: личные суммы, срок авансового отчёта,
# E2E-конвейер обновления (реальный git), анти-мерцание, стандарты HTML.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import os
import subprocess

from tests.conftest import login

QR = ("t=20260927T1529&s=2150.00&fn=7381440700130934"
      "&i=33079&fp=3673437411&n=1")


class TestPersonalSum:
    def test_patch_and_dict(self, client):
        hdr = login(client, "admin", "admin123")
        rid = client.post("/api/v1/receipts/scan",
                          json={"qr_data": QR, "source": "manual"},
                          headers=hdr).json()["receipt"]["id"]
        r = client.patch(f"/api/v1/receipts/{rid}",
                         json={"personal_sum": 150.5}, headers=hdr)
        assert r.status_code == 200, r.text
        got = client.get(f"/api/v1/receipts/{rid}", headers=hdr).json()
        assert got["personal_sum"] == 150.5

    def test_employee_cannot_set(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "user"}).json()
        reg = client.post("/api/v1/auth/register",
                          json={"token": inv["token"], "username": "emp18",
                                "password": "secret2026", "full_name": "Сотрудник"})
        assert reg.status_code == 200, reg.text
        ehdr = login(client, "emp18", "secret2026")
        rid = client.post("/api/v1/receipts/scan",
                          json={"qr_data": QR.replace("fp=3673437411", "fp=3673499999"),
                                "source": "manual"}, headers=ehdr).json()["receipt"]["id"]
        r = client.patch(f"/api/v1/receipts/{rid}",
                         json={"personal_sum": 100}, headers=ehdr)
        assert r.status_code in (400, 403, 422), r.text

    def test_migration_column_exists(self, client):
        from app.database import engine
        from sqlalchemy import inspect
        cols = {c["name"] for c in inspect(engine).get_columns("receipts")}
        assert "personal_sum" in cols


class TestAdvanceDeadline:
    def test_settings_roundtrip(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/app", headers=hdr,
                       json={"advance_deadline_days": 7})
        assert r.status_code == 200
        got = client.get("/api/v1/settings/app", headers=hdr).json()
        assert got["advance_deadline_days"] == 7
        bad = client.put("/api/v1/settings/app", headers=hdr,
                         json={"advance_deadline_days": 0})
        assert bad.status_code == 422

    def test_accountant_can_read(self, client):
        hdr = login(client, "admin", "admin123")
        inv = client.post("/api/v1/invites", headers=hdr,
                          json={"role": "accountant"}).json()
        client.post("/api/v1/auth/register",
                    json={"token": inv["token"], "username": "acc18",
                          "password": "secret2026", "full_name": "Бух"})
        ahdr = login(client, "acc18", "secret2026")
        r = client.get("/api/v1/settings/app", headers=ahdr)
        assert r.status_code == 200 and "advance_deadline_days" in r.json()


class TestUpdatePipelineE2E:
    """Полный конвейер: backup → fetch → reset → clean → deps → проверка.
    «Старая версия» эмулируется локальным коммитом с пониженной версией —
    update обязан перезатереть локальный дрейф кодом с GitHub."""

    def test_apply_pipeline_real_git(self, tmp_path, monkeypatch):
        import app.services.updater as up
        dst = tmp_path / "appcopy"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks",
                        os.getcwd(), str(dst)], check=True)
        # «старая версия»: понижаем APP_VERSION (актуальную читаем из config)
        import re as _re
        cur = _re.search(r'APP_VERSION: str = "([^"]+)"',
                         open("app/config.py", encoding="utf-8").read()).group(1)
        cfg = dst / "app" / "config.py"
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       .replace(f'APP_VERSION: str = "{cur}"',
                                'APP_VERSION: str = "0.0.9"'), encoding="utf-8")
        subprocess.run(["git", "-C", str(dst), "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", str(dst), "config", "user.name", "t"],
                       check=True)
        subprocess.run(["git", "-C", str(dst), "commit", "-qam", "old"], check=True)
        # заглушка БД — чтобы preupdate-копия создалась как в бою
        (dst / "data").mkdir(exist_ok=True)
        (dst / "data" / "ymaster_check.db").write_bytes(b"fake-db-for-test")
        monkeypatch.setattr(up, "APP_DIR", str(dst))
        monkeypatch.setattr(up, "_append_history", lambda *a, **k: None)
        up._do_apply("1.8.0", "Rimlin-UNC/1c_chek", "arena/01a0caaa-1c-chek")
        assert up.job.success, getattr(up.job, "log", None) or getattr(up.job, "error", None)
        # локальный дрейф перезатёрт кодом с origin (версия снова 1.8.0)
        new_cfg = cfg.read_text(encoding="utf-8")
        assert f'APP_VERSION: str = "{cur}"' in new_cfg
        backups = os.listdir(dst / "data" / "backups")
        assert any(b.startswith("db-preupdate-") for b in backups)


class TestNoFlickerAndStandards:
    def test_sig_skip_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "viewDashboard._sig" in js and "viewReceipts._sig" in js

    def test_sw_assets_paths_exist(self):
        sw = open("app/static/sw.js", encoding="utf-8").read()
        import re
        paths = re.findall(r"'(/[^\s']*)'", sw.split("ASSETS = [")[1].split("];")[0])
        import pathlib
        missing = []
        for p in paths:
            if p == "/":
                continue
            # URL /css/app.css обслуживается из app/static/css/app.css (SPA)
            fs = pathlib.Path("app/static") / p.lstrip("/")
            if not fs.exists():
                missing.append(p)
        assert not missing, f"в кэше SW несуществующие файлы: {missing}"
        assert "/js/printpack.js" in sw

    def test_html_standards(self):
        from html.parser import HTMLParser
        from collections import Counter
        VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                'link', 'meta', 'param', 'source', 'track', 'wbr'}

        class A(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = Counter()
                self.blanks = 0
            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if 'id' in d:
                    self.ids[d['id']] += 1
                if tag == 'a' and d.get('target') == '_blank' \
                        and 'noopener' not in (d.get('rel') or ''):
                    self.blanks += 1
        a = A()
        a.feed(open("app/static/index.html", encoding="utf-8").read())
        assert not {k: v for k, v in a.ids.items() if v > 1}, "дубли id"
        assert a.blanks == 0, "a target=_blank без rel=noopener"
