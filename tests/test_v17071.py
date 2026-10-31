# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — замок выпуска v1.70.71: проверка ФНС после сохранения
# настроек (расшифровка мастер-токена) и пошаговая диагностика связи.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
CONFIG = APP / "config.py"
INDEX = APP / "static" / "index.html"
SW = APP / "static" / "sw.js"
MANIFEST = APP / "static" / "manifest.webmanifest"
APP_JS = APP / "static" / "js" / "app.js"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"

OWNER_QUOTE = "но не работает"


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8")


class TestRelease17071:
    VERSION = "1.70.71"
    PREVIOUS = "1.70.70"

    def _current(self) -> str:
        return re.search(r'APP_VERSION: str = "([^"]+)"',
                         _read(CONFIG)).group(1)

    def test_all_sync_points(self):
        ver = self._current()
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 71)
        assert f"ymaster-check-v{ver}" in _read(SW)
        assert f'"version": "{ver}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={ver}" in index and f"app.js?v={ver}" in index

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self.VERSION, \
            "запись выпуска не первая"

    def test_whats_new_entry(self):
        block = _read(APP_JS).split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]
        assert block.count("{ icon:") == 3
        for needle in ("токен", "Диагностика", "мастер-токен"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for head in ("Подготовка", "Что оказалось не так", "Решение",
                     "Отклонено", "Проверено"):
            assert f"### {head}" in section
        flat = section.lower()
        for needle in ("расшифров", "enc:v1", "диагностик"):
            assert needle in flat, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Причина", "Решение", "Интерфейс", "Проверено"):
            assert needle in section

    def test_owner_report_is_quoted(self):
        for path in (CHANGELOG, JOURNAL):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            assert OWNER_QUOTE in flat, Path(path).name


class TestFixIsLocked:
    """Дефект: движок читал зашифрованный токен как есть."""

    def test_engine_uses_decrypting_reader(self):
        src = _read(APP / "services" / "external.py")
        block = src.split("def _settings")[1].split("def provider_chain")[0]
        assert "appsettings.get_setting" in block
        assert "r.value for r in" not in block, \
            "нельзя читать секреты прямо из таблицы"

    def test_diagnose_available(self):
        src = _read(APP / "services" / "fns.py")
        assert "def diagnose(" in src
        for step in ("DNS", "Соединение", "TLS", "Мастер-токен"):
            assert step in src

    def test_diagnose_endpoint_and_button(self):
        sr = _read(APP / "routers" / "settings_routes.py")
        assert '/fns/diagnose' in sr
        js = _read(APP_JS)
        assert 'id="fns-diag"' in js
        assert "/api/v1/settings/fns/diagnose" in js

    def test_no_double_prefix(self):
        src = _read(APP / "services" / "external.py")
        assert 'message=f"ФНС: {msg}"' not in src or "startswith" in src
