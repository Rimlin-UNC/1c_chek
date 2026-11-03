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
        assert md.split("\n", 1)[0] == f"## [{VERSION}] - 2026-11-03"
        entry = md.split("\n## [1.70.91]", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("резервная копия", "pip пропускается", "Схема базы не меняется", "48 из 48",
                       "(T-57)", "«Токены компаний»", "только её чеки", "только его хеш", "«Отозвать»",
                       "Общий токен по-прежнему видит все компании", "docs/INTEGRATION_1C.md",
                       "с 1.16.0", "с зашифрованной строкой", "Первые автотесты интеграции 1С"):
            assert needle in entry, needle

    def test_whats_new_first(self):
        js = read("app/static/js/app.js")
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == VERSION
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 1
        for needle in ("«Токены компаний»", "только свои чеки", "показывается один раз", "работает как раньше"):
            assert needle in block, needle

    def test_journal_entry_first(self):
        journal = read("docs/ui_journal.md")
        assert journal.startswith(f"## v{VERSION} — «Выгрузка в 1С»: токены компаний")
        entry = journal.split("\n## v1.70.91", 1)[0]
        for needle in ("«Выпустить»", "«Отозвать»", "показывается один раз", "одна карточка"):
            assert needle in entry, needle

    def test_changed_blocks(self):
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert sorted(n for n, v in reg.items() if v == VERSION) == ["Печать и выгрузка в 1С"]
        assert all(tuple(map(int, v.split("."))) <= (1, 70, 92) for v in reg.values())

    def test_readme_latest_release(self):
        readme = read("README.md")
        assert f"## 🆕 Последний выпуск — v{VERSION} (03.11.2026)" in readme
        assert "Последний выпуск — v1.70.91" not in readme
        assert f"**Ставьте {VERSION} вместо\n1.70.91.**" in readme


class TestSchema:
    def test_no_new_tables_or_columns(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared == 50


class TestCompanyTokens:
    def test_endpoints_take_scope_from_token(self):
        from app.routers import onec
        src = inspect.getsource(onec)
        assert src.count("scope = _require_token(x_api_token, db)") == 4      # ping, pull, ack, enterprise-data
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
        assert f"Ставить сразу v{VERSION}" in tasks["T-01"]["Блокер"]
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        assert ledger[-1]["Версия"] == VERSION
