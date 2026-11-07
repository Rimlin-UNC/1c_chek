"""v1.70.102: несколько банков по API у компании — Т-Банк, Сбербанк, Альфа-Банк, Модульбанк рядом с Точкой (T-73, ADR-32).

Поведение проверяют tests/test_bank_multi.py (ключи, сертификат клиента, продление токенов Сбера, курсоры и страницы,
сверка ИНН, ошибки банков, права, дубли с файлом, сводка аутсорсера) и tests/js/bank_multi.test.js (поля под банк,
список банков компании, переключение, тело запроса). Сценарии — в DOM-стенде tests/ui_harness.js и в Playwright
(tests/e2e/app.spec.js). Здесь — синхронизация выпуска, схема, проводка, документация и база знаний.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.70.102"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRelease:
    """Выпуск 1.70.102 — исторические проверки (точные пины — в tests/test_v170103.py)."""

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

    def test_updater_order(self):
        from app.services import updater
        assert updater._is_newer(VERSION, "1.70.101") is True
        assert updater._is_newer("1.70.101", VERSION) is False

    def test_changelog_entry_fits_update_window(self):
        md = read("CHANGELOG.md")
        head = f"## [{VERSION}] - 2026-11-05"
        assert head in md
        entry = head + md.split(head, 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Несколько банков по API", "резервная копия", "поверх 1.70.95–1.70.101 не меняются — шаг pip "
                       "пропускается", "откатится само", "1 новая колонка cert_enc в bank_connections", "48 из 48",
                       "1.70.82–1.70.101", "Т-Банк и Модульбанк", "refresh_token", ".p12/.pfx", "сверяет ИНН",
                       "не задваивается", "удаляет ключи только этого банка", "ВТБ, Газпромбанк, Райффайзенбанк",
                       "худшее состояние её банков", "Проверено на синтетике", "T-74", "T-63", "T-64", "T-71"):
            assert needle in entry, needle

    def test_whats_new_has_entry(self):
        js = read("app/static/js/app.js")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Все банки клиента в карточке", "Т-Банк, Сбербанк, Альфа-Банк и Модульбанк",
                       "Ключи — только на чтение и зашифрованно", "сертификат клиента (PEM или .p12)"):
            assert needle in block, needle

    def test_journal_entry(self):
        journal = read("docs/ui_journal.md")
        head = f"## v{VERSION} — несколько банков по API: Т-Банк, Сбербанк, Альфа-Банк и Модульбанк в карточке компании"
        assert head in journal
        entry = journal.split(head, 1)[1].split("\n## v", 1)[0]
        for needle in ("«Банки компании»", "«открыт ниже»", "«Открыть»", "«Где взять ключи:", "«Ключ API»",
                       "«refresh_token»", "«Пароль ключа (если есть)»", "«Проверить ключ и подключить»",
                       "«Отключить банк «Т-Банк»?»", "«Другие банки — выписка файлом»", "«Т-Банк, API, сама»",
                       "USER_GUIDE §7.10", "две карточки"):
            assert needle in entry, needle

    def test_changed_blocks_not_older_than_release(self):
        from app.config import settings
        from app.services import updater
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        new = "Банки по API (Т-Банк, Сбербанк, Альфа-Банк, Модульбанк)"
        for name in ("Банк по API (Точка)", new, "Рабочее место аутсорсера"):
            assert self._ver(reg[name]) >= self._ver(VERSION), name
        assert all(self._ver(v) <= self._ver(settings.APP_VERSION) for v in reg.values())
        before = {name: ver for name, ver in reg.items() if name != new}
        assert {"name": new, "from": "", "to": VERSION} in updater.diff_blocks(before, reg)

    def test_readme_latest_release_not_older(self):
        m = re.search(r"## 🆕 Последний выпуск — v([\d.]+) ", read("README.md"))
        assert m and self._ver(m.group(1)) >= self._ver(VERSION)


class TestSchema:
    def test_cert_column_without_new_tables(self):
        declared = sum(len(re.findall(r"^\s*__tablename__\s*=", p.read_text(encoding="utf-8"), re.M))
                       for p in (ROOT / "app").rglob("*.py"))
        assert declared >= 62                                   # в 1.70.102 новых таблиц нет; v1.70.104 — прибавились
        from app.bank.models import BankConnection
        col = BankConnection.__table__.columns["cert_enc"]
        assert col.server_default is not None and not BankConnection.__table__.foreign_keys   # ADR-09
        database = read("app/database.py")
        assert "ALTER TABLE bank_connections ADD COLUMN cert_enc TEXT DEFAULT ''" in database
        rehearsal = read("qa/upgrade_rehearsal.py")
        assert '"bank_columns": sorted(c["name"] for c in inspect(engine).get_columns("bank_connections")' in rehearsal
        assert 'and up.get("bank_columns") == ["cert_enc"]' in rehearsal
        db = read("docs/DATABASE.md")
        assert "| Несколько банков по API (v1.70.102) |" in db and "`bank_connections.cert_enc`" in db
        m = re.search(r"(\d+) объявлени[яй] `__tablename__`", db)           # v1.70.104: число — не меньше
        assert m and int(m.group(1)) >= 62


class TestWiring:
    def test_routes_registry_and_constants(self):
        from app.main import app
        paths = list(app.openapi()["paths"])
        for path in ("/api/v1/bank/connection", "/api/v1/bank/connection/check", "/api/v1/bank/connection/jwt",
                     "/api/v1/bank/connection/sync", "/api/v1/bank/connection/authorize"):
            assert path in paths, path
        from app.bank import connect, providers
        assert list(connect.BANKS) == ["tochka", "tbank", "sber", "alfa", "modul"]
        assert connect.METHODS == {"oauth": "OAuth 2.0", "jwt": "JWT-ключ", "token": "Ключ API",
                                   "refresh": "OAuth 2.0 — токены из кабинета"}
        assert {k: v["methods"] for k, v in providers.PROVIDERS.items()} == {
            "tochka": ["oauth", "jwt"], "tbank": ["token"], "sber": ["refresh"], "alfa": ["token"], "modul": ["token"]}
        assert providers.DAILY_MAX_DAYS == 45 and len(providers.FILE_BANKS) == 8
        router = read("app/bank/router.py")
        assert 'BankId = Literal["tochka", "tbank", "sber", "alfa", "modul"]' in router
        src = read("app/bank/connect.py")
        assert 'out.append({"company_id": company.id, "bank": conn.bank, **_sync(db, conn, company, None)})' in src
        assert "if conn.bank != \"tochka\":\n        return _sync_other(db, conn, company, user)" in src
        assert "class TochkaError(BankApiError):" in read("app/bank/tochka.py")
        http = read("app/bank/http.py")
        assert 'tempfile.mkstemp(prefix="ymbank-", suffix=".pem")' in http and "os.unlink(path)" in http
        clients = read("app/clients/service.py")
        assert 'rank = {"reauth": 4, "error": 3, "awaiting": 2, "new": 1, "active": 0}' in clients
        assert 'info["last_error"] = f"{title}: {conn.last_error}"[:200]' in clients


class TestScreen:
    def test_bank_module_and_css(self):
        screen = read("app/static/js/bank.js")
        for needle in ("export function bankMeta(d, id)", "export function bankListHtml(list, current)",
                       "export function bankFileBanksHtml(list)", "export function bankReadFile(file)",
                       "'/api/v1/bank/connection/check'", "tbank: 'Т-Банк, API'", 'id="bankc-token-box"',
                       'id="bankc-refresh-box"', 'id="bankc-cert-box"', "Другие банки — выписка файлом",
                       "Где взять ключи: "):
            assert needle in screen, needle
        tops = re.findall(r"^(?:export )?(?:async )?(?:function|const|let) (\w+)", screen, re.M)
        assert all(t.startswith(("bank", "BANK", "viewBank")) for t in tops), tops   # общая область сборки
        css = read("app/static/css/app.css")
        for cls in (".bank-conn-list {", ".bank-conn-row {", ".bank-conn-row.is-current {", ".bank-conn-files {",
                    ".bank-conn-cert textarea {"):
            assert cls in css, cls
        spec = read("tests/e2e/app.spec.js")
        assert "test('банки по API: карточка компании — Т-Банк по ключу API, список банков, ключ не возвращается'" in spec
        assert (ROOT / "tests/js/bank_multi.test.js").exists()


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_banks(self):
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_banks102.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("панель «Банк по API» — список банков компании (Точка и Т-Банк)",
                     "Т-Банк открывается кнопкой; банки без API — выписка файлом"):
            assert f"PASS v1.70.102: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestDocsAndKb:
    def test_adr_guide_manual(self):
        arch = read("docs/ARCHITECTURE.md")
        assert "| [ADR-32](#adr-32) |" in arch and "## ADR-32" in arch and "`bank_connections.cert_enc`" in arch
        assert "**с версии / даты:** 1.70.102, 2026-11-05" in arch
        guide = read("docs/USER_GUIDE.md")
        assert "### 7.10. Несколько банков по API: Т-Банк, Сбербанк, Альфа-Банк, Модульбанк (с v1.70.102)" in guide
        design = read("docs/bank_api.md")
        for needle in ("ADR-32", "alfacomdevelopment/alfa-api-sdk", "developers.sber.ru", "developer.tbank.ru",
                       "api.modulbank.ru", "**требует уточнения**", "только на синтетике"):
            assert needle in design, needle
        from app.services.manual_content import SECTIONS, build_manual
        bank = next(s for s in SECTIONS if s["id"] == "bank")
        assert "С версии 1.70.102 — несколько банков у компании." in bank["html"] and "(пока Точка)" not in bank["html"]
        assert len(SECTIONS) >= 33 and build_manual()["updated"] >= "2026-11-05"   # v1.70.108: не раньше; v1.70.112: +1 раздел

    def test_kb(self):
        tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
        row = tasks["T-73"]
        assert row["Статус"] == "done" and row["Задача"].endswith(f"выпущено в {VERSION}")
        assert row["Issue/PR"] == "https://github.com/Rimlin-UNC/1c_chek/issues/57" and "ADR-32" in row["Связанные ADR"]
        assert row["Источник"].endswith("docs/bank_api.md")
        assert tasks["T-74"]["Статус"] in ("todo", "done") and tasks["T-74"]["Issue/PR"].endswith("/issues/58")
        for tid, issue in (("T-63", 48), ("T-64", 49), ("T-71", 56)):
            assert tasks[tid]["Статус"] in ("todo", "done", "blocked") and tasks[tid]["Issue/PR"].endswith(f"/issues/{issue}"), tid
        m = re.search(r"Ставить сразу v([\d.]+)", tasks["T-01"]["Блокер"])      # v1.70.103: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        for tid in ("T-41", "T-42", "T-43", "T-44", "T-45", "T-46", "T-47", "T-48", "T-49"):
            assert tasks[tid]["Источник"].endswith("docs/kb/10_research.md"), tid
        roadmap = read("docs/kb/02_roadmap.md")
        assert "**Готово 05.11.2026 (выпуск v1.70.102):** [#57](https://github.com/Rimlin-UNC/1c_chek/issues/57) T-73" \
            in roadmap
        m = re.search(r"T-01 — установить на рабочий сервер v([\d.]+) ", roadmap)  # v1.70.103: не старше
        assert m and TestRelease._ver(m.group(1)) >= TestRelease._ver(VERSION)
        ledger = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        versions = [r["Версия"] for r in ledger]
        row = ledger[versions.index(VERSION)]
        assert row["Опора репетиции"] == "да"
        assert ledger[versions.index("1.70.101")]["Откат на неё"] == "можно"
        assert all("," not in v for v in row.values()), "в полях реестра нет запятых"
