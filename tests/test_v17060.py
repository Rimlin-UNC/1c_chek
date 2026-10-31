"""v1.70.60 — QA-цепочка: ESLint + Vitest + Playwright + Newman.

Задача владельца (дословно, существенная часть): «Протестируй и Начни с
ESLint + Vitest/Jest + Playwright + Postman/Newman — этого достаточно,
чтобы покрыть типовые ошибки в коде, API и пользовательских сценариях.
Реализуй поэтапно, тем более многое уже сделано. Мне нужна здоровая и
качественная программа быстрая и надёжная.»

Этапы (многое действительно уже было: ESLint внедрён в v1.70.59):
 1. ESLint — flat-config в корне, ворота нуля ошибок на наших модулях.
 2. Vitest (jsdom) — юнит-тесты чистых функций: экранирование, форматы
    чисел и дат, подписи статусов и ролей, геометрия и раскрой
    термопечати, хранилище токена и форма ошибки API (27 проверок).
 3. Playwright — браузерные сценарии: вход, отказ входа, настройки с
    читаемыми оглавлениями и сворачиванием; сервер поднимает конфиг на
    отдельной базе. В среде без загрузки браузеров сценарии проверяются
    перечислением (`playwright test --list`).
 4. Newman (Postman-коллекция) — 10 типовых сценариев API, 21 утверждение:
    вход и токен, отказ входа, доступ с токеном и без, 404 JSON на
    неизвестном маршруте, SPA-фолбэк, манифест, service worker.

Находка по ходу прогона: Newman делит банку cookie между запросами, а
вход ставит сессионную cookie — поэтому сценарии «без токена» стоят в
коллекции ДО входа, иначе «без токена» незаметно становилось «с cookie».

Что здесь проверяется:
 1. Конфиги и скрипты цепочки на месте, коллекция валидна и полна.
 2. Ворота Vitest: если раннер установлен — юниты зелёные.
 3. Ворота Playwright: сценарии перечисляются; с браузерами — проходят.
 4. Ворота Newman: коллекция проходит против живого сервера.
 5. Ритуал выпуска 1.70.60 и историзация TestRelease17059.
"""

import json
import os
import pathlib
import re
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
PACKAGE_JSON = ROOT / "package.json"
VITEST_CONFIG = ROOT / "vitest.config.mjs"
PLAYWRIGHT_CONFIG = ROOT / "playwright.config.js"
ESLINT_CONFIG = ROOT / "eslint.config.mjs"
COLLECTION = ROOT / "qa" / "postman" / "ymaster_check.postman_collection.json"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"
RELEASE_59_TESTS = ROOT / "tests" / "test_v17059.py"

OWNER_QUOTE_A = ("начни с eslint + vitest/jest + playwright + "
                 "postman/newman")
OWNER_QUOTE_B = ("мне нужна здоровая и качественная программа быстрая "
                 "и надёжная")

VITEST_BIN = ROOT / "node_modules" / ".bin" / "vitest"
PLAYWRIGHT_BIN = ROOT / "node_modules" / ".bin" / "playwright"
NEWMAN_BIN = ROOT / "node_modules" / ".bin" / "newman"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _browsers_installed() -> bool:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    cand = pathlib.Path(base) if base else pathlib.Path.home() / ".cache" / \
        "ms-playwright"
    return cand.is_dir() and any(cand.iterdir())


class TestToolchain:
    """Четыре инструмента цепочки представлены в репозитории."""

    def test_package_scripts_cover_the_chain(self):
        pkg = json.loads(_read(PACKAGE_JSON))
        for script in ("lint", "test:js", "test:e2e", "test:api"):
            assert script in pkg["scripts"], script

    def test_configs_exist(self):
        for path in (VITEST_CONFIG, PLAYWRIGHT_CONFIG, ESLINT_CONFIG):
            assert path.is_file(), path

    def test_vitest_units_exist(self):
        tests = sorted((ROOT / "tests" / "js").glob("*.test.js"))
        assert len(tests) >= 3, tests
        names = " ".join(p.name for p in tests)
        for needle in ("ui", "printpack", "api"):
            assert needle in names, needle

    def test_playwright_specs_exist(self):
        specs = sorted((ROOT / "tests" / "e2e").glob("*.spec.js"))
        assert specs, "нет браузерных сценариев"

    def test_collection_is_valid_and_complete(self):
        data = json.loads(_read(COLLECTION))
        names = [i["name"] for i in data["item"]]
        assert len(names) == 10, names
        # сценарии без аутентификации идут ДО входа: cookie-сессия не
        # должна маскировать «без токена»
        assert names.index("компании без токена отклоняются") < \
            names.index("вход администратора даёт токен")
        assert names.index("неверный пароль отклоняется") < \
            names.index("вход администратора даёт токен")
        for item in data["item"]:
            assert item.get("event"), f"без утверждений: {item['name']}"


class TestVitestGate:
    """Юнит-тесты JS: если раннер установлен — всё зелёное."""

    def test_units_pass(self):
        if not VITEST_BIN.exists():
            import pytest
            pytest.skip("Vitest не установлен: npm install (не обязателен в CI)")
        proc = subprocess.run([str(VITEST_BIN), "run"], cwd=ROOT,
                              capture_output=True, text=True, timeout=600,
                              check=False)
        assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]


class TestPlaywrightGate:
    """Браузерные сценарии: перечисляются всегда, проходят с браузерами."""

    def test_specs_are_listed(self):
        if not PLAYWRIGHT_BIN.exists():
            import pytest
            pytest.skip("Playwright не установлен: npm install")
        proc = subprocess.run([str(PLAYWRIGHT_BIN), "test", "--list"],
                              cwd=ROOT, capture_output=True, text=True,
                              timeout=600, check=False)
        assert proc.returncode == 0, proc.stderr[-1500:]
        assert "Total: 4 tests" in proc.stdout, proc.stdout[-800:]

    def test_scenarios_pass_with_browsers(self):
        if not PLAYWRIGHT_BIN.exists():
            import pytest
            pytest.skip("Playwright не установлен: npm install")
        if not _browsers_installed():
            import pytest
            pytest.skip("браузеры Playwright не загружены: "
                        "npx playwright install chromium")
        proc = subprocess.run([str(PLAYWRIGHT_BIN), "test"], cwd=ROOT,
                              capture_output=True, text=True, timeout=900,
                              check=False)
        assert proc.returncode == 0, proc.stdout[-2000:]


class TestNewmanGate:
    """Коллекция API проходит против живого сервера."""

    def test_collection_passes(self):
        if not NEWMAN_BIN.exists():
            import pytest
            pytest.skip("Newman не установлен: npm install")
        db = "/tmp/newman_gate_v17060.db"
        for suffix in ("", "-wal", "-shm"):
            pathlib.Path(db + suffix).unlink(missing_ok=True)
        env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}",
                   YM_OSMON_OFF="1", RATE_LIMIT_PER_MINUTE="2000000",
                   RATE_LIMIT_PER_HOUR="2000000")
        server = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), "run.py",
             "--host", "127.0.0.1", "--port", "8002"],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 40
            import urllib.request
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:8002/", timeout=2) as r:
                        if r.status == 200:
                            break
                except OSError:
                    time.sleep(1)
            proc = subprocess.run(
                [str(NEWMAN_BIN), "run", str(COLLECTION),
                 "--env-var", "baseUrl=http://127.0.0.1:8002"],
                cwd=ROOT, capture_output=True, text=True, timeout=600,
                check=False)
            assert proc.returncode == 0, proc.stdout[-2500:]
            assert "21" in proc.stdout or "assertions" in proc.stdout
        finally:
            server.terminate()
            server.wait(timeout=20)
            for suffix in ("", "-wal", "-shm"):
                pathlib.Path(db + suffix).unlink(missing_ok=True)


class TestRelease17060:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.61).
    Здесь остаётся запись выпуска 1.70.60 в истории."""

    VERSION = "1.70.60"
    PREVIOUS = "1.70.59"

    def test_version_is_release(self):
        src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        # живое значение сверяет выпуск-владелец; здесь — форма релиза
        assert re.search(r"APP_VERSION: str = \"1\.70\.\d+\"", src)

    def test_changelog_has_release(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        assert f"## [{self.VERSION}]" in text

    def test_changelog_quotes_the_owner(self):
        section = CHANGELOG.read_text(encoding="utf-8").split(
            f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        assert "начни с eslint + vitest/jest + playwright" in flat

    def test_journal_has_release(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = JOURNAL.read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_whats_new_entry_exists_in_history(self):
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        assert f"'{self.VERSION}': [" in js

    def test_trackers_reference_a_release(self):
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert re.search(r'"1\.70\.\d+"', text)

    def test_sw_cache_name_is_versioned(self):
        sw = SW.read_text(encoding="utf-8")
        assert re.search(r"ymaster-check-v1\.70\.\d+", sw)

    def test_no_placeholder_left(self):
        for path in (CHANGELOG, JOURNAL):
            assert "TODO" not in path.read_text(encoding="utf-8")
