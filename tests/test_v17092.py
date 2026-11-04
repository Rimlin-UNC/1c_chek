"""v1.70.92: токен 1С на компанию (T-57); исправлено — токен из «Настроек» не принимался.

Поведение проверяет tests/test_onec_company_tokens.py — первые автотесты интеграции
1С по HTTP: изоляция компаний (pull, EnterpriseData, ack, ping), прежнее поведение
общего токена, хранение хешем, перевыпуск и отзыв, только администратор, токен из
«Настроек» (зашифрован) принимается. Здесь — синхронизация выпуска, проводка,
документация и база знаний. Схема базы не меняется.
"""
from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.92"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def flat(rel: str) -> str:
    return " ".join(read(rel).split())


class TestRelease:
    """Выпуск 1.70.92 — исторические проверки (точные пины — в tests/test_v17093.py)."""

    @staticmethod
    def _ver(value: str) -> tuple[int, ...]:
        return tuple(int(x) for x in value.split("."))

    def test_sync_points(self):
        from app.config import settings
        current = settings.APP_VERSION
        assert self._ver(current) >= self._ver(VERSION)
        sw = read("app/static/sw.js")
        assert f"ymaster-check-v{current}" in sw and sw.count(f"?v={current}") == 2

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-03"
        assert head in md
        entry = md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(head + entry) <= 4000, len(head + entry)
        for needle in ("(T-57)", "«Токены компаний»", "только его хеш", "с 1.16.0"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1 and "«Токены компаний»" in block

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        entry = journal.split(f"## v{VERSION} — ", 1)[1].split("\n## v", 1)[0]
        assert "«Выпустить»" in entry and "«Отозвать»" in entry

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert self._ver(reg["Печать и выгрузка в 1С"]) >= self._ver(VERSION)
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 50


class TestCompanyTokens:
    def test_endpoints_take_scope_from_token(self):
        from app.routers import onec
        src = inspect.getsource(onec)
        # каждый маршрут 1С берёт область из токена: ping, чеки (pull, ack, enterprise-data), с 1.70.96 — первичка
        routes = len(re.findall(r"^@router\.(?:get|post)\(", src, re.M))
        assert routes >= 4 and src.count("scope = _require_token(x_api_token, db)") == routes
        assert "Токен компании не даёт доступа к чекам другой компании" in src
        assert "if r and scope and r.company_id != scope:" in src
        assert "hmac" not in src                                             # сравнение — в onec_tokens

    def test_tokens_hashed_and_global_unsealed(self):
        from app.services import onec_tokens
        assert onec_tokens.META == "onec_token:" and onec_tokens.INDEX == "onec_token_hash:"
        assert "hashlib.sha256" in inspect.getsource(onec_tokens._digest)
        assert "get_setting(db, \"onec_api_token\"" in inspect.getsource(onec_tokens.global_token)
        assert "hmac.compare_digest" in inspect.getsource(onec_tokens.resolve)

    def test_admin_endpoints_and_ui(self):
        from app.routers import settings_routes
        for fn in ("onec_company_tokens", "issue_onec_company_token", "revoke_onec_company_token"):
            assert "require_admin" in inspect.getsource(getattr(settings_routes, fn)), fn
        js = read("app/static/js/app.js")
        assert "'<div id=\"onec-company-tokens\" style=\"margin-top:12px\"></div>'" in js
        assert "`/api/v1/settings/onec/companies/${encodeURIComponent(b.dataset.id)}/token`" in js


class TestDocs:
    def test_integration_and_guide(self):
        assert "### 1.1. Токен компании (с 1.70.92) — рекомендуется" in read("docs/INTEGRATION_1C.md")
        assert "С 1.70.92 администратор выдаёт базе 1С каждой компании свой токен" in flat("docs/USER_GUIDE.md")

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        assert tasks["T-57"]["Статус"] == "done" and tasks["T-57"]["Задача"].endswith(f"выпущено в {VERSION}")
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.93: не старше
        assert m and tuple(map(int, m.group(1).split("."))) >= (1, 70, 92)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert any(r["Версия"] == VERSION for r in ledger)
