# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Формирование пакетов выгрузки чеков в 1С (формат EnterpriseData).
Поддержка: JSON и XML, применение настраиваемого маппинга реквизитов.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime

from ..models import Receipt, ReceiptItem, MappingSetting

# Документы 1С, поддерживаемые на текущем этапе
TARGET_OBJECTS = [
    {"code": "ПоступлениеТоваровУслуг", "name": "Поступление товаров и услуг", "cfg": "БП 3.0 / ERP 2 / УТ 11"},
    {"code": "АвансовыйОтчет", "name": "Авансовый отчёт", "cfg": "БП 3.0 / ERP 2"},
    {"code": "ПриходныйКассовыйОрдер", "name": "Приходный кассовый ордер", "cfg": "БП 3.0"},
]

# Справочник полей чека, доступных в маппинге
SOURCE_FIELDS = [
    {"code": "fn", "name": "ФН — номер фискального накопителя", "type": "string"},
    {"code": "fd", "name": "ФД — номер фискального документа", "type": "string"},
    {"code": "fp", "name": "ФП — фискальный признак", "type": "string"},
    {"code": "receipt_date", "name": "Дата и время расчёта", "type": "datetime"},
    {"code": "total_sum", "name": "Сумма чека", "type": "number"},
    {"code": "operation", "name": "Признак расчёта (1/2)", "type": "number"},
    {"code": "qr_data", "name": "Строка QR-кода целиком", "type": "string"},
    {"code": "created_at", "name": "Дата сканирования", "type": "datetime"},
    {"code": "assignee", "name": "Сотрудник (подотчётник)", "type": "string"},
    {"code": "comment", "name": "Комментарий", "type": "string"},
    {"code": "fns_status", "name": "Статус проверки ФНС", "type": "string"},
]

TRANSFORMS = [
    {"code": "direct", "name": "Как есть"},
    {"code": "kopecks", "name": "Сумма в копейки (×100)"},
    {"code": "date_iso", "name": "Дата в ISO 8601 (1С)"},
    {"code": "operation_sign", "name": "Признак расчёта → ВидОперации"},
    {"code": "constant", "name": "Константа (из параметра)"},
]

# Маппинг по умолчанию (создаётся при первом запуске)
DEFAULT_MAPPING = [
    {"source_field": "total_sum", "target_object": "ПоступлениеТоваровУслуг", "target_field": "СуммаДокумента", "transform": "kopecks", "transform_param": "", "position": 0},
    {"source_field": "receipt_date", "target_object": "ПоступлениеТоваровУслуг", "target_field": "Дата", "transform": "date_iso", "transform_param": "", "position": 1},
    {"source_field": "fn", "target_object": "ПоступлениеТоваровУслуг", "target_field": "ФН", "transform": "direct", "transform_param": "", "position": 2},
    {"source_field": "fd", "target_object": "ПоступлениеТоваровУслуг", "target_field": "ФД", "transform": "direct", "transform_param": "", "position": 3},
    {"source_field": "fp", "target_object": "ПоступлениеТоваровУслуг", "target_field": "ФП", "transform": "direct", "transform_param": "", "position": 4},
    {"source_field": "operation", "target_object": "ПоступлениеТоваровУслуг", "target_field": "ВидОперации", "transform": "operation_sign", "transform_param": "", "position": 5},
    {"source_field": "total_sum", "target_object": "АвансовыйОтчет", "target_field": "СуммаДокумента", "transform": "kopecks", "transform_param": "", "position": 6},
    {"source_field": "receipt_date", "target_object": "АвансовыйОтчет", "target_field": "Дата", "transform": "date_iso", "transform_param": "", "position": 7},
    {"source_field": "qr_data", "target_object": "АвансовыйОтчет", "target_field": "Комментарий", "transform": "direct", "transform_param": "", "position": 8},
]


# --------------------------------------------------------------------------
#  Трансформации значений
# --------------------------------------------------------------------------
def apply_transform(value, transform: str, param: str = ""):
    if transform == "kopecks":
        try:
            return int(round(float(value) * 100))
        except (TypeError, ValueError):
            return value
    if transform == "date_iso":
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%dT%H:%M:%S")
        return value
    if transform == "operation_sign":
        return {"1": "Приход", "2": "ВозвратПрихода", "3": "Расход", "4": "ВозвратРасхода"}.get(
            str(value), "Приход")
    if transform == "constant":
        return param
    return value


def _receipt_field(receipt: Receipt, field_code: str):
    if field_code == "fn":
        return receipt.fn
    if field_code == "fd":
        return receipt.fd
    if field_code == "fp":
        return receipt.fp
    if field_code == "receipt_date":
        return receipt.receipt_date
    if field_code == "total_sum":
        return receipt.total_sum
    if field_code == "operation":
        return receipt.operation
    if field_code == "qr_data":
        return receipt.qr_data
    if field_code == "created_at":
        return receipt.created_at
    if field_code == "fns_status":
        return receipt.fns_status
    if field_code == "assignee":
        return getattr(receipt, "assignee", "") or None
    if field_code == "comment":
        return getattr(receipt, "comment", "") or None
    return None


def _apply_mapping(receipt: Receipt, mapping: list[MappingSetting],
                   target_object: str) -> dict:
    """Дополнительные реквизиты по пользовательскому маппингу."""
    out: dict = {}
    for m in mapping:
        if not m.is_active or m.target_object != target_object:
            continue
        value = _receipt_field(receipt, m.source_field)
        if value is None:
            continue
        if isinstance(value, datetime):
            value = value.isoformat()
        out[m.target_field] = apply_transform(value, m.transform, m.transform_param)
    return out


# --------------------------------------------------------------------------
#  EnterpriseData: JSON
# --------------------------------------------------------------------------
def _ed_receipt_json(receipt: Receipt, target_object: str,
                     mapping: list[MappingSetting]) -> dict:
    """Одна выгрузка чека в структуре EnterpriseData (JSON)."""
    return {
        "ИмяПКО": target_object,
        "КлючевыеСвойства": {
            "Ссылка": f"ymaster-check-{receipt.id[:8]}-{receipt.fn}-{receipt.fd}",
            "Номер": receipt.fd,
            "От": (receipt.receipt_date or receipt.created_at).strftime("%Y-%m-%dT%H:%M:%S"),
            "Дата": (receipt.receipt_date or receipt.created_at).strftime("%Y-%m-%dT%H:%M:%S"),
            "СуммаДокумента": round(receipt.total_sum, 2),
            "ВалютаДокумента": {"Код": "643", "Наименование": "Российский рубль"},
            "Контрагент": {
                "Наименование": (getattr(receipt, "assignee", "") or
                                 "Подотчётное лицо (укажите сотрудника)"),
            },
        },
        "ДополнительныеРеквизиты": _apply_mapping(receipt, mapping, target_object),
        "ФискальныеРеквизитыЧека": {
            "ФН": receipt.fn,
            "ФД": receipt.fd,
            "ФП": receipt.fp,
            "ПризнакРасчета": {1: "Приход", 2: "ВозвратПрихода",
                               3: "Расход", 4: "ВозвратРасхода"}.get(receipt.operation, "Приход"),
            "СтрокаЧекаQR": receipt.qr_data,
            "СтатусПроверкиФНС": receipt.fns_status,
        },
        "Товары": [
            {
                "Номенклатура": {"Наименование": item.name},
                "Количество": item.quantity,
                "Цена": round(item.price, 2),
                "Сумма": round(item.total, 2),
                "СтавкаНДС": f"НДС{item.vat_rate}" if item.vat_rate not in ("none", "") else "БезНДС",
                "СуммаНДС": round(item.vat_sum, 2),
            }
            for item in receipt.items
        ],
    }


def build_enterprise_data_json(receipts: list[Receipt],
                               mapping: list[MappingSetting],
                               target_object: str) -> str:
    """Полный пакет EnterpriseData в формате JSON."""
    return json.dumps({
        "ФорматВерсии": "1.3",
        "ИмяПакета": "ЯмастерЧек",
        "Поставщик": "ООО Ямастер (ymaster.ru)",
        "ДатаФормирования": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "Данные": {
            "Контейнер": [
                _ed_receipt_json(r, target_object, mapping) for r in receipts
            ]
        },
    }, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
#  EnterpriseData: XML
# --------------------------------------------------------------------------
def build_enterprise_data_xml(receipts: list[Receipt],
                              mapping: list[MappingSetting],
                              target_object: str) -> str:
    """Полный пакет EnterpriseData в формате XML."""
    root = ET.Element("EnterpriseData", {
        "ФорматВерсии": "1.3", "ИмяПакета": "ЯмастерЧек",
        "Поставщик": "ООО Ямастер (ymaster.ru)",
    })
    container = ET.SubElement(root, "Данные")
    cont = ET.SubElement(container, "Контейнер")
    for r in receipts:
        obj = ET.SubElement(cont, "Object", {"ИмяПКО": target_object})
        ks = ET.SubElement(obj, "КлючевыеСвойства")
        for tag, val in (
            ("Ссылка", f"ymaster-check-{r.id[:8]}-{r.fn}-{r.fd}"),
            ("Номер", r.fd),
            ("Дата", (r.receipt_date or r.created_at).strftime("%Y-%m-%dT%H:%M:%S")),
            ("СуммаДокумента", f"{r.total_sum:.2f}"),
        ):
            ET.SubElement(ks, tag).text = val
        fr = ET.SubElement(obj, "ФискальныеРеквизитыЧека")
        for tag, val in (("ФН", r.fn), ("ФД", r.fd), ("ФП", r.fp),
                         ("СтрокаЧекаQR", r.qr_data),
                         ("СтатусПроверкиФНС", r.fns_status)):
            ET.SubElement(fr, tag).text = str(val)
        if r.items:
            goods = ET.SubElement(obj, "Товары")
            for it in r.items:
                row = ET.SubElement(goods, "СтрокаТовары")
                for tag, val in (
                    ("Номенклатура", it.name),
                    ("Количество", f"{it.quantity:g}"),
                    ("Цена", f"{it.price:.2f}"),
                    ("Сумма", f"{it.total:.2f}"),
                    ("СтавкаНДС", it.vat_rate),
                ):
                    ET.SubElement(row, tag).text = str(val)
    rough = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8")
    return pretty.decode("utf-8")


# --------------------------------------------------------------------------
#  Плоский JSON для Push-режима (1С ← GET /onec/v1/receipts/pull)
# --------------------------------------------------------------------------
def build_push_payload(receipts: list[Receipt],
                       mapping: list[MappingSetting],
                       target_object: str) -> dict:
    """Компактный payload для вытягивания 1С (быстрый разбор на стороне 1С)."""
    return {
        "provider": "Ямастер Чек (ymaster.ru)",
        "format": "ymaster-push-v1",
        "target_object": target_object,
        "receipts": [
            {
                "id": r.id,
                "fn": r.fn,
                "fd": r.fd,
                "fp": r.fp,
                "date": (r.receipt_date or r.created_at).strftime("%Y-%m-%dT%H:%M:%S"),
                "sum": round(r.total_sum, 2),
                "sum_kopecks": int(round(r.total_sum * 100)),
                "operation": r.operation,
                "fns_status": r.fns_status,
                "assignee": getattr(r, "assignee", "") or "",
                "comment": getattr(r, "comment", "") or "",
                "qr": r.qr_data,
                "items": [
                    {
                        "name": it.name,
                        "qty": it.quantity,
                        "price": round(it.price, 2),
                        "sum": round(it.total, 2),
                        "vat_rate": it.vat_rate,
                        "vat_sum": round(it.vat_sum, 2),
                    } for it in r.items
                ],
                "extra": _apply_mapping(r, mapping, target_object),
            } for r in receipts
        ],
    }
