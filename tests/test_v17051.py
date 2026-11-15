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

OWNER_QUOTE_HEAD = "выполни тз docs/monitor.md"

OWNER_QUOTE_TAIL = "внедри аккуратно чтоб не сломать нашу программу"


class TestNothingElseIsBroken:
    def test_previous_cards_survive(self):
        js = _read(APP_JS)
        for card in ("st-webhooks", "st-consent", "st-pool", "st-checko",
                     "st-onec", "st-clients", "st-transfer", "st-adguard"):
            assert f'id="{card}"' in js, card

    def test_own_charts_module_is_untouched(self):
        """Решение владельца charts=chartjs не отменяет собственные графики:
        charts.js остаётся для остальных экранов."""
        charts = _read(STATIC / "js" / "charts.js")
        assert "export function barChart" in charts
        assert "export function donutChart" in charts
        assert "без внешних зависимостей" in charts

    def test_spa_catch_all_prefix_is_unchanged(self):
        src = _read(APP / "main.py")
        assert '("api/", "onec/", "ws/", "maxapp/", "b2b/")' in src

    def test_vendor_jsqr_survives(self):
        assert (VENDOR / "jsQR.js").exists()
        assert "/js/vendor/jsQR.js" in _read(INDEX)



class TestNoMixedScriptTokens:
    """Слепой участок аудита v1.70.48 закрыт.

    Правило аудита ловит латиницу внутри русских слов, но пропускает токен, в
    котором латинских знаков больше, чем кириллических: guard `cyr < lat`
    объявляет его «латинским словом, а не смесью». Через этот guard прошли два
    настоящих дефекта, оба найдены замером при подготовке этого выпуска:

    * `docs/dev_plan_checkpool.md` — «импersonate_pool_start», где «имп»
      набрано кириллицей (U+0438, U+043C, U+043F) перед латинским продолжением.
      Настоящее имя действия в коде — `impersonate_pool_start`
      (`app/pool/router_admin.py`, перевод в `app/services/audit_human.py`),
      поэтому поиск по документации не находил строку, а поиск по коду того,
      что в ней написано, не находил ничего. Исправлено.
    * `docs/ui_journal.md` этого выпуска — «с zamyslом» вместо «с задуманной».
      Исправлено.

    Пять токенов смешанного состава проверены руками и не являются дефектами;
    они перечислены ниже, чтобы находку не искали повторно.
    """

    EXEMPT = {
        # Имя класса в коде: кириллическое название и общепринятая
        # аббревиатура «QR». Прежнее, присутствует в HEAD.
        "СтрокаЧекаQR",
        # Артефакт разбора: токенизатор склеил «n» из escape-последовательности
        # \\n со следующим русским словом в тексте confirm() модуля osm.js.
        "nОпрос",  # Исторический пример в журнале изменений.
        # Цитата исправленного дефекта в записях прошлых выпусков: слово стоит
        # в кавычках именно как пример того, что было найдено и заменено на
        # «предоставлена».
        "udzielена",
    }

    def _findings(self):
        from tests.test_v17048 import FILE_EXT, _tokens, corpus

        out = []
        for rel, line, _kind, text in corpus():
            for part in _tokens(text):
                if FILE_EXT.search(part):
                    continue
                if part in self.EXEMPT:
                    continue
                has_cyr = bool(re.search(r"[а-яёА-ЯЁ]", part))
                has_lat = bool(re.search(r"[A-Za-z]", part))
                if has_cyr and has_lat:
                    out.append(f"{rel}:{line} {part!r}")
        return out

    def test_scan_is_clean(self):
        found = self._findings()
        assert not found, f"токены со смесью письменностей: {found[:8]}"

    def test_lock_is_not_vacuous(self):
        """Замок не вырожден: исключения действительно встречаются в корпусе,
        значит проверка выше не просто ничего не нашла."""
        from tests.test_v17048 import _tokens, corpus

        seen = set()
        for _rel, _line, _kind, text in corpus():
            seen.update(_tokens(text))
        present = self.EXEMPT & seen
        assert len(present) == len(self.EXEMPT), \
            f"исключения не найдены в корпусе: {sorted(self.EXEMPT - present)}"

    def test_documented_action_names_match_the_code(self):
        """Регрессия найденного дефекта: имена действий аудита в плане
        совпадают с именами в коде посимвольно."""
        plan = _read(DOCS / "dev_plan_checkpool.md")
        router = _read(APP / "pool" / "router_admin.py")
        for action in ("impersonate_pool_start", "impersonate_pool_stop"):
            assert action in router, f"{action}: нет в коде"
        assert "impersonate_pool_start/stop" in plan


class TestRelease17051:
    """v1.70.52: выпуск 1.70.51 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17052, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.50).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.
    """

    VERSION = "1.70.51"
    PREVIOUS = "1.70.50"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(APP / "config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 51)

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 5, "в записи выпуска не пять пунктов"
        for needle in ("облак", "вкладк", "порог", "секрет", "журнал"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"MAX", "Telegram", "service", "worker"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("вкладк", "порог", "журнал", "секрет", "облак",
                       "кэш-бастер", "Отклонено", "ретенц"):
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
                       "Отклонённые варианты", "вкладк", "кэш-бастер"):
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
