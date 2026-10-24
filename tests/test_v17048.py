# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.48: командная тестировка, язык и глоссарий.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерий приёмки (указание владельца): «Проведи командную тестировку
# программы, проверь программу на ошибки как в коде так и ошибки по
# русскому языку чтобы всё соответствовало.»
#
# Решения владельца, которые исполнены:
#  - полный состав ролей: прогон тестов, статический анализ, валидаторы
#    разметки и стилей, живой стенд, редактор русского языка;
#  - язык проверяется на поверхностях интерфейса и в документации
#    (комментарии и docstring'и в объём не входят);
#  - найденное чинится и закрепляется замком, чтобы не вернулось;
#  - глоссарий — замок соответствия: канон и запрещённые варианты.
#
# Что закреплено:
#  1. однородные знаки: латиница, неотличимая от кириллицы на глаз, не
#     встречается внутри русских слов (найдено и исправлено пять, одно
#     из них — домен в сообщении пользователю);
#  2. «ё» там, где она обязательна, и список слов, где её НЕТ по норме —
#     чтобы следующий аудит не «исправил» правильный текст;
#  3. знак номера пишется с пробелом: «№ 1», а не «№1»;
#  4. глоссарий: Чек-Пул, Ямастер Чек, контур B2B, ООО «Ямастер» —
#     канон и запрещённые варианты, дословные цитаты владельца исключены;
#  5. определённые термины оферты пишутся с прописной, а состоявшийся
#     канон строчной «редакции» зафиксирован как канон, а не как дефект;
#  6. два дефекта кода, найденные правилами-индикаторами ошибки, не
#     возвращаются: замыкание связывает переменную цикла, два isinstance
#     объединены в один вызов;
#  7. реестр отклонённых находок: ложные срабатывания записаны с причиной,
#     а не выброшены молча;
#  8. ритуал выпуска: точки синхронизации, кэш-бастеры, «Что нового»,
#     CHANGELOG и журнал вёрстки.
# ======================================================================
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
DOCS = ROOT / "docs"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"

INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
APP_JS = STATIC / "js" / "app.js"

OFFERTA_PY = APP / "legal" / "offerta.py"
PAGE_PY = APP / "legal" / "page.py"

CYR = re.compile(r"[а-яёА-ЯЁ]")

# Латинские буквы, неотличимые от кириллицы на глаз. Именно они опасны:
# «чеk.ymaster.ru» выглядит как домен, но не ищется и не копируется.
HOMOGLYPH = "ABCEHKMOPTXabcehkmoptyx"

# Имена файлов и маски форматов legitimately смешивают письменности.
FILE_EXT = re.compile(
    r"\.(?:bsl|py|js|md|db|svg|css|html|json|txt|sqlite3?|sh|env|png|csv|"
    r"log|ico|webmanifest|xml|ya?ml|toml|cfg|ini)$", re.I)

LITERAL = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|"
                     r"`((?:[^`\\]|\\.)*)`", re.S)


def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# Корпус: только то, что читает человек — интерфейс и документация.
# Комментарии и docstring'и в объём не входят (решение владельца).
# ---------------------------------------------------------------------
def _py_files():
    for path in sorted(APP.rglob("*.py")):
        if "vendor" in path.parts or "__pycache__" in path.parts:
            continue
        yield path


def _py_rows(path):
    """Строковые литералы модуля без docstring'ов: (строка, текст)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


def _web_rows(path):
    """Разметка и скрипты: текстовые узлы и литералы, без тегов."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".html":
        body = re.sub(r"<script.*?</script>", " ", text, flags=re.S)
        body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
        body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
        for match in re.finditer(r">([^<>]+)<", body):
            yield body[:match.start()].count("\n") + 1, match.group(1)
        for match in re.finditer(r"(?:placeholder|title|aria-label|alt|value)"
                                 r"=\"([^\"]*)\"", body):
            yield body[:match.start()].count("\n") + 1, match.group(1)
        return
    for match in LITERAL.finditer(text):
        value = match.group(1) or match.group(2) or match.group(3) or ""
        if value.strip():
            yield text[:match.start()].count("\n") + 1, value


def _doc_files():
    yield README
    yield CHANGELOG
    for path in sorted(DOCS.rglob("*.md")):
        yield path


def _doc_rows(path):
    """Проза документации: без ограждённых и индентированных блоков кода,
    без инлайн-кода и без адресов ссылок — они legitimately латинские."""
    fenced = False
    for number, raw in enumerate(path.read_text(encoding="utf-8",
                                                errors="replace").split("\n"), 1):
        if raw.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or raw.startswith("    "):
            continue
        line = re.sub(r"`[^`]*`", " КОД ", raw)
        line = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", line)
        line = re.sub(r"https?://\S+", " ", line)
        if line.strip():
            yield number, line


def corpus():
    """(относительный путь, номер строки, группа, текст) — весь проверяемый
    текст проекта в объёме, который утвердил владелец."""
    for path in _py_files():
        for line, text in _py_rows(path):
            if CYR.search(text):
                yield str(path.relative_to(ROOT)), line, "py", text
    for path in sorted(STATIC.rglob("*")):
        if not path.is_file() or path.suffix not in (".js", ".html"):
            continue
        if "vendor" in path.parts:
            continue
        for line, text in _web_rows(path):
            if CYR.search(text):
                yield str(path.relative_to(ROOT)), line, "web", text
    for path in _doc_files():
        for line, text in _doc_rows(path):
            if CYR.search(text):
                yield str(path.relative_to(ROOT)), line, "md", text


def _tokens(text):
    """Слова, где смешаны письменности: разбиваем по разделителям и дефисам,
    чтобы «API-ключ» и «fn/i/fp/сумма» не склеивались в одно «слово»."""
    for chunk in re.split(r"[^0-9A-Za-zА-Яа-яЁё_-]+", text):
        for part in chunk.split("-"):
            if len(part) >= 3 and CYR.search(part):
                yield part


# ======================================================================
# 1. Однородные знаки: латиница внутри русских слов
# ======================================================================
class TestNoHomoglyphsInsideRussianWords:
    """Пять дефектов найдено и исправлено. Самый опасный — домен в
    сообщении пользователю: «чеk.ymaster.ru» с латинской k выглядит как
    рабочий адрес, но не копируется и не ищется поиском по проекту."""

    def _findings(self):
        out = []
        for rel, line, _kind, text in corpus():
            for part in _tokens(text):
                if FILE_EXT.search(part):
                    continue                      # имя файла
                if sum(c.isupper() for c in part) > len(part) * 0.7:
                    continue                      # маска формата: ГГГГММДДTЧЧММ
                latin = [c for c in part if c in HOMOGLYPH]
                if not latin:
                    continue
                cyr = len(re.findall(r"[а-яёА-ЯЁ]", part))
                lat = len(re.findall(r"[A-Za-z]", part))
                if cyr < lat:
                    continue                      # латинское слово, не смесь
                out.append(f"{rel}:{line} {part!r} (латиница {''.join(latin)!r})")
        return out

    def test_scan_is_clean(self):
        found = self._findings()
        assert not found, f"латиница внутри русских слов: {found[:8]}"

    def test_scan_is_not_vacuous(self):
        """Замок не пустой: на тексте с подменой он бы сработал."""
        assert len(list(corpus())) > 5000, "корпус подозрительно мал"
        planted = "адрес чеk.ymaster.ru открыт"
        caught = [part for part in _tokens(planted)
                  if CYR.search(part) and any(c in HOMOGLYPH for c in part)]
        assert caught, "прибор не видит подмену буквы"

    def test_fixed_places_read_correctly(self):
        assert "на chek.ymaster.ru" in _read(APP / "routers" / "maxapp.py")
        assert "кусок HttpOnly уже дублирует токен" in _read(
            DOCS / "audit_v1671.md")
        assert "в схеме не объявлены) и ворота" in _read(
            DOCS / "qa_audit_v17038.md")
        changelog = _read(CHANGELOG)
        assert "сторонние сервисы (почта, SMS) не добавлялись" in changelog
        assert "**Отказ в старте**" in changelog


# ======================================================================
# 2. «ё»: обязательна там, где меняет слово, и НЕ нужна в норме
# ======================================================================
# Слова, где «ё» обязательна: написание через «е» — орфографическая ошибка.
YO_REQUIRED = ("идет", "идете", "идешь", "еще", "прием", "приема", "приему",
               "приемом", "учет", "учета", "учету", "учетом", "трех",
               "четырех", "всетаки", "жесткий", "жесткая", "жестко",
               "съемка", "съемки", "счет", "счету", "счетом", "включен",
               "отключен", "завершен", "подключен", "переведен")

# Слова, которые аудит v1.70.48 ЗРЯ счёл дефектом: в них «ё» нет по норме.
# Записаны здесь, чтобы следующая проверка не «исправила» правильный текст:
#   иду́т, про́верен, на́значен, при́нят, сохранено́ — ударение не на окончании;
#   счета́ — множественное число от «счёт».
YO_FORBIDDEN_TO_FIX = ("идут", "проверен", "назначен", "принят", "сохранено",
                       "счета")


class TestYoIsWhereTheNormRequiresIt:
    def test_no_missing_yo(self):
        found = []
        for rel, line, _kind, text in corpus():
            for word in YO_REQUIRED:
                if re.search(rf"(?<![а-яёА-ЯЁ-]){word}(?![а-яёА-ЯЁ])",
                             text, re.I):
                    found.append(f"{rel}:{line} {word!r}")
        assert not found, f"пропущена «ё»: {found[:8]}"

    def test_correct_words_are_still_there(self):
        """Замок не vacuous: слова без «ё», которые правильны, присутствуют
        в проекте — значит, проверка выше не просто ничего не нашла."""
        blob = "\n".join(text for _r, _l, _k, text in corpus())
        present = [w for w in YO_FORBIDDEN_TO_FIX
                   if re.search(rf"(?<![а-яёА-ЯЁ-]){w}(?![а-яёА-ЯЁ])", blob)]
        assert len(present) >= 3, f"слишком мало правильных форм: {present}"

    def test_fixed_places_read_correctly(self):
        spec = _read(DOCS / "SPECIFICATION.md")
        assert "эндпоинт приёма сканирований" in spec
        assert "на стороне 1С для приёма данных" in spec
        assert "| Приём данных сканирования (QR + изображение) |" in spec
        assert "схему БД с учётом требований масштабирования" in spec


# ======================================================================
# 3. Знак номера с пробелом
# ======================================================================
class TestNumeroSignTakesASpace:
    """ГОСТ и справочники: знак номера отделяется от числа пробелом.
    В проекте было 12 правильных и 10 неправильных — приведено к канону."""

    def test_no_glued_numero(self):
        found = []
        for rel, line, _kind, text in corpus():
            for match in re.finditer(r"№(\d)", text):
                found.append(f"{rel}:{line} №{match.group(1)}")
        assert not found, f"знак номера без пробела: {found[:8]}"

    def test_spaced_form_is_used(self):
        blob = "\n".join(text for _r, _l, _k, text in corpus())
        # порог ниже фактических 18: замок ловит исчезновение канона,
        # а не точное число — иначе каждая правка текста его роняет
        assert len(re.findall(r"№ \d", blob)) >= 15, \
            "канон «№ N» исчез — проверка выше стала пустой"


# ======================================================================
# 4. Глоссарий: канон и запрещённые варианты
# ======================================================================
CANON = {
    "Чек-Пул": (r"чек-пул", r"чекпул", r"Чек\s+Пул(?![а-яё])", r"ЧЕК\s+ПУЛ"),
    "Ямастер Чек": (r"Ямастер-Чек", r"[Яя]мастерчек", r"Ямастер\s+чек\b"),
    "контур B2B": (r"B2B-контур", r"контур\s+b2b"),
    "ООО «Ямастер»": (r'ООО\s+"Ямастер"', r"ООО\s+„Ямастер“",
                      r"ООО\s+(?!«)Ямастер"),
}

# Дословные цитаты требования владельца: в них «ООО Ямастер» написано так,
# как написал владелец. Цитата не редактируется — это документ о задаче.
OWNER_QUOTE_MARKER = "напиши мне оферту"


class TestGlossaryIsUniform:
    def _findings(self):
        out = []
        for rel, line, _kind, text in corpus():
            if OWNER_QUOTE_MARKER in text:
                continue
            for canon, patterns in CANON.items():
                for pattern in patterns:
                    for match in re.finditer(pattern, text):
                        out.append(f"{rel}:{line} {canon} → {match.group(0)!r}")
        return out

    def test_no_deviation_from_canon(self):
        found = self._findings()
        assert not found, f"отклонения от глоссария: {found[:8]}"

    def test_canon_is_actually_used(self):
        blob = "\n".join(text for _r, _l, _k, text in corpus())
        for canon in CANON:
            assert canon in blob, f"канон «{canon}» не встречается"

    def test_owner_quote_is_the_only_exception(self):
        """Исключение одно и оно осознанное: дословная цитата владельца."""
        count = 0
        for _rel, _line, _kind, text in corpus():
            if re.search(r"ООО\s+(?!«)Ямастер", text):
                assert OWNER_QUOTE_MARKER in text, \
                    "«ООО Ямастер» вне дословной цитаты владельца"
                count += 1
        assert count >= 2, f"цитата владельца потеряна: {count}"

    def test_case_banners_stay_out_of_scope(self):
        """«ЧЕК-ПУЛ» капсом — баннеры в комментариях app.js. Комментарии не
        входят в объём проверки (решение владельца), поэтому корпус их не
        видит; здесь зафиксировано, что они остались на месте."""
        js = _read(APP_JS)
        assert js.count("ЧЕК-ПУЛ") == 4, "капс-баннеры комментариев изменены"


# ======================================================================
# 5. Определённые термины оферты
# ======================================================================
class TestDefinedTermsOfTheOffer:
    """Термины, которые документ определяет, пишутся с прописной: так
    читатель отличает определённое понятие от слова в общем смысле.
    Найдено расхождение «Сервис» 8/1 и «Программа» 2/1 — исправлено."""

    def _normative(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from app.legal import offerta
        parts = list(offerta.preamble())
        for section in offerta.sections():
            parts.extend(section["clauses"])
        return "\n".join(parts), offerta

    def test_terms_are_capitalised_in_normative_text(self):
        text, offerta = self._normative()
        terms = [d["term"] for d in offerta.definitions()]
        assert len(terms) == 13, f"состав терминов изменился: {len(terms)}"
        mixed = []
        for term in terms:
            head = term.split()[0]
            if head.lower() == head:
                continue
            low = re.findall(rf"(?<![А-Яа-яЁё]){re.escape(head.lower())}"
                             rf"(?![А-Яа-яЁё])", text)
            up = re.findall(rf"(?<![А-Яа-яЁё]){re.escape(head)}"
                            rf"(?![А-Яа-яЁё])", text)
            if up and low:
                mixed.append(f"{term}: {len(up)} прописных / {len(low)} строчных")
        assert not mixed, f"термины смешивают регистр: {mixed}"

    def test_lowercase_canon_of_redakciya_is_deliberate(self):
        """«Редакция» определена как термин, но в теле документа consistently
        пишется со строчной (0 прописных / 4 строчных). Это состоявшийся
        канон, а не дефект: галочка кабинета и журнал согласий используют ту
        же строчную форму, и она закреплена замком v1.70.47."""
        text, _offerta = self._normative()
        assert not re.findall(r"(?<![А-Яа-яЁё])Редакци[а-яё]+", text), \
            "в теле оферты появилась прописная «Редакция» — канон разъехался"
        assert "редакция " in _read(CABINET), \
            "галочка кабинета перестала называть редакцию строчной"

    def test_preamble_uses_the_defined_terms(self):
        text, _offerta = self._normative()
        assert "использовать Программу, Сервис и данные ни в каких целях" in text
        assert "история редакций хранится в журнале согласий Сервиса" in text

    def test_page_label_matches_the_term(self):
        """Метка страницы оферты названа тем же термином, что в документе:
        «Правообладатель» — 20 прописных в тексте, 0 строчных."""
        assert "· Правообладатель: {_e(d['operator'])}" in _read(PAGE_PY)

    def test_plural_generic_service_is_not_a_term(self):
        """«внешних сервисов» — общее понятие, а не определённый термин,
        поэтому строчная буква здесь правильна. Записано, чтобы следующая
        проверка не начала «чинить» корректный текст."""
        text, _offerta = self._normative()
        assert len(re.findall(r"(?<![А-Яа-яЁё])сервисов", text)) == 4


# ======================================================================
# 6. Дефекты кода, найденные правилами-индикаторами ошибки
# ======================================================================
class TestCodeFindingsStayFixed:
    """B023 и SIM101 — не стиль: первое про позднюю привязку переменной
    цикла в замыкании, второе про лишний обход. Оба исправлены."""

    def test_race_test_binds_the_loop_variable(self):
        src = _read(ROOT / "tests" / "test_v17021.py")
        assert "def racer(barrier=barrier):" in src, \
            "замыкание снова не связывает переменную цикла"

    def test_isinstance_is_a_single_call(self):
        src = _read(ROOT / "tests" / "test_v17028.py")
        assert "isinstance(d, (float, int))" in src
        assert "isinstance(d, float) or isinstance(d, int)" not in src

    def test_style_only_findings_are_left_alone(self):
        """Пять RET504 (именованная переменная перед return) отклонены:
        это стиль, а не ошибка, и имя переменной документирует смысл.
        Записано здесь, чтобы их не начали «чинить» вслепую."""
        src = _read(APP / "b2b" / "pipeline.py")
        assert "    return out" in src
        ext = _read(APP / "services" / "external.py")
        assert "        return rows" in ext


# ======================================================================
# 7. Реестр отклонённых находок
# ======================================================================
REJECTED = (
    ("чек пула", "родительный падеж («каждый чек пула»), а не термин"),
    ("1C", "в прозе ноль: только шестнадцатеричные константы и имена файлов"),
    ("идут", "«ё» в них нет по норме: иду́т, про́верен, на́значен, при́нят"),
    ("счета", "множественное число от «счёт», пишется без «ё»"),
    ("Контур B2B", "прописная в начале заголовка, а не отклонение от канона"),
    ("ЧЕК-ПУЛ", "капс-баннеры в комментариях скрипта, вне объёма проверки"),
    ("ООО Ямастер", "дословная цитата требования владельца, не редактируется"),
    ('"Ямастер"', "прямые кавычки в примере канонизации названия, docstring"),
    ("по этому сценарию", "предлог с указательным местоимением — норма"),
    ("ни кто", "в цитате владельца; продукт пишет «никто» правильно"),
    ("ГГГГММДДTЧЧММ", "маска формата даты, латинская T в ней обязательна"),
    ("0..100", "техническая запись диапазона, а не две точки"),
    ("двойной пробел", "выравнивание колонок в docstring, вне объёма"),
    ("выравнивание таблиц", "исходное выравнивание, рендер его схлопывает"),
    ("RET504", "именованная переменная перед return — стиль, не ошибка"),
)


class TestRejectedFindingsAreRecorded:
    """Правило журнала: ложное срабатывание записывается с причиной, а не
    выбрасывается молча — иначе следующий аудит ищет его заново."""

    def test_register_is_not_empty(self):
        assert len(REJECTED) >= 12, f"реестр усох: {len(REJECTED)}"

    def test_every_entry_has_a_reason(self):
        for needle, reason in REJECTED:
            assert needle.strip(), "пустая находка"
            assert len(reason) > 15, f"причина не объясняет: {needle}"

    def test_changelog_records_the_register(self):
        section = _read(CHANGELOG).split("## [1.70.48]", 1)[1].split(
            "## [1.70.47]", 1)[0]
        for needle, _reason in REJECTED:
            assert needle in section, \
                f"в CHANGELOG не записано отклонение {needle!r}"


# ======================================================================
# 8. Ритуал выпуска
# ======================================================================
class TestRelease17048:
    """Точки синхронизации версии: их ведёт файл текущего выпуска.

    Прежние классы (1.70.38…1.70.47) оставляют у себя только запись выпуска
    в истории — приём тот же, что в предыдущих релизах.
    """

    VERSION = "1.70.48"
    PREVIOUS = "1.70.47"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(APP / "config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read(SW)
        assert f'"version": "{self.VERSION}"' in _read(MANIFEST)
        index = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in index
        assert f"app.js?v={self.VERSION}" in index
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        sw = _read(SW)
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_no_previous_version_left_in_sync_points(self):
        for path in (APP / "config.py", INDEX, SW, MANIFEST, MAX_HTML, CABINET):
            assert self.PREVIOUS not in _read(path), f"{path.name}: старая версия"

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self.VERSION, "запись выпуска не первая"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 6, "в записи выпуска не шесть пунктов"
        for needle in ("тестиров", "латиниц", "глоссар", "термин", "номер",
                       "отклон"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"API", "chek", "ymaster", "ru", "MAX", "oferta", "B"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("тестиров", "однородн", "глоссар", "ruff", "bandit",
                       "html-validate", "stylelint", "зонд", "Отклонено"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in ("проведи командную тестировку программы",
                       "ошибки по русскому языку чтобы всё соответствовало"):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "однородн", "глоссар"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            assert "командную тестировку" in _read(path), \
                f"{path.name}: нет цитаты владельца"
