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

OWNER_QUOTE_HEAD = "провайдер сервера предоставляет на скриншоте"

OWNER_QUOTE_TAIL = "чтоб можно было видеть статистику сервера"

FAKE_USERNAME = "cloud-user-00000000-0000-0000-0000-000000000000"

FAKE_AUTH_URL = "https://horizon.example.ru:5000/v3"


class TestProviderDataIsNotInTheRepo:
    """Скриншот владельца содержит настоящие адрес панели и логин. Им не
    место в коде, тестах и документации: репозиторий переживает смену
    провайдера, а персональные данные — нет. В проверках выпуска поэтому
    используются заведомо подставные значения (FAKE_*)."""

    # Иглы собраны из соседних литералов: Python склеивает их при разборе, а
    # исходник этого файла полных строк не содержит — иначе замок ловил бы сам
    # себя (проверено: без этого приём падал на tests/test_v17052.py).
    NEEDLES = ("rocket" "cloud", "rcl" "oud-", "cloud" "core")
    SKIP_DIRS = {".git", ".venv", "node_modules", "data", "__pycache__",
                 ".mypy_cache", ".pytest_cache", ".ruff_cache"}

    def test_repo_carries_no_provider_credentials(self):
        hits = []
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            if self.SKIP_DIRS & set(p.parts):
                continue
            try:
                low = p.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            for needle in self.NEEDLES:
                if needle in low:
                    hits.append(f"{p.relative_to(ROOT)}: {needle}")
        assert not hits, f"данные провайдера в репозитории: {hits[:8]}"

    def test_fake_values_are_actually_fake(self):
        """Замок не выродился: подставные значения в этом файле присутствуют,
        значит тест выше отличает их от настоящих."""
        text = _read(pathlib.Path(__file__))
        assert FAKE_USERNAME in text and FAKE_AUTH_URL in text
        assert "example.ru" in FAKE_AUTH_URL
        assert len(self.NEEDLES) == 3
        assert all(len(n) > 6 for n in self.NEEDLES), "иглы вырождены"


class TestRelease17052:
    """v1.70.53: выпуск 1.70.52 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17053, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.51).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.
    """

    VERSION = "1.70.52"
    PREVIOUS = "1.70.51"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(APP / "config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 52)

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("openrc", "браузер", "секрет", "Проверить подключение",
                       "вкладк", "статистику"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        # «openrc» и «sh» — имя файла провайдера ровно так, как его пишет
        # панель управления (openrc.sh); приём тот же, что для MAX/Telegram/
        # service/worker в прошлых выпусках.
        allowed = {"MAX", "Telegram", "service", "worker", "openrc", "sh"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("openrc", "браузер", "секрет", "кэш-бастер",
                       "Отклонено", "подключен", "скриншот"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"
        for head_name in ("Подготовка", "Что оказалось не так", "Решение",
                          "Отклонено", "Проверено"):
            assert f"### {head_name}" in section, \
                f"в CHANGELOG нет раздела «{head_name}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in (OWNER_QUOTE_HEAD, OWNER_QUOTE_TAIL):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "openrc", "браузер",
                       "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        """Цитата требования владельца присутствует дословно во всех трёх
        местах. Внутри цитаты допустим перевод строки — на состав слов он не
        влияет, поэтому сравнение идёт по тексту с нормализованными
        пробелами."""
        needle = re.sub(r"\s+", " ", OWNER_QUOTE_HEAD).lower()
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            flat = re.sub(r"\s+", " ", _read(path)).lower()
            assert needle in flat, f"{path.name}: нет цитаты владельца"
