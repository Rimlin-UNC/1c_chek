"""«Комбайн первички»: схема ФНС 5.03, печатные формы из PDF, сканы и фото (T-61, v1.70.95).

Всё — на синтетических документах (ADR-14). Настоящий Tesseract в CI не запускается: языковых
моделей там нет, а скачивать их из тестов нельзя. Его заменяют поддельный движок
(для ocr_worker.py) и поддельный обработчик (для ocr.py). Замеры на настоящем Tesseract
записаны в docs/primary_docs_design.md: 1,5–2 с на страницу, пик 70–150 МБ.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import threading
import types
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.primary import ocr, ocr_worker, print_form, schema, service, upd
from tests.primary_samples import SELLER_INN, inn10, inn12, upd_xml
from tests.test_primary_upd import _upload, firms  # noqa: F401 — фикстура компаний и бухгалтеров
from tests.test_v1140 import _mk_user

FIXTURE = Path("tests/fixtures/primary/upd_print_text.pdf")
XSD = Path("app/primary/xsd/ON_NSCHFDOPPR_1_997_01_05_03_05.xsd")
TODAY = date(2026, 11, 4)

# Фрагменты настоящих выдач Tesseract 5.5.3 по синтетической печатной форме (замер 04.11.2026)
OCR_SCAN = """Универсальный Счет-фактура № УТ-215 от 14 октября 2026 г. (1)
передаточный Исправление № -- OT -- (1a)
документ Продавец: ООО «СтройБаза» (2)
Статус: 1 ИНН/КПП продавца: 5001007329/500101001 (26)
Покупатель: ООО «Ромашка-Строй» (6)
ИНН/КПП покупателя: 7704123450/770401001 (66)
Валюта: наименование, код Российский рубль, 643 (7)
CEM-500 1 | Цемент M500, мешок 50 кг — 796 шт 10 500,00 5 000,00 аа 22% 1100,00 6 100,00
АВМ-12 2 | Арматура А500С 412 мм — 168 т 1,250 64 000,00 80 000,00 аа 22% 17 600,00 97 600,00
— 3 | Доставка до объекта — 796 шт 1 1500,00 1500,00 аа 22% 330,00 1 830,00
Всего к оплате (9) 86 500,00 x 19 030,00 105 530,00
"""
OCR_PHOTO = """Универсальный | Счет-Фоктура № УТ-215 от 14 октября 2026 г. a
документ Продавец: 500 «СтройБаза» Q
Статус: 1 ИНН/КПП продавца: `5001007329/500101001 (26)
Покупатель: 500 „Ромашка-Строй» (6)
ИННИКПП покупателя: 7704123450/7770401091 (66)
Валюта: наименование, KOR Российский рубль. 643 (7)
Set 1 | Цемент M500, мешок 50 кг 796 шт 10 500,00 5 000,00 aera 22% 1 100,00 6 100,00
АВМ-12 | 2 | Арматура ASOOC 912 мм - 158 т 1,250 64 000,00 80 000,00 Ba 22% 17 600,00 97 600,00
‘Всего к оплате (9) 4 86 500,00 x 19 030,00 105 530,00 |
"""
# Автоматическая разметка разнесла подписи и значения по разным блокам (скан 300 точек на дюйм)
OCR_BLOCKS = """Универсальный
передаточный
документ

Статус: 1

Счет-фактура №

Продавец:

ИНН/КПП продавца:

Покупатель:

ИНН/КПП покупателя:

УТ-215 от 14 октября 2026 г.
ООО «СтройБаза»
5001007329/500101001
ООО «Ромашка-Строй»
7704123450/770401001
"""
OCR_BLOCKS_LAYOUT = """Универсальный  Счет-фактура №  УТ-215 от 14 октября 2026 г.  (1)
документ  Продавец:  ООО «СтройБаза»  (2)
Статус: 1  ИНН/КПП продавца:  5001007329/500101001  (2б)
Покупатель:  ООО «Ромашка-Строй»  (6)
ИНН/КПП покупателя:  7704123450/770401001  (6б)
CEM-500  1  Цемент М500, мешок 50 кг  --  796  шт  10  500,00  5 000,00  без  22%  1 100,00  6 100,00
Всего к оплате (9)  5 000,00  X  1 100,00  6 100,00
"""


def _codes(issues: list[dict], level: str | None = None) -> set[str]:
    return {i["code"] for i in issues if level is None or i["level"] == level}


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


# ------------------------------------------------------------------ схема ФНС 5.03
def test_schema_file_is_the_verified_official_one():
    data = XSD.read_bytes()
    assert git_blob_sha1(data) == "aa486acc71ba2bc6a88e47915fce600ad566bf53"
    assert hashlib.sha256(data).hexdigest() == "f98a1ac91cb840ff341976f7636d8a98d51a43e9b1cbbf3e97386551aacdb49a"
    text = data.decode("cp1251")
    assert 'value="22%"' in text and 'value="22/122"' in text and "targetNamespace" not in text
    readme = Path("app/primary/xsd/README.md").read_text(encoding="utf-8")
    assert "aa486acc71ba2bc6a88e47915fce600ad566bf53" in readme and "ЕД-7-26/970@" in readme


@pytest.mark.parametrize("kw", [
    {}, {"function": "ДОП"}, {"function": "СЧФ"}, {"encoding": "utf-8"}, {"correction": ("1", "12.03.2026")},
    {"seller": ("ИП", inn12(), ("Иванов", "Иван", "Иванович"))}, {"buyer": ("ИН", "Foreign Ltd")},
    {"lines": [{"name": "Услуга", "qty": "1", "price": "100.00", "base": "100.00", "rate": "без НДС",
                "vat": "без НДС", "total": "100.00"}]},
])
def test_synthetic_documents_pass_the_official_schema(kw):
    result = schema.validate(upd_xml(**kw), "5.03")
    assert result["checked"] and result["valid"], result["errors"]
    assert result["revision"] == "05"
    assert schema.issues(result) == [{"level": "info", "code": "xsd_ok",
                                      "message": "Файл соответствует схеме ФНС 5.03 (редакция 05)"}]


def test_schema_errors_are_explained_in_russian():
    no_signer = schema.validate(upd_xml(signer=False), "5.03")
    assert no_signer["checked"] and not no_signer["valid"]
    assert no_signer["errors"] == [{"path": "Файл › Документ",
                                    "message": "«Документ»: не хватает вложенного элемента «Подписант»"}]
    bad_rate = schema.validate(upd_xml(lines=[{"name": "Песок", "qty": "1", "price": "100.00", "base": "100.00",
                                              "rate": "21%", "vat": "21.00", "total": "121.00"}]), "5.03")
    assert any("недопустимое значение «21%»" in e["message"] for e in bad_rate["errors"])
    warning = schema.issues(no_signer)[0]
    assert warning["level"] == "warning" and warning["code"] == "xsd" and "Подписант" in warning["message"]
    assert warning["details"] == ["Файл › Документ: «Документ»: не хватает вложенного элемента «Подписант»"]


@pytest.mark.parametrize("raw, expected", [
    ("Element 'АдрИнф': The attribute 'НаимСтран' is required but missing.", "«АдрИнф»: нет обязательного атрибута «НаимСтран»"),
    ("Element 'Документ': Missing child element(s). Expected is one of ( СвПродПер, Подписант ).",
     "«Документ»: не хватает вложенного элемента — нужен один из: СвПродПер, Подписант"),
    ("Element 'ДопСведТов': This element is not expected. Expected is ( ИнфПолФХЖ2 ).",
     "«ДопСведТов»: элемент здесь не ожидается — на этом месте нужен «ИнфПолФХЖ2»"),
    ("Element 'СвЮЛУч', attribute 'ИННЮЛ': [facet 'length'] The value '123' has a length of '3'; this differs from "
     "the allowed length of '10'.", "«СвЮЛУч», атрибут «ИННЮЛ»: значение «123»: длина должна быть 10, а не 3"),
    ("Element 'СведТов', attribute 'КолТов': 'abc' is not a valid value of the atomic type 'xs:decimal'.",
     "«СведТов», атрибут «КолТов»: «abc» — не число"),
    ("No matching global declaration available for the validation root.", "Корневой элемент не соответствует формату 5.03"),
    ("Element 'X': something new from libxml2", "«X»: something new from libxml2"),
])
def test_libxml2_messages_translated(raw, expected):
    assert schema.message_ru(raw) == expected


def test_newest_revision_first_and_any_revision_accepts(tmp_path, monkeypatch):
    """Редакция 01 (без ставки 22 %) не принимает документ 2026 года — а 05 принимает."""
    old = XSD.read_bytes().replace(b'<xs:enumeration value="22%"/>', b"").replace(b'<xs:enumeration value="22/122"/>', b"")
    (tmp_path / "ON_NSCHFDOPPR_1_997_01_05_03_01.xsd").write_bytes(old)
    monkeypatch.setattr(schema, "XSD_DIR", tmp_path)
    schema.reset_cache()
    try:
        only_old = schema.validate(upd_xml(), "5.03")
        assert only_old["revision"] == "01" and not only_old["valid"]
        assert any("«22%»" in e["message"] for e in only_old["errors"])
        (tmp_path / XSD.name).write_bytes(XSD.read_bytes())
        schema.reset_cache()
        assert schema.available()["revisions"] == ["05", "01"]
        both = schema.validate(upd_xml(), "5.03")
        assert both["valid"] and both["revision"] == "05"
    finally:
        schema.reset_cache()


def test_schema_check_degrades_gracefully(monkeypatch, tmp_path):
    assert schema.validate(upd_xml(version="5.01"), "5.01")["checked"] is False
    assert schema.issues(schema.validate(upd_xml(version="5.01"), "5.01")) == []
    monkeypatch.setitem(sys.modules, "lxml", None)          # «модуль не установлен»
    schema.reset_cache()
    try:
        result = schema.validate(upd_xml(), "5.03")
        assert result == {"checked": False, "valid": None, "revision": "", "errors": [],
                          "reason": "не установлен модуль lxml"}
        assert _codes(schema.issues(result)) == {"xsd_skipped"}
    finally:
        monkeypatch.delitem(sys.modules, "lxml")
        schema.reset_cache()
    monkeypatch.setattr(schema, "XSD_DIR", tmp_path)        # схем нет
    schema.reset_cache()
    try:
        assert schema.available() == {"available": False, "revisions": [], "reason": "нет файлов схемы ФНС в app/primary/xsd"}
    finally:
        schema.reset_cache()


def test_entities_never_reach_lxml():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
            b'<\xd0\xa4\xd0\xb0\xd0\xb9\xd0\xbb>&b;</\xd0\xa4\xd0\xb0\xd0\xb9\xd0\xbb>')
    with pytest.raises(upd.UpdError):
        upd.parse(bomb)


def test_upload_adds_schema_verdict(client, firms):  # noqa: F811
    _, a, _ = firms
    good = upd_xml(buyer=("ООО «Покупатель»", a["inn"], "770401001"), number=f"X-{uuid4().hex[:6]}")
    bad = upd_xml(buyer=("ООО «Покупатель»", a["inn"], "770401001"), number=f"Y-{uuid4().hex[:6]}", signer=False)
    res = _upload(client, a["hdr"], ("good.xml", good), ("bad.xml", bad)).json()
    ok, warn = res["results"]
    assert ok["document"]["status"] == "ok" and _codes(ok["issues"]) == {"xsd_ok"}
    assert warn["document"]["status"] == "warnings" and _codes(warn["issues"], "warning") == {"xsd"}
    detail = client.get(f"/api/v1/primary/{warn['document']['id']}", headers=a["hdr"]).json()
    assert detail["source"] == "xml" and "Подписант" in json.dumps(detail["issues"], ensure_ascii=False)


# ------------------------------------------------------------------ исправления разбора 5.03
def test_503_foreign_party_name_and_citizen():
    assert upd.parse(upd_xml(buyer=("ИН", "Foreign Ltd")))["buyer"] == {"kind": "ИН", "name": "Foreign Ltd",
                                                                       "inn": "", "kpp": ""}
    assert upd.parse(upd_xml(version="5.01", buyer=("ИН", "Old Ltd")))["buyer"]["name"] == "Old Ltd"
    xml = ('<?xml version="1.0" encoding="utf-8"?><Файл ИдФайл="ON_NSCHFDOPPR_1" ВерсФорм="5.03" ВерсПрог="т">'
           '<Документ КНД="1115131" Функция="СЧФДОП" ДатаИнфПр="10.03.2026" ВремИнфПр="10.00.00">'
           '<СвСчФакт НомерДок="1" ДатаДок="10.03.2026"><СвПрод><ИдСв><СвЮЛУч НаимОрг="ООО «А»" ИННЮЛ="'
           f'{SELLER_INN}" КПП="500101001"/></ИдСв></СвПрод><СвПокуп><ИдСв><СвФЛУч><ФИО Фамилия="Петров" Имя="Пётр"/>'
           '</СвФЛУч></ИдСв></СвПокуп><ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/></СвСчФакт>'
           '<ТаблСчФакт><СведТов НомСтр="1" НаимТов="Доска" НалСт="без НДС" СтТовБезНДС="10.00" СтТовУчНал="10.00">'
           '<СумНал><БезНДС>без НДС</БезНДС></СумНал></СведТов><ВсегоОпл СтТовБезНДСВсего="10.00" '
           'СтТовУчНалВсего="10.00"><СумНалВсего><БезНДС>без НДС</БезНДС></СумНалВсего></ВсегоОпл></ТаблСчФакт>'
           '<Подписант СпосПодтПолном="1"><ФИО Фамилия="Т" Имя="Т"/></Подписант></Документ></Файл>').encode()
    parsed = upd.parse(xml)
    assert parsed["buyer"] == {"kind": "ФЛ", "name": "Петров Пётр", "inn": "", "kpp": ""}
    assert "buyer" not in _codes(upd.check(parsed, today=TODAY))
    assert schema.validate(xml, "5.03")["valid"]


@pytest.mark.parametrize("raw, kind", [("9,09%", "10/110"), ("16,67%", "20/120"), ("18,03%", "22/122"),
                                       ("18.03%", "22/122"), ("22/122", "22/122")])
def test_calculated_rates_are_fractions(raw, kind):
    assert upd.vat_rate(raw) == kind


def test_draft_checks_skip_edo_format_rules():
    draft = print_form.parse_text(OCR_SCAN)
    issues = upd.check(draft, today=TODAY, electronic=False)
    assert not _codes(issues) & {"version", "version_503", "buyer_title", "lines"}
    assert upd.status_of(issues) == "ok"
    empty = dict(draft, lines=[])
    assert "lines_absent" in _codes(upd.check(empty, today=TODAY, electronic=False), "info")
    assert "lines" in _codes(upd.check(empty, today=TODAY), "error")


# ------------------------------------------------------------------ печатная форма
def test_text_pdf_from_1c_like_print_form():
    plain, layout, pages = print_form.pdf_text(FIXTURE.read_bytes())
    assert pages == 1 and print_form.has_text(plain)
    d = print_form.parse_variants([(plain, layout), (plain, "")])
    assert (d["number"], d["date"], d["function"], d["kind"]) == ("УТ-215", date(2026, 10, 14), "СЧФДОП", "УПД")
    assert d["seller"] == {"kind": "ЮЛ", "name": "ООО «СтройБаза»", "inn": "5001007329", "kpp": "500101001"}
    assert d["buyer"] == {"kind": "ЮЛ", "name": "ООО «Ромашка-Строй»", "inn": "7704123450", "kpp": "770401001"}
    assert d["totals"] == {"without_vat": 86500.0, "vat": 19030.0, "with_vat": 105530.0}
    assert [(ln["position"], ln["item_code"], ln["unit_code"], ln["unit_name"], ln["quantity"], ln["price"],
             ln["sum_without_vat"], ln["vat_rate"], ln["vat_sum"], ln["sum_with_vat"]) for ln in d["lines"]] == [
        (1, "CEM-500", "796", "шт", 10.0, 500.0, 5000.0, "22", 1100.0, 6100.0),
        (2, "ARM-12", "168", "т", 1.25, 64000.0, 80000.0, "22", 17600.0, 97600.0),
        (3, "", "796", "шт", 1.0, 1500.0, 1500.0, "22", 330.0, 1830.0)]
    assert d["uncertain"] == [] and upd.status_of(upd.check(d, today=TODAY, electronic=False)) == "ok"


def test_ocr_text_of_a_clean_scan():
    d = print_form.parse_text(OCR_SCAN)
    assert (d["number"], d["date"], d["function"]) == ("УТ-215", date(2026, 10, 14), "СЧФДОП")
    assert d["seller"]["inn"] == "5001007329" and d["buyer"]["kpp"] == "770401001"
    assert [ln["sum_with_vat"] for ln in d["lines"]] == [6100.0, 97600.0, 1830.0]
    assert d["totals"]["with_vat"] == 105530.0 and d["uncertain"] == []


def test_ocr_noise_of_a_phone_photo_is_fixed_or_flagged():
    d = print_form.parse_text(OCR_PHOTO)
    assert d["number"] == "УТ-215" and d["date"] == date(2026, 10, 14)
    assert d["seller"]["name"] == "ООО «СтройБаза»" and d["buyer"]["name"] == "ООО «Ромашка-Строй»"
    assert d["buyer"]["kpp"] == "7770401091" and d["uncertain"] == ["buyer_kpp"]     # 10 цифр — проверить глазами
    assert "kpp" in _codes(upd.check(d, today=TODAY, electronic=False), "warning")
    assert d["totals"] == {"without_vat": 86500.0, "vat": 19030.0, "with_vat": 105530.0}  # лишняя «4» отброшена
    assert [ln["quantity"] for ln in d["lines"]] == [10.0, 1.25]


def test_labels_and_values_in_separate_blocks_use_word_layout():
    with pytest.raises(print_form.PrintFormError):
        print_form.parse_text("документ без нужных строк")
    merged = print_form.parse_variants([(OCR_BLOCKS, OCR_BLOCKS_LAYOUT), (OCR_BLOCKS_LAYOUT, OCR_BLOCKS_LAYOUT)])
    assert merged["number"] == "УТ-215" and merged["seller"]["inn"] == "5001007329"
    assert merged["buyer"]["name"] == "ООО «Ромашка-Строй»" and len(merged["lines"]) == 1
    assert merged["totals"]["with_vat"] == 6100.0


@pytest.mark.parametrize("tokens, joinable, expected", [
    (["10", "500,00", "5", "000,00"], True, (10.0, 500.0, 5000.0, True)),
    (["1,250", "64", "000,00", "80", "000,00"], True, (1.25, 64000.0, 80000.0, True)),
    (["1", "1500,00", "1500,00"], True, (1.0, 1500.0, 1500.0, True)),
    (["10", "500,00", "5000,00"], False, (10.0, 500.0, 5000.0, True)),
    (["12", "345,00", "1", "234", "567,80"], True, (12.0, 345.0, 1234567.8, False)),
    (["500,00"], True, (None, None, 500.0, False)),
    ([], True, (None, None, None, False)),
])
def test_number_solver_uses_arithmetic(tokens, joinable, expected):
    assert print_form.solve_left(tokens, joinable) == expected


@pytest.mark.parametrize("raw, expected", [
    ("14 октября 2026 г.", date(2026, 10, 14)), ("14.10.2026", date(2026, 10, 14)), ("14.10.26", date(2026, 10, 14)),
    ("3 мая 2026", date(2026, 5, 3)), ("31 февраля 2026 г.", None), ("14 гыгы 2026", None), ("", None),
])
def test_dates_in_words_and_digits(raw, expected):
    assert print_form.parse_date(raw) == expected


def test_line_without_vat_and_fraction_rate():
    no_vat = print_form.parse_line("1 Монтаж перегородок -- 796 шт 2 1 000,00 2 000,00 без акциза без НДС без НДС 2 000,00")
    assert (no_vat["vat_rate"], no_vat["vat_sum"], no_vat["sum_with_vat"], no_vat["quantity"]) == ("none", 0.0, 2000.0, 2.0)
    fraction = print_form.parse_line("2 Предоплата -- -- -- 6 100,00 X 22/122 1 100,00 6 100,00")
    assert fraction["vat_rate"] == "22/122" and fraction["sum_without_vat"] == 6100.0
    assert print_form.parse_line("Всего к оплате (9) 86 500,00 X 19 030,00 105 530,00") is None
    assert print_form.parse_line("Руководитель организации ____ Тестов Т. Т.") is None


@pytest.mark.parametrize("line, expected", [
    ("- 3 | доставка до объекта 796 шт - 1 1500,00 1500.00 к. 22% 330,00 1830.00",
     (3, "", "доставка до объекта", "796", "шт", 1.0)),                         # прочерк после единицы
    ("-  3 | доставка до объекта  796  шт  1  1500,00  1500.00  к.  22%  330,00  1830.00",
     (3, "", "доставка до объекта", "796", "шт", 1.0)),                         # номер слипся с наименованием
    ("Set 1 | Цемент M500, мешок 50 кг 796  шт  10  500,00  5000,00  aera  22%  1100,00  6100,00",
     (1, "Set", "Цемент M500, мешок 50 кг", "796", "шт", 10.0)),                # ОКЕИ в ячейке наименования
    ("1  Услуга монтажа  --  --  усл. ед  2  1 000,00  2 000,00  без  22%  440,00  2 440,00",
     (1, "", "Услуга монтажа", "", "усл. ед", 2.0)),                            # единица из двух слов
    ("2 Арматура A500C 912 мм ” — 168 т 1,250 64 000,00 80 000,00 аа 22% 17 600,00 97 600,00",
     (2, "", "Арматура A500C 912 мм", "168", "т", 1.25)),                       # мусор OCR в хвосте имени
    ("4 Песок речной 3 1 500,00 4 500,00 без 22% 990,00 5 490,00",
     (4, "", "Песок речной", "", "", 3.0)),                                     # «речной» — не единица
    ("5 Щебень 20-40 006 um 2 1 000,00 2 000,00 без 22% 440,00 2 440,00",
     (5, "", "Щебень 20-40", "006", "um", 2.0)),                                # нечитаемая единица после ОКЕИ
])
def test_line_head_variants(line, expected):
    r = print_form.parse_line(line)
    assert (r["position"], r["item_code"], r["name"], r["unit_code"], r["unit_name"], r["quantity"]) == expected


@pytest.mark.parametrize("text, message", [
    ("ТОВАРНАЯ НАКЛАДНАЯ ТОРГ-12 Поставщик ...", "товарная накладная"),
    ("Акт выполненных работ № 5 от 01.10.2026 ... акт ...", "Это акт"),
    ("Счет на оплату № 77 от 01.10.2026 Поставщик ...", "счёт на оплату"),
    ("Просто письмо без реквизитов", "Не похоже на УПД"),
])
def test_other_documents_are_explained(text, message):
    with pytest.raises(print_form.PrintFormError, match=message):
        print_form.parse_text(text)


def test_pdf_limits_and_scans_without_text():
    from pypdf import PdfWriter
    with pytest.raises(print_form.PrintFormError, match="не PDF"):
        print_form.pdf_text(b"GIF89a")
    with pytest.raises(print_form.PrintFormError, match="не читается"):
        print_form.pdf_text(b"%PDF-1.4 broken")
    many = PdfWriter()
    for _ in range(print_form.MAX_PDF_PAGES + 1):
        many.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    many.write(buf)
    with pytest.raises(print_form.PrintFormError, match="страниц"):
        print_form.pdf_text(buf.getvalue())
    blank = PdfWriter()
    blank.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    blank.write(buf)
    plain, _layout, pages = print_form.pdf_text(buf.getvalue())
    assert pages == 1 and not print_form.has_text(plain)


# ------------------------------------------------------------------ обработчик OCR (поддельный движок)
def _png(width: int, height: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (width, height), 255).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def fake_tesserocr(monkeypatch):
    """Поддельный tesserocr: «читает» печатную форму, только если лист альбомный (ширина > высоты)."""
    module = types.ModuleType("tesserocr")
    module.__version__ = "0.0-test"
    module.tesseract_version = lambda: "tesseract 5.5.3-test\n"
    module.PSM = types.SimpleNamespace(AUTO=3)
    module.RIL = types.SimpleNamespace(WORD="word", TEXTLINE="line")
    seen = []

    class Item:
        def __init__(self, text, box):
            self.text, self.box = text, box

        def GetUTF8Text(self, _level):  # noqa: N802 — имена как у tesserocr
            return self.text

        def BoundingBox(self, _level):  # noqa: N802
            return self.box

        def Baseline(self, _level):  # noqa: N802
            return ((self.box[0], self.box[3]), (self.box[2], self.box[3]))

    class Api:
        def __init__(self, path, lang, psm):
            self.args = (path, lang, psm)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def SetImage(self, img):  # noqa: N802
            self.img = img
            seen.append(img.size)

        def Recognize(self):  # noqa: N802
            self.upright = self.img.width > self.img.height

        def GetIterator(self):  # noqa: N802
            return self

        def GetUTF8Text(self):  # noqa: N802
            return OCR_SCAN if self.upright else "эм ихаелроц 00‘0 ‘LL"

        def MeanTextConf(self):  # noqa: N802
            return 88 if self.upright else 57

    def iterate_level(api, level):
        if level == "line":
            return [Item("строка", (10, 10, 900, 30))]
        words = (["Счет-фактура", "№", "УТ-215"] if api.upright else ["эм", "ихаелроц"])
        return [Item(w, (10 + 120 * i, 10, 100 + 120 * i, 30)) for i, w in enumerate(words)]

    module.PyTessBaseAPI = Api
    module.iterate_level = iterate_level
    monkeypatch.setitem(sys.modules, "tesserocr", module)
    return seen


@pytest.mark.usefixtures("fake_tesserocr")
def test_worker_reads_image_and_rotates_portrait_scan(tmp_path):
    landscape = tmp_path / "a.png"
    landscape.write_bytes(_png(1600, 1100))
    out = ocr_worker.main([str(landscape), str(tmp_path), "rus+eng", "5", "1"])
    assert out["ok"] and out["engine"] == "Tesseract 5.5.3-test (tesserocr 0.0-test)"
    assert out["pages"][0]["angle"] == 0 and "УТ-215" in out["pages"][0]["text"]
    assert base64.b64decode(out["preview"])[:3] == b"\xff\xd8\xff" and out["peak_mb"] > 0
    portrait = tmp_path / "p.png"
    portrait.write_bytes(_png(1100, 1600))
    turned = ocr_worker.main([str(portrait), str(tmp_path), "rus+eng", "5", "0"])
    assert turned["pages"][0]["angle"] in (90, 270) and turned["preview"] is None
    assert "Счет-фактура" in turned["pages"][0]["text"]


def test_worker_renders_scanned_pdf(tmp_path, fake_tesserocr):
    from PIL import Image
    pdf = tmp_path / "scan.pdf"
    Image.new("RGB", (1650, 1170), "white").save(pdf, resolution=200)
    out = ocr_worker.main([str(pdf), str(tmp_path), "rus+eng", "5", "1"])
    assert out["ok"] and len(out["pages"]) == 1 and fake_tesserocr[-1][0] > fake_tesserocr[-1][1]


@pytest.mark.usefixtures("fake_tesserocr")
def test_worker_limits(tmp_path, monkeypatch):
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"\x89PNG not an image")
    with pytest.raises(ocr_worker.WorkerError, match="не открывается"):
        ocr_worker.main([str(junk), str(tmp_path), "rus", "5", "0"])
    monkeypatch.setattr(ocr_worker, "MAX_PIXELS", 10_000)
    big = tmp_path / "big.png"
    big.write_bytes(_png(400, 400))
    with pytest.raises(ocr_worker.WorkerError, match="слишком большая"):
        ocr_worker.main([str(big), str(tmp_path), "rus", "5", "0"])
    from PIL import Image
    assert max(ocr_worker.prepare(Image.new("L", (7000, 5000))).size) == ocr_worker.MAX_SIDE
    assert max(ocr_worker.prepare(Image.new("L", (600, 400))).size) == 1000


def test_layout_rebuild_joins_label_and_value_columns():
    words = [{"t": "Продавец:", "x0": 100, "y0": 100, "x1": 220, "y1": 120},
             {"t": "ООО", "x0": 900, "y0": 116, "x1": 950, "y1": 136},     # лист наклонён: значение ниже
             {"t": "«СтройБаза»", "x0": 956, "y0": 116, "x1": 1100, "y1": 136},
             {"t": "Покупатель:", "x0": 100, "y0": 160, "x1": 240, "y1": 180}]
    slope = ocr_worker.median_slope([(100, 110, 1100, 126)])
    assert slope == pytest.approx(0.016)
    assert ocr_worker.build_layout(words, slope) == "Продавец:  ООО «СтройБаза»\nПокупатель:"
    assert ocr_worker.build_layout(words, 0.0).splitlines()[0] == "Продавец:"     # без поправки — разъехались бы
    assert ocr_worker.build_layout([], 0.0) == ""


# ------------------------------------------------------------------ ocr.py: состояние, модели, запуск
def test_status_explains_what_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "TESSDATA_DIR", tmp_path)
    monkeypatch.setattr(ocr, "SYSTEM_TESSDATA", ())
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    monkeypatch.setattr(ocr, "_engine", lambda: "")
    assert not ocr.status()["ready"] and "tesserocr" in ocr.status()["reason"]
    monkeypatch.setattr(ocr, "_engine", lambda: "tesserocr")
    state = ocr.status()
    assert not state["ready"] and state["missing"] == ["rus", "eng"] and "Скачать модели" in state["reason"]
    for lang in ("rus", "eng"):
        (tmp_path / f"{lang}.traineddata").write_bytes(b"x")
    assert ocr.status()["ready"] and ocr.status()["tessdata"] == str(tmp_path)
    with pytest.raises(ocr.OcrUnavailable):
        monkeypatch.setattr(ocr, "_engine", lambda: "")
        ocr.recognize(b"%PDF", ".pdf")


def test_models_are_verified_by_hash(monkeypatch, tmp_path):
    body = b"traineddata-test"
    monkeypatch.setattr(ocr, "TESSDATA_DIR", tmp_path)
    monkeypatch.setattr(ocr, "SYSTEM_TESSDATA", ())
    monkeypatch.setattr(ocr, "MODELS", {"tst": {"sha1": ocr.git_blob_sha1(body), "size": len(body)}})
    calls = []

    def flaky(url, _headers):
        calls.append(url)
        if "api.github.com" in url:
            raise OSError("нет сети")
        return body

    log = ocr.download_models(fetch=flaky)["log"]
    assert (tmp_path / "tst.traineddata").read_bytes() == body and log == ["tst: скачано с raw.githubusercontent.com"]
    assert "git/blobs/" + ocr.git_blob_sha1(body) in calls[0]
    assert ocr.download_models(fetch=flaky)["log"] == ["tst: уже есть"]
    (tmp_path / "tst.traineddata").unlink()
    with pytest.raises(ocr.OcrError, match="не совпал по хешу"):
        ocr.download_models(fetch=lambda url, headers: b"poisoned-model!")
    assert not (tmp_path / "tst.traineddata").exists() and not list(tmp_path.glob(".tst.*"))
    assert ocr.git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"     # как у git hash-object
    assert set(ocr.MODELS) == {"tst"}


def _ready(monkeypatch, tmp_path, script: str):
    worker = tmp_path / "fake_worker.py"
    worker.write_text(script, encoding="utf-8")
    monkeypatch.setattr(ocr, "WORKER", worker)
    monkeypatch.setattr(ocr, "LOCK_FILE", tmp_path / "ocr.lock")
    monkeypatch.setattr(ocr, "LAST_FILE", tmp_path / "ocr_last.json")
    monkeypatch.setattr(ocr, "status", lambda: {"ready": True, "tessdata": str(tmp_path), "reason": ""})


def test_recognize_runs_isolated_worker_and_remembers_stats(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path, "import json, os, sys\n"
           "data = open(sys.argv[1], 'rb').read()\n"
           "print(json.dumps({'ok': True, 'engine': 'test', 'pages': [{'text': str(len(data)), 'layout': '',"
           " 'confidence': 90, 'angle': 0}], 'preview': None, 'seconds': 0.1, 'peak_mb': 42,"
           " 'omp': os.environ.get('OMP_THREAD_LIMIT'), 'args': sys.argv[2:]}))\n")
    answer = ocr.recognize(b"0123456789", ".png")
    assert answer["pages"][0]["text"] == "10" and answer["omp"] == "1"
    assert answer["args"] == [str(tmp_path), "rus+eng", "5", "0"]
    last = json.loads((tmp_path / "ocr_last.json").read_text(encoding="utf-8"))
    assert last["peak_mb"] == 42 and last["pages"] == 1 and last["engine"] == "test"


def test_recognize_failures_are_explained(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path, "print('{\"ok\": false, \"error\": \"Картинка не открывается\"}')\n")
    with pytest.raises(ocr.OcrError, match="Картинка не открывается"):
        ocr.recognize(b"x", ".png")
    _ready(monkeypatch, tmp_path, "print('not json')\n")
    with pytest.raises(ocr.OcrError, match="сбоем"):
        ocr.recognize(b"x", ".png")
    _ready(monkeypatch, tmp_path, "import time\ntime.sleep(5)\n")
    monkeypatch.setattr(ocr, "TIMEOUT", 1)
    with pytest.raises(ocr.OcrError, match="не уложилось"):
        ocr.recognize(b"x", ".png")


def test_one_document_at_a_time(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path, "print('{\"ok\": true, \"pages\": []}')\n")
    entered, release = threading.Event(), threading.Event()

    def hold():
        with ocr._one_at_a_time(5):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ocr.OcrBusy, match="другой документ"):
            ocr.recognize(b"x", ".png", wait=0.3)
    finally:
        release.set()
        thread.join(5)
    assert ocr.recognize(b"x", ".png")["pages"] == []


# ------------------------------------------------------------------ API: черновик → подтверждение
def _recognize(client, hdr, name: str, body: bytes, company_id: str | None = None):
    data = {"company_id": company_id} if company_id else None
    return client.post("/api/v1/primary/recognize", headers=hdr, files={"file": (name, body, "application/pdf")},
                       data=data)


def _fake_ocr(monkeypatch, text: str = OCR_SCAN):
    def fake(_data, _suffix, *, preview=False, **_options):
        return {"ok": True, "engine": "Tesseract test", "pages": [{"text": text, "layout": "", "confidence": 80,
                                                                   "angle": 0}],
                "preview": base64.b64encode(b"\xff\xd8\xff").decode() if preview else None,
                "stats": {"seconds": 1.5, "peak_mb": 90}}
    monkeypatch.setattr(service.ocr, "recognize", fake)


def test_pdf_draft_then_confirm_then_duplicate(client, firms):  # noqa: F811
    _, a, _ = firms
    res = _recognize(client, a["hdr"], "upd.pdf", FIXTURE.read_bytes())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "pdf" and body["ocr"] is None and body["company_id"] == a["id"]
    assert body["draft"]["number"] == "УТ-215" and body["draft"]["date"] == "2026-10-14"
    assert body["file_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert "buyer_company" in _codes(body["issues"], "warning")       # покупатель в образце — не эта компания
    assert client.get("/api/v1/primary?q=УТ-215", headers=a["hdr"]).json()["total"] == 0   # база не тронута
    draft = dict(body["draft"], buyer=dict(body["draft"]["buyer"], inn=a["inn"]))
    payload = {"source": "pdf", "token": body["token"], "file_name": "upd.pdf",
               "file_sha256": body["file_sha256"], "draft": draft}
    saved = client.post("/api/v1/primary/confirm", headers=a["hdr"], json=payload)
    assert saved.status_code == 200, saved.text
    doc = saved.json()["document"]
    assert saved.json()["result"] == "created" and doc["source"] == "pdf" and doc["status"] == "ok"
    assert doc["company_id"] == a["id"] and doc["lines_count"] == 3 and doc["total_with_vat"] == 105530.0
    detail = client.get(f"/api/v1/primary/{doc['id']}", headers=a["hdr"]).json()
    assert "source_pdf" in _codes(detail["issues"], "info") and detail["file_name"] == "upd.pdf"
    again = client.post("/api/v1/primary/confirm", headers=a["hdr"], json=payload).json()
    assert again["result"] == "duplicate" and again["document"]["id"] == doc["id"]
    assert _recognize(client, a["hdr"], "upd.pdf", FIXTURE.read_bytes()).json()["duplicate"]["id"] == doc["id"]


def test_confirm_refuses_forged_or_foreign_tokens(client, firms):  # noqa: F811
    _, a, b = firms
    body = _recognize(client, a["hdr"], "upd.pdf", FIXTURE.read_bytes()).json()
    base = {"source": "pdf", "token": body["token"], "file_sha256": body["file_sha256"], "draft": body["draft"]}
    token = body["token"]
    # v1.70.98: последний знак меняется всегда (раньше с вероятностью 1/16 «подделка» совпадала с настоящим токеном)
    forged = token[:-1] + ("1" if token[-1] == "0" else "0")
    assert forged != token
    for change in ({"file_sha256": "0" * 64}, {"source": "scan"}, {"token": forged}, {"token": "x"}):
        res = client.post("/api/v1/primary/confirm", headers=a["hdr"], json={**base, **change})
        assert res.status_code == 400 and "распознайте файл заново" in res.json()["detail"]
    assert client.post("/api/v1/primary/confirm", headers=b["hdr"], json=base).status_code == 400
    old = service.sign("user", "sha", "pdf", issued=1)
    assert not service.token_ok(old, "user", "sha", "pdf") and service.token_ok(service.sign("u", "s", "pdf"), "u", "s", "pdf")
    too_long = {**base, "draft": {**body["draft"], "lines": [{"name": "x"}] * 501}}
    assert client.post("/api/v1/primary/confirm", headers=a["hdr"], json=too_long).status_code == 422


def test_manual_entry_without_file(client, firms):  # noqa: F811
    _, a, b = firms
    number = f"Р-{uuid4().hex[:6]}"
    draft = {"function": "СЧФДОП", "number": number, "date": "2026-10-20",
             "seller": {"name": "ООО «Песок»", "inn": inn10(), "kpp": "500101001"},
             "buyer": {"name": "Мы", "inn": a["inn"], "kpp": "770401001"},
             "totals": {"without_vat": 1000, "vat": 220, "with_vat": 1220}, "lines": []}
    res = client.post("/api/v1/primary/confirm", headers=a["hdr"], json={"source": "manual", "draft": draft}).json()
    doc = res["document"]
    assert res["result"] == "created" and doc["source"] == "manual" and doc["file_name"] == ""
    assert doc["status"] == "ok" and doc["lines_count"] == 0 and doc["vat_total"] == 220
    assert {"lines_absent", "source_manual"} <= _codes(res["issues"], "info")
    again = client.post("/api/v1/primary/confirm", headers=a["hdr"], json={"source": "manual", "draft": draft}).json()
    assert again["result"] == "duplicate"
    forced = client.post("/api/v1/primary/confirm", headers=b["hdr"],
                         json={"source": "manual", "company_id": a["id"], "draft": dict(draft, number=number + "-b")}).json()
    assert forced["document"]["company_id"] == b["id"]               # бухгалтер пишет только в свою компанию
    broken = dict(draft, number=number + "-x", seller={"name": "ООО «Песок»", "inn": "1234567890", "kpp": ""})
    bad = client.post("/api/v1/primary/confirm", headers=a["hdr"], json={"source": "manual", "draft": broken}).json()
    assert bad["document"]["status"] == "errors" and "inn" in _codes(bad["issues"], "error")


def test_scan_needs_ocr_and_uses_it_when_ready(client, firms, monkeypatch):  # noqa: F811
    _, a, _ = firms
    monkeypatch.setattr(service.ocr, "status", lambda: {"ready": False, "reason": "нет языковых моделей: rus"})
    res = _recognize(client, a["hdr"], "scan.png", _png(50, 50))
    assert res.status_code == 409 and "нет языковых моделей" in res.json()["detail"]
    _fake_ocr(monkeypatch)
    body = _recognize(client, a["hdr"], "scan.png", _png(50, 50)).json()
    assert body["source"] == "scan" and body["ocr"]["engine"] == "Tesseract test" and body["preview"] is None
    assert body["draft"]["seller"]["inn"] == "5001007329" and len(body["draft"]["lines"]) == 3
    from pypdf import PdfWriter
    blank = PdfWriter()
    blank.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    blank.write(buf)
    scanned = _recognize(client, a["hdr"], "scan.pdf", buf.getvalue()).json()
    assert scanned["source"] == "scan" and scanned["preview"].startswith("data:image/jpeg;base64,")
    payload = {"source": "scan", "token": body["token"], "file_name": "scan.png", "file_sha256": body["file_sha256"],
               "draft": body["draft"]}
    assert client.post("/api/v1/primary/confirm", headers=a["hdr"], json=payload).json()["document"]["source"] == "scan"


def test_recognize_rejects_wrong_input(client, firms, monkeypatch):  # noqa: F811
    _, a, _ = firms
    res = _recognize(client, a["hdr"], "upd.xml", upd_xml())
    assert res.status_code == 400 and "XML и ZIP" in res.json()["detail"]
    assert _recognize(client, a["hdr"], "empty.pdf", b"").status_code == 400
    monkeypatch.setattr("app.primary.router.MAX_ZIP_BYTES", 100)
    assert _recognize(client, a["hdr"], "big.pdf", b"%PDF" + b"0" * 200).status_code == 400
    monkeypatch.undo()
    res = _recognize(client, a["hdr"], "torg.pdf", b"%PDF-1.4 broken")
    assert res.status_code == 422 and "не читается" in res.json()["detail"]
    monkeypatch.setattr(service.ocr, "recognize", lambda *a_, **k: (_ for _ in ()).throw(ocr.OcrBusy("занято")))
    monkeypatch.setattr(service.ocr, "status", lambda: {"ready": True})
    busy = _recognize(client, a["hdr"], "scan.png", _png(20, 20))
    assert busy.status_code == 429 and busy.json()["detail"] == "занято"


def test_hints_from_previous_documents(client, firms, monkeypatch):  # noqa: F811
    _, a, _ = firms
    xml = upd_xml(buyer=("ООО «Покупатель»", a["inn"], "770401001"), number=f"H-{uuid4().hex[:6]}")
    assert _upload(client, a["hdr"], ("prev.xml", xml)).json()["created"] == 1
    _fake_ocr(monkeypatch, OCR_SCAN.replace("ООО «СтройБаза» (2)", "СтроиБаза (2)").replace("7704123450", a["inn"]))
    body = _recognize(client, a["hdr"], "scan.png", _png(30, 30)).json()
    assert body["draft"]["seller"]["name"] == "СтроиБаза"
    assert body["hints"]["seller"] == {"name": "ООО «СтройБаза»", "kpp": "500101001", "from": "прошлые документы"}
    assert body["hints"]["buyer"]["from"] == "карточка компании"


def test_roles_for_new_endpoints(client, firms, monkeypatch):  # noqa: F811
    hdr, a, _ = firms
    user = _mk_user(client, hdr, a["id"], f"prim_r_{uuid4().hex[:10]}", role="user")
    assert _recognize(client, user, "u.pdf", FIXTURE.read_bytes()).status_code == 403
    assert client.post("/api/v1/primary/confirm", headers=user, json={"source": "manual", "draft": {}}).status_code == 403
    assert client.get("/api/v1/primary/ocr", headers=user).status_code == 403
    state = client.get("/api/v1/primary/ocr", headers=a["hdr"]).json()
    assert {"ready", "engine", "missing", "reason", "last", "max_pages"} <= set(state)
    assert client.post("/api/v1/primary/ocr/models", headers=a["hdr"]).status_code == 403
    monkeypatch.setattr(ocr, "download_models", lambda: {"ready": True, "log": ["rus: уже есть"]})
    assert client.post("/api/v1/primary/ocr/models", headers=hdr).json()["log"] == ["rus: уже есть"]

    def offline():
        raise ocr.OcrError("Модель rus не скачана")
    monkeypatch.setattr(ocr, "download_models", offline)
    res = client.post("/api/v1/primary/ocr/models", headers=hdr)
    assert res.status_code == 424 and "не скачана" in res.json()["detail"]


# ------------------------------------------------------------------ база: колонка source
def test_source_column_is_added_to_existing_table(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    import app.database as database
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:     # таблица в виде 1.70.94 — без source
        conn.execute(text("CREATE TABLE primary_documents (id VARCHAR(36) PRIMARY KEY, doc_number VARCHAR(100))"))
        conn.execute(text("INSERT INTO primary_documents (id, doc_number) VALUES ('d1', 'УТ-1')"))
    monkeypatch.setattr(database, "engine", eng)
    database._ensure_schema()
    assert "source" in {c["name"] for c in inspect(eng).get_columns("primary_documents")}
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO primary_documents (id, doc_number) VALUES ('d2', 'УТ-2')"))   # как старая версия
        assert conn.execute(text("SELECT source FROM primary_documents ORDER BY id")).scalars().all() == ["xml", "xml"]
    database._ensure_schema()      # повторный запуск ничего не ломает
    eng.dispose()
