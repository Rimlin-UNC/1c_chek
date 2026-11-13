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
    """Выпуск 1.70.112 — исторические проверки (точные пины — в tests/test_v170113.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 5
        assert read("app/static/index.html").count(f"?v={current}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={current}") == 2

    def test_updater_order(self):
        from app.services import updater
        for older in ("1.70.111", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-06"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109–1.70.111 не меняются — шаг pip пропускается",
                       "откатится само", "добавляются 3 новые таблицы (approval_settings, advance_approvals, "
                       "advance_approval_events); старые таблицы не меняются", "прежняя версия новых таблиц просто не видит",
                       "48 из 48", "1.70.82–1.70.111", "T-46, #31, ADR-24, ADR-42", "Выключено по умолчанию",
                       "«Подписать и отправить»", "72 часа", "Telegram и MAX", "SHA-256", "Лист согласования",
                       "docs/pep_regulation_template.md", "SQLite и PostgreSQL", "второе — 409", "T-62"):
            assert needle in entry, needle

    def test_whats_new_kept(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Авансовые отчёты — с подписью", "«Согласования»", "Этап без ответа 72 часа — согласовано",
                       "Включает администратор", "Каждое решение — в журнале", "Telegram и MAX"):
            assert needle in block, needle
        assert "'1.70.111': [" in js                         # история выпусков на месте

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — Новый раздел «Согласования»: авансовый отчёт с простой подписью"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«Чеки и учёт» → «Согласования»", "«Ждут меня · Мои отчёты · Все»", "«＋ Авансовый отчёт»",
                       "«🔒 Содержимое\n  и журнал целы»", "«Подписать и отправить»", "«🖨 Лист согласования»",
                       "ADR-24 и ADR-42", "две\n  карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Печать и выгрузка в 1С", "Роли и доступ", "Согласование авансовых отчётов"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)

    def test_manual(self):
        from app.services.manual_content import SECTIONS, build_manual
        manual = build_manual()
        assert manual["updated"] >= "2026-11-06"
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
        assert declared >= 68                                    # v1.70.113: +bank_incomes, bank_links

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
        m, n = re.search(r"(\d+) объявлений `__tablename__`", db), re.search(r"все (\d+) таблиц(?:ы)? приложения", db)
        assert m and n and int(m.group(1)) >= 68 and int(n.group(1)) >= 68           # v1.70.113: таблиц больше
        assert "| Согласование авансовых отчётов (v1.70.112) | `approval_settings`" in db
        assert "с 1.70.112 — `approval_settings`, `advance_approvals` и `advance_approval_events`" in db

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t46 = tasks["T-46"]
        assert t46["Статус"] == "done" and t46["Задача"].endswith(f"выпущено в {VERSION}")
        assert t46["Issue/PR"] == ISSUE and t46["Связанные ADR"] == "ADR-24, ADR-42" and t46["Обновлено"] == "2026-11-06"
        assert t46["Источник"].endswith("docs/kb/10_research.md")
        t01 = tasks["T-01"]["Блокер"]
        m = re.search(r"Ставить сразу v([\d.]+)", t01)                      # v1.70.113: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert "без замороженного «Моего дела»" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 06.11.2026 (выпуск v{VERSION}):** [#31]({ISSUE}) T-46" in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.113: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        # v1.70.114: строка «дальше» двигается выпусками — проверяем формат и что это не закрытые задачи
        assert re.search(r"- Дальше по порядку: \[#\d+\]\(https://github\.com/Rimlin-UNC/1c_chek/issues/\d+\) T-\d+", agent)
        assert "T-46" not in agent and "T-78" not in agent
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", row["Коммит"])
        assert ledger["1.70.111"]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        m = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb)          # v1.70.113: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
