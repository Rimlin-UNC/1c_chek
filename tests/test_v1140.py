# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.14.0: «Собрать авансовый отчёт»
# (POST /api/v1/receipts/advance-report: период, сотрудники, CSV-данные,
# изоляция пространства, контроль качества выборки).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import itertools
import shutil
import subprocess
import os
import uuid


def worker_bundle_path():
    """Уникальный путь бандла на воркер pytest-xdist (параллельный прогон)."""
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"/tmp/ymaster_ui_bundle_{w}.js"




from tests.conftest import login
from tests.test_v1121 import worker_bundle_path

# v1.73.8: several test modules reuse this helper. A random start (not 300)
# keeps generated ФН/ФД/ФП unique even if pytest/xdist loads the module under
# two names in one worker; otherwise the second /receipts/scan is a duplicate.
_SEQ = itertools.count((os.getpid() * 1000 + uuid.uuid4().int % 1000) % 1_000_000, 10)


def _qr(sum_: str = "250.00") -> str:
    n = next(_SEQ)
    return (f"t=20261006T1530&s={sum_}&fn=7381440700{n:07d}"
            f"&i={n}&fp={700000000 + n}&n=1")


def _mk_user(client, hdr, comp_id, username, role="user"):
    inv = client.post("/api/v1/invites", json={
        "role": role, "company_id": comp_id}, headers=hdr).json()
    reg = client.post("/api/v1/auth/register", json={
        "token": inv["token"], "username": username,
        "password": "parol123", "full_name": username.title()})
    return {"Authorization": "Bearer " + reg.json()["access_token"]}


class TestAdvanceReport:
    def _prepare(self, client, hdr):
        n = next(_SEQ)
        comp = client.post("/api/v1/companies", json={
            "name": f"ООО Отчётник {n}"}, headers=hdr).json()
        hu = _mk_user(client, hdr, comp["id"], f"ao_user{n}")
        r1 = client.post("/api/v1/receipts/scan", json={
            "qr_data": _qr("100.00"), "source": "web"}, headers=hu).json()["receipt"]["id"]
        r2 = client.post("/api/v1/receipts/scan", json={
            "qr_data": _qr("250.50"), "source": "web"}, headers=hu).json()["receipt"]["id"]
        # подотчётники и статьи — правит бухгалтер/админ
        client.patch(f"/api/v1/receipts/{r1}", json={
            "assignee": "Иван Отчётный", "category": "Хозрасходы",
            "total_sum": 100.0}, headers=hdr)
        client.patch(f"/api/v1/receipts/{r2}", json={
            "assignee": "Пётр Готовый", "category": "Топливо",
            "total_sum": 250.5}, headers=hdr)
        return comp, hu, [r1, r2]

    def test_basics_totals_and_groups(self, client):
        hdr = login(client, "admin", "admin123")
        comp, hu, ids = self._prepare(client, hdr)
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "2026-01-01", "date_to": "2026-12-31",
                              "company_id": comp["id"]})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["total"]["count"] == 2
        assert abs(rep["total"]["sum"] - 350.5) < 0.01
        assert {a["name"] for a in rep["by_assignee"]} == {"Иван Отчётный", "Пётр Готовый"}
        cats = {c["name"]: c["sum"] for c in rep["by_category"]}
        assert cats["Хозрасходы"] == 100.0 and cats["Топливо"] == 250.5
        assert rep["company"]["id"] == comp["id"]
        # строки отсортированы по сотрудникам
        assert rep["rows"][0]["assignee"] == "Иван Отчётный"

    def test_personal_amount_returned_for_safe_print(self, client):
        hdr = login(client, "admin", "admin123")
        comp, _, ids = self._prepare(client, hdr)
        patch = client.patch(f"/api/v1/receipts/{ids[0]}",
                             json={"personal_sum": 35}, headers=hdr)
        assert patch.status_code == 200
        rep = client.post("/api/v1/receipts/advance-report", headers=hdr,
                          json={"date_from": "2026-01-01", "date_to": "2026-12-31",
                                "company_id": comp["id"]}).json()
        assert next(r for r in rep["rows"] if r["id"] == ids[0])["personal_sum"] == 35

    def test_assignee_filter_and_ids_mode(self, client):
        hdr = login(client, "admin", "admin123")
        comp, hu, ids = self._prepare(client, hdr)
        # один сотрудник
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "2026-01-01", "date_to": "2026-12-31",
                              "company_id": comp["id"], "assignee": "Иван Отчётный"})
        rep = r.json()
        assert rep["total"]["count"] == 1
        assert abs(rep["total"]["sum"] - 100.0) < 0.01
        # только выбранные чеки — период игнорируется
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "1999-01-01", "date_to": "1999-12-31",
                              "receipt_ids": [ids[1]]})
        rep = r.json()
        assert rep["total"]["count"] == 1 and rep["rows"][0]["id"] == ids[1]

    def test_only_valid_and_bad_dates(self, client):
        hdr = login(client, "admin", "admin123")
        comp, hu, ids = self._prepare(client, hdr)
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "2026-13-01", "date_to": "2026-12-31"})
        assert r.status_code == 422
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "2026-12-31", "date_to": "2026-01-01"})
        assert r.status_code == 422
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "2026-01-01", "date_to": "2026-12-31",
                              "company_id": comp["id"], "only_valid": True})
        assert r.status_code == 200   # чеков valid может не быть — просто пусто

    def test_scope_user_forbidden_accountant_own_company(self, client):
        hdr = login(client, "admin", "admin123")
        comp, hu, ids = self._prepare(client, hdr)
        comp2 = client.post("/api/v1/companies", json={
            "name": f"ООО Другая {next(_SEQ)}"}, headers=hdr).json()
        # пользователь — 403
        r = client.post("/api/v1/receipts/advance-report", headers=hu,
                        json={"date_from": "2026-01-01", "date_to": "2026-12-31"})
        assert r.status_code == 403
        # бухгалтер другой компании не видит чужие чеки
        ha2 = _mk_user(client, hdr, comp2["id"], "ao_acc2", role="accountant")
        r = client.post("/api/v1/receipts/advance-report", headers=ha2,
                        json={"date_from": "2026-01-01", "date_to": "2026-12-31"})
        assert r.status_code == 200
        assert r.json()["total"]["count"] == 0
        # бухгалтер своей компании видит свои
        ha1 = _mk_user(client, hdr, comp["id"], "ao_acc1", role="accountant")
        r = client.post("/api/v1/receipts/advance-report", headers=ha1,
                        json={"date_from": "2026-01-01", "date_to": "2026-12-31"})
        assert r.json()["total"]["count"] == 2

    def test_empty_period(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/receipts/advance-report", headers=hdr,
                        json={"date_from": "1999-01-01", "date_to": "1999-01-31"})
        assert r.status_code == 200
        rep = r.json()
        assert rep["total"]["count"] == 0 and rep["rows"] == []


class TestVersion1140:
    def test_versions_synced(self):
        # версия берётся из config — тест проверяет синхронность всех точек
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        from app.config import settings
        assert settings.APP_VERSION == ver

    def test_ui_strings(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.14.0':" in js
        assert "aoShowPreview" in js and "aoMultiPrint" in js and "aoCsvDownload" in js
        assert "Собрать авансовый отчёт" in js
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "v1.14.0" in css


class TestUiHarnessV1140:
    def test_dom_harness_advance_report(self):
        """Node DOM-песочница: диалог «Собрать авансовый отчёт» собирает
        запрос, показывает предпросмотр, сводную печать и CSV."""
        if shutil.which("node") is None:
            import pytest
            pytest.skip("node недоступен")
        from tests.test_v1121 import _build_bundle
        open(worker_bundle_path(), "w", encoding="utf-8").write(
            _build_bundle())
        r = subprocess.run(["node", "tests/ui_harness.js", worker_bundle_path()],
                           capture_output=True, text=True, timeout=120, cwd=".")
        out = (r.stdout + r.stderr)
        assert "HARNESS_OK" in out, out[-2000:]
        assert "HARNESS FAIL" not in out, out[-2000:]


class TestAdvanceReportFolder:
    """v1.73.7: «Загрузки/АО/<сокращённое название компании>» — проверка контракта и архива."""

    def test_short_name_is_returned_for_folder(self, client):
        from app.database import SessionLocal
        from app.models import Company

        hdr = login(client, "admin", "admin123")
        n = next(_SEQ)
        comp = client.post("/api/v1/companies", json={
            "name": f"Общество с ограниченной ответственностью Папка {n}"}, headers=hdr).json()
        db = SessionLocal()
        try:
            row = db.get(Company, comp["id"])
            row.short_name = "ООО «Папка»"
            db.commit()
        finally:
            db.close()
        rep = client.post("/api/v1/receipts/advance-report", headers=hdr,
                          json={"date_from": "2026-01-01", "date_to": "2026-12-31",
                                "company_id": comp["id"]}).json()
        assert rep["company"]["short_name"] == "ООО «Папка»"
        assert rep["requisites"]["Организация"]["НаименованиеСокращенное"] == "ООО «Папка»"

    def test_zip_fallback_really_creates_folders(self):
        import pathlib
        import subprocess
        import tempfile
        import zipfile

        if shutil.which("node") is None:
            import pytest
            pytest.skip("node недоступен")
        root = pathlib.Path(__file__).resolve().parents[1]
        js = (root / "app/static/js/ao_folder.js").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            mod = pathlib.Path(tmp) / "ao_folder.mjs"
            out = pathlib.Path(tmp) / "ao.zip"
            mod.write_text(js, encoding="utf-8")
            script = """
const ao = await import('file://' + process.argv[1]);
const rep = { requisites: { 'Организация': { 'НаименованиеСокращенное': 'ООО "Ромашка"' } } };
const segments = ao.aoFolderSegments(rep);
const base = 'АО-2026-11-01';
const files = ao.aoBundleFiles({ base, csvText: '\\ufeff№;Сумма', jsonText: '{}',
  htmlText: '<html>АО</html>' });
const zip = ao.aoBuildZip(files, segments, ao.aoZipReadme(segments, base));
await (await import('node:fs/promises')).writeFile(process.argv[2],
  new Uint8Array(await zip.arrayBuffer()));
"""
            run = subprocess.run(["node", "--input-type=module", "-e", script,
                                  str(mod), str(out)], capture_output=True, text=True,
                                 timeout=60, cwd=str(root), check=False)
            assert run.returncode == 0, run.stdout + run.stderr
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                folder = "АО/ООО «Ромашка»/"
                assert "ПРОЧТИТЕ.txt" in names
                assert "АО/" in names and folder in names
                for ext in ("html", "csv", "json"):
                    assert f"{folder}АО-2026-11-01.{ext}" in names
                assert archive.testzip() is None
                assert archive.read(f"{folder}АО-2026-11-01.csv").startswith(b"\xef\xbb\xbf")
                assert b"<html>" in archive.read(f"{folder}АО-2026-11-01.html")

    def test_client_and_service_worker_know_about_folder(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        js = (root / "app/static/js/app.js").read_text(encoding="utf-8")
        mod = (root / "app/static/js/ao_folder.js").read_text(encoding="utf-8")
        sw = (root / "app/static/sw.js").read_text(encoding="utf-8")
        assert 'id="ao-save"' in js and 'id="ao-folder"' in js
        assert "aoSaveReportToDownloads" in js and "aoFolderHandleForSave" in js
        assert "Загрузки" in mod and "showDirectoryPicker" in mod
        assert "startIn: 'downloads'" in mod and "0x0800" in mod
        assert "aoAsciiFilename" in mod and "aoWriteBundle" in mod
        assert "/js/ao_folder.js" in sw
