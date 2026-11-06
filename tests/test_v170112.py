"""v1.70.112: согласование авансовых отчётов — сотрудник → руководитель → бухгалтер с простой подписью (T-46, #31).

Поведение — tests/test_approvals.py (SQLite и PostgreSQL), экран — tests/js/approvals.test.js, путь пользователя —
браузерный сценарий в tests/e2e/app.spec.js. Здесь — точки выпуска, маршруты, регистрация таблиц, документы и база
знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.112"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/31"
TABLES = ["advance_approval_events", "advance_approvals", "approval_settings"]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
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

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.111", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-06"
        entry = md.split("\n## [1.70.111]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109–1.70.111 не меняются — шаг pip пропускается",
                       "откатится само", "добавляются 3 новые таблицы (approval_settings, advance_approvals, "
                       "advance_approval_events); старые таблицы не меняются", "прежняя версия новых таблиц просто не видит",
                       "48 из 48", "1.70.82–1.70.111", "T-46, #31, ADR-24, ADR-42", "Выключено по умолчанию",
                       "«Подписать и отправить»", "72 часа", "Telegram и MAX", "SHA-256", "Лист согласования",
                       "docs/pep_regulation_template.md", "SQLite и PostgreSQL", "второе — 409", "T-62"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Авансовые отчёты — с подписью", "«Согласования»", "Этап без ответа 72 часа — согласовано",
                       "Включает администратор", "Каждое решение — в журнале", "Telegram и MAX"):
            assert needle in block, needle
        assert "'1.70.111': [" in js                         # история выпусков на месте

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — Новый раздел «Согласования»: авансовый отчёт с простой подписью")
        entry = journal.split("\n## v1.70.111", 1)[0]
        for needle in ("«Чеки и учёт» → «Согласования»", "«Ждут меня · Мои отчёты · Все»", "«＋ Авансовый отчёт»",
                       "«🔒 Содержимое\n  и журнал целы»", "«Подписать и отправить»", "«🖨 Лист согласования»",
                       "ADR-24 и ADR-42", "две\n  карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        changed = ["Печать и выгрузка в 1С", "Роли и доступ", "Согласование авансовых отчётов"]
        assert sorted(n for n, v in reg.items() if v == VERSION) == changed
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 112) for v in reg.values())
        before = {k: v for k, v in reg.items() if k != "Согласование авансовых отчётов"}
        before.update({"Роли и доступ": "1.70.110", "Печать и выгрузка в 1С": "1.70.100"})
        diff = {d["name"]: (d["from"], d["to"]) for d in updater.diff_blocks(before, reg)}
        assert diff["Роли и доступ"] == ("1.70.110", VERSION) and diff["Печать и выгрузка в 1С"] == ("1.70.100", VERSION)

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (06.11.2026)" in readme
        assert "Последний выпуск — v1.70.111" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.111.**" in readme
        section = readme.split(f"## 🆕 Последний выпуск — v{VERSION}", 1)[1].split("\n## ", 1)[0]
        assert "Добавляются 3 новые таблицы согласования" in section and "**Согласования:**" in section
        assert "**Из 1.70.111:**" in section and "docs/pep_regulation_template.md" in section

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        manual = build_manual()
        assert manual["updated"] == "2026-11-06"
        by_id = {s["id"]: s for s in SECTIONS}
        sec = by_id["approvals"]
        assert sec["audience"] == "all" and sec["image"] == "/img/manual/approvals.jpg"
        assert (ROOT / "app/static/img/manual/approvals.jpg").read_bytes()[:2] == b"\xff\xd8"     # JPEG
        for needle in ("<b>С версии 1.70.112 — раздел «Согласования».</b>", "<b>«Подписать и отправить»</b>",
                       "<b>72 часа</b>", "docs/pep_regulation_template.md"):
            assert needle in sec["html"], needle
        assert all(s.get("image") for s in SECTIONS)


# ====================================================================== код
class TestCode:
    def test_routes_and_audit(self):
        from app.main import app
        from app.services.audit_human import humanize_audit
        paths = app.openapi()["paths"]
        assert {"get", "post"} <= set(paths["/api/v1/approvals"])
        assert {"get", "put"} <= set(paths["/api/v1/approvals/settings"])
        for path in ("/api/v1/approvals/{approval_id}/sign", "/api/v1/approvals/{approval_id}/decision",
                     "/api/v1/approvals/{approval_id}/cancel"):
            assert "post" in paths[path], path
        for path in ("/api/v1/approvals/employees", "/api/v1/approvals/preview", "/api/v1/approvals/{approval_id}"):
            assert "get" in paths[path], path
        title, details = humanize_audit("approval_approved", {"number": "АО-2026-11-03", "stage": "manager"})
        assert title == "авансовый отчёт согласован (ПЭП)" and "этап: руководитель" in details
        title, _ = humanize_audit("approvals_settings", {"enabled": True, "timeout_hours": 72})
        assert title == "изменены настройки согласования авансовых отчётов"
        assert "approval_signed: ['ok', '✍️']" in read("app/static/js/app.js")

    def test_tables_registered_everywhere(self):
        from app.approvals import models
        from app.database import Base
        assert sorted(t for t in Base.metadata.tables if t in TABLES) == TABLES
        assert models.AdvanceApproval.__mapper__.version_id_col is not None      # одновременные решения
        assert "from .approvals import models as _approvals_models" in read("app/database.py")
        assert "from app.approvals import models as _approvals" in read("tests/test_migration_compat_postgres.py")
        assert 'up.get("approvals_tables") == ["advance_approval_events", "advance_approvals", "approval_settings"]' \
            in read("qa/upgrade_rehearsal.py")
        assert '"tests/test_approvals.py"' in read("qa/pg_local.py")
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 68

    def test_tick_me_and_space(self):
        main = read("app/main.py")
        once = main.split("def _doc_requests_once", 1)[1].split("\nasync def ", 1)[0]
        assert "approvals.tick(db)" in once and 'log.exception("approvals tick failed")' in once
        assert "app.include_router(approvals_router.router)" in main
        assert 'd["approvals_enabled"] = approvals.is_enabled(db, user.company_id)' in read("app/routers/auth_routes.py")
        from app.services import rbac
        for role in ("admin", "accountant", "manager", "user"):
            assert "approvals" in rbac.space_views(role), role
        assert "approvals" not in rbac.space_views("donor")
        js = read("app/static/js/app.js")
        assert "import { viewApprovals } from './approvals.js';" in js
        assert "$$('.approvals-only').forEach(el => el.classList.toggle('hidden', !isAdmin()));" in js
        assert "api.get('/api/v1/approvals?box=todo&limit=1')" in js
        assert 'class="nav-item approvals-only hidden"' in read("app/static/index.html")

    def test_service_rules(self):
        from app.approvals import service
        assert service.DEFAULTS["enabled"] is False and service.DEFAULTS["timeout_hours"] == 72
        assert service.TIMEOUT_RANGE == (24, 168) and service.CHANNELS == ("telegram", "max")
        assert service.PEP_ACTIONS == ("signed", "approved", "rejected", "cancelled")
        src = read("app/approvals/service.py")
        assert "hmac.new(key, _seal_payload(prev, e).encode(\"utf-8\"), hashlib.sha256)" in src
        assert "approval.notified_key = key" in src and "except (StaleDataError, IntegrityError):" in src
        from app.approvals.models import AdvanceApprovalEvent
        assert any(ix.unique for ix in AdvanceApprovalEvent.__table__.indexes)    # номер записи журнала уникален
        assert "from ..services import ao_numbers" in src
        assert "ao_numbers.reserve(db, body.company_id, body.report_date, request_id, body.start_after)" \
            in read("app/routers/receipts.py")

    def test_screen_module(self):
        js = read("app/static/js/approvals.js")
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", js, re.M)
        assert tops and all(t.startswith(("approvals", "APPROVALS", "viewApprovals")) for t in tops), tops
        for needle in ("export async function viewApprovals(container, opts = {})", "{ doc_hash: d.doc_hash, confirm: true }",
                       "export function approvalsSheetHtml(d)", "export function approvalsConfirmText(a, name, kind)"):
            assert needle in js, needle

    def test_e2e_scenario_and_seed(self):
        spec = read("tests/e2e/app.spec.js")
        assert "test('согласование авансового отчёта: сотрудник → руководитель → бухгалтер с простой подписью'" in spec
        assert "test.slow();" in spec and "await expect(done.locator('.ap-pep')).toHaveCount(3" in spec
        seed = read("qa/playwright_seed.py")
        assert "Company(name='E2E Согласование')" in seed and "'e2e_boss_appr'" in seed
        assert "f'99999999999903{number:02d}'" in seed                 # свои фискальные номера, не …0101


# ====================================================================== документы и база знаний
class TestDocsAndKb:
    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-42](#adr-42) |" in arch and "## ADR-42" in arch
        adr = arch.split("## ADR-42", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.70.112, 2026-11-06", "`app/approvals`", "`approval_settings`",
                       "`advance_approvals`", "`advance_approval_events`", "HMAC-SHA256", "`auto_approved`",
                       "`skipped`", "`_doc_requests_once`", "(второе — 409)", "[ADR-24](#adr-24)", "[ADR-09](#adr-09)",
                       "[ADR-14](#adr-14)", "№ 3210-У", "**требует уточнения**", "T-46"):
            assert needle in adr, needle
        adr24 = arch.split("## ADR-24", 1)[1].split("## ADR-25", 1)[0]
        assert "Реализация — [ADR-42](#adr-42), выпуск 1.70.112." in adr24
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.16. Согласование авансовых отчётов (с v1.70.112)" in guide and "### 7.14." not in guide
        assert "[docs/pep_regulation_template.md](pep_regulation_template.md)" in guide

    def test_pep_regulation_template(self):
        tpl = read("docs/pep_regulation_template.md")
        for needle in ("# Положение о простой электронной подписи при согласовании авансовых отчётов (шаблон)",
                       "часть 2 статьи 9 63-ФЗ", "часть 2 статьи 6 63-ФЗ", "№ 3210-У", "статья 29\n402-ФЗ",
                       "**Требует уточнения:**", "Приложение 1. Лист ознакомления", "HMAC-SHA256",
                       "https://www.consultant.ru/document/cons_doc_LAW_112701/"):
            assert needle in tpl, needle

    def test_database_doc(self):
        db = read("docs/DATABASE.md")
        assert "68 объявлений `__tablename__`" in db and "все 68 таблиц приложения" in db
        assert "| Согласование авансовых отчётов (v1.70.112) | `approval_settings`" in db
        assert "с 1.70.112 — `approval_settings`, `advance_approvals` и `advance_approval_events`" in db

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t46 = tasks["T-46"]
        assert t46["Статус"] == "done" and t46["Задача"].endswith(f"выпущено в {VERSION}")
        assert t46["Issue/PR"] == ISSUE and t46["Связанные ADR"] == "ADR-24, ADR-42" and t46["Обновлено"] == "2026-11-06"
        assert t46["Источник"].endswith("docs/kb/10_research.md")
        t01 = tasks["T-01"]["Блокер"]
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.111 без замороженного «Моего дела»" in t01
        assert "с каждой из 1.70.82–1.70.111 — 48 из 48 проверок" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 06.11.2026 (выпуск v{VERSION}):** [#31]({ISSUE}) T-46" in roadmap
        assert f"T-01 — установить на рабочий сервер v{VERSION} (включает 1.70.82–1.70.111" in roadmap
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert "- Дальше по порядку: [#47](https://github.com/Rimlin-UNC/1c_chek/issues/47) T-62" in agent
        assert "T-46" not in agent and "T-78" not in agent
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
        assert ledger[-1]["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", ledger[-1]["Коммит"])
        assert ledger[-2]["Версия"] == "1.70.111" and ledger[-2]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        assert f"| Последний выпуск | **v{VERSION}** — согласование авансовых отчётов (T-46; ADR-24, ADR-42)" in kb
        assert "#3 — v1.70.82–v1.70.112 |" in kb
