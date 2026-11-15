"""v1.70.110: контракт «интерфейс ↔ API» (T-41, #26, ADR-40) и исправления, которые он нашёл.

Сам контракт — tests/test_ui_api_contract.py (Python, SQLite и PostgreSQL) и
tests/js/edit_form_contract.test.js (форма правки в поясе UTC+5). Здесь — точки выпуска и
исправлений: время чека в форме правки и на сервере, стёртые поля карточки, окно пользователя,
кабинет B2B, изоляция теста ряда динамики, документы и база знаний. Только синтетика (ADR-14).
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.110"
ISSUE = "https://github.com/Rimlin-UNC/1c_chek/issues/26"
T78 = "https://github.com/Rimlin-UNC/1c_chek/issues/62"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ====================================================================== выпуск
class TestRelease:
    """Выпуск 1.70.110 — исторические проверки (точные пины — в tests/test_v170111.py)."""

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
        for older in ("1.70.109", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-06"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109 не меняются — шаг pip пропускается", "откатится само",
                       "Схема базы не меняется", "48 из 48", "1.70.82–1.70.109", "ADR-40", "T-41, #26",
                       "в Челябинске 15:56 по чеку сохранялось как 10:56", "«15:56+05:00» — это 15:56",
                       "следующим выпуском (T-78)", "стёртые «Сотрудник» и «Комментарий» действительно стираются",
                       "пункта «без компании»", "«Восстановить доступ» больше не шлёт причину", "acorn",
                       "376 вызовов", "SQLite и PostgreSQL", "правило очистки 1.70.89", "поясе UTC+5", "T-46"):
            assert needle in entry, needle

    def test_whats_new_kept(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Время чека больше не сдвигается", "в Челябинске на 5 часов", "поправим по QR",
                       "Формы проверяются против сервера", "стёртый в карточке комментарий теперь стирается"):
            assert needle in block, needle
        assert "'1.70.109': [" in js                         # история выпусков на месте

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — Формы чека: время при правке не сдвигается, стёртое поле стирается"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«Дата и время» показывает время кассы", "«💾 Сохранить»", "«— без компании —»",
                       "«Восстановить доступ»", "tests/test_ui_api_contract.py", "ADR-40", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        for name in ("Единый реестр чеков", "Роли и доступ"):
            assert self._ver(reg[name]) >= self._ver(VERSION)
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)

    def test_manual(self):
        from app.services.manual_content import build_manual
        manual = build_manual()
        assert manual["updated"] >= "2026-11-06"
        html = "".join(s.get("html", "") for s in manual["sections"])
        assert "<b>С версии 1.70.110 — время чека при правке.</b>" in html
        assert "Стёртые в карточке «Сотрудник» и «Комментарий» теперь действительно стираются." in html


# ====================================================================== исправления
class TestFixes:
    def test_edit_form_sends_wall_time_only_when_changed(self):
        js = read("app/static/js/app.js")
        edit = js.split("async function openEditReceipt(r, onSaved) {", 1)[1].split("\nfunction openNotifyDialog", 1)[0]
        assert "const dateShown = _dtLocal(r.receipt_date);" in edit
        assert 'value="${dateShown}"' in edit
        assert "if (dateEl.value && dateEl.value !== dateShown) body.receipt_date = dateEl.value;" in edit
        assert "toISOString()" not in edit                  # пересчёт в UTC сдвигал время чека

    def test_server_keeps_wall_time(self):
        from app.schemas import ReceiptPatch
        msk = timezone(timedelta(hours=3))
        assert ReceiptPatch(receipt_date="2026-09-22T15:56:00+05:00").receipt_date == datetime(2026, 9, 22, 15, 56)
        assert ReceiptPatch(receipt_date=datetime(2026, 9, 22, 12, 56, tzinfo=msk)).receipt_date == \
            datetime(2026, 9, 22, 12, 56)
        assert ReceiptPatch(receipt_date="2026-09-22T16:10").receipt_date == datetime(2026, 9, 22, 16, 10)
        assert ReceiptPatch().receipt_date is None

    def test_drawer_clears_and_user_dialog_and_b2b(self):
        js = read("app/static/js/app.js")
        drawer = js.split("async function receiptDrawer(id) {", 1)[1].split("\n }", 1)[0]
        assert "assignee: drawer.querySelector('#d-assignee').value.trim(),\n" in drawer
        assert "comment: drawer.querySelector('#d-comment').value.trim(),\n" in drawer
        assert ".value.trim() || null" not in drawer
        assert """<select id="u-company">${u && u.company_id ? '' : '<option value="">— без компании —</option>'}""" in js
        assert "/block`, { reason })" in js and "/unblock`, {});" in js
        assert "const path = blocked ? 'block' : 'unblock';" not in js

    def test_dynamics_test_is_isolated(self):
        src = read("tests/test_v17045.py")
        seed = src.split("    def seed(self):", 1)[1].split("    def test_route_is_documented_everywhere", 1)[0]
        assert '{"status": "dyn_hidden"}' in seed and '{"status": "verified"}' in seed


# ====================================================================== контракт
class TestContract:
    def test_artifacts(self):
        mjs = read("tests/ui_api_calls.mjs")
        assert "import { parse } from 'acorn';" in mjs
        for needle in ("join(ROOT, 'app', 'static', 'maxapp', 'maxapp.js')", "join(ROOT, 'app', 'static', 'b2b', 'b2b.js')",
                       "function nullWhenEmpty(value)", "conditions[key] = guards"):
            assert needle in mjs, needle
        pkg = json.loads(read("package.json"))
        assert pkg["devDependencies"]["acorn"] == "^8.19.0"
        lock = json.loads(read("package-lock.json"))
        assert lock["packages"][""]["devDependencies"]["acorn"] == "^8.19.0"
        contract = read("tests/test_ui_api_contract.py")
        for name in ("test_every_ui_call_hits_a_route", "test_every_ui_body_field_is_accepted_by_the_server",
                     "test_patch_never_sends_a_cleared_field_as_null", "test_receipt_patch_handles_every_model_field",
                     "test_edit_form_save_loses_nothing", "test_manual_entry_form",
                     "test_self_check_catches_the_1_70_89_rule", "test_route_list_matches_openapi"):
            assert f"def {name}(" in contract, name
        assert '"tests/test_ui_api_contract.py"' in read("qa/pg_local.py")
        vitest = read("tests/js/edit_form_contract.test.js")
        assert "process.env.TZ = 'Asia/Yekaterinburg';" in vitest and "fnSource('openEditReceipt')" in vitest


# ====================================================================== документы и база знаний
class TestDocsAndKb:
    def test_adr_and_guide(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-40](#adr-40) |" in arch and "## ADR-40" in arch
        adr = arch.split("## ADR-40", 1)[1].split("## Как добавлять решение", 1)[0]
        for needle in ("**с версии / даты:** 1.70.110, 2026-11-06", "`tests/ui_api_calls.mjs`", "`ReceiptPatch`",
                       "`fns.get_ticket`", "урок 1.70.89/1.70.90", "[ADR-14](#adr-14)", "T-41, T-78",
                       "[#26](https://github.com/Rimlin-UNC/1c_chek/issues/26)"):
            assert needle in adr, needle
        guide = read("docs/USER_GUIDE.md")
        assert "- С 1.70.110 «Дата и время» в форме правки («✏️») — местное время кассы, как в QR:" in guide
        assert "### 7.14." not in guide                       # 7.14 убран в 1.70.108

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        t41, t78 = tasks["T-41"], tasks["T-78"]
        assert t41["Статус"] == "done" and t41["Задача"].endswith(f"выпущено в {VERSION}")
        assert t41["Issue/PR"] == ISSUE and t41["Связанные ADR"] == "ADR-40" and t41["Обновлено"] == "2026-11-06"
        assert t78["Статус"] in ("todo", "done") and t78["Приоритет"] == "P0" and t78["Issue/PR"] == T78
        assert t78["Связанные ADR"].startswith("ADR-14, ADR-40") and "администратора" in t78["Задача"]
        t01 = tasks["T-01"]["Блокер"]
        m = re.search(r"Ставить сразу v([\d.]+)", t01)                      # v1.70.111: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        assert "без замороженного «Моего дела»" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 06.11.2026 (выпуск v{VERSION}):** [#26]({ISSUE}) T-41" in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.111: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert "T-41" not in agent
        assert "выпущено в 1.70.110 (ADR-40)" in read("docs/kb/10_research.md")
        ledger = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
        row = ledger[VERSION]
        assert row["Опора репетиции"] == "да" and all("," not in v for v in row.values())
        assert row["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", row["Коммит"])
        assert ledger["1.70.109"]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        m = re.search(r"\| Последний выпуск \| \*\*v([\d.]+)\*\*", kb)          # v1.70.111: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        m = re.search(r"issues #4–#(\d+)", kb)                                     # v1.70.113: задач больше
        assert m and int(m.group(1)) >= 62
