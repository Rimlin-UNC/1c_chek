# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.12.1: восстановление запуска интерфейса.
# РЕГРЕССИЯ 1.12.0: разорванная шаблонная строка в viewUsers (страница
# «Пользователи») ломала ВЕСЬ app.js — приложение не открывалось в
# браузере, хотя node --check ложно проходил. Страж: полный парс бандла
# всех модулей через node vm (если node недоступен — тест пропускается).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import shutil
import subprocess

import pytest

from tests.conftest import login

JS_DIR = "app/static/js"
MODULES = ["ui.js", "icons.js", "charts.js", "scanner.js",
           "printpack.js", "api.js", "app.js"]


def _build_bundle() -> str:
    parts = []
    for name in MODULES:
        src = open(f"{JS_DIR}/{name}", encoding="utf-8").read()
        src = re.sub(r"^import[^;]+;", "", src, flags=re.M | re.S)
        src = re.sub(r"^export \{[^}]*\};?", "", src, flags=re.M)
        src = re.sub(r"^export (async )?(default )?(function|const|class)",
                     r"\1\3", src, flags=re.M)
        parts.append(src)
    return "\n".join(parts)


class TestUiBundleParses:
    @pytest.mark.skipif(shutil.which("node") is None,
                        reason="node недоступен на этой машине")
    def test_full_bundle_parses(self):
        """ВЕСЬ клиентский код обязан парситься целиком: одна синтаксическая
        ошибка в любом модуле = приложение не открывается вовсе."""
        open("/tmp/ymaster_ui_bundle.js", "w", encoding="utf-8").write(
            _build_bundle())
        r = subprocess.run(
            ["node", "-e",
             "new (require('vm').Script)(require('fs').readFileSync("
             "'/tmp/ymaster_ui_bundle.js','utf8')); console.log('PARSE_OK')"],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (r.stdout + r.stderr)[:2000]
        assert "PARSE_OK" in r.stdout

    def test_no_broken_template_after_ternary(self):
        """Точечный страж бага 1.12.0: тернарник не должен закрывать
        внешний шаблон (`` : ''}`` с разрывом литерала)."""
        app = open(f"{JS_DIR}/app.js", encoding="utf-8").read()
        assert re.search(r"\? `\n[^`]*` : ''\}`\n\n\s*<div", app) is None


class TestCompanyNamesFromNotes:
    def test_extract_name(self):
        from app.database import company_name_from_note as f
        assert f("Иванова — ООО «Альфа-Трейд»") == "ООО «Альфа-Трейд»"
        assert f("Петров - ООО «Альфа-Трейд»") == "ООО «Альфа-Трейд»"
        assert f("Смирнова — ИП Смирнов А.В.") == "ИП Смирнов А.В."
        assert f("ООО «Бета-Строй»") == "ООО «Бета-Строй»"   # дефис внутри — не режем
        assert f("Иванова —бухгалтерия") == "Иванова —бухгалтерия"  # тире без пробелов — не разделитель
        assert f("") == "" and f(None) == ""

    def test_repair_merges_polluted_companies(self, client):
        """На сервере, где миграция 1.12.0 уже создала компании из ПОЛНОГО
        текста памятки, ремонт переименовывает их в названия и сливает
        сотрудников одной организации в одну компанию."""
        from app.database import (SessionLocal, repair_company_names)
        from app.models import AppSetting, Company

        hdr = login(client, "admin", "admin123")
        # создаём «испорченное» состояние как после миграции 1.12.0
        c1 = client.post("/api/v1/companies",
                         json={"name": "Иванова — ООО «Стрела»"},
                         headers=hdr).json()
        c2 = client.post("/api/v1/companies",
                         json={"name": "Петров — ООО «Стрела»"},
                         headers=hdr).json()
        c3 = client.post("/api/v1/companies",
                         json={"name": "ООО «Стрела»"}, headers=hdr).json()
        # сотрудники в «испорченных» компаниях
        for cid, uname in ((c1["id"], "strela_u1"), (c2["id"], "strela_u2")):
            r = client.post("/api/v1/users", json={
                "username": uname, "password": "parol123",
                "role": "user", "company_id": cid}, headers=hdr)
            assert r.status_code == 200
        # сбрасываем флаг и запускаем ремонт
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(
                AppSetting.key == "v1112_company_names_repaired").first()
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()
        out = repair_company_names()
        assert out.get("merged", 0) >= 2, out
        # обе «испорченные» компании слились в «ООО «Стрела»»
        comps = client.get("/api/v1/companies", headers=hdr).json()
        by_id = {c["id"]: c for c in comps}
        assert c1["id"] not in by_id and c2["id"] not in by_id
        assert any(c["id"] == c3["id"] for c in comps)
        # сотрудники перепривязаны
        users = client.get("/api/v1/users", headers=hdr).json()
        moved = [u for u in users
                 if u["username"] in ("strela_u1", "strela_u2")]
        assert all(u["company_id"] == c3["id"] for u in moved)
        # идемпотентность: повтор ничего не ломает
        repair_company_names()
        comps2 = client.get("/api/v1/companies", headers=hdr).json()
        assert len([c for c in comps2 if c["name"] == "ООО «Стрела»"]) == 1
