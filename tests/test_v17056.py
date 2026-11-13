# Исторические проверки выпуска и общефункциональные регрессии.
# Рабочий облачный контур удалён по поручению владельца (#105).
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
DOCS = ROOT / "docs"
APP_JS = STATIC / "js" / "app.js"
INDEX = STATIC / "index.html"
VENDOR = STATIC / "js" / "vendor"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"


def _read(path):
    return pathlib.Path(path).read_text(encoding="utf-8")

OWNER_QUOTE_HEAD = "keystone пройден, но nova отказала: attributeerror"

OWNER_QUOTE_TAIL = "исправь как должно быть в документации"

FAKE_AUTH_URL = "https://os-api.example.ru/identity"


class TestRelease17056:
    """Точки синхронизации версии ведёт файл текущего выпуска (1.70.57).
    Здесь остаётся запись выпуска 1.70.56 в истории."""

    VERSION = "1.70.56"
    PREVIOUS = "1.70.55"

    def test_version_is_release(self):
        src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        # живое значение сверяет выпуск-владелец; здесь — форма релиза
        assert re.search(r"APP_VERSION: str = \"1\.(?:7[0-9])\.\d+\"", src)

    def test_changelog_has_release(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{self.VERSION}]" in text

    def test_changelog_quotes_the_owner(self):
        section = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split(
            f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        assert "get_hypervisor_statistics" in flat

    def test_journal_has_release(self):
        text = (ROOT / "docs" / "ui_journal.md").read_text(encoding="utf-8")
        assert f"## v{self.VERSION} —" in text

    def test_journal_is_above_previous(self):
        text = (ROOT / "docs" / "ui_journal.md").read_text(encoding="utf-8")
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS}")

    def test_whats_new_entry_exists_in_history(self):
        js = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        assert f"'{self.VERSION}': [" in js

    def test_trackers_reference_a_release(self):
        # история: трекеры существуют и сверяют версию выпуска формой
        for year in range(33, 38):
            text = (ROOT / "tests" / f"test_v170{year}.py").read_text(
                encoding="utf-8")
            assert re.search(r'"1\.(?:7[0-9])\.\d+"', text)

    def test_sw_cache_name_is_versioned(self):
        sw = (ROOT / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        assert re.search(r"ymaster-check-v1\.(?:7[0-9])\.\d+", sw)

    def test_no_placeholder_left(self):
        for path in (ROOT / "CHANGELOG.md", ROOT / "docs" / "ui_journal.md"):
            assert "TODO" not in path.read_text(encoding="utf-8")
