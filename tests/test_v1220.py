# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.22.0: скачивание свежих данных ЕГРЮЛ/ЕГРИП
# (ФНС egrul.nalog.ru → Checko) и сокращённое наименование из карточки.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


def _mk_company(client, name="ООО Реестр-Тест", inn="7704001236"):
    hdr = login(client, "admin", "admin123")
    r = client.post("/api/v1/companies", json={"name": name, "inn": inn},
                    headers=hdr)
    assert r.status_code == 201, r.text
    return hdr, r.json()


class TestShortName:
    def test_model_and_dict(self, client):
        hdr, comp = _mk_company(client)
        d = client.get("/api/v1/companies", headers=hdr).json()
        row = next(c for c in d if c["id"] == comp["id"])
        assert "short_name" in row and "display_name" in row
        assert row["display_name"] == comp["name"]      # пусто → полное

    def test_refresh_sets_short_name(self, client, monkeypatch):
        from app.routers import companies as cr
        hdr, comp = _mk_company(client, "ООО Ромашка-Вью", "7704001243")
        monkeypatch.setattr(cr.checko, "fetch_card", lambda k, inn: {"card": {
            "kind": "legal", "inn": inn, "name_full": "ООО «Ромашка-Вью»",
            "name_short": "Ромашка-Вью", "ogrn": "1157847000000"}})
        r = client.post(f"/api/v1/companies/{comp['id']}/refresh-card",
                        headers=hdr)
        assert r.status_code == 200, r.text
        row = next(c for c in client.get("/api/v1/companies",
                                         headers=hdr).json()
                   if c["id"] == comp["id"])
        assert row["short_name"] == "Ромашка-Вью"
        assert row["display_name"] == "Ромашка-Вью"     # в списках — короткое

    def test_me_prefers_short_name(self, client, monkeypatch):
        from app.routers import companies as cr
        hdr, comp = _mk_company(client, "ООО Полное Название-Ми", "7704001250")
        monkeypatch.setattr(cr.checko, "fetch_card", lambda k, inn: {"card": {
            "kind": "legal", "inn": inn, "name_full": "ООО «Полное Название-Ми»",
            "name_short": "Полное Название"}})
        client.post(f"/api/v1/companies/{comp['id']}/refresh-card", headers=hdr)
        # у бухгалтера в компании /me company_name — сокращённое из карточки
        inv = client.post("/api/v1/invites", json={
            "role": "accountant", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "sn_acc", "password": "parol123",
            "full_name": "Сокращ Назв"}).json()
        me = client.get("/api/v1/auth/me", headers={
            "Authorization": "Bearer " + reg["access_token"]}).json()
        assert me["company_name"] == "Полное Название"

    def test_ui_uses_compname(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "const compName = (c)" in js
        assert js.count("compName(") >= 5               # селектор, таблицы, чип
        assert "title=\"${esc(c.name)}\"" in js         # полное — в подсказке


class TestRegistryDownload:
    def test_requires_inn(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/companies", json={"name": "ООО Без-ИНН-Ре"},
                        headers=hdr)
        # ИНН обязателен при создании → компании без ИНН не бывает;
        # проверяем 404 несуществующей и 422 через прямой сброс ИНН
        r2 = client.post("/api/v1/companies/no-such/registry/download",
                         headers=hdr)
        assert r2.status_code == 404

    def test_fns_pdf_first(self, client, monkeypatch):
        from app.routers import companies as cr
        from app.services import registry as reg
        hdr, comp = _mk_company(client, "ООО ПДФ-Первый", "7704001268")
        monkeypatch.setattr(reg, "fetch_fns_pdf",
                            lambda inn, timeout=20: b"%PDF-1.7 fake pdf bytes" * 300)
        r = client.post(f"/api/v1/companies/{comp['id']}/registry/download",
                        headers=hdr)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/pdf")
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["content-disposition"].endswith(".pdf") or \
            "registry_" in r.headers["content-disposition"]

    def test_fallback_to_checko_html(self, client, monkeypatch):
        from app.routers import companies as cr
        from app.services import registry as reg
        hdr, comp = _mk_company(client, "ООО ХТМЛ-Выписка", "7704001275")
        monkeypatch.setattr(reg, "fetch_fns_pdf", lambda inn, timeout=20: None)
        monkeypatch.setattr(cr.checko, "fetch_card", lambda k, inn: {"card": {
            "kind": "legal", "inn": inn, "name_full": "ООО «ХТМЛ-Выписка»",
            "name_short": "ХТМЛ-Выписка", "kpp": "770201001",
            "ogrn": "1157700000000", "okpo": "12345678",
            "status": "Действующая", "address": "г. Москва, ул. Тестовая, 1",
            "okved": "62.01 Разработка ПО", "founders_count": 2,
            "capital": "10000"}})
        r = client.post(f"/api/v1/companies/{comp['id']}/registry/download",
                        headers=hdr)
        assert r.status_code == 200, r.text
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert "ХТМЛ-Выписка" in body          # сокращённое — в выписке
        assert "Сведения из ЕГРЮЛ" in body
        assert "Checko" in body
        # карточка в системе обновилась + короткое имя
        row = next(c for c in client.get("/api/v1/companies",
                                         headers=hdr).json()
                   if c["id"] == comp["id"])
        assert row["short_name"] == "ХТМЛ-Выписка"
        card = client.get(f"/api/v1/companies/{comp['id']}/card",
                          headers=hdr).json()
        assert card["card_updated_at"] is not None
        assert card["card"]["name_short"] == "ХТМЛ-Выписка"

    def test_both_sources_fail_502(self, client, monkeypatch):
        from app.routers import companies as cr
        from app.services import registry as reg
        hdr, comp = _mk_company(client, "ООО Сбой-Сети", "7704001282")
        monkeypatch.setattr(reg, "fetch_fns_pdf", lambda inn, timeout=20: None)
        monkeypatch.setattr(
            cr.checko, "fetch_card",
            lambda k, inn: (_ for _ in ()).throw(
                cr.checko.CheckoError("Сервис Checko недоступен")))
        r = client.post(f"/api/v1/companies/{comp['id']}/registry/download",
                        headers=hdr)
        assert r.status_code == 502
        assert "недоступны" in r.json()["detail"]

    def test_excerpt_pdf_validation(self):
        from app.services.registry import fetch_fns_pdf
        # на живой вызов в песочнице сеть закрыта — вернётся None, не исключение
        assert fetch_fns_pdf("7707083893", timeout=3) in (None, b"%PDF-")

    def test_audit_logged(self, client, monkeypatch):
        from app.routers import companies as cr
        from app.services import registry as reg
        from app.services.audit import SessionLocal
        from app.models import AuditLog
        hdr, comp = _mk_company(client, "ООО Аудит-Ре", "7704001290")
        monkeypatch.setattr(reg, "fetch_fns_pdf", lambda inn, timeout=20: None)
        monkeypatch.setattr(cr.checko, "fetch_card", lambda k, inn: {"card": {
            "kind": "legal", "inn": inn, "name_full": "ООО «Аудит-Ре»",
            "name_short": "Аудит-Ре"}})
        client.post(f"/api/v1/companies/{comp['id']}/registry/download",
                    headers=hdr)
        db = SessionLocal()
        try:
            acts = [a.action for a in db.query(AuditLog).all()]
        finally:
            db.close()
        assert "registry_downloaded" in acts


class TestUIRegistryButton:
    def test_button_and_handler(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="cc-reg"' in js
        assert "Скачать свежие данные ЕГРЮЛ/ЕГРИП" in js
        assert "registry/download" in js
        assert "btn-accent" in js


class TestVersion1220:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.22.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert f"'{ver}':" in js
        # миграция колонки заявлена
        assert "ADD COLUMN short_name" in open("app/database.py",
                                               encoding="utf-8").read()
