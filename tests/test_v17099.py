"""v1.70.99: банк по API (Точка — OAuth 2.0 или JWT-ключ), эскалация напоминаний, массовое обновление ЕГРЮЛ (#50).

Поведение проверяют tests/test_bank_api.py (синтетический банк: вход по OAuth, одноразовая отметка, продление токена,
выписка «не готова», дубли с файлом, JWT, права, фоновая загрузка, TLS), tests/test_docreq_escalation.py (эскалация),
tests/test_bulk_refresh_order.py (#50) и tests/js/bank_api.test.js (панель). Сценарии — в DOM-стенде
tests/ui_harness.js и в Playwright (tests/e2e/app.spec.js). Здесь — синхронизация выпуска, схема, проводка,
документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.99"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    def test_sync_points(self):
        from app.config import settings
        assert settings.APP_VERSION == VERSION
        for rel in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                    "app/static/manifest.webmanifest", "app/static/maxapp/index.html",
                    "app/static/b2b/index.html"):
            assert VERSION in read(rel), rel
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{VERSION}" in sw and sw.count(f"?v={VERSION}") == 2
        assert read("app/static/index.html").count(f"?v={VERSION}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={VERSION}") == 2
        assert f'"version": "{VERSION}"' in read("app/static/manifest.webmanifest")

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-05"
        entry = md.split("\n## [1.70.98]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Банк по API", "OAuth 2.0", "JWT-ключ", "(#50)", "резервная копия",
                       "поверх 1.70.95–1.70.98 не меняются — шаг pip пропускается", "откатится само",
                       "новая таблица bank_connections", "bank_statements.source («file»)",
                       "doc_request_settings.escalate_days (3)", "48 из 48", "1.70.82–1.70.98",
                       "TOCHKA_CLIENT_ID и TOCHKA_CLIENT_SECRET", "только на чтение счетов и выписок",
                       "Отметка запроса одноразовая и живёт 15 минут", "новый refresh_token сохраняется сразу",
                       "Раз в 6 часов", "не задваивается", "только зашифрованными", "Сертификаты Минцифры",
                       "«Передано руководителю»", "0 — не передавать", "«компаний: 0»", "T-50", "T-68"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Выписка из банка — сама, без файлов", "«Банк по API»", "OAuth 2.0 или JWT-ключ",
                       "раз в 6 часов", "Доступ только на чтение", "Напоминания с передачей руководителю",
                       "«Передано руководителю»", "а не одни и те же 40"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Банк по API» и эскалация напоминаний")
        entry = journal.split("\n## v1.70.98", 1)[0]
        for needle in ("«🏦 Банк по API»", "«нужно подключить\n  заново»", "«*4321 USD — не загружается»",
                       "«задан —\n  пусто, не менять»", "«🔐 Подключить через Точку»", "«Проверить ключ и подключить»",
                       "«⬇ Загрузить сейчас»", "«Точка, API»", "«Передано руководителю»", "«ещё ждут обновления: N",
                       "USER_GUIDE §7.4 и §7.6", "две\n  карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == [
            "Банк по API (Точка)", "Банковская выписка", "Что нужно от клиента"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 99) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != "Банк по API (Точка)"}
        assert {"name": "Банк по API (Точка)", "from": "", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (05.11.2026)" in readme
        assert "Последний выпуск — v1.70.98" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.98.**" in readme
        assert "В базе появится 1 новая таблица и 2 колонки" in readme


class TestSchema:
    def test_connections_table_secrets_only_sealed(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 60
        from app.bank.models import BankConnection, BankStatement
        table = BankConnection.__table__
        assert BankConnection.__tablename__ == "bank_connections" and not table.foreign_keys   # ADR-09
        cols = {c.name: c for c in table.columns}
        for name in ("client_secret_enc", "jwt_enc", "access_enc", "refresh_enc", "accounts_enc"):
            assert str(cols[name].type) == "TEXT", name
        assert not {"client_secret", "jwt", "access_token", "refresh_token", "accounts", "account_id"} & set(cols)
        assert cols["consent_id"].type.length == 128 and cols["oauth_state"].index
        assert any(getattr(c, "name", "") == "uq_bank_connections_company_bank" for c in table.constraints)
        assert BankStatement.__table__.columns["source"].server_default.arg == "file"
        from app.docreq.models import DocRequestSettings
        assert DocRequestSettings.__table__.columns["escalate_days"].server_default.arg == "3"
        db_py = read("app/database.py")
        assert ('("source", "ALTER TABLE bank_statements ADD COLUMN source VARCHAR(10) DEFAULT \'file\'")' in db_py)
        assert ('("escalate_days", "ALTER TABLE doc_request_settings ADD COLUMN escalate_days INTEGER DEFAULT 3")'
                in db_py)
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert 'up.get("bank_tables") == ["bank_connections", "bank_operations", "bank_reimbursements",' in rehearsal
        db = read("docs/DATABASE.md")
        assert "60 объявлений `__tablename__`" in db and "| Банк по API (v1.70.99) |" in db
        assert "все 60 таблиц приложения" in db and "с 1.70.99 — `bank_connections`" in db


class TestWiring:
    def test_routes_callback_and_background(self, client):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/bank/connection", "/api/v1/bank/connection/authorize", "/api/v1/bank/connection/jwt",
                     "/api/v1/bank/connection/sync"):
            assert path in paths, path
        assert "/api/v1/bank/tochka/callback" not in paths                       # служебный адрес возврата
        # FastAPI 0.162 держит подключённые роутеры лениво — адрес возврата проверяется запросом
        res = client.get("/api/v1/bank/tochka/callback", follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"].startswith("/?bank=error&reason=")
        main = read("app/main.py")
        assert "from .bank import connect as bank_api" in main and "bank_api.sync_due(db)" in main
        assert 'log.exception("bank api tick failed")' in main

    def test_bank_client_is_read_only_and_verifies_tls(self):
        from app.bank import connect, tochka
        assert tochka.AUTH_BASE == "https://enter.tochka.com/connect"
        assert tochka.API_BASE == "https://enter.tochka.com/uapi"
        assert tochka.SCOPE == "accounts customers statements"
        assert not any(p.startswith(("Create", "Edit", "Make")) for p in tochka.PERMISSIONS)   # только чтение
        assert "ReadStatements" in tochka.PERMISSIONS and tochka.CONSENT_DAYS == 365
        assert connect.CALLBACK_PATH == "/api/v1/bank/tochka/callback"
        assert connect.STATE_TTL.total_seconds() == 15 * 60 and connect.SYNC_EVERY.total_seconds() == 6 * 3600
        assert connect.MAX_PER_TICK == 3 and connect.OVERLAP_DAYS == 3 and connect.MAX_BACK_DAYS == 366
        src = read("app/bank/tochka.py")
        assert "verify=False" not in src and "CERT_NONE" not in src and '"russian_trusted_ca.pem"' in src
        env = read(".env.example")
        assert "\nTOCHKA_CLIENT_ID=\n" in env and "\nTOCHKA_CLIENT_SECRET=\n" in env      # только имена, без значений

    def test_audit_phrases(self):
        from app.services.audit_human import humanize_audit as describe
        assert describe("bank_connected", {"company": "Ромашка", "bank": "Точка Банк", "method": "OAuth 2.0",
                                           "accounts": 2})[0] == "банк подключён по API"
        assert describe("bank_synced", {"company": "Ромашка", "operations": 9, "saved": 3, "matched": 2})[0] == \
            "выписка загружена из банка"
        assert describe("bank_connection_saved", {})[0] == "сохранено подключение банка по API"
        assert describe("bank_connection_removed", {})[0] == "банк отключён"
        bulk = describe("company_cards_bulk_refreshed", {"ok": 40, "errors": 1, "remaining": 25})
        assert bulk == ("массовое обновление карточек из ЕГРЮЛ", "компаний: 40, ошибок: 1, ещё ждут обновления: 25")
        js = read("app/static/js/app.js")
        assert "bank_connected: ['ok', '🏦']" in js and "bank_synced: ['ok', '🏦']" in js

    def test_bulk_refresh_order_and_escalation(self):
        src = read("app/routers/companies.py")
        assert "base.order_by(_case((Company.card_updated_at.is_(None), 0), else_=1)," in src
        assert '"total": total, "remaining": remaining,' in src
        from app.docreq import service as docreq
        assert docreq.DEFAULTS["escalate_days"] == 3 and docreq.ESCALATE_ROLE == "manager"
        assert docreq.ESCALATE_MAX == 30
        js = read("app/static/js/docreq.js")
        assert 'id="dreq-s-escalate" type="number" min="0" max="30"' in js
        assert "escalated: 'Передано руководителю'," in js and "escalate_days: escalate," in js


class TestScreen:
    def test_panel_and_card(self):
        screen = read("app/static/js/bank.js")
        for needle in ("export function bankConnHtml(c)", "export function bankConnBody(get, companyId)",
                       "export async function bankConnectPanel(el, companyId, opts = {})",
                       "export function bankReturnNotice(loc = window.location)", "'/api/v1/bank/connection/authorize'",
                       "'/api/v1/bank/connection/jwt'", "'/api/v1/bank/connection/sync'", "window.location.assign(url);",
                       "get('bankc-auth').classList.toggle('hidden', jwt);", '<div class="glass card" id="bank-conn"></div>',
                       "box.classList.add('hidden');"):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("bank", "BANK", "viewBank")) for t in tops), tops       # общая область сборки
        app_js = read("app/static/js/app.js")
        assert "import { viewBank, bankConnectPanel, bankReturnNotice } from './bank.js';" in app_js
        assert "bankConnectPanel(slot.querySelector('#cc-bank'), c.id);" in app_js
        assert '<div class="info-callout" id="cc-bank"></div>' in app_js
        assert app_js.index("const bankBack = bankReturnNotice();") < app_js.index("if (isAdmin()) initCompanyFilter();")
        css = read("app/static/css/app.css")
        assert ".bank-conn-head {" in css and ".bank-conn-uri {" in css and ".bank-conn-skeleton {" in css
        spec = read("tests/e2e/app.spec.js")
        assert "test('банк по API: карточка компании — Точка, OAuth 2.0, секрет не возвращается'" in spec
        assert "bankReturnNotice: () => null, bankConnectPanel: stub()," in read("tests/js/telegram_login.test.js")


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_bank_api(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_bank99.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("в карточке компании есть блок «Банк по API»", "карточка запросила подключение банка именно этой компании",
                     "бухгалтер — состояние подключения своей компании запрошено",
                     "панель «Банк по API»: подключён, счёт *0001", "кнопка «Загрузить сейчас», секретов нет"):
            assert f"PASS v1.70.99: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_design_adr_guide_manual(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("**Сделано в 1.70.99:**", "## Банк по API (1.70.99): решения", "| Способ доступа |",
                       "| Чьё приложение OAuth |", "| Защита возврата |", "| Токены |", "| Компания и счета |",
                       "| Выписка |", "| Не готова |", "| Дубли |", "| Секреты |", "| TLS |", "| Сбои |",
                       "**Эскалация напоминаний (T-67,"):
            assert needle in design, needle
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-29](#adr-29) |" in arch and "## ADR-29" in arch and "`bank_connections`" in arch
        assert "**с версии / даты:** 1.70.99, 2026-11-05" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.6. Банк по API: выписка сама (с v1.70.99) — бухгалтер/админ" in guide
        assert "**Эскалация (с v1.70.99).**" in guide
        from app.services.manual_content import SECTIONS
        bank = next(s for s in SECTIONS if s["id"] == "bank")
        assert "С версии 1.70.99 — выписка из банка сама." in bank["html"] and "JWT-ключ" in bank["html"]
        docreq = next(s for s in SECTIONS if "Что нужно от клиента" in s["title"] or s["id"] == "docreq")
        assert "С версии 1.70.99 — эскалация." in docreq["html"]

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        for tid, issue in (("T-65", 50), ("T-66", 51), ("T-67", 52)):
            row = tasks[tid]
            assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}"), tid
            assert row["Issue/PR"] == f"https://github.com/Rimlin-UNC/1c_chek/issues/{issue}", tid
        assert "ADR-29" in tasks["T-66"]["Связанные ADR"]
        assert tasks["T-68"]["Статус"] == "todo" and tasks["T-68"]["Issue/PR"].endswith("/issues/53")
        assert tasks["T-50"]["Приоритет"] == "P1"
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.98" in tasks["T-01"]["Блокер"]
        for tid in ("T-41", "T-42", "T-43", "T-44", "T-45", "T-46", "T-47", "T-48", "T-49"):
            assert tasks[tid]["Источник"].endswith("docs/kb/10_research.md"), tid
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.99):** [#51](https://github.com/Rimlin-UNC/1c_chek/issues/51) T-66" \
            in roadmap
        assert "T-01 — установить на рабочий сервер v1.70.99 (включает 1.70.82–1.70.98; P0)" in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert ledger[-2]["Версия"] == "1.70.98" and ledger[-2]["Откат на неё"] == "можно"
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
