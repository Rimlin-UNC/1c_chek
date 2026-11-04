"""Электронный УПД и счёт-фактура в формате ФНС (5.01–5.03): разбор и проверки (T-53).

Файл — недоверенный ввод: разбор через defusedxml (DTD и внешние сущности запрещены),
предел размера, коды маркировки не читаются. Имена реквизитов 5.01 и 5.03 различаются
(НомерСчФ → НомерДок, ДатаСчФ → ДатаДок, ИспрСчФ → ИспрДок) — берётся то, что есть.
Чистые функции: байты на входе, словарь на выходе; база не трогается.

Проверки — то, на чём бухгалтеры теряют вычет и время (docs/primary_docs_design.md):
обязательные реквизиты (402-ФЗ, ст. 9), контрольные суммы ИНН и формат КПП, арифметика
строк и итогов, ставка НДС на переходе к 22 % (425-ФЗ, с 01.01.2026), формат 5.03 —
единственный в ЭДО с 01.04.2026 (приказ ФНС ЕД-7-26/970@ в редакции ЕД-7-26/1032@).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from ..services.companies_util import inn_is_valid

MAX_BYTES = 5 * 1024 * 1024
KINDS = {"СЧФ": "Счёт-фактура", "СЧФДОП": "УПД", "ДОП": "УПД"}
CORRECTIONS = frozenset({"КСЧФ", "КСЧФДИС", "ДИС"})
KNOWN_VERSIONS = frozenset({"5.01", "5.02", "5.03"})
ONLY_503_FROM = date(2026, 4, 1)
VAT_22_FROM = date(2026, 1, 1)

# Ставка из реквизита НалСт → вид ставки, как fns_refs.vat_canonical
_VAT_TEXT = {"22%": "22", "20%": "20", "18%": "18", "10%": "10", "7%": "7", "5%": "5", "0%": "0",
             "безндс": "none", "22/122": "22/122", "20/120": "20/120", "18/118": "18/118",
             "10/110": "10/110", "7/107": "7/107", "5/105": "5/105",
             "ндсисчисляетсяналоговымагентом": "agent"}
_PERCENT = {"22": 22, "20": 20, "18": 18, "10": 10, "7": 7, "5": 5, "0": 0}
_FRACTION = {"22/122": (22, 122), "20/120": (20, 120), "18/118": (18, 118), "10/110": (10, 110),
             "7/107": (7, 107), "5/105": (5, 105)}
_KPP = re.compile(r"^\d{4}[\dA-Z]{2}\d{3}$")


class UpdError(ValueError):
    """Файл нельзя прочитать как УПД или счёт-фактуру — сообщение для пользователя."""


# ------------------------------------------------------------------ разбор
def _local(tag) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _kids(element, name: str) -> list:
    return [] if element is None else [k for k in element if _local(k.tag) == name]


def _kid(element, name: str):
    found = _kids(element, name)
    return found[0] if found else None


def _attr(element, *names: str) -> str:
    if element is None:
        return ""
    for name in names:
        value = (element.get(name) or "").strip()
        if value:
            return value
    return ""


def _date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date() if raw else None
    except ValueError:
        return None


def _num(raw: str | None) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(Decimal(str(raw).replace(",", ".").replace(" ", "")))
    except (InvalidOperation, ValueError):
        return None


def vat_rate(raw: str) -> str:
    """НалСт («22%», «без НДС», «22/122») → вид ставки; неизвестное — как есть."""
    key = (raw or "").strip().lower().replace(" ", "").replace("\u00a0", "")
    return _VAT_TEXT.get(key, (raw or "").strip() or "none")


def _amount(element, name: str) -> float | None:
    """Сумма налога: <СумНал><СумНал>183.00</СумНал></СумНал> или <БезНДС> (тогда 0)."""
    node = _kid(element, name)
    if node is None:
        return None
    inner = _kid(node, "СумНал")
    if inner is not None:
        return _num(inner.text)
    if _kid(node, "БезНДС") is not None:
        return 0.0
    return _num(node.text)


def _party(element) -> dict:
    ident = _kid(element, "ИдСв")
    org = _kid(ident, "СвЮЛУч")
    if org is not None:
        return {"kind": "ЮЛ", "name": _attr(org, "НаимОрг"), "inn": re.sub(r"\D", "", _attr(org, "ИННЮЛ")),
                "kpp": _attr(org, "КПП").upper()}
    person = _kid(ident, "СвИП")
    if person is not None:
        fio = _kid(person, "ФИО")
        name = " ".join(p for p in (_attr(fio, "Фамилия"), _attr(fio, "Имя"), _attr(fio, "Отчество")) if p)
        return {"kind": "ИП", "name": f"ИП {name}".strip(), "inn": re.sub(r"\D", "", _attr(person, "ИННФЛ")),
                "kpp": ""}
    foreign = _kid(ident, "СвИнНеУч")
    if foreign is not None:
        return {"kind": "ИН", "name": _attr(foreign, "НаимОрг"), "inn": "", "kpp": ""}
    return {"kind": "", "name": "", "inn": "", "kpp": ""}


def _line(element, index: int) -> dict:
    extra = _kid(element, "ДопСведТов")
    raw_rate = element.get("НалСт", "")
    try:
        position = int(element.get("НомСтр") or index)
    except ValueError:
        position = index
    return {
        "position": position,
        "name": (element.get("НаимТов") or "").strip(),
        "unit_code": _attr(element, "ОКЕИ_Тов"),
        "unit_name": _attr(element, "НаимЕдИзм") or _attr(extra, "НаимЕдИзм"),
        "quantity": _num(element.get("КолТов")),
        "price": _num(element.get("ЦенаТов")),
        "sum_without_vat": _num(element.get("СтТовБезНДС")),
        "vat_rate": vat_rate(raw_rate),
        "vat_raw": raw_rate,
        "vat_sum": _amount(element, "СумНал"),
        "sum_with_vat": _num(element.get("СтТовУчНал")),
        "item_code": _attr(extra, "КодТов", "АртикулТов"),
    }


def parse(data: bytes) -> dict:
    """Разобрать XML УПД или счёта-фактуры. Не документ ФНС — UpdError."""
    if len(data) > MAX_BYTES:
        raise UpdError("Файл больше 5 МБ — это не похоже на один документ")
    try:
        root = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise UpdError("Это не XML-документ ЭДО или файл повреждён") from exc
    if _local(root.tag) != "Файл":
        raise UpdError("Корневой элемент не «Файл» — это не документ в формате ФНС")
    doc = _kid(root, "Документ")
    if doc is None:
        raise UpdError("В файле нет элемента «Документ»")
    info = _kid(doc, "СвСчФакт")
    correction = _kid(info, "ИспрДок")     # 5.03; в 5.01 — ИспрСчФ (элемент без детей ложен — только is None)
    if correction is None:
        correction = _kid(info, "ИспрСчФ")
    table = _kid(doc, "ТаблСчФакт")
    totals = _kid(table, "ВсегоОпл")
    function = (doc.get("Функция") or "").strip()
    return {
        "file_id": (root.get("ИдФайл") or "").strip(),
        "format_version": (root.get("ВерсФорм") or "").strip(),
        "function": function,
        "kind": KINDS.get(function, "Документ"),
        "number": _attr(info, "НомерДок", "НомерСчФ"),
        "date": _date(_attr(info, "ДатаДок", "ДатаСчФ")),
        "currency": _attr(info, "КодОКВ") or _attr(_kid(info, "ДенИзм"), "КодОКВ") or "643",
        "correction_number": _attr(correction, "НомИспр", "НомИспрСчФ"),
        "correction_date": _date(_attr(correction, "ДатаИспр", "ДатаИспрСчФ")),
        "seller": _party(_kid(info, "СвПрод")),
        "buyer": _party(_kid(info, "СвПокуп")),
        "lines": [_line(el, i) for i, el in enumerate(_kids(table, "СведТов"), 1)],
        "totals": {"without_vat": _num(_attr(totals, "СтТовБезНДСВсего")),
                   "with_vat": _num(_attr(totals, "СтТовУчНалВсего")),
                   "vat": _amount(totals, "СумНалВсего")},
    }


# ------------------------------------------------------------------ проверки
def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _close(a: float | None, b: float | None, tolerance: float) -> bool:
    return a is None or b is None or abs(a - b) <= tolerance + 1e-9


def check(doc: dict, *, company_inn: str = "", today: date | None = None) -> list[dict]:
    """Замечания: [{level: error|warning|info, code, message}] — по-русски, для бухгалтера."""
    today = today or date.today()
    issues: list[dict] = []

    def add(level: str, code: str, message: str) -> None:
        issues.append({"level": level, "code": code, "message": message})

    function, version, day = doc["function"], doc["format_version"], doc["date"]
    if doc["file_id"].upper().startswith(("ON_NSCHFDOPPOK", "ON_NKORSCHFDOPPOK")):
        add("error", "buyer_title", "Это титул покупателя — загрузите файл продавца (ON_NSCHFDOPPR)")
    if function in CORRECTIONS:
        add("error", "correction_doc", "Корректировочный документ (КСЧФ, ДИС) пока не поддерживается")
    elif function not in KINDS:
        add("error", "function", f"Функция документа «{function or 'не указана'}» не поддерживается")
    if version not in KNOWN_VERSIONS:
        add("warning", "version", f"Версия формата «{version or 'не указана'}» неизвестна — проверьте документ")
    elif version != "5.03" and day and day >= ONLY_503_FROM:
        add("warning", "version_503",
            f"С 01.04.2026 в ЭДО принимается только формат 5.03, а документ — в формате {version}")
    if not doc["number"]:
        add("error", "number", "Нет номера документа")
    if not day:
        add("error", "date", "Нет даты документа или она в неверном виде")
    elif day > today + timedelta(days=1):
        add("warning", "date_future", f"Дата документа {day:%d.%m.%Y} — в будущем: проверьте год")
    seller, buyer = doc["seller"], doc["buyer"]
    if not seller["name"] or (seller["kind"] != "ИН" and not seller["inn"]):
        add("error", "seller", "Нет сведений о продавце: наименование и ИНН")
    if function in ("СЧФ", "СЧФДОП") and (not buyer["name"] or (buyer["kind"] != "ИН" and not buyer["inn"])):
        add("error", "buyer", "Нет сведений о покупателе: наименование и ИНН")
    for role, party in (("продавца", seller), ("покупателя", buyer)):
        if party["inn"] and not inn_is_valid(party["inn"]):
            add("error", "inn", f"ИНН {role} {party['inn']} — неверная контрольная сумма")
        if party["kpp"] and len(party["inn"]) == 10 and not _KPP.match(party["kpp"]):
            add("warning", "kpp", f"КПП {role} {party['kpp']} — неверный формат")
        if function in ("СЧФ", "СЧФДОП") and len(party["inn"]) == 10 and not party["kpp"]:
            add("warning", "kpp_missing", f"Нет КПП {role}: в счёте-фактуре он обязателен для организации")
    if seller["inn"] and seller["inn"] == buyer["inn"]:
        add("warning", "same_party", f"Продавец и покупатель — одна организация (ИНН {seller['inn']})")
    if company_inn and buyer["inn"] and buyer["inn"] != company_inn:
        add("warning", "buyer_company",
            f"Покупатель в документе — другая организация (ИНН {buyer['inn']}), а не ваша (ИНН {company_inn})")
    if doc["correction_date"] and day and doc["correction_date"] < day:
        add("warning", "correction_date", "Дата исправления раньше даты самого документа")
    if doc["correction_number"]:
        when = doc["correction_date"].strftime("%d.%m.%Y") if doc["correction_date"] else "без даты"
        add("info", "correction", f"Исправление № {doc['correction_number']} от {when} — заменяет первоначальный документ")

    lines = doc["lines"]
    if not lines:
        add("error", "lines", "В документе нет строк с товарами, работами или услугами")
    old_rate = new_rate = False
    for ln in lines:
        n, base, vat, total = ln["position"], ln["sum_without_vat"], ln["vat_sum"], ln["sum_with_vat"]
        if not ln["name"]:
            add("error", "line_name", f"Строка {n}: нет наименования")
        if ln["quantity"] is not None and ln["price"] is not None and base is not None:
            product = round(ln["quantity"] * ln["price"], 2)
            if not _close(product, base, max(0.01, abs(ln["quantity"]) * 0.005)):
                add("warning", "line_product",
                    f"Строка {n}: количество × цена = {_money(product)}, а стоимость без НДС — {_money(base)}")
        rate = ln["vat_rate"]
        if rate in _PERCENT and base is not None and vat is not None:
            expected = round(base * _PERCENT[rate] / 100, 2)
            if not _close(expected, vat, 0.01):
                add("warning", "line_vat", f"Строка {n}: НДС {_money(vat)}, а {rate} % от {_money(base)} — {_money(expected)}")
        elif rate in _FRACTION and total is not None and vat is not None:
            top, bottom = _FRACTION[rate]
            expected = round(total * top / bottom, 2)
            if not _close(expected, vat, 0.01):
                add("warning", "line_vat", f"Строка {n}: НДС {_money(vat)}, а по ставке {rate} — {_money(expected)}")
        elif rate == "none" and vat:
            add("warning", "line_vat", f"Строка {n}: «без НДС», но сумма налога {_money(vat)}")
        if base is not None and total is not None and not _close(base + (vat or 0), total, 0.01):
            add("warning", "line_total",
                f"Строка {n}: стоимость с НДС {_money(total)}, а без НДС плюс налог — {_money(base + (vat or 0))}")
        old_rate |= rate in ("20", "20/120")
        new_rate |= rate in ("22", "22/122")
    if day and day >= VAT_22_FROM and old_rate:
        add("warning", "vat_20_in_2026",
            f"Ставка 20 % в документе от {day:%d.%m.%Y}: с 01.01.2026 основная ставка — 22 %. "
            "Допустимо для аванса, полученного до 2026 года, — проверьте")
    if day and day < VAT_22_FROM and new_rate:
        add("warning", "vat_22_before_2026", f"Ставка 22 % в документе от {day:%d.%m.%Y} — до 2026 года её не было")

    totals = doc["totals"]
    tolerance = 0.01 + 0.005 * len(lines)
    for key, label, value in (("without_vat", "без НДС", "sum_without_vat"), ("vat", "НДС", "vat_sum"),
                              ("with_vat", "с НДС", "sum_with_vat")):
        parts = [ln[value] for ln in lines if ln[value] is not None]
        if totals[key] is not None and parts and not _close(round(sum(parts), 2), totals[key], tolerance):
            add("warning", f"total_{key}",
                f"Итог {label} {_money(totals[key])}, а сумма строк — {_money(round(sum(parts), 2))}")
    return issues


def status_of(issues: list[dict]) -> str:
    levels = {i["level"] for i in issues}
    return "errors" if "error" in levels else "warnings" if "warning" in levels else "ok"
