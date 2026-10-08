# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.0: «Источник данных чека» — только
# proverkacheka.com (максимум полей), мёртвые источники выведены,
# ФНС-блок (мастер-токен) не тронут, зависимости в норме.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1570:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.57.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.56.2" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.0':") < js.index("'1.56.2':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.0]") == 1
        assert ch.index("## [1.57.0]") < ch.index("## [1.56.2]")


class TestDeadSourcesRemoved:
    def test_no_dead_code_in_engine(self):
        src = open("app/services/external.py", encoding="utf-8").read()
        for gone in ("fetch_crpt", "fetch_fns_app", "fetch_ofdru",
                     "CRPT_URL", "OFDRU_URL", "FNS_APP_URL",
                     "FNS_APP_HEADERS", "_fns_app_session", "irkkt",
                     "ofd.ru"):
            assert gone not in src, gone

    def test_no_dead_fields_in_settings_api(self):
        sr = open("app/routers/settings_routes.py", encoding="utf-8").read()
        assert "ofd_ru_token" not in sr and "fns_app" not in sr
        sc = open("app/schemas.py", encoding="utf-8").read()
        assert "ofd_ru_token" not in sc and "fns_app" not in sc

    def test_no_dead_fields_in_admin_ui(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "ext-fns-inn" not in js and "ext-ofd" not in js
        assert "ofd_ru_token:" not in js and "fns_app_inn:" not in js
        # публичный текст лендинга больше не обещает «Честный знак»
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "Честный знак" not in idx

    def test_chain_ignores_dead_order(self, monkeypatch):
        """Сохранённый порядок с мёртвыми источниками игнорируется."""
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "external_order": "fns_api,fns_app,crpt,ofd_ru,proverkacheka",
            "proverkacheka_token": "tok", "fns_master_token": "",
        })
        chain = eng.provider_chain(None)
        assert chain == ["proverkacheka", "mock"]

    def test_chain_fns_api_on_perspective(self, monkeypatch):
        """Мастер-токен ФНС задан → fns_api автоматически первым."""
        from app.services import external
        eng = external.ExternalFetchEngine()
        monkeypatch.setattr(eng, "_settings", lambda db: {
            "external_order": "proverkacheka",
            "proverkacheka_token": "tok", "fns_master_token": "master",
        })
        chain = eng.provider_chain(None)
        assert chain == ["fns_api", "proverkacheka", "mock"]

    def test_status_tuple_clean(self):
        src = open("app/services/external.py", encoding="utf-8").read()
        assert 'for p in ("fns_api", "proverkacheka", "mock"):' in src


class TestProverkachekaMaxData:
    def test_fetch_matches_documentation(self):
        """POST /api/v1/check/get, form qrraw+token, Cookie ENGID=1.1."""
        src = open("app/services/external.py", encoding="utf-8").read()
        assert 'PROVERKACHEKA_URL = "https://proverkacheka.com/api/v1/check/get"' in src
        f = src.split("def fetch_proverkacheka")[1].split("def ")[0]
        assert 'data = {"qrraw": qr_raw}' in f
        assert 'data["token"] = token' in f
        assert '"Cookie": "ENGID=1.1"' in f

    def test_new_extra_fields_parsed(self):
        """ФФД-версия, адрес источника, мета позиций."""
        from app.services.external import parse_receipt_payload
        payload = {
            "user": 'ООО "Тест-Магазин"', "userInn": "7801234567",
            "dateTime": "2026-10-05T12:30:00", "totalSum": 23220,
            "retailPlace": "Магазин", "retailPlaceAddress": "г. СПб",
            "fiscalDriveNumber": "738", "fiscalDocumentNumber": 5321,
            "fiscalSign": 1291708960, "fiscalDocumentFormatVer": 4,
            "appliedTaxationType": 1,
            "metadata": {"subtype": "receipt", "ofdId": "ofd5",
                         "address": "188760, Ленинградская обл.",
                         "receiveDate": "2026-08-17T12:29:31Z"},
            "items": [
                {"name": "Розетка", "price": 13410, "sum": 13410,
                 "quantity": 1, "nds": 11, "paymentType": 4,
                 "productType": 1, "itemsQuantityMeasure": 0},
                {"name": "Вилка", "price": 9810, "sum": 9810,
                 "quantity": 1, "nds": 11},
            ],
        }
        res = parse_receipt_payload(payload, known_rub=232.20)
        assert res.found and res.total_sum == 232.20
        ex = res.extra
        assert ex["ffd_version"] == 4
        assert "Ленинградская" in ex["source_address"]
        im = {m["pos"]: m for m in ex["items_meta"]}
        assert im[1]["paymenttype"] == 4 and im[1]["producttype"] == 1
        assert im[1]["itemsquantitymeasure"] == 0
        assert 2 not in im                       # без мета-полей — записи нет
        assert im[1]["pos"] == 1

    def test_ext_json_columns_intact(self):
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert "receipt.ext_json = json.dumps(res.extra" in src
        assert "receipts ADD COLUMN ext_json" in open(
            "app/database.py", encoding="utf-8").read()


class TestFnsBlockUntouched:
    def test_fns_card_still_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Проверка чеков (ФНС)</div>" in js
        # карточка ФНС живёт на своём эндпоинте /settings/fns (master_token)
        assert 'id="fns-token"' in js
        assert "api.put('/api/v1/settings/fns'" in js
        sr = open("app/routers/settings_routes.py", encoding="utf-8").read()
        assert "/fns" in sr and "master_token" in sr
        sc = open("app/schemas.py", encoding="utf-8").read()
        assert "master_token" in sc

    def test_sources_card_rewritten(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "(v1.57.0)</span>" in js
        assert "единственного стабильно работающего источника" in js
        assert "на перспективу" in js.split("📥 Источники данных чека")[1][:1800]
        assert "Токен proverkacheka.com" in js

    def test_external_status_configured(self):
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        blk = src.split('"configured"')[1][:400]
        assert "fns_api" in blk and "proverkacheka" in blk
        assert "fns_app" not in blk and "crpt" not in blk


class TestChainFunctional:
    def test_chain_without_tokens(self, monkeypatch):
        from app.services.external import ExternalFetchEngine
        from app.database import SessionLocal
        monkeypatch.setattr(
            "app.services.external.settings.FNS_MASTER_TOKEN", "",
            raising=False)
        eng = ExternalFetchEngine()
        db = SessionLocal()
        try:
            chain = eng.provider_chain(db)
        finally:
            db.close()
        assert chain[-1] == "mock"
        assert not any(c in ("crpt", "ofd_ru") or c.startswith("fns_app")
                       or c.startswith("custom") for c in chain)


class TestDependencies:
    def test_requirements_cover_http_client(self):
        req = open("requirements.txt", encoding="utf-8").read()
        assert "httpx>=" in req
        assert "uvicorn[standard]" in req     # WebSocket-поддержка сервера

    def test_imports_match_requirements(self):
        """Автотест соответствия импортов и requirements (если есть)."""
        import subprocess, sys, shutil
        if shutil.which("python3") is None:
            return
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_dependencies.py",
             "-q", "--no-header", "-x"], capture_output=True, text=True,
            timeout=300)
        assert "passed" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-600:]


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Проверка чеков (ФНС и источники)"] == "1.57.0"
        assert reg["Сканирование чеков"] == "1.56.2"     # не задет
        assert reg["Чек-Пул"] == "1.56.1"                # не задет
