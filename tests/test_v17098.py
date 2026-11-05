"""v1.70.98: банковская выписка (T-45) и новая заставка загрузки «chek.ymaster.ru».

Поведение проверяют tests/test_bank.py (разбор формата 1С, виды операций, сопоставление с чеками и УПД, запросы в
«Что нужно от клиента» и их самозакрытие, чужая выписка, изоляция компаний, «к возмещению», срок хранения),
tests/js/bank.test.js (экран), tests/js/ym_splash.test.js (контроллер заставки) и tests/js/splash.test.js (вход
не остаётся под заставкой). Сценарии — в DOM-стенде tests/ui_harness.js и в Playwright (tests/e2e/app.spec.js).
Здесь — синхронизация выпуска, схема, проводка, разметка заставки, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.98"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.98 — исторические проверки (точные пины — в tests/test_v17099.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 2
        assert read("app/static/index.html").count(f"?v={current}") == 2
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-04"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("(T-45)", "4 новые таблицы (bank_statements, bank_operations, bank_reimbursements, bank_settings)",
                       "kl_to_1c.txt", "«🏦 платёж без документов»", "«К возмещению»", "заставка chek.ymaster.ru"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2 and "Банковская выписка: платежи без документов" in block
        assert "Новая заставка загрузки" in block

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        assert "90vw × 90dvh" in entry and "«⬆ Загрузить выписку»" in entry and "USER_GUIDE §7.5" in entry

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Банковская выписка", "Заставка загрузки", "Первичка (электронные УПД)", "Роли и доступ",
                     "Что нужно от клиента"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_four_new_tables_without_foreign_keys_to_old_ones(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 59                                                    # v1.70.99: +bank_connections
        from app.bank.models import BankOperation, BankReimbursement, BankSettings, BankStatement
        models = (BankStatement, BankOperation, BankReimbursement, BankSettings)
        assert [m.__tablename__ for m in models] == ["bank_statements", "bank_operations", "bank_reimbursements",
                                                    "bank_settings"]
        for model in models:
            assert not model.__table__.foreign_keys, model.__tablename__        # ADR-09: откат их не замечает
        ops = BankOperation.__table__
        assert {c.name for c in ops.columns} >= {"op_hash", "op_date", "amount", "kind", "card_tail", "counterparty_inn",
                                                 "purpose", "status", "match_type", "match_id"}
        assert ops.columns["purpose"].type.length == 210 and ops.columns["card_tail"].type.length == 4
        assert not {"account", "bik", "bank_name", "payer_account"} & {c.name for c in ops.columns}   # минимум данных
        assert any(getattr(c, "name", "") == "uq_bank_operations_company_hash" for c in ops.constraints)
        assert BankSettings.__table__.columns["retention_days"].server_default.arg == "1095"
        assert BankReimbursement.__table__.columns["receipt_id"].unique
        assert "from .bank import models as _bank_models" in read("app/database.py")
        assert "from app.bank import models as _bank" in read("tests/test_migration_compat_postgres.py")
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"bank_tables": sorted(t for t in inspect(engine).get_table_names() if t.startswith("bank_"))' in rehearsal
        check = rehearsal.split('up.get("bank_tables") == [', 1)[1].split("]", 1)[0]       # v1.70.99: +bank_connections
        assert all(f'"{t}"' in check for t in ("bank_operations", "bank_reimbursements", "bank_settings",
                                                  "bank_statements"))
        assert "| Банковская выписка (v1.70.98) |" in read("docs/DATABASE.md")


class TestWiring:
    def test_routes_roles_and_docreq(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/bank", "/api/v1/bank/statements", "/api/v1/bank/statements/{statement_id}",
                     "/api/v1/bank/match", "/api/v1/bank/operations/{op_id}", "/api/v1/bank/reimburse",
                     "/api/v1/bank/reimburse.csv", "/api/v1/bank/reimburse/{receipt_id}", "/api/v1/bank/settings"):
            assert path in paths, path
        from app.services import rbac
        views = {role: set(rbac._ROLE_VIEWS[role]) for role in rbac._ROLE_VIEWS}
        assert "bank" in views["admin"] and "bank" in views["accountant"]
        assert "bank" not in views["manager"] and "bank" not in views["user"]
        from app.docreq import service as docreq
        assert docreq.KINDS["payment"] == "платёж без документов" and docreq.PERSONAL == ("receipt", "payment")
        src = read("app/docreq/service.py")
        assert 'add("payment", bank.request_title(op), "bank_op", op.id' in src
        assert 'if req.kind == "payment" and req.ref_type == "bank_op":' in src
        from app.bank import parser, service
        assert parser.MAX_BYTES == 5 * 1024 * 1024 and parser.PURPOSE_MAX == 210
        assert parser.NEEDS_DOCUMENTS == ("card", "transfer")
        assert (service.CARD_BEFORE, service.CARD_AFTER, service.DOC_BEFORE, service.DOC_AFTER) == (3, 1, 120, 60)
        assert service.COVER_TAIL == 3 and service.RETENTION == (30, 3650) and service.DEFAULT_RETENTION == 1095
        main = read("app/main.py")
        assert "app.include_router(bank_router.router)" in main
        assert 'if _bank_purged["day"] != today:' in main and "bank.purge(db)" in main        # срок — раз в сутки
        assert "    purge(db, company_id)                    # срок сократили" in read("app/bank/service.py")
        assert "| bank (банк: выписка" in read("docs/rbac_matrix.md")
        human = read("app/services/audit_human.py")
        assert 'if action == "bank_statement_uploaded":' in human and 'if action == "bank_reimbursement_decided":' in human

    def test_screen_wired(self):
        screen = read("app/static/js/bank.js")
        for needle in ("'/api/v1/bank/statements'", "/api/v1/bank/reimburse.csv?", "/api/v1/bank/reimburse?",
                       "/api/v1/bank/match?", "'/api/v1/bank/settings'", "id=\"bank-upload\"", "id=\"bank-file\"",
                       "id=\"bank-rematch\"", "id=\"bank-settings\"", "id=\"bank-kpi\"",
                       "export async function viewBank(container, opts = {})"):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("bank", "BANK", "viewBank")) for t in tops), tops   # общая область сборки
        app_js = read("app/static/js/app.js")
        assert re.search(r"import \{ viewBank[^}]*\} from './bank\.js';", app_js)   # v1.70.99: + панель банка по API
        assert "bank: 'Банк: выписка'," in app_js and "bank_statement_uploaded: ['ok', '🏦']" in app_js
        # пространства admin и accountant; v1.70.100: после «bank» в обоих — ещё «clients» (рабочее место аутсорсера)
        assert app_js.count("'primary', 'docreq', 'bank', ") == 2
        assert '<a href="#/bank" data-view="bank" class="nav-item accountant-only">' in read("app/static/index.html")
        assert '"api.js", "bank.js", "primary.js", "docreq.js"' in read("tests/test_v1121.py")
        assert "payment: '🏦 платёж без документов'" in read("app/static/js/docreq.js")
        assert ".bank-tabs {" in read("app/static/css/app.css")

    def test_admin_companies_on_direct_link(self):
        app_js = read("app/static/js/app.js")
        assert "async function adminCompanies() {" in app_js
        # первичка, docreq, банк; v1.70.100: + «🏢 Клиенты» бухгалтера на экране «Пользователи»
        assert app_js.count("const companies = await adminCompanies();") >= 3
        spec = read("tests/e2e/app.spec.js")
        for name in ("заставка загрузки: версия с сервера, «Готово к запуску» и уход",
                     "банк: платёж без документов из выписки уходит в «Что нужно от клиента»",
                     "прямая ссылка: у администратора список компаний не пустой"):
            assert f"test('{name}'" in spec, name
        assert "await expect(page.locator('#login-screen')).toBeHidden({ timeout: 15000 });" in spec

    def test_forged_token_always_differs(self):
        src = read("tests/test_primary_recognize.py")
        assert 'forged = token[:-1] + ("1" if token[-1] == "0" else "0")' in src and "assert forged != token" in src


class TestSplash:
    def test_markup_head_and_csp(self):
        html = read("app/static/index.html")
        head, body = html.split("</head>", 1)
        assert head.index('<link rel="stylesheet" href="/css/splash.css">') < head.index('href="/css/app.css?v=')
        assert '<script src="/js/splash.js" defer></script>' in head
        assert body.lstrip().startswith("<body>\n  <!-- v1.70.98: заставка загрузки «chek.ymaster.ru»")
        assert '<div id="ym-splash" class="ym-splash">' in body
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "встроенный скрипт — CSP его запретит"
        for needle in ('id="ym-splash-status" role="status" aria-live="polite"',
                       '<progress class="ym-splash__native" id="ym-splash-progress" max="100" value="0"',
                       "Версия <span id=\"ym-splash-version\">…</span><i>•</i>Выпуск 2026<i>•</i>ООО «Ямастер»",
                       'class="ym-splash__qr"', "<span class=\"ym-splash__lbl\">Инициализация</span>",
                       "<span class=\"ym-splash__lbl\">Подключение модулей</span>",
                       "<span class=\"ym-splash__lbl\">Готово к запуску</span>", 'id="ym-splash-reload"'):
            assert needle in body, needle
        assert "boot-splash" not in read("app/static/js/app.js")

    def test_styles(self):
        css = read("app/static/css/splash.css")
        for needle in ("width: 90vw; height: 90vh; height: 90dvh;", "@media (prefers-color-scheme: dark)",
                       "@media (prefers-reduced-motion: reduce)", "@media (max-aspect-ratio: 23/20)",
                       ".ym-splash::before", "perspective: 1900px", "animation: ym-safety .4s 15s forwards;",
                       ".ym-splash.is-ready {", ".ym-splash.is-gone { display: none; }", "z-index: 400",
                       ".ym-splash__particles i:nth-child(12) {", ".ym-splash__native {"):
            assert needle in css, needle
        assert "data-variant" not in css and "__ring" not in css                    # «Объём» не выбран

    def test_controller_and_app_integration(self):
        js = read("app/static/js/splash.js")
        assert not re.search(r"^\s*(?:import|export)\b", js, re.M)                   # обычный скрипт, не модуль
        # v1.70.100: таймлайн — доли заданного администратором времени (по умолчанию 5 с) вместо мгновений
        # 3-секундной заставки; быстрый повтор в той же вкладке ('ym-splash-seen') заменён настройкой длительности,
        # автотесты (webdriver) по-прежнему быстрые — не дольше 1,3 с
        for needle in ("window.ymSplash = {", "ready: function () { isReady = true; }", "setVersion: function (v)",
                       "var quick = !!navigator.webdriver;", "performance.now() > 14000",
                       "[0.15 * T, 25, 'Инициализация…', 1], [0.40 * T, 68, 'Подключение модулей…', 2],",
                       "[0.62 * T, 90, 'Финальная настройка…', 3], [0.80 * T, 100, 'Готово к запуску', 4]];",
                       "link[href*=\"app.css?v=\"]", "new CustomEvent('ym-splash-done')"):
            assert needle in js, needle
        app_js = read("app/static/js/app.js")
        assert "if (window.ymSplash) { window.ymSplash.ready(); return; }" in app_js
        assert "if (window.ymSplash) window.ymSplash.setVersion(about.version);" in app_js
        assert "'/css/splash.css', '/js/splash.js'," in read("app/static/sw.js")
        assert "document.getElementById('ym-splash')" in read("tests/js/splash.test.js")
        assert (ROOT / "tests/js/ym_splash.test.js").exists()


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_bank(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_bank98.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("бухгалтер открыл «Банк: выписка» — раздел в его пространстве", "экран банковской выписки нарисован",
                     "операции запрошены у сервера", "виден платёж без документов",
                     "в итоге — платежи без документов и сумма к возмещению"):
            assert f"PASS v1.70.98: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_design_adr_guide_manual(self):
        design = read("docs/primary_docs_design.md")
        for needle in ("**Сделано в 1.70.98:**", "## Банковская выписка (1.70.98): решения", "| Что храним |",
                       "| Что не нужно |", "| Покупка по карте |", "| Оплата поставщику |", "| К возмещению |",
                       "| Защита |", "| Срок хранения |", "«в т.ч. НДС» в назначении — не признак налога"):
            assert needle in design, needle
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-28](#adr-28) |" in arch and "## ADR-28" in arch and "`bank_reimbursements`" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.5. Банк: выписка (с v1.70.98) — бухгалтер/админ" in guide
        from app.services.manual_content import SECTIONS
        section = next(s for s in SECTIONS if s["id"] == "bank")
        assert section["audience"] == "accountant" and "С версии 1.70.98" in section["html"]
        assert "«К возмещению»" in section["html"] and "не примет" in section["html"]

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t45 = tasks["T-45"]
        assert t45["Статус"] == "done" and t45["Задача"].endswith(f"выпущено в {VERSION}")
        assert t45["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/30"
        assert t45["Связанные ADR"] == "ADR-14, ADR-28"
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.99: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 98)
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 04.11.2026 (выпуск v1.70.98):** [#30](https://github.com/Rimlin-UNC/1c_chek/issues/30) T-45" \
            in roadmap
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        rows = {r["Версия"]: r for r in ledger}
        assert rows[VERSION]["Опора репетиции"] == "да" and rows["1.70.97"]["Откат на неё"] == "можно"
        assert all("," not in v for v in rows[VERSION].values()), "в полях реестра нет запятых"
