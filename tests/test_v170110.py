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
        for older in ("1.70.109", "1.70.106"):           # на рабочем сервере — 1.70.106 (владелец, 06.11.2026)
            assert updater._is_newer(VERSION, older) is True
            assert updater._is_newer(older, VERSION) is False

    def test_changelog_first_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-06"
        entry = md.split("\n## [1.70.109]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "поверх 1.70.109 не меняются — шаг pip пропускается", "откатится само",
                       "Схема базы не меняется", "48 из 48", "1.70.82–1.70.109", "ADR-40", "T-41, #26",
                       "в Челябинске 15:56 по чеку сохранялось как 10:56", "«15:56+05:00» — это 15:56",
                       "следующим выпуском (T-78)", "стёртые «Сотрудник» и «Комментарий» действительно стираются",
                       "пункта «без компании»", "«Восстановить доступ» больше не шлёт причину", "acorn",
                       "376 вызовов", "SQLite и PostgreSQL", "правило очистки 1.70.89", "поясе UTC+5", "T-46"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Время чека больше не сдвигается", "в Челябинске на 5 часов", "поправим по QR",
                       "Формы проверяются против сервера", "стёртый в карточке комментарий теперь стирается"):
            assert needle in block, needle
        assert "'1.70.109': [" in js                         # история выпусков на месте

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — Формы чека: время при правке не сдвигается, стёртое поле стирается")
        entry = journal.split("\n## v1.70.109", 1)[0]
        for needle in ("«Дата и время» показывает время кассы", "«💾 Сохранить»", "«— без компании —»",
                       "«Восстановить доступ»", "tests/test_ui_api_contract.py", "ADR-40", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        changed = ["Единый реестр чеков", "Роли и доступ"]
        assert sorted(n for n, v in reg.items() if v == VERSION) == sorted(changed)
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"           # закреплено tests/test_v1660.py
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 110) for v in reg.values())
        before = {**reg, "Единый реестр чеков": "1.70.91", "Роли и доступ": "1.70.100"}
        assert updater.diff_blocks(before, reg) == [
            {"name": "Роли и доступ", "from": "1.70.100", "to": VERSION},
            {"name": "Единый реестр чеков", "from": "1.70.91", "to": VERSION}]

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (06.11.2026)" in readme
        assert "Последний выпуск — v1.70.109" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.109.**" in readme
        section = readme.split(f"## 🆕 Последний выпуск — v{VERSION}", 1)[1].split("\n## ", 1)[0]
        assert "Схема базы не меняется" in section and "**Время чека больше не сдвигается:**" in section
        assert "**Формы проверяются против сервера:**" in section and "(T-78)" in section

    def test_manual(self):
        from app.services.manual_content import build_manual
        manual = build_manual()
        assert manual["updated"] == "2026-11-06"
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
        assert t78["Статус"] == "todo" and t78["Приоритет"] == "P0" and t78["Issue/PR"] == T78
        assert t78["Связанные ADR"] == "ADR-14, ADR-40" and "кнопкой администратора" in t78["Задача"]
        t01 = tasks["T-01"]["Блокер"]
        assert f"Ставить сразу v{VERSION} — включает 1.70.82–1.70.109 без замороженного «Моего дела»" in t01
        assert "с каждой из 1.70.82–1.70.109 — 48 из 48 проверок" in t01 and "(исправление уже сдвинутых — T-78)" in t01
        roadmap = read("docs/kb/02_roadmap.md")
        assert f"**Готово 06.11.2026 (выпуск v{VERSION}):** [#26]({ISSUE}) T-41" in roadmap
        assert f"T-01 — установить на рабочий сервер v{VERSION} (включает 1.70.82–1.70.109" in roadmap
        agent = roadmap.split("**Можно брать агенту без подтверждения**", 1)[1].split("\n\n", 1)[0]
        assert f"[#62]({T78}) T-78" in agent and "[#31](https://github.com/Rimlin-UNC/1c_chek/issues/31) T-46" in agent
        assert "T-41" not in agent
        assert "выпущено в 1.70.110 (ADR-40)" in read("docs/kb/10_research.md")
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION and ledger[-1]["Опора репетиции"] == "да"
        assert all("," not in v for v in ledger[-1].values()), "в полях реестра нет запятых"
        assert ledger[-1]["Коммит"] == "после публикации" or re.fullmatch(r"[0-9a-f]{40}", ledger[-1]["Коммит"])
        assert ledger[-2]["Версия"] == "1.70.109" and ledger[-2]["Откат на неё"] == "можно"
        kb = read("docs/kb/README.md")
        assert f"| Последний выпуск | **v{VERSION}** — контракт «интерфейс ↔ API» (T-41; ADR-40)" in kb
        assert "#3 — v1.70.82–v1.70.110 |" in kb and "issues #4–#62" in kb
