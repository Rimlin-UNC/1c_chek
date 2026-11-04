"""Первичка: выгрузка в 1С (T-60, v1.70.96).

Два пути — для разных случаев:

1. **Файл УПД 5.03** (XML по формату ФНС, титул продавца) — для типовой кнопки 1С:Бухгалтерии 3.0
   «Покупки → Поступление (акты, накладные, УПД) → Загрузить → Из файла». Так 1С загружает УПД из
   ЭДО Лайт и от поставщиков без оператора ЭДО: создаёт документ поступления и предлагает
   сопоставить номенклатуру. Программист и доработка 1С не нужны.
   Исходный XML в реестре не хранится (T-53), а у PDF, сканов и ручного ввода его и нет, поэтому
   файл собирается из реквизитов реестра и до выдачи проверяется по официальной схеме ФНС
   (schema.py). Подписанта и адресов в реестре нет: адреса и сведения о передаче в схеме
   необязательны и не пишутся, а обязательный «Подписант» помечен «не указан» с пояснением
   в «ДопСведПодп». Акциз в схеме необязателен — не пишется. Юридической силы файл не имеет:
   это файл обмена для своей 1С, а не документ для контрагента.

2. **Пакет EnterpriseData** (JSON или XML, как у выгрузки чеков) — «ПоступлениеТоваровУслуг» и
   «СчетФактураПолученный» для обработок и расширения 1С (T-52): /onec/v1/primary/*.

Документ, из которого нельзя честно собрать файл (нет номера или даты, у продавца нет ИНН,
отрицательные суммы, неизвестная ставка), не выгружается: причина показывается бухгалтеру.
"""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..config import settings
from ..services.exporter import VAT_1C
from . import schema
from .models import PrimaryDocument

MSK = timezone(timedelta(hours=3))            # Москва без перехода на летнее время с 2014 года
KND = "1115131"
FILE_PREFIX = "ON_NSCHFDOPPR"
# Наименование документа по факту хозяйственной жизни — общая формулировка УПД со статусом 1 и 2
DOC_NAME = "Документ об отгрузке товаров (выполнении работ), передаче имущественных прав (документ об оказании услуг)"
SIGNER_NOTE = "Подписант не указан: файл собран программой «Ямастер Чек» из реестра первички для загрузки в 1С"
VAT_XML = {"0": "0%", "5": "5%", "7": "7%", "10": "10%", "18": "18%", "20": "20%", "22": "22%",
           "5/105": "5/105", "7/107": "7/107", "10/110": "10/110", "18/118": "18/118", "20/120": "20/120",
           "22/122": "22/122", "none": "без НДС", "agent": "НДС исчисляется налоговым агентом"}
CURRENCIES = {"643": "Российский рубль", "840": "Доллар США", "978": "Евро", "156": "Юань",
              "933": "Белорусский рубль", "398": "Тенге"}
_INN10 = re.compile(r"^(?:\d[1-9]|[1-9]\d)\d{8}$")          # шаблоны ИННЮЛТип, ИННФЛТип и КППТип схемы
_INN12 = re.compile(r"^(?:\d[1-9]|[1-9]\d)\d{10}$")
_KPP = re.compile(r"^(?:\d[1-9]|[1-9]\d)\d{2}[0-9A-Z]{2}\d{3}$")
_OKEI = re.compile(r"^\d{3,4}$")
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_IP_PREFIX = re.compile(r"^(?:ИП|Индивидуальный\s+предприниматель)\s+", re.IGNORECASE)


class ExportSkip(ValueError):
    """Документ нельзя выгрузить файлом УПД — причина для бухгалтера."""


def _text(value, limit: int) -> str:
    """Строка для XML: без недопустимых в XML 1.0 символов и лишних пробелов."""
    return " ".join(_BAD_XML.sub("", str(value or "")).split())[:limit]


def _day(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _dec(value, places: int) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExportSkip(f"не число: {value}") from exc
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _money(value) -> str:
    return f"{_dec(value, 2):.2f}"


def _qty(value) -> str:
    text = f"{_dec(value, 11):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fio(name: str) -> dict[str, str] | None:
    parts = _IP_PREFIX.sub("", name).split()
    if len(parts) < 2:
        return None
    out = {"Фамилия": parts[0][:60], "Имя": parts[1][:60]}
    if len(parts) > 2:
        out["Отчество"] = " ".join(parts[2:])[:60]
    return out


def _party(parent: ET.Element, tag: str, name: str, inn: str, kpp: str, who: str) -> None:
    """СвПрод или СвПокуп: юрлицо, ИП или физлицо — по ИНН и наименованию из реестра."""
    name, inn, kpp = _text(name, 1000), (inn or "").strip(), (kpp or "").strip().upper()
    holder = ET.SubElement(ET.SubElement(parent, tag), "ИдСв")
    if len(inn) == 10:
        if not _INN10.match(inn):
            raise ExportSkip(f"ИНН {who} {inn} не по формату")
        if not name:
            raise ExportSkip(f"нет наименования {who}")
        attrs = {"НаимОрг": name, "ИННЮЛ": inn}
        if kpp:
            if not _KPP.match(kpp):
                raise ExportSkip(f"КПП {who} {kpp} не по формату")
            attrs["КПП"] = kpp
        ET.SubElement(holder, "СвЮЛУч", attrs)
        return
    fio = _fio(name)
    if len(inn) == 12:
        if not _INN12.match(inn):
            raise ExportSkip(f"ИНН {who} {inn} не по формату")
        if _IP_PREFIX.match(name) or tag == "СвПрод":
            if fio is None:
                raise ExportSkip(f"у {who}-ИП нет фамилии и имени полностью")
            ET.SubElement(ET.SubElement(holder, "СвИП", {"ИННФЛ": inn}), "ФИО", fio)
            return
        person = ET.SubElement(holder, "СвФЛУч", {"ИННФЛ": inn})
        if fio:
            ET.SubElement(person, "ФИО", fio)
        return
    if inn:
        raise ExportSkip(f"ИНН {who} {inn} не по формату")
    if tag == "СвПрод":
        raise ExportSkip("у продавца нет ИНН (иностранная организация?) — такой документ внесите в 1С вручную")
    if fio is None:
        raise ExportSkip("покупатель не указан")
    ET.SubElement(ET.SubElement(holder, "СвФЛУч"), "ФИО", fio)


def file_id(doc: PrimaryDocument) -> str:
    """Постоянный идентификатор файла: повторная выгрузка даёт тот же ИдФайл."""
    day = doc.doc_date.strftime("%Y%m%d") if doc.doc_date else "00000000"
    return f"{FILE_PREFIX}_{doc.buyer_inn or '0'}_{doc.seller_inn or '0'}_{day}_{doc.id}"


def _line(table: ET.Element, position: int, ln, rate_text: str) -> bool:
    """Строка СведТов. Возвращает True, если у строки НДС есть (не «без НДС»)."""
    attrs = {"НомСтр": str(position)}
    name = _text(ln.name, 2000)
    if name:
        attrs["НаимТов"] = name
    if _OKEI.match((ln.unit_code or "").strip()):
        attrs["ОКЕИ_Тов"] = ln.unit_code.strip()
    unit = _text(ln.unit_name, 255)
    if unit:
        attrs["НаимЕдИзм"] = unit
    for key, value in (("КолТов", ln.quantity), ("ЦенаТов", ln.price), ("СтТовБезНДС", ln.sum_without_vat),
                       ("СтТовУчНал", ln.sum_with_vat), ("СумНал", ln.vat_sum)):
        if value is not None and value < 0:
            raise ExportSkip(f"в строке {position} отрицательная сумма — внесите документ в 1С вручную")
    if ln.quantity:                                # схема: количество строго больше нуля
        attrs["КолТов"] = _qty(ln.quantity)
    if ln.price is not None:
        attrs["ЦенаТов"] = _qty(ln.price)
    if ln.sum_without_vat is not None:
        attrs["СтТовБезНДС"] = _money(ln.sum_without_vat)
    attrs["НалСт"] = rate_text
    if ln.sum_with_vat is not None:
        attrs["СтТовУчНал"] = _money(ln.sum_with_vat)
    # порядок атрибутов не важен для схемы, но так файл читается как образцы ФНС
    order = ("НомСтр", "НаимТов", "ОКЕИ_Тов", "НаимЕдИзм", "КолТов", "ЦенаТов", "СтТовБезНДС", "НалСт", "СтТовУчНал")
    row = ET.SubElement(table, "СведТов", {k: attrs[k] for k in order if k in attrs})
    code = _text(ln.item_code, 100)
    if code:
        ET.SubElement(row, "ДопСведТов", {"КодТов": code})
    vat = ET.SubElement(row, "СумНал")
    if ln.vat_rate == "none":
        ET.SubElement(vat, "БезНДС").text = "без НДС"
        return False
    amount = ln.vat_sum
    if amount is None and ln.sum_with_vat is not None and ln.sum_without_vat is not None:
        amount = round(ln.sum_with_vat - ln.sum_without_vat, 2)
    if amount is None or amount < 0:
        raise ExportSkip(f"в строке {position} нет суммы НДС")
    ET.SubElement(vat, "СумНал").text = _money(amount)
    return True


def upd_xml(doc: PrimaryDocument, now: datetime | None = None) -> tuple[str, bytes]:
    """Документ реестра → (имя файла, XML УПД 5.03 в windows-1251). Нельзя собрать честно — ExportSkip."""
    if doc.function not in ("СЧФ", "СЧФДОП", "ДОП"):
        raise ExportSkip(f"функция документа «{doc.function or 'не указана'}» не выгружается")
    number = _text(doc.doc_number, 1000)
    if not number or not doc.doc_date:
        raise ExportSkip("нет номера или даты документа")
    if not doc.lines:
        raise ExportSkip("в документе нет строк")
    currency = (doc.currency or "643").strip()
    if currency not in CURRENCIES:
        raise ExportSkip(f"валюта с кодом {currency} — внесите документ в 1С вручную")
    stamp = (now or datetime.now(MSK)).astimezone(MSK)
    name = file_id(doc)
    root = ET.Element("Файл", {"ИдФайл": name, "ВерсФорм": "5.03",
                               "ВерсПрог": f"Ямастер Чек {settings.APP_VERSION}"[:40]})
    attrs = {"КНД": KND, "Функция": doc.function}
    if doc.function != "СЧФ":
        attrs.update({"ПоФактХЖ": DOC_NAME, "НаимДокОпр": DOC_NAME})
    attrs.update({"ДатаИнфПр": stamp.strftime("%d.%m.%Y"), "ВремИнфПр": stamp.strftime("%H.%M.%S")})
    document = ET.SubElement(root, "Документ", attrs)
    info = ET.SubElement(document, "СвСчФакт", {"НомерДок": number, "ДатаДок": _day(doc.doc_date)})
    if doc.correction_number:
        fix = doc.correction_number.strip()
        if not fix.isdigit() or not 1 <= int(fix) <= 999 or not doc.correction_date:
            raise ExportSkip(f"номер или дата исправления не по формату: «{fix}»")
        ET.SubElement(info, "ИспрДок", {"НомИспр": str(int(fix)), "ДатаИспр": _day(doc.correction_date)})
    _party(info, "СвПрод", doc.seller_name, doc.seller_inn, doc.seller_kpp, "продавца")
    _party(info, "СвПокуп", doc.buyer_name, doc.buyer_inn, doc.buyer_kpp, "покупателя")
    ET.SubElement(info, "ДенИзм", {"КодОКВ": currency, "НаимОКВ": CURRENCIES[currency]})
    table = ET.SubElement(document, "ТаблСчФакт")
    positions = [ln.position for ln in doc.lines]
    keep = all(isinstance(p, int) and 0 < p < 10 ** 6 for p in positions) and len(set(positions)) == len(positions)
    with_vat = False
    for index, ln in enumerate(doc.lines, 1):
        rate = VAT_XML.get(ln.vat_rate or "none")
        if rate is None:
            raise ExportSkip(f"в строке {index} ставка НДС «{ln.vat_rate}» не из перечня ФНС")
        with_vat = _line(table, ln.position if keep else index, ln, rate) or with_vat
    for value in (doc.total_without_vat, doc.total_with_vat, doc.vat_total):
        if value is not None and value < 0:
            raise ExportSkip("отрицательные итоги — внесите документ в 1С вручную")
    totals = ET.SubElement(table, "ВсегоОпл", {"СтТовБезНДСВсего": _money(doc.total_without_vat or 0),
                                               "СтТовУчНалВсего": _money(doc.total_with_vat or 0)})
    vat_total = ET.SubElement(totals, "СумНалВсего")
    if not with_vat and not doc.vat_total:
        ET.SubElement(vat_total, "БезНДС").text = "без НДС"
    else:
        ET.SubElement(vat_total, "СумНал").text = _money(doc.vat_total or 0)
    signer = ET.SubElement(document, "Подписант", {"СпосПодтПолном": "1", "ДопСведПодп": SIGNER_NOTE})
    ET.SubElement(signer, "ФИО", {"Фамилия": "Не указана", "Имя": "Не указано"})
    # windows-1251, как у файлов ЭДО; символы вне кодировки (м³, эмодзи) — ссылками &#NNN;
    body = ET.tostring(root, encoding="windows-1251", xml_declaration=False)
    return f"{name}.xml", b'<?xml version="1.0" encoding="windows-1251"?>\n' + body


def prepare(docs: list[PrimaryDocument], now: datetime | None = None) -> list[dict]:
    """Файлы для выгрузки: [{doc, name, data}] или [{doc, reason}] — по каждому документу."""
    out = []
    for doc in docs:
        try:
            name, data = upd_xml(doc, now)
        except ExportSkip as exc:
            out.append({"doc": doc, "reason": str(exc)})
            continue
        check = schema.validate(data, "5.03")
        if check.get("checked") and not check.get("valid"):
            first = (check.get("errors") or [{"message": "неизвестная ошибка"}])[0]["message"]
            out.append({"doc": doc, "reason": f"файл не прошёл схему ФНС: {first}"})
            continue
        out.append({"doc": doc, "name": name, "data": data, "xsd": bool(check.get("valid"))})
    return out


def zip_files(ready: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in ready:
            archive.writestr(item["name"], item["data"])
    return buf.getvalue()


# ------------------------------------------------------------------ EnterpriseData (как у выгрузки чеков)
def vat_1c(rate: str) -> str:
    """Ставка реестра → значение перечисления «СтавкиНДС» 1С («НДС22», «БезНДС»)."""
    if rate == "agent":
        return "НДСИсчисляетсяНалоговымАгентом"
    return VAT_1C.get(rate or "none", f"НДС{rate}")


def _party_ed(name: str, inn: str, kpp: str) -> dict:
    kind = "ФизическоеЛицо" if len(inn or "") == 12 else "ЮридическоеЛицо"
    return {"Наименование": name, "ИНН": inn, "КПП": kpp, "ЮридическоеФизическоеЛицо": kind}


def _iso(value: date | None) -> str:
    return value.strftime("%Y-%m-%dT00:00:00") if value else ""


def ed_objects(doc: PrimaryDocument) -> list[dict]:
    """Документ реестра → объекты EnterpriseData: поступление (УПД, ДОП) и счёт-фактура (СЧФ, СЧФДОП)."""
    ref = f"ymaster-primary-{doc.id}"
    seller = _party_ed(doc.seller_name, doc.seller_inn, doc.seller_kpp)
    buyer = {"Наименование": doc.buyer_name, "ИНН": doc.buyer_inn, "КПП": doc.buyer_kpp}
    money = {"Код": doc.currency or "643", "Наименование": CURRENCIES.get(doc.currency or "643", "")}
    correction = ({"Номер": doc.correction_number, "Дата": _iso(doc.correction_date)}
                  if doc.correction_number else None)
    out = []
    if doc.function in ("СЧФДОП", "ДОП"):
        out.append({
            "ИмяПКО": "ПоступлениеТоваровУслуг",
            "КлючевыеСвойства": {"Ссылка": ref, "Номер": doc.doc_number, "Дата": _iso(doc.doc_date),
                                 "Организация": buyer, "Контрагент": seller},
            "ВалютаДокумента": money, "СуммаДокумента": round(doc.total_with_vat or 0, 2),
            "СуммаВключаетНДС": True,
            "ПервичныйДокумент": {"Вид": doc.kind, "Функция": doc.function, "Источник": doc.source or "xml",
                                  "ИдФайла": doc.file_id, "Исправление": correction},
            "Товары": [{
                "НомерСтроки": ln.position,
                "Номенклатура": {"Наименование": ln.name, "Код": ln.item_code},
                "ЕдиницаИзмерения": {"Код": ln.unit_code, "Наименование": ln.unit_name},
                "Количество": ln.quantity, "Цена": ln.price, "СуммаБезНДС": ln.sum_without_vat,
                "СтавкаНДС": vat_1c(ln.vat_rate), "СуммаНДС": ln.vat_sum, "Сумма": ln.sum_with_vat,
            } for ln in doc.lines],
        })
    if doc.function in ("СЧФДОП", "СЧФ"):
        invoice = {
            "ИмяПКО": "СчетФактураПолученный",
            "КлючевыеСвойства": {"Ссылка": f"{ref}-sf", "Номер": doc.doc_number, "Дата": _iso(doc.doc_date),
                                 "Организация": buyer, "Контрагент": seller},
            "КодВидаОперации": "01", "УПД": doc.function == "СЧФДОП", "ВалютаДокумента": money,
            "СуммаДокумента": round(doc.total_with_vat or 0, 2), "СуммаНДС": round(doc.vat_total or 0, 2),
            "Исправление": correction,
        }
        if doc.function == "СЧФДОП":
            invoice["ДокументОснование"] = {"ИмяПКО": "ПоступлениеТоваровУслуг", "Ссылка": ref}
        out.append(invoice)
    return out


def _package_head() -> dict:
    return {"ФорматВерсии": "1.3", "ИмяПакета": "ЯмастерЧек.Первичка", "Поставщик": "ООО «Ямастер» (ymaster.ru)",
            "ДатаФормирования": datetime.now(MSK).strftime("%Y-%m-%dT%H:%M:%S")}


def ed_json(docs: list[PrimaryDocument]) -> str:
    objects = [obj for doc in docs for obj in ed_objects(doc)]
    return json.dumps({**_package_head(), "Данные": {"Контейнер": objects}}, ensure_ascii=False, indent=2)


def _xml_value(parent: ET.Element, key: str, value) -> None:
    node = ET.SubElement(parent, key)
    if isinstance(value, dict):
        for k, v in value.items():
            if v is not None:
                _xml_value(node, k, v)
    elif isinstance(value, list):
        for item in value:
            _xml_value(node, "Строка", item)
    elif isinstance(value, bool):
        node.text = "true" if value else "false"
    elif value is not None:
        node.text = _text(value, 4000) if isinstance(value, str) else f"{value}"


def ed_xml(docs: list[PrimaryDocument]) -> str:
    head = _package_head()
    root = ET.Element("EnterpriseData", {k: v for k, v in head.items() if k != "ДатаФормирования"})
    root.set("ДатаФормирования", head["ДатаФормирования"])
    container = ET.SubElement(ET.SubElement(root, "Данные"), "Контейнер")
    for doc in docs:
        for obj in ed_objects(doc):
            node = ET.SubElement(container, "Object", {"ИмяПКО": obj["ИмяПКО"]})
            for key, value in obj.items():
                if key != "ИмяПКО" and value is not None:
                    _xml_value(node, key, value)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ------------------------------------------------------------------ плоский JSON для 1С (pull)
def pull_item(doc: PrimaryDocument) -> dict:
    return {
        "id": doc.id, "kind": doc.kind, "function": doc.function, "number": doc.doc_number,
        "date": doc.doc_date.isoformat() if doc.doc_date else None,
        "correction_number": doc.correction_number,
        "correction_date": doc.correction_date.isoformat() if doc.correction_date else None,
        "seller": {"name": doc.seller_name, "inn": doc.seller_inn, "kpp": doc.seller_kpp},
        "buyer": {"name": doc.buyer_name, "inn": doc.buyer_inn, "kpp": doc.buyer_kpp},
        "currency": doc.currency, "status": doc.status, "source": doc.source or "xml", "file_id": doc.file_id,
        "upd_file": file_id(doc) + ".xml", "company_id": doc.company_id,
        "totals": {"without_vat": doc.total_without_vat, "vat": doc.vat_total, "with_vat": doc.total_with_vat},
        "lines": [{
            "position": ln.position, "name": ln.name, "unit_code": ln.unit_code, "unit_name": ln.unit_name,
            "qty": ln.quantity, "price": ln.price, "sum_without_vat": ln.sum_without_vat, "vat_rate": ln.vat_rate,
            "vat_1c": vat_1c(ln.vat_rate), "vat_sum": ln.vat_sum, "sum_with_vat": ln.sum_with_vat,
            "item_code": ln.item_code,
        } for ln in doc.lines],
    }
