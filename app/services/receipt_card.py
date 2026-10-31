# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — полная карточка чека (v1.70.72)
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
#
# Задача: выжать из чека ФНС ВСЁ, что сервис отдаёт, и разложить это по
# группам — продавец, касса, деньги, налоги, кассир, покупатель, позиции
# (с маркировкой, агентом, поставщиком, страной, отраслевым реквизитом),
# дополнительные реквизиты. Из этих же данных считается регион продавца
# и отрасль покупки, чтобы потом фильтровать пул чеков.
#
# Источник структуры — Ticket открытого API ФНС (JSON-строка в
# GetTicketResponse). Ключи ищутся без учёта регистра и по алиасам:
# у разных версий сервиса встречается retailPlaceAddress/retailAddress,
# operator/cashier, measurementUnit/unitCode и т. п.
# ======================================================================
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import fns_refs, goods, regions


# --------------------------------------------------------------------------
#  Вспомогательные чтения (устойчивые к регистру и алиасам)
# --------------------------------------------------------------------------
def _low(d: dict) -> dict:
    return {str(k).lower(): v for k, v in (d or {}).items() if isinstance(d, dict)} \
        if isinstance(d, dict) else {}


def _get(d: dict, *keys, default=None):
    """Первое непустое значение по алиасам (регистр ключей не важен)."""
    if not isinstance(d, dict):
        return default
    low = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        val = low.get(str(key).lower())
        if val not in (None, "", [], {}):
            return val
    return default


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _divisor(receipt_total: float | None, raw_total) -> float:
    """Копейки или рубли: определяем один раз по итогу чека."""
    raw = _num(raw_total)
    known = _num(receipt_total)
    if known and raw:
        if abs(raw / 100.0 - known) <= max(1.0, known * 0.5):
            return 100.0
        if abs(raw - known) <= max(1.0, known * 0.5):
            return 1.0
    return 100.0 if raw >= 100000 else 1.0


def _dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            # время из ФНС приходит как unix-метка; в базе храним naive UTC
            return datetime.fromtimestamp(
                int(value), timezone.utc).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M",
                "%Y%m%dT%H%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)         # noqa: DTZ007
        except ValueError:
            continue
    return None


def _industry_details(raw) -> list[dict]:
    """Отраслевой реквизит ФОИВ (теги 1260–1265) — как есть, с подписями."""
    out: list[dict] = []
    for one in (raw if isinstance(raw, list) else [raw]):
        if not isinstance(one, dict):
            continue
        code = _get(one, "idFoiv", "idFOIV", "foiv", default="")
        out.append({
            "foiv": str(code or ""),
            "foiv_name": fns_refs.foiv_name(code),
            "doc_date": str(_get(one, "foundationDocDateTime",
                                 "docDate", default="") or ""),
            "doc_number": str(_get(one, "foundationDocNumber",
                                   "docNum", default="") or ""),
            "value": str(_get(one, "industryPropValue", "attrValue",
                               default="") or ""),
            "label_mode": _get(one, "labelCodeProcesMode"),
            "label_check": _get(one, "checkingLabeledProdResult",
                                "checkingProdInformationResult"),
        })
    return out


def _marking(raw) -> list[dict]:
    """Коды маркировки (DataMatrix) из позиции, если они есть."""
    out: list[dict] = []
    for one in (raw if isinstance(raw, list) else [raw]):
        if not isinstance(one, dict):
            continue
        out.append({
            "type": _get(one, "productIdType", "type", default=""),
            "gtin": str(_get(one, "gtin", "productId", default="") or ""),
            "serial": str(_get(one, "serial", "sernum", default="") or ""),
            "raw": str(_get(one, "rawProductCode", "raw", default="") or "")[:100],
        })
    return out


# --------------------------------------------------------------------------
#  Нормализация Ticket ФНС → полный документ
# --------------------------------------------------------------------------
def normalize_ticket(ticket: dict, receipt_total: float | None = None) -> dict:
    """Ticket ФНС → документ с группами полей (в рублях, с подписями)."""
    if not isinstance(ticket, dict) or not ticket:
        return {}
    receipt = ticket.get("receipt") if isinstance(ticket.get("receipt"), dict) \
        else ticket
    div = _divisor(receipt_total, _get(receipt, "totalSum", "total"))

    def money(value) -> float:
        return round(_num(value) / div, 2)

    seller_name = str(_get(receipt, "user", "operatorName", "seller",
                           default="") or "")
    seller_inn = str(_get(receipt, "userInn", "inn", default="") or "")
    address = str(_get(receipt, "retailPlaceAddress", "retailAddress",
                       "address", default="") or "")
    place = str(_get(receipt, "retailPlace", "retailPlaceName",
                     default="") or "")
    geo = regions.detect(seller_inn, address)

    items: list[dict] = []
    for pos, raw_item in enumerate(_get(receipt, "items", default=[]) or []):
        if not isinstance(raw_item, dict):
            continue
        name = str(_get(raw_item, "name", "itemName", default="") or
                   f"Позиция {pos + 1}")
        product_type = _get(raw_item, "productType")
        industry, group = goods.classify_name(name, product_type)
        unit_code = _get(raw_item, "measurementUnit", "unitCode",
                         "itemsQuantityMeasure", "quantityMeasure")
        industry, group = goods.classify_name(name, product_type)
        items.append({
            "pos": pos + 1,
            "name": name[:1000],
            "quantity": _num(_get(raw_item, "quantity", "qty", default=1)),
            "price": money(_get(raw_item, "price")),
            "total": money(_get(raw_item, "sum", "total", "itemSum")),
            "nds": str(_get(raw_item, "nds", default="none")),
            "nds_name": fns_refs.nds_name(_get(raw_item, "nds", default="none")),
            "nds_sum": money(_get(raw_item, "ndsSum", "ndsSumm")),
            "payment_type": _get(raw_item, "paymentType"),
            "payment_type_name": fns_refs.payment_type_name(
                _get(raw_item, "paymentType")),
            "product_type": product_type,
            "product_type_name": fns_refs.product_type_name(product_type),
            "measure_code": str(unit_code or ""),
            "measure": fns_refs.measure_name(unit_code),
            "group": group,
            "industry": industry,
            "marking": _marking(_get(raw_item, "productCodeData",
                                     "itemsProductCode", "marking")),
            "agent": {
                "type": _get(_get(raw_item, "agentInfo", default={}) or {},
                             "type", default=""),
                "name": str(_get(_get(raw_item, "agentInfo", default={}) or {},
                                 "name", default="") or ""),
                "inn": str(_get(_get(raw_item, "agentInfo", default={}) or {},
                                "inn", default="") or ""),
            } if _get(raw_item, "agentInfo") else {},
            "supplier": {
                "name": str(_get(_get(raw_item, "supplierInfo", default={}) or {},
                                 "name", default="") or ""),
                "inn": str(_get(_get(raw_item, "supplierInfo", default={}) or {},
                                "inn", default="") or ""),
                "phones": _get(_get(raw_item, "supplierInfo", default={}) or {},
                               "phones", default=""),
            } if _get(raw_item, "supplierInfo") else {},
            "country": _get(raw_item, "countryCode", "countryOfOriginCode"),
            "customs": _get(raw_item, "customsDeclaration",
                            "customsDeclarationNumber"),
            "excise": money(_get(raw_item, "excise", "exciseSum")),
            "industry_details": _industry_details(
                _get(raw_item, "itemsIndustryDetails", "industryDetails")),
        })

    nds: list[dict] = []
    for rate in ("nds0", "nds5", "nds7", "nds10", "nds18", "nds20", "ndsNo"):
        val = _get(receipt, rate)
        if val not in (None, "", 0):
            nds.append({"rate": rate[3:], "rate_name": fns_refs.nds_name(
                rate[3:] if rate[3:].isdigit() else ("none" if rate == "ndsNo"
                                                     else rate[3:])),
                "sum": money(val)})
    amounts = _get(receipt, "amountsReceiptNds", default={}) or {}
    for one in (_get(amounts, "amountsNds", default=[]) or []):
        if isinstance(one, dict):
            nds.append({"rate": _get(one, "nds", default=""),
                        "rate_name": fns_refs.nds_name(_get(one, "nds")),
                        "sum": money(_get(one, "ndsSum"))})

    props: list[dict] = []
    raw_props = _get(receipt, "properties")
    props_list = raw_props if isinstance(raw_props, list) else (
        [raw_props] if isinstance(raw_props, dict) else [])
    for one in props_list:
        if isinstance(one, dict):
            props.append({"name": str(_get(one, "key", "propertyName",
                                          default="") or "")[:200],
                          "value": str(_get(one, "value", "propertyValue",
                                           default="") or "")[:500]})

    operation = _get(receipt, "operationType", "operation")
    cash, ecash = money(_get(receipt, "cashTotalSum")), money(
        _get(receipt, "ecashTotalSum"))
    if cash and ecash:
        pay_type = "смешанная"
    elif cash:
        pay_type = "наличные"
    elif ecash:
        pay_type = "безналичные"
    else:
        pay_type = ""

    return {
        "kind": str(ticket.get("kind", "") or ""),
        "ticket_id": _get(ticket, "id", default=""),
        "ofd_id": str(_get(ticket, "ofdId", "ofd", default="") or ""),
        "date_time": _dt(_get(receipt, "dateTime", "date")),
        "operation": int(operation) if str(operation).isdigit() else None,
        "operation_name": fns_refs.OPERATION_TYPES.get(
            int(operation) if str(operation).isdigit() else 0, ""),
        "seller": {
            "name": seller_name[:500],
            "inn": seller_inn[:20],
            "kpp": str(_get(receipt, "userKpp", "kpp", default="") or "")[:12],
            "address": address[:500],
            "place": place[:300],
            "region_code": geo["region_code"],
            "region_name": geo["region_name"],
            "city": geo["city"],
            "postal": geo["postal"],
            "region_source": geo["source"],
        },
        "kkt": {
            "reg_id": str(_get(receipt, "kktRegId", default="") or ""),
            "fn": str(_get(receipt, "fiscalDriveNumber", default="") or ""),
            "fd": str(_get(receipt, "fiscalDocumentNumber", default="") or ""),
            "fp": str(_get(receipt, "fiscalSign", default="") or ""),
            "shift": _get(receipt, "shiftNumber"),
            "request": _get(receipt, "requestNumber"),
            "ffd": str(_get(receipt, "fiscalDocumentFormatVer", default="") or ""),
            "message_fp": _get(receipt, "messageFiscalSign"),
            "redefine_mask": _get(receipt, "redefineMask"),
            "receipt_code": _get(receipt, "receiptCode"),
        },
        "money": {
            "total": money(_get(receipt, "totalSum", "total")),
            "cash": cash,
            "ecash": ecash,
            "prepaid": money(_get(receipt, "prepaidSum")),
            "credit": money(_get(receipt, "creditSum")),
            "provision": money(_get(receipt, "provisionSum")),
            "pay_type": pay_type,
        },
        "tax": {
            "taxation": _get(receipt, "taxationType", "appliedTaxationType"),
            "taxation_name": fns_refs.taxation_name(
                _get(receipt, "taxationType", "appliedTaxationType")),
            "nds": nds,
        },
        "cashier": {
            "name": str(_get(receipt, "operator", "cashier", default="") or ""),
            "inn": str(_get(receipt, "operatorInn", default="") or ""),
        },
        "buyer": {
            "name": str(_get(receipt, "buyer", default="") or ""),
            "contact": str(_get(receipt, "buyerPhoneOrAddress", default="") or ""),
        },
        "items": items,
        "properties": props,
        "industry_details": _industry_details(
            _get(receipt, "industryReceiptDetails")),
        "industry": goods.classify_items(items),
        "raw": ticket,
    }


# --------------------------------------------------------------------------
#  Применение документа к чеку (данные + индексы для фильтров)
# --------------------------------------------------------------------------
def apply_to_receipt(db, receipt, ticket: dict, *, force_items: bool = False,
                     source: str = "fns") -> dict:
    """Записать полный документ ФНС в чек и позиции.

    Возвращает нормализованный документ. Ничего не затирает вручную:
    если бухгалтер правил чек (details_source == manual_edit), машинные
    данные заполняют только пустые поля.
    """
    from ..models import ReceiptItem

    doc = normalize_ticket(ticket, receipt.total_sum)
    if not doc:
        return {}
    human = receipt.details_source == "manual_edit"
    seller = doc["seller"]
    money = doc["money"]
    tax = doc["tax"]

    def fill(attr: str, value) -> None:
        if value in (None, "", 0):
            return
        if human and getattr(receipt, attr, "") not in (None, "", 0):
            return
        setattr(receipt, attr, value)

    fill("merchant_name", seller["name"])
    fill("merchant_inn", seller["inn"])
    fill("merchant_address", seller["address"])
    fill("cashier", doc["cashier"]["name"])
    if money["cash"]:
        fill("cash_sum", money["cash"])
    if money["ecash"]:
        fill("ecash_sum", money["ecash"])

    # v1.70.72: индексы для фильтров пула (регион, отрасль, налоги, оплата)
    if seller["region_code"] and not (human and receipt.region_code):
        receipt.region_code = seller["region_code"]
        receipt.region_name = seller["region_name"] or ""
        receipt.region_city = seller["city"] or ""
    if tax["taxation"] not in (None, ""):
        if not (human and receipt.taxation):
            receipt.taxation = str(tax["taxation"])[:40]
            receipt.taxation_name = tax["taxation_name"][:60]
    if money["pay_type"] and not (human and receipt.pay_type):
        receipt.pay_type = money["pay_type"]
    industry = doc["industry"].get("industry") or ""
    if industry and not (human and receipt.industry):
        receipt.industry = industry[:100]
        receipt.industry_lc = industry.casefold()[:100]
        receipt.product_group = (doc["industry"].get("group") or "")[:100]

    # Позиции: расширенные поля (тип, единица, маркировка, поставщик…)
    if doc["items"] and (force_items or not receipt.items):
        receipt.items.clear()
        for it in doc["items"][:200]:
            receipt.items.append(ReceiptItem(
                name=it["name"][:1000],
                quantity=it["quantity"] or 1.0,
                price=it["price"],
                total=it["total"],
                vat_rate=(it["nds"] or "none")[:10],
                vat_sum=it["nds_sum"],
                position=it["pos"] - 1,
                product_type=int(it["product_type"])
                if str(it["product_type"]).isdigit() else None,
                payment_type=int(it["payment_type"])
                if str(it["payment_type"]).isdigit() else None,
                unit=it["measure"][:20],
                product_group=it["group"][:100],
                industry=it["industry"][:100],
                extra_json=json.dumps(
                    {k: v for k, v in (("marking", it["marking"]),
                                       ("agent", it["agent"]),
                                       ("supplier", it["supplier"]),
                                       ("country", it["country"]),
                                       ("customs", it["customs"]),
                                       ("excise", it["excise"]),
                                       ("industry_details", it["industry_details"]))
                     if v}, ensure_ascii=False),
            ))
    elif receipt.items:
        # Позиции уже были — проставим отрасль/группу по наименованиям,
        # чтобы фильтры работали и для чеков, полученных до v1.70.72.
        enrich_items(receipt)

    # Расширенные данные источника: слияние, а не замена
    try:
        ext = json.loads(receipt.ext_json) if receipt.ext_json else {}
        ext = ext if isinstance(ext, dict) else {}
    except ValueError:
        ext = {}
    ext.update({
        "taxation": tax["taxation_name"],
        "pay_type": money["pay_type"],
        "region": seller["region_name"] or seller["region_code"],
        "region_city": seller["city"],
        "industry": industry,
        "product_group": doc["industry"].get("group", ""),
        "industry_source": "по наименованиям позиций",
        "nds_totals": tax["nds"],
        "kkt_reg_id": doc["kkt"]["reg_id"],
        "fiscal_drive_number": doc["kkt"]["fn"],
        "fiscal_document_number": doc["kkt"]["fd"],
        "fiscal_sign": doc["kkt"]["fp"],
        "shift_number": doc["kkt"]["shift"],
        "request_number": doc["kkt"]["request"],
        "ffd_version": doc["kkt"]["ffd"],
        "retail_place": seller["place"],
        "ofd_id": doc["ofd_id"],
        "source_code": "200",
        "properties": doc["properties"],
        "industry_details": doc["industry_details"],
        "items_meta": [
            {"pos": it["pos"], "payment_type": it["payment_type_name"],
             "product_type": it["product_type_name"],
             "measure": it["measure"], "group": it["group"]}
            for it in doc["items"] if it["product_type"] or it["payment_type"]]
        if any(it["product_type"] or it["payment_type"] for it in doc["items"])
        else [],
    })
    receipt.ext_json = json.dumps(
        {k: v for k, v in ext.items() if v not in (None, "", [], {})},
        ensure_ascii=False)
    receipt.details_source = receipt.details_source or source
    receipt.full_data = bool(receipt.full_data or doc["items"])
    return doc


def enrich_items(receipt) -> int:
    """Проставить группу/отрасль позициям, у которых их ещё нет.

    Работает без обращения к ФНС — только по сохранённым наименованиям.
    Возвращает число обновлённых позиций.
    """
    changed = 0
    for it in receipt.items:
        industry, group = goods.classify_name(
            it.name, getattr(it, "product_type", None))
        upd = False
        if not (it.product_group or ""):
            it.product_group = group[:100]
            upd = True
        if not (it.industry or ""):
            it.industry = industry[:100]
            upd = True
        changed += 1 if upd else 0
    return changed


def enrich_receipt(db, receipt) -> dict:
    """Достроить регион/отрасль/группы по уже сохранённым данным чека.

    Не тратит лимит ФНС: берёт raw_data (сохранённый ответ ФНС), адрес и
    ИНН продавца. Используется кнопкой «Разобрать данные чеков» и для
    чеков, проверенных до v1.70.72.
    """
    raw = {}
    try:
        raw = json.loads(receipt.raw_data or "{}")
    except ValueError:
        raw = {}
    ticket = ((raw.get("fns") or {}).get("ticket")
              if isinstance(raw.get("fns"), dict) else None)
    if isinstance(ticket, dict) and ticket:
        apply_to_receipt(db, receipt, ticket)
        enrich_items(receipt)
        return {"ticket": True, "items": len(receipt.items)}

    # Чека ФНС в сохранённом виде нет — работаем с реквизитами чека
    geo = regions.detect(receipt.merchant_inn, receipt.merchant_address)
    if geo["region_code"]:
        receipt.region_code = geo["region_code"]
        receipt.region_name = geo["region_name"] or ""
        receipt.region_city = geo["city"] or ""
    enrich_items(receipt)
    calc = goods.classify_items(
        [{"name": it.name, "total": it.total,
          "product_type": getattr(it, "product_type", None)}
         for it in receipt.items])
    if calc.get("industry") and not receipt.industry:
        receipt.industry = calc["industry"][:100]
        receipt.industry_lc = calc["industry"].casefold()[:100]
        receipt.product_group = calc["group"][:100]
    return {"ticket": False, "items": len(receipt.items)}


# --------------------------------------------------------------------------
#  Документ для интерфейса: из сохранённых данных, без обращения к ФНС
# --------------------------------------------------------------------------
def build_card(receipt) -> dict:
    """Полная карточка чека для интерфейса (полный документ + строка ФНС)."""
    raw = {}
    try:
        raw = json.loads(receipt.raw_data or "{}")
    except ValueError:
        raw = {}
    fns_raw = raw.get("fns") if isinstance(raw.get("fns"), dict) else {}
    ticket = fns_raw.get("ticket") if isinstance(fns_raw, dict) else None
    doc = normalize_ticket(ticket, receipt.total_sum) \
        if isinstance(ticket, dict) and ticket else {}

    # Если полного документа ФНС нет — собираем из того, что есть в чеке
    if not doc:
        geo = regions.detect(receipt.merchant_inn, receipt.merchant_address)
        doc = {
            "kind": "kkt" if receipt.fn else "",
            "date_time": receipt.receipt_date,
            "operation": receipt.operation,
            "operation_name": fns_refs.OPERATION_TYPES.get(
                receipt.operation or 1, ""),
            "seller": {
                "name": receipt.merchant_name or "",
                "inn": receipt.merchant_inn or "",
                "address": receipt.merchant_address or "",
                "region_code": (getattr(receipt, "region_code", "") or
                                geo["region_code"]),
                "region_name": (getattr(receipt, "region_name", "") or
                                geo["region_name"]),
                "city": (getattr(receipt, "region_city", "") or geo["city"]),
                "postal": geo["postal"],
                "region_source": (getattr(receipt, "region_code", "") and
                                  "receipt") or geo["source"] or "",
            },
            "kkt": {"reg_id": "", "fn": receipt.fn, "fd": receipt.fd,
                    "fp": receipt.fp, "shift": None, "request": None,
                    "ffd": "", "message_fp": None, "redefine_mask": None},
            "money": {"total": receipt.total_sum,
                      "cash": receipt.cash_sum, "ecash": receipt.ecash_sum,
                      "prepaid": 0.0, "credit": 0.0, "provision": 0.0,
                      "pay_type": getattr(receipt, "pay_type", "") or ""},
            "tax": {"taxation": getattr(receipt, "taxation", "") or "",
                    "taxation_name": getattr(receipt, "taxation_name", "") or "",
                    "nds": []},
            "cashier": {"name": receipt.cashier or "", "inn": ""},
            "buyer": {"name": "", "contact": ""},
            "items": [],
            "properties": [],
            "industry_details": [],
            "industry": {},
            "raw": {},
        }

    # Позиции: из чека (в них есть группы/маркировка), документ — дополнение
    items = []
    doc_items = {it["pos"]: it for it in doc.get("items", [])}
    for idx, row in enumerate(receipt.items):
        extra = {}
        try:
            extra = json.loads(row.extra_json) if row.extra_json else {}
        except ValueError:
            extra = {}
        src = doc_items.get(idx + 1, {})
        industry = row.industry or src.get("industry") or ""
        group = row.product_group or src.get("group") or ""
        items.append({
            "pos": idx + 1,
            "name": row.name,
            "quantity": row.quantity,
            "price": row.price,
            "total": row.total,
            "nds": row.vat_rate or "none",
            "nds_name": fns_refs.nds_name(row.vat_rate),
            "nds_sum": row.vat_sum,
            "measure": row.unit or src.get("measure") or "",
            "product_type": row.product_type,
            "product_type_name": fns_refs.product_type_name(row.product_type)
            if row.product_type else src.get("product_type_name", ""),
            "payment_type": row.payment_type,
            "payment_type_name": fns_refs.payment_type_name(row.payment_type)
            if row.payment_type else src.get("payment_type_name", ""),
            "group": group,
            "industry": industry,
            "marking": extra.get("marking") or [],
            "agent": extra.get("agent") or {},
            "supplier": extra.get("supplier") or {},
            "country": extra.get("country") or "",
            "customs": extra.get("customs") or "",
            "excise": extra.get("excise") or 0.0,
            "industry_details": extra.get("industry_details") or [],
        })
    if not items and doc.get("items"):
        items = doc["items"]

    calc = goods.classify_items(
        [{"name": it["name"], "total": it["total"],
          "product_type": it.get("product_type")} for it in items])
    ext = {}
    try:
        ext = json.loads(receipt.ext_json) if receipt.ext_json else {}
    except ValueError:
        ext = {}

    return {
        "receipt_id": receipt.id,
        "document": {
            **{k: v for k, v in doc.items() if k != "raw"},
            "items": items,
            "date_time": (doc.get("date_time").isoformat()
                          if isinstance(doc.get("date_time"), datetime)
                          else receipt.receipt_date.isoformat()
                          if receipt.receipt_date else None),
            "industry": calc or doc.get("industry") or {},
            "industry_source": "по наименованиям позиций",
            "region": {
                "code": getattr(receipt, "region_code", "") or
                (doc.get("seller") or {}).get("region_code", ""),
                "name": getattr(receipt, "region_name", "") or
                (doc.get("seller") or {}).get("region_name", ""),
                "city": getattr(receipt, "region_city", "") or
                (doc.get("seller") or {}).get("city", ""),
            },
            "taxation_name": getattr(receipt, "taxation_name", "") or
            (doc.get("tax") or {}).get("taxation_name", ""),
            "pay_type": getattr(receipt, "pay_type", "") or
            (doc.get("money") or {}).get("pay_type", ""),
            "receipt_note": {
                "fns_status": receipt.fns_status,
                "fns_message": receipt.fns_message,
                "details_source": receipt.details_source,
                "checked_at": (receipt.fns_checked_at.isoformat()
                               if receipt.fns_checked_at else None),
                "cache_days": 30,
            },
            "extra": ext,
            "fns_answer": fns_raw if isinstance(fns_raw, dict) else {},
            "qr": receipt.qr_data,
            "qr_png": f"/api/v1/receipts/{receipt.id}/qr.png",
        },
        "has_fns_document": bool(doc.get("raw")),
    }
