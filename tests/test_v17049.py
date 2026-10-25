# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.49: один корпоративный адрес.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерий приёмки (указание владельца): «Проверь все места где этот адрес
# info@… и замени на chek@ymaster.ru». Прежний адрес в этом файле целиком
# НЕ пишется и собирается из двух частей (OLD_MAIL): сторож v1.70.45 ищет
# целую строку во всех отслеживаемых файлах, кроме своего собственного, и
# этот файл не должен выглядеть для сторожа нарушением. Тот же приём уже
# применён в qa/probe_b2b.py и в самом стороже.
#
# Решение владельца: unify_everywhere — к корпоративному адресу приводятся
# подсказка поля, пример в сообщении об отказе, оба адреса в плановых
# документах и почтовая фикстура теста согласия.
#
# Что закреплено:
#  1. у домена в отслеживаемых файлах остался ровно один адрес, четыре
#     посторонних ящика того же домена приведены к корпоративному;
#  2. все значения по умолчанию в коде совпадают между собой: настройка
#     согласия, почта продавца, почта контура B2B, почта поддержки в
#     письмах и значение в оферте — это один и тот же адрес;
#  3. все почтовые ссылки mailto: ведут на корпоративный адрес;
#  4. сторожа, которые запрещают прежнему адресу возвращаться, содержат
#     запрещённое значение — иначе они ничего не запрещают;
#  5. сторонняя атрибуция в поставщике (не наш домен) не переписана;
#  6. запись о переносе v1.70.45 в журнале изменений осталась как есть:
#     история не переписывается;
#  7. фикстура теста согласия отличается от значения по умолчанию, поэтому
#     проверка «настройка перекрывает значение из кода» не опустела;
#  8. примеры адресов посетителя и клиента корпоративными НЕ стали —
#     реестр отклонённых находок с причиной;
#  9. правка планового документа не переписала его переводы строк;
# 10. ритуал выпуска: точки синхронизации, кэш-бастеры, «Что нового»,
#     CHANGELOG и журнал вёрстки.
# ======================================================================
from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
DOCS = ROOT / "docs"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = DOCS / "ui_journal.md"
PLAN = DOCS / "plan.md"
DEV_PLAN = DOCS / "dev_plan_checkpool.md"

INDEX = STATIC / "index.html"
MAX_HTML = STATIC / "maxapp" / "index.html"
CABINET = STATIC / "b2b" / "index.html"
SW = STATIC / "sw.js"
MANIFEST = STATIC / "manifest.webmanifest"
APP_JS = STATIC / "js" / "app.js"

SETTINGS_ROUTES = APP / "routers" / "settings_routes.py"
CONSENT_PY = APP / "services" / "consent.py"
CONSENT_TESTS = ROOT / "tests" / "test_v17037.py"
GUARD_TESTS = ROOT / "tests" / "test_v17045.py"
PROBE = ROOT / "qa" / "probe_b2b.py"
VENDOR_META = APP / "vendor" / "qrcode" / "METADATA.txt"

# Домен и прежний адрес собираются из частей: целый прежний адрес в этом
# файле означал бы нарушение собственного замка v1.70.45 (см. шапку).
DOMAIN = "ymaster." + "ru"
OLD_MAIL = "info@" + "ymaster.ru"
CANON = "chek@ymaster.ru"

# Четыре ящика того же домена, которые жили рядом с корпоративным и были
# приведены к нему решением владельца (unify_everywhere).
UNIFIED_PREFIXES = ("info@", "privacy@", "admin@", "support@")

# Сторож v1.70.45 исключает из поиска только свой файл.
GUARD_EXCLUDE = ":!tests/test_v17045.py"


def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _raw(path) -> bytes:
    return pathlib.Path(path).read_bytes()


def _git_grep(*args) -> str:
    out = subprocess.run(["git", "grep", *args], capture_output=True,
                         text=True, cwd=ROOT, check=False)
    return out.stdout


# ======================================================================
# 1. У домена один адрес
# ======================================================================
class TestSingleCorporateAddress:
    """Ядро задачи: прежний адрес уже был перенесён в v1.70.45, но рядом с
    ним оставались четыре других ящика того же домена. Решение владельца —
    привести все к корпоративному."""

    def test_domain_has_exactly_one_address(self):
        # файл сторожа исключён: ему запрещённое значение положено содержать
        # по смыслу — приём тот же, что в замке v1.70.45
        out = _git_grep("-ohE", r"[A-Za-z0-9._%+-]+@ymaster\.ru", "--",
                        ".", GUARD_EXCLUDE)
        found = set(out.split())
        # проверка не пуста: адрес встречается сотни раз
        assert len(out.split()) > 300, f"адресов найдено {len(out.split())}"
        assert found == {CANON}, f"у домена больше одного адреса: {sorted(found)}"

    def test_unified_prefixes_are_gone(self):
        for prefix in UNIFIED_PREFIXES:
            address = prefix + DOMAIN
            files = _git_grep("-l", address, "--", ".").split()
            if prefix == "info@":
                # единственный файл, которому запрещённое значение положено
                # содержать по смыслу, — сторож, который его запрещает
                assert files == ["tests/test_v17045.py"], files
            else:
                assert files == [], f"{address} остался: {files}"

    def test_count_did_not_collapse(self):
        """Массовый откат правки был бы виден по числу вхождений: до правки
        их было 383, запись выпуска добавила ещё одно."""
        out = _git_grep("-o", CANON, "--", ".")
        assert len(out.split()) >= 380, f"вхождений {len(out.split())}"

    def test_address_is_pure_ascii(self):
        """Однородный знак в адресе неотличим на глаз, но адрес перестаёт
        работать — приём, найденный аудитом v1.70.48."""
        assert all(ord(ch) < 128 for ch in CANON)
        assert re.fullmatch(r"chek@ymaster\.ru", CANON)
        mixed = _git_grep("-nE", r"[A-Za-z0-9._%+-]*[А-Яа-яЁё][A-Za-z0-9._%+-]*@ymaster",
                          "--", ".")
        assert mixed == "", f"адрес со смешанным письмом: {mixed[:200]}"

    def test_placeholder_in_the_client_script(self):
        js = _read(APP_JS)
        found = re.findall(r'placeholder="([^"]*@[^"]*)"', js)
        ours = [p for p in found if p.endswith(DOMAIN)]
        assert ours == [CANON] * len(ours) and ours, ours
        # замок v1.70.58 (test_v1584) требует не меньше двух таких подсказок;
        # после приведения примера их три
        assert js.count(f'placeholder="{CANON}"') == 3, js.count(CANON)

    def test_refusal_message_shows_the_corporate_example(self):
        src = _read(SETTINGS_ROUTES)
        assert f"адрес вида {CANON}" in src
        assert "privacy@" not in src, "в сообщении остался прежний пример"

    def test_planning_documents_use_the_corporate_address(self):
        assert CANON in _read(PLAN)
        assert CANON in _read(DEV_PLAN)
        for prefix in UNIFIED_PREFIXES[1:]:
            assert prefix + DOMAIN not in _read(PLAN), prefix
            assert prefix + DOMAIN not in _read(DEV_PLAN), prefix

    def test_the_plan_edit_kept_its_line_endings(self):
        """Плановый документ хранится с переводами строк CRLF: правка одного
        адреса не должна переписать все 905 строк файла."""
        raw = _raw(PLAN)
        assert raw.count(b"\r\n") == raw.count(b"\n") == 905
        assert _raw(DEV_PLAN).count(b"\r\n") == 0


# ======================================================================
# 2. Значения по умолчанию в коде — один и тот же адрес
# ======================================================================
class TestDefaultsAgree:
    """Пять констант в коде задают почту для разных поверхностей. Если они
    разойдутся, пользователь увидит разные ящики в оферте, в кабинете и в
    письмах."""

    def test_all_code_defaults_are_the_same(self):
        from app.b2b import content, letters
        from app.config import settings
        from app.legal import offerta
        from app.services import consent

        values = {
            "CONSENT_CONTACT_DEFAULT": consent.CONSENT_CONTACT_DEFAULT,
            "VENDOR_EMAIL": settings.VENDOR_EMAIL,
            "b2b.content.CONTACT_EMAIL": content.CONTACT_EMAIL,
            "b2b.letters.SUPPORT": letters.SUPPORT,
            "legal.offerta.CONTACT_DEFAULT": offerta.CONTACT_DEFAULT,
        }
        assert set(values.values()) == {CANON}, values

    def test_offer_takes_the_address_from_the_consent_service(self):
        """Оферта не хранит свою копию адреса, а берёт её из сервиса
        согласий: вторая копия разошлась бы при первой же правке."""
        src = _read(APP / "legal" / "offerta.py")
        assert "CONTACT_DEFAULT = consent.CONSENT_CONTACT_DEFAULT" in src
        assert re.search(r'"[A-Za-z0-9._%+-]+@ymaster\.ru"', src) is None, \
            "в модуле оферты зашит собственный адрес"

    def test_all_mailto_links_are_corporate(self):
        out = _git_grep("-ohE", r"mailto:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",
                        "--", ".")
        links = out.split()
        assert links and len(links) >= 8, f"ссылок {len(links)}"
        assert set(links) == {"mailto:" + CANON}, sorted(set(links))


# ======================================================================
# 3. Сторожа обязаны содержать запрещённое значение
# ======================================================================
class TestGuardsKeepTheForbiddenAddress:
    """Замок, который запрещает прежнему адресу возвращаться, содержит этот
    адрес буквально — иначе ему нечего искать. Такие места не «чинятся»."""

    def test_literal_old_address_lives_only_in_its_guard(self):
        files = _git_grep("-l", OLD_MAIL, "--", ".").split()
        assert files == ["tests/test_v17045.py"], files

    def test_guards_assemble_the_needle_from_parts(self):
        split_form = '"info@" + "ymaster.ru"'
        for path in (PROBE, GUARD_TESTS):
            assert split_form in _read(path), path.name

    def test_this_file_does_not_trip_the_guard(self):
        """Самопроверка приёма: если целый прежний адрес попадёт в этот файл,
        сторож v1.70.45 начнёт падать на нём же."""
        assert OLD_MAIL not in _read(__file__)
        out = subprocess.run(
            ["git", "grep", "-l", OLD_MAIL, "--", GUARD_EXCLUDE],
            capture_output=True, text=True, cwd=ROOT, check=False)
        assert out.stdout.strip() == "", out.stdout

    def test_third_party_attribution_is_not_rewritten(self):
        """В поставщике лежит чужой адрес другой компании из уведомления об
        авторстве (BSD). Он не наш и замене не подлежит."""
        meta = _read(VENDOR_META)
        assert "info@lincolnloop.com" in meta
        assert DOMAIN not in meta

    def test_migration_record_survives_in_the_changelog(self):
        """Запись v1.70.45 сообщает измеренные на тот момент числа. История
        не переписывается, даже если прежний адрес больше нигде не живёт."""
        text = _read(CHANGELOG)
        section = text.split("## [1.70.45]", 1)[1].split("## [1.70.44]", 1)[0]
        assert "242" in section and "288" in section
        assert OLD_MAIL not in section


# ======================================================================
# 4. Фикстура теста согласия не совпадает со значением по умолчанию
# ======================================================================
class TestConsentFixtureStaysDistinct:
    """Тест v1.70.37 проверяет, что адрес из настройки ПЕРЕКРЫВАЕТ значение
    по умолчанию. Если подставить в него корпоративный адрес, проверка
    опустеет: тест начнёт проходить и тогда, когда настройка не применяется."""

    def _fixture(self) -> str:
        found = re.search(r'MAIL = "([^"]+)"', _read(CONSENT_TESTS))
        assert found, "фикстура MAIL пропала из теста согласия"
        return found.group(1)

    def test_fixture_differs_from_the_default(self):
        from app.services.consent import CONSENT_CONTACT_DEFAULT
        mail = self._fixture()
        assert mail != CONSENT_CONTACT_DEFAULT, \
            "фикстура совпала со значением по умолчанию — проверка опустела"

    def test_fixture_uses_a_reserved_example_domain(self):
        """RFC 2606 резервирует домен для примеров: письмо на него не уйдёт."""
        assert self._fixture().endswith("@example.com")

    def test_refusal_lock_follows_the_message(self):
        """Замок на сообщение об отказе обновлён вместе с сообщением: иначе
        правка примера в коде роняла бы прогон молча."""
        src = _read(CONSENT_TESTS)
        assert '"chek@" in r.json()["detail"]' in src
        assert '"privacy@" in r.json()["detail"]' not in src

    def test_override_is_still_asserted_by_content(self):
        """Суть проверки — адрес из настройки попадает в текст согласия, а не
        просто сохраняется в таблицу."""
        src = _read(CONSENT_TESTS)
        assert "self.MAIL in doc[\"text\"]" in src
        assert 'st["contact"] == self.MAIL' in src


# ======================================================================
# 5. Реестр отклонённых находок
# ======================================================================
# Политика владельца (v1.70.48): ложное срабатывание записывается с причиной,
# а не выбрасывается молча — иначе следующий аудит ищет его заново.
REJECTED = [
    ("app/vendor/qrcode/METADATA.txt:9", "info@lincolnloop.com",
     "адрес другой компании в уведомлении об авторстве поставщика (BSD); "
     "не наш домен, перезапись испортила бы атрибуцию"),
    ("qa/probe_b2b.py:354", "прежний адрес из частей",
     "сторож обязан содержать запрещённое значение, иначе нечего искать"),
    ("tests/test_v17045.py:2405,2429,3282", "прежний адрес и его описание",
     "замок v1.70.45 запрещает прежнему адресу возвращаться; это и есть "
     "запрет, а не упоминание в прозе"),
    ("CHANGELOG.md, раздел 1.70.45", "запись о переносе (242 файла)",
     "история не переписывается: числа верны на момент той записи"),
    ("tests/test_v17037.py:154", "почтовая фикстура",
     "не приведена к корпоративному сознательно: фикстура обязана отличаться "
     "от значения по умолчанию, иначе проверка перекрытия опустеет; взят "
     "зарезервированный RFC 2606 домен"),
    ("app/b2b/verify.py:155", "имя@доменкомпании.ru",
     "маска формата в сообщении о неполном адресе, а не почтовый ящик; "
     "замена на реальный адрес превратила бы подсказку в чужой ящик"),
    ("app/static/index.html:60", "you@mail.ru",
     "подсказка поля на лендинге показывает пример адреса ПОСЕТИТЕЛЯ, а не "
     "наш ящик"),
    ("app/static/js/app.js", "ivanov@company.ru, dir@company.ru",
     "примеры корпоративной почты КЛИЕНТА в форме контура B2B; приводить их "
     "к нашему адресу нельзя по смыслу поля"),
]


class TestRejectedFindingsAreRecorded:
    def test_registry_is_not_empty(self):
        assert len(REJECTED) >= 8

    def test_every_entry_names_the_place_and_the_reason(self):
        for place, needle, reason in REJECTED:
            assert place and needle and len(reason) > 20, place

    def test_format_masks_are_still_masks(self):
        """Маска формата осталась маской: её не заменили настоящим ящиком."""
        assert "имя@доменкомпании.ru" in _read(APP / "b2b" / "verify.py")
        assert 'placeholder="you@mail.ru"' in _read(INDEX)

    def test_client_examples_are_still_the_clients(self):
        js = _read(APP_JS)
        for example in ("ivanov@company.ru", "dir@company.ru"):
            assert example in js, example


# ======================================================================
# 6. Ритуал выпуска
# ======================================================================
class TestRelease17049:
    """v1.70.50: выпуск 1.70.49 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17050, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.48).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.
    """

    VERSION = "1.70.49"
    PREVIOUS = "1.70.48"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(APP / "config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 49)

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 4, "в записи выпуска не четыре пункта"
        for needle in ("корпоративн", "подсказк", "опусте", "истори",
                       "кэш-бастер"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"chek", "ymaster", "ru", "service", "worker"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("корпоративн", CANON, "подсказк", "пример", "планов",
                       "сторож", "кэш-бастер", "Отклонено"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"

    def test_changelog_quotes_the_owner(self):
        section = _read(CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in ("проверь все места где этот адрес",
                       f"замени на {CANON.lower()}"):
            assert needle in flat, f"в цитате владельца нет «{needle}»"
        # прежний адрес в цитате выпущен многоточием: целая строка в
        # отслеживаемом файле означала бы нарушение сторожа v1.70.45
        assert OLD_MAIL not in section

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("Подготовка", "План", "Решение", "Проверка",
                       "Отклонённые варианты", "адрес", "кэш-бастер"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        text = _read(JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            assert "Проверь все места где этот адрес" in _read(path), \
                f"{path.name}: нет цитаты владельца"
