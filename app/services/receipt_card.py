# ======================================================================
# Ямастер Чек — полная карточка чека (v1.70.72+)
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
#
# Задача: выжать из чека ФНС ВСЁ, что сервис отдаёт в GetTicketResponse
# (RawData=true, ФФД 1.05 / 1.1 / 1.2), и разложить это по группам —
# продавец, касса/ОФД, деньги, налоги (включая ставки 2025–2026 гг. 5 %,
# 7 %, 22 % и расчётные), кассир, покупатель/B2B, чек коррекции, позиции
# (с маркировкой DataMatrix и productCodeNew, агентом, поставщиком/providerInn,
# страной, ГТД, акцизом, дробным количеством, доп. реквизитом предмета и
# отраслевым реквизитом ФОИВ), а также все свойства чека (теги 1084, 1192).
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


_PRODUCT_CODE_NEW_TYPES = (
    "gs1m", "kmk", "ean13", "ean8", "itf14",
    "gs10", "short", "furs", "egais20", "egais30", "unknown",
)


def _marking(raw, raw_new=None) -> list[dict]:
    """Коды маркировки (DataMatrix) из ФФД 1.05/1.1 и productCodeNew ФФД 1.2."""
    out: list[dict] = []
    for one in (raw if isinstance(raw, list) else [raw]):
        if not isinstance(one, dict):
            continue
        out.append({
            "type": _get(one, "productIdType", "type", default=""),
            "gtin": str(_get(one, "gtin", "productId", default="") or ""),
            "serial": str(_get(one, "serial", "sernum", default="") or ""),
            "raw": str(_get(one, "rawProductCode", "raw", default="") or "")[:200],
        })
    if isinstance(raw_new, dict):
        low_new = _low(raw_new)
        for kind in _PRODUCT_CODE_NEW_TYPES:
            val = low_new.get(kind)
            entries = val if isinstance(val, list) else ([val] if val is not None else [])
            for entry in entries:
                if isinstance(entry, dict):
                    out.append({
                        "type": _get(entry, "productIdType", default=kind),
                        "gtin": str(_get(entry, "gtin", "productId", default="") or ""),
                        "serial": str(_get(entry, "sernum", "serial", default="") or ""),
                        "raw": str(_get(entry, "rawProductCode", "raw", default="") or "")[:200],
                    })
                elif isinstance(entry, (str, int)):
                    out.append({
                        "type": kind,
                        "gtin": "",
                        "serial": "",
                        "raw": str(entry)[:200],
                    })
    return out


def _extract_props_list(*sources) -> list[dict]:
    """Собрать массив дополнительных свойств из properties / propertiesUser."""
    props: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src in sources:
        seq = src if isinstance(src, list) else ([src] if isinstance(src, dict) else [])
        for one in seq:
            if not isinstance(one, dict):
                continue
            pname = str(_get(one, "key", "propertyName", "name", default="") or "")[:200]
            pval = str(_get(one, "value", "propertyValue", default="") or "")[:500]
            if not pname and not pval:
                continue
            pair = (pname, pval)
            if pair not in seen:
                seen.add(pair)
                props.append({"name": pname, "value": pval})
    return props


def _extract_supplier(raw_item: dict) -> dict:
    """Поставщик позиции: supplierInfo (тег 1224) + providerInn (тег 1226)."""
    s_info = _get(raw_item, "supplierInfo", default={})
    s_dict = s_info if isinstance(s_info, dict) else {}
    s_name = str(_get(s_dict, "name", default="") or "")
    s_inn = str(_get(s_dict, "inn", default="")
                or _get(raw_item, "providerInn", default="") or "").strip()
    s_phones = _get(s_dict, "phones", "supplierPhones", default="")
    if not (s_name or s_inn or s_phones):
        return {}
    out: dict = {"name": s_name, "inn": s_inn}
    if s_phones:
        out["phones"] = s_phones
    return out


def _extract_agent(raw_item: dict) -> dict:
    """Агент позиции: agentInfo + paymentAgentByProductType (тег 1222)."""
    a_info = _get(raw_item, "agentInfo", default={})
    a_dict = a_info if isinstance(a_info, dict) else {}
    a_type = _get(a_dict, "type", default=None)
    if a_type in (None, ""):
        a_type = _get(raw_item, "paymentAgentByProductType", default="")
    a_name = str(_get(a_dict, "name", default="") or "")
    a_inn = str(_get(a_dict, "inn", default="") or "")
    pay_agent = _get(a_dict, "payingAgent", default={})
    if isinstance(pay_agent, dict) and not a_name:
        a_name = str(_get(pay_agent, "operation", default="") or "")
    mto = _get(a_dict, "moneyTransferOperator", default={})
    if isinstance(mto, dict):
        a_name = a_name or str(_get(mto, "name", default="") or "")
        a_inn = a_inn or str(_get(mto, "inn", default="") or "")
    if a_type in (None, "") and not a_name and not a_inn:
        return {}
    return {"type": a_type, "name": a_name, "inn": a_inn}


# Плоские теги сумм НДС в заголовке чека ФНС (включая ставки 2025–2026 гг.)
_HEADER_NDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("nds0", "0"),
    ("nds5", "5"),
    ("nds7", "7"),
    ("nds10", "10"),
    ("nds18", "18"),
    ("nds20", "20"),
    ("nds22", "22"),
    ("nds5105", "5/105"),
    ("nds7107", "7/107"),
    ("nds10110", "10/110"),
    ("nds18118", "18/118"),
    ("nds20120", "20/120"),
    ("nds22122", "22/122"),
    ("ndsNo", "No"),
)


# --------------------------------------------------------------------------
#  Нормализация Ticket ФНС → полный документ
# --------------------------------------------------------------------------
def normalize_ticket(ticket: dict, receipt_total: float | None = None) -> dict:
    """Ticket ФНС → документ с группами полей (в рублях, с подписями)."""
    if not isinstance(ticket, dict) or not ticket:
        return {}
    if isinstance(ticket.get("receipt"), dict):
        receipt = ticket["receipt"]
    elif isinstance(ticket.get("document"), dict) and isinstance(
            ticket["document"].get("receipt"), dict):
        receipt = ticket["document"]["receipt"]
    else:
        receipt = ticket
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
        name = str(_get(raw_item, "name", "itemName", "nomination",
                        default="") or f"Позиция {pos + 1}")
        product_type = _get(raw_item, "productType")
        unit_code = _get(raw_item, "measurementUnit", "unitCode",
                         "itemsQuantityMeasure", "quantityMeasure")
        raw_nds = _get(raw_item, "nds", default="none")
        industry, group = goods.classify_name(name, product_type)
        frac = _get(raw_item, "fractionalQuantity")
        frac_dict = ({
            "numerator": _get(frac, "numerator"),
            "denominator": _get(frac, "denominator"),
        } if isinstance(frac, dict) else {})
        item_props = _extract_props_list(
            _get(raw_item, "propertiesItem", "itemProperties"))
        product_code = str(_get(raw_item, "productCode", "barCode",
                                "okpd2", "tnved", default="") or "")[:120]

        items.append({
            "pos": pos + 1,
            "name": name[:1000],
            "quantity": _num(_get(raw_item, "quantity", "qty", default=1)),
            "price": money(_get(raw_item, "price", "itemPrice")),
            "total": money(_get(raw_item, "sum", "total", "itemSum")),
            "nds": str(raw_nds),
            "nds_canonical": fns_refs.vat_canonical(raw_nds, coded=True),
            # v1.70.93: в ответе ФНС — код тега 1199 (табл. 8 ФФД, с 2026 года и 22 %)
            "nds_name": fns_refs.nds_name(raw_nds, coded=True),
            "nds_sum": money(_get(raw_item, "ndsSum", "ndsSumm")),
            "payment_type": _get(raw_item, "paymentType"),
            "payment_type_name": fns_refs.payment_type_name(
                _get(raw_item, "paymentType")),
            "product_type": product_type,
            "product_type_name": fns_refs.product_type_name(product_type),
            "measure_code": str(unit_code) if unit_code not in (None, "") else "",
            "measure": fns_refs.measure_name(unit_code),
            "group": group,
            "industry": industry,
            "marking": _marking(
                _get(raw_item, "productCodeData", "itemsProductCode", "marking"),
                _get(raw_item, "productCodeNew"),
            ),
            "agent": _extract_agent(raw_item),
            "supplier": _extract_supplier(raw_item),
            "country": _get(raw_item, "countryCode", "countryOfOriginCode"),
            "customs": _get(raw_item, "customsDeclaration",
                            "customsDeclarationNumber"),
            "excise": money(_get(raw_item, "excise", "exciseSum")),
            "industry_details": _industry_details(
                _get(raw_item, "itemsIndustryDetails", "industryDetails")),
            "item_properties": item_props,
            "fractional_quantity": frac_dict,
            "product_code": product_code,
        })

    nds: list[dict] = []
    seen_nds_rates: set[str] = set()
    for field_name, rate_code in _HEADER_NDS_FIELDS:
        val = _get(receipt, field_name)
        if val not in (None, "", 0):
            rate_for_name = "none" if rate_code == "No" else rate_code
            nds.append({
                "rate": rate_code,
                "rate_name": fns_refs.nds_name(rate_for_name),
                "sum": money(val),
            })
            seen_nds_rates.add(rate_for_name)
    amounts = _get(receipt, "amountsReceiptNds", default={}) or {}
    for one in (_get(amounts, "amountsNds", default=[]) or []):
        if isinstance(one, dict):
            raw_r = _get(one, "nds", default="")
            canon_r = fns_refs.vat_canonical(raw_r, coded=True)
            if canon_r in seen_nds_rates:
                continue
            nds.append({
                "rate": raw_r,
                "rate_name": fns_refs.nds_name(raw_r, coded=True),
                "sum": money(_get(one, "ndsSum")),
            })

    props = _extract_props_list(
        _get(receipt, "properties"),
        _get(receipt, "propertiesUser"),
    )
    props_data = str(_get(receipt, "propertiesData", default="") or "")[:200]
    if props_data and not any(p["value"] == props_data for p in props):
        props.append({"name": "Доп. реквизит чека (1192)", "value": props_data})

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

    meta = _get(receipt, "metadata", default={})
    meta_dict = meta if isinstance(meta, dict) else {}
    ofd_id = str(_get(ticket, "ofdId", "ofd", default="")
                 or _get(meta_dict, "ofdId", default="") or "")

    correction = {}
    corr_type = _get(receipt, "correctionType")
    corr_date = _get(receipt, "correctionBaseDate")
    corr_num = _get(receipt, "correctionBaseNumber")
    corr_name = _get(receipt, "correctionBaseName")
    if any(v not in (None, "") for v in (corr_type, corr_date, corr_num, corr_name)):
        correction = {
            "type": corr_type,
            "base_date": str(corr_date or ""),
            "base_number": str(corr_num or ""),
            "base_name": str(corr_name or ""),
        }

    return {
        "kind": str(_get(ticket, "kind", default="")
                    or _get(meta_dict, "subtype", default="") or ""),
        "ticket_id": _get(ticket, "id", default=""),
        "ofd_id": ofd_id,
        "date_time": _dt(_get(receipt, "dateTime", "date", "docDateTime")),
        "operation": int(operation) if str(operation).isdigit() else None,
        "operation_name": fns_refs.OPERATION_TYPES.get(
            int(operation) if str(operation).isdigit() else 0, ""),
        "seller": {
            "name": seller_name[:500],
            "inn": seller_inn[:20],
            "kpp": str(_get(receipt, "userKpp", "kpp", default="") or "")[:12],
            "address": address[:500],
            "place": place[:300],
            "email": str(_get(receipt, "sellerAddress", default="") or "")[:200],
            "sender_address": str(_get(receipt, "senderAddress", default="") or "")[:200],
            "fns_site": str(_get(receipt, "fnsSite", default="") or "")[:200],
            "region_code": geo["region_code"],
            "region_name": geo["region_name"],
            "city": geo["city"],
            "postal": geo["postal"],
            "region_source": geo["source"],
        },
        "kkt": {
            "reg_id": str(_get(receipt, "kktRegId", default="") or "").strip(),
            "number_kkt": str(_get(receipt, "numberKkt", default="") or "").strip(),
            "fn": str(_get(receipt, "fiscalDriveNumber", default="") or ""),
            "fd": str(_get(receipt, "fiscalDocumentNumber", default="") or ""),
            "fp": str(_get(receipt, "fiscalSign", default="") or ""),
            "shift": _get(receipt, "shiftNumber"),
            "request": _get(receipt, "requestNumber"),
            "ffd": str(_get(receipt, "fiscalDocumentFormatVer", default="") or ""),
            "message_fp": _get(receipt, "messageFiscalSign"),
            "redefine_mask": _get(receipt, "redefineMask", "redefine_mask"),
            "receipt_code": _get(receipt, "receiptCode", "bsoCode"),
            "machine_number": str(_get(receipt, "machineNumber", default="") or ""),
            "internet_sign": _get(receipt, "internetSign"),
            "ofd_inn": str(_get(receipt, "ofdInn", default="") or "")[:20],
            "ofd_name": str(_get(receipt, "ofdName", default="") or "")[:200],
            "payment_agent_type": _get(receipt, "paymentAgentType"),
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
            "name": str(_get(receipt, "buyer", "clientInfo", default="") or ""),
            "inn": str(_get(receipt, "buyerInn", default="") or "")[:20],
            "contact": str(_get(receipt, "buyerPhoneOrAddress", default="") or ""),
        },
        "correction": correction,
        "properties_data": props_data,
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
    kkt = doc["kkt"]

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
    elif seller.get("city") and not receipt.region_city:
        receipt.region_city = seller["city"][:120]
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

    # Позиции: сохраняем фискальный код НДС в vat_rate (как ожидает exporter.vat_1c
    # для details_source != 'manual_edit'), а каноническую ставку и все теги ФФД —
    # в extra_json позиции.
    if doc["items"] and (force_items or not receipt.items):
        receipt.items.clear()
        for it in doc["items"][:200]:
            item_extra = {
                k: v for k, v in (
                    ("marking", it["marking"]),
                    ("agent", it["agent"]),
                    ("supplier", it["supplier"]),
                    ("country", it["country"]),
                    ("customs", it["customs"]),
                    ("excise", it["excise"]),
                    ("industry_details", it["industry_details"]),
                    ("measure_code", it["measure_code"]),
                    ("vat_canonical", it["nds_canonical"]),
                    ("item_properties", it.get("item_properties")),
                    ("fractional_quantity", it.get("fractional_quantity")),
                    ("product_code", it.get("product_code")),
                ) if v not in (None, "", [], {}, 0, 0.0)
            }
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
                extra_json=json.dumps(item_extra, ensure_ascii=False),
            ))
    if receipt.items:
        from ..goods import service as goods_service
        goods_service.classify_receipt(db, receipt)

    # Расширенные данные источника: слияние без перезаписи сохранённых ключей
    try:
        ext = json.loads(receipt.ext_json) if receipt.ext_json else {}
        ext = ext if isinstance(ext, dict) else {}
    except ValueError:
        ext = {}
    derived_ext = {
        "taxation": tax["taxation_name"],
        "pay_type": money["pay_type"],
        "region": seller["region_name"] or seller["region_code"],
        "region_city": seller["city"],
        "postal_code": seller["postal"],
        "industry": industry,
        "product_group": doc["industry"].get("group", ""),
        "industry_source": "по наименованиям позиций",
        "nds_totals": tax["nds"],
        "ticket_id": doc["ticket_id"],
        "ticket_kind": doc["kind"],
        "fns_date_time": (doc["date_time"].isoformat()
                          if isinstance(doc.get("date_time"), datetime)
                          else ""),
        "operation_type": doc["operation"],
        "seller_kpp": seller["kpp"],
        "seller_email": seller["email"],
        "sender_address": seller["sender_address"],
        "fns_site": seller["fns_site"],
        "cashier_inn": doc["cashier"]["inn"],
        "buyer_inn": doc["buyer"]["inn"],
        "kkt_reg_id": kkt["reg_id"],
        "number_kkt": kkt["number_kkt"],
        "fiscal_drive_number": kkt["fn"],
        "fiscal_document_number": kkt["fd"],
        "fiscal_sign": kkt["fp"],
        "shift_number": kkt["shift"],
        "request_number": kkt["request"],
        "ffd_version": kkt["ffd"],
        "message_fiscal_sign": kkt["message_fp"],
        "redefine_mask": kkt["redefine_mask"],
        "receipt_code": kkt["receipt_code"],
        "machine_number": kkt["machine_number"],
        "internet_sign": kkt["internet_sign"],
        "payment_agent_type": kkt["payment_agent_type"],
        "retail_place": seller["place"],
        "ofd_id": doc["ofd_id"],
        "ofd_inn": kkt["ofd_inn"],
        "ofd_name": kkt["ofd_name"],
        "prepaid_sum": money["prepaid"] if money["prepaid"] else None,
        "credit_sum": money["credit"] if money["credit"] else None,
        "provision_sum": money["provision"] if money["provision"] else None,
        "correction": doc["correction"],
        "properties_data": doc["properties_data"],
        "properties": doc["properties"],
        "industry_details": doc["industry_details"],
        "items_meta": [
            {"pos": it["pos"], "payment_type": it["payment_type_name"],
             "product_type": it["product_type_name"],
             "measure": it["measure"], "group": it["group"]}
            for it in doc["items"] if it["product_type"] or it["payment_type"]]
        if any(it["product_type"] or it["payment_type"] for it in doc["items"])
        else [],
    }
    for key, value in derived_ext.items():
        if value not in (None, "", [], {}):
            ext.setdefault(key, value)
    receipt.ext_json = json.dumps(ext, ensure_ascii=False)
    receipt.details_source = receipt.details_source or source
    receipt.full_data = bool(receipt.full_data or doc["items"])
    if db is not None:
        try:
            from . import policy
            policy.refresh_receipt(db, receipt)
        except Exception:                          # noqa: BLE001
            pass
    return doc


def enrich_items(receipt) -> int:
    """Проставить категорию, группу и отрасль позициям без обращения к ФНС."""
    from sqlalchemy.orm import object_session

    from ..goods import service as goods_service
    return goods_service.classify_receipt(object_session(receipt), receipt)["items"]


def enrich_receipt(db, receipt) -> dict:
    """Достроить регион/отрасль/группы по уже сохранённым данным чека."""
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

    geo = regions.detect(receipt.merchant_inn, receipt.merchant_address)
    if geo["region_code"] and not (receipt.details_source == "manual_edit" and receipt.region_code):
        receipt.region_code = geo["region_code"]
        receipt.region_name = geo["region_name"] or ""
        receipt.region_city = geo["city"] or ""
    if geo.get("city") and not receipt.region_city:
        receipt.region_city = geo["city"][:120]
    from ..goods import service as goods_service
    goods_service.classify_receipt(db, receipt)
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

    ext = {}
    try:
        ext = json.loads(receipt.ext_json) if receipt.ext_json else {}
        ext = ext if isinstance(ext, dict) else {}
    except ValueError:
        ext = {}

    if not doc:
        geo = regions.detect(receipt.merchant_inn, receipt.merchant_address)
        doc = {
            "kind": ext.get("ticket_kind") or ("kkt" if receipt.fn else ""),
            "ticket_id": ext.get("ticket_id", ""),
            "ofd_id": ext.get("ofd_id", ""),
            "date_time": receipt.receipt_date,
            "operation": receipt.operation,
            "operation_name": fns_refs.OPERATION_TYPES.get(
                receipt.operation or 1, ""),
            "seller": {
                "name": receipt.merchant_name or "",
                "inn": receipt.merchant_inn or "",
                "kpp": ext.get("seller_kpp", ""),
                "address": receipt.merchant_address or "",
                "place": ext.get("retail_place", ""),
                "email": ext.get("seller_email", ""),
                "sender_address": ext.get("sender_address", ""),
                "fns_site": ext.get("fns_site", ""),
                "region_code": (getattr(receipt, "region_code", "") or
                                geo["region_code"]),
                "region_name": (getattr(receipt, "region_name", "") or
                                geo["region_name"]),
                "city": (getattr(receipt, "region_city", "") or geo["city"]),
                "postal": ext.get("postal_code") or geo["postal"],
                "region_source": (getattr(receipt, "region_code", "") and
                                  "receipt") or geo["source"] or "",
            },
            "kkt": {
                "reg_id": ext.get("kkt_reg_id", ""),
                "number_kkt": ext.get("number_kkt", ""),
                "fn": receipt.fn, "fd": receipt.fd, "fp": receipt.fp,
                "shift": ext.get("shift_number"),
                "request": ext.get("request_number"),
                "ffd": ext.get("ffd_version", ""),
                "message_fp": ext.get("message_fiscal_sign"),
                "redefine_mask": ext.get("redefine_mask"),
                "receipt_code": ext.get("receipt_code"),
                "machine_number": ext.get("machine_number", ""),
                "internet_sign": ext.get("internet_sign"),
                "ofd_inn": ext.get("ofd_inn", ""),
                "ofd_name": ext.get("ofd_name", ""),
                "payment_agent_type": ext.get("payment_agent_type"),
            },
            "money": {
                "total": receipt.total_sum,
                "cash": receipt.cash_sum, "ecash": receipt.ecash_sum,
                "prepaid": float(ext.get("prepaid_sum") or 0.0),
                "credit": float(ext.get("credit_sum") or 0.0),
                "provision": float(ext.get("provision_sum") or 0.0),
                "pay_type": getattr(receipt, "pay_type", "") or "",
            },
            "tax": {
                "taxation": getattr(receipt, "taxation", "") or "",
                "taxation_name": getattr(receipt, "taxation_name", "") or "",
                "nds": ext.get("nds_totals") or [],
            },
            "cashier": {
                "name": receipt.cashier or "",
                "inn": ext.get("cashier_inn", ""),
            },
            "buyer": {
                "name": "",
                "inn": ext.get("buyer_inn", ""),
                "contact": "",
            },
            "correction": ext.get("correction") or {},
            "properties_data": ext.get("properties_data", ""),
            "items": [],
            "properties": ext.get("properties") or [],
            "industry_details": ext.get("industry_details") or [],
            "industry": {},
            "raw": {},
        }

    from ..goods.view import item_view
    items = []
    doc_items = {it["pos"]: it for it in doc.get("items", [])}
    coded_nds = (bool(getattr(receipt, "details_source", ""))
                 and getattr(receipt, "details_source", "") != "manual_edit")
    for idx, row in enumerate(receipt.items):
        extra = {}
        try:
            extra = json.loads(row.extra_json) if row.extra_json else {}
        except ValueError:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        src = doc_items.get(idx + 1, {})
        industry = row.industry or src.get("industry") or ""
        group = row.product_group or src.get("group") or ""
        nds_label = (fns_refs.nds_name(extra["vat_canonical"])
                     if extra.get("vat_canonical")
                     else fns_refs.nds_name(row.vat_rate, coded=coded_nds))
        items.append({
            "pos": idx + 1,
            "name": row.name,
            "quantity": row.quantity,
            "price": row.price,
            "total": row.total,
            "nds": row.vat_rate or "none",
            "nds_name": nds_label,
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
            "marking": extra.get("marking") or src.get("marking") or [],
            "agent": extra.get("agent") or src.get("agent") or {},
            "supplier": extra.get("supplier") or src.get("supplier") or {},
            "country": extra.get("country") or src.get("country") or "",
            "customs": extra.get("customs") or src.get("customs") or "",
            "excise": extra.get("excise") or src.get("excise") or 0.0,
            "industry_details": (extra.get("industry_details")
                                 or src.get("industry_details") or []),
            "item_properties": (extra.get("item_properties")
                                or src.get("item_properties") or []),
            "fractional_quantity": (extra.get("fractional_quantity")
                                    or src.get("fractional_quantity") or {}),
            "product_code": (extra.get("product_code")
                             or src.get("product_code") or ""),
            **item_view(row),
        })
    if not items and doc.get("items"):
        items = doc["items"]

    calc = goods.classify_items(
        [{"name": it["name"], "total": it["total"],
          "product_type": it.get("product_type"),
          "group": it.get("group") if it.get("industry") else "",
          "industry": it.get("industry") or ""} for it in items])

    saved_data_json = json.dumps({"fns_answer": fns_raw, "extra": ext},
                                 ensure_ascii=False, indent=2)
    return {
        "receipt_id": receipt.id,
        "saved_data_json": saved_data_json,
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
