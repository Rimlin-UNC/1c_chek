"""Печатная форма УПД (формат 5.03) из данных реестра — T-62, v1.70.114.

Владелец (06.11.2026, #47): «программа должна собрать электронный УПД в печатный формат
максимально как оригинал, дать распечатать в PDF и поправить те поля, которые можно
заполнять — есть шаблон, в котором собираются данные из электронных УПД, сканов и фото».

Как сделано:

  • вид формы — утверждённый (постановление Правительства № 1137, письмо ФНС
    ММВ-20-3/96@): шапка со строками (1)–(8), таблица с графами А–9, «Всего к оплате (9)»,
    подписи и основание передачи. Реквизиты берутся из записи реестра — той же, что уходит
    в 1С, поэтому печатная форма и файл для 1С не расходятся;
  • заполняемые поля — то, чего в электронном документе нет или что зависит от бумаги:
    адреса сторон, грузоотправитель и грузополучатель, платёжно-расчётный документ, документ
    об отгрузке, идентификатор госконтракта, основание передачи, подписанты, число листов.
    Их правят в окне «Печатная форма» и запоминают к документу (колонка print_json);
  • PDF — печатью браузера («Печать → Сохранить как PDF»): сервер не тянет рендерер PDF на
    2 ГБ (ADR-25), а вид на бумаге и на экране — один и тот же HTML (ADR-45).

Чистые функции: документ + правки на входе, HTML на выходе. База здесь не трогается.
"""
from __future__ import annotations

import json
from datetime import date
from html import escape

from .models import PrimaryDocument

# Порядок и подписи полей — один список для модели запроса, базы и интерфейса.
FIELDS = (
    "status", "seller_address", "buyer_address", "shipper", "consignee", "payment_doc",
    "shipment_doc", "contract_id", "currency", "basis", "sheets",
    "signer_chief_role", "signer_chief", "signer_accountant_role", "signer_accountant",
    "signer_entrepreneur",
)
LIMITS = {
    "status": 1, "seller_address": 300, "buyer_address": 300, "shipper": 300, "consignee": 300,
    "payment_doc": 200, "shipment_doc": 200, "contract_id": 100, "currency": 100, "basis": 300,
    "sheets": 3, "signer_chief_role": 200, "signer_chief": 200, "signer_accountant_role": 200,
    "signer_accountant": 200, "signer_entrepreneur": 200,
}

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")
CURRENCIES = {"643": "Российский рубль", "978": "Евро", "840": "Доллар США", "398": "Тенге",
              "933": "Белорусский рубль", "980": "Гривна", "156": "Юань"}
# как fns_refs/upd: ставка в записи — код, на бумаге — проценты словами
VAT_LABELS = {"none": "без НДС", "agent": "НДС исчисляется налоговым агентом",
              "22": "22%", "20": "20%", "18": "18%", "10": "10%", "7": "7%", "5": "5%", "0": "0%",
              "22/122": "22/122%", "20/120": "20/120%", "18/118": "18/118%", "10/110": "10/110%",
              "7/107": "7/107%", "5/105": "5/105%"}
# Графы формы по постановлению № 1137: (номер графы, название). Номер печатается отдельной строкой,
# как в бланке; ширина колонок задана долями, поэтому длинные названия переносятся по словам.
COLUMNS = (
    ("А", "Код товара/работ, услуг"),
    ("1", "№ п/п"),
    ("1а", "Наименование товара (описание выполненных работ, оказанных услуг), имущественного права"),
    ("1б", "Код вида товара"),
    ("2", "Единица измерения: код"),
    ("2а", "Единица измерения: условное обозначение (национальное)"),
    ("3", "Количество (объём)"),
    ("4", "Цена (тариф) за единицу измерения"),
    ("5", "Стоимость товаров (работ, услуг), имущественных прав без налога — всего"),
    ("6", "В том числе сумма акциза"),
    ("7", "Налоговая ставка"),
    ("8", "Сумма налога, предъявляемая покупателю"),
    ("9", "Стоимость товаров (работ, услуг), имущественных прав с налогом — всего"),
)
# Первая графа узкая, наименование — самое широкое место бланка
COLUMN_WIDTHS = "5%,5%,24%,6%,5%,6%,7%,8%,9%,6%,7%,7%,6%"


class PrintFormError(ValueError):
    """Документ нельзя напечатать — сообщение для человека."""


# ------------------------------------------------------------------ формат значений
def money(value: float | None) -> str:
    """16400.5 → «16 400,50»; пусто → «—» (в бумажной форме пустая графа — прочерк)."""
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def qty(value: float | None) -> str:
    """Количество до трёх знаков без хвостовых нулей: 1.25 → «1,25», 10 → «10»."""
    if value is None:
        return "—"
    text = f"{value:,.3f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    return text.replace(",", " ").replace(".", ",")


def date_words(day: date | None) -> str:
    """14.10.2026 → «14 октября 2026 г.» (так печатает 1С)."""
    if day is None:
        return "—"
    return f"{day.day} {MONTHS[day.month - 1]} {day.year} г."


def vat_label(rate: str | None) -> str:
    code = (rate or "").strip().lower().replace(" ", "")
    if not code:
        return "без НДС"
    return VAT_LABELS.get(code, code)


def currency_label(code: str | None) -> str:
    value = (code or "643").strip() or "643"
    return f"{CURRENCIES.get(value, 'Валюта')}, {value}"


def status_title(function: str) -> str:
    """Статус документа: 2 — только передаточный документ (акт), 1 — счёт-фактура и передаточный."""
    return "2" if (function or "").upper() == "ДОП" else "1"


# ------------------------------------------------------------------ правки заполняемых полей
def defaults(doc: PrimaryDocument) -> dict:
    """Значения по умолчанию: что программа знает сама — из записи реестра."""
    count = len(doc.lines or [])
    shipment = ""
    if doc.doc_number:
        shipment = f"№ п/п 1–{max(count, 1)} № {doc.doc_number} от {date_words(doc.doc_date)}"
    buyer = (doc.buyer_name or "").strip()
    return {
        "status": status_title(doc.function),
        "seller_address": "",
        "buyer_address": "",
        "shipper": "он же" if buyer else "",
        "consignee": buyer,
        "payment_doc": "—",
        "shipment_doc": shipment or "—",
        "contract_id": "",
        "currency": currency_label(doc.currency),
        "basis": "",
        "sheets": "1",
        "signer_chief_role": "Руководитель организации или иное уполномоченное лицо",
        "signer_chief": "",
        "signer_accountant_role": "Главный бухгалтер или иное уполномоченное лицо",
        "signer_accountant": "",
        "signer_entrepreneur": "",
    }


def clean(raw: dict | None) -> dict:
    """Правки из базы или запроса: только известные поля, строки, без хвостов, в пределах длины."""
    out: dict = {}
    for key, value in (raw or {}).items():
        if key not in LIMITS:
            continue
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        out[key] = value.strip()[:LIMITS[key]]
    if out.get("status") not in (None, "1", "2"):
        out.pop("status", None)
    if "sheets" in out and not out["sheets"].isdigit():
        out.pop("sheets", None)
    return out


def form(doc: PrimaryDocument, saved: dict | None = None) -> dict:
    """Поля печатной формы: значения документа, поверх — правки.

    saved=None — берём сохранённые к документу правки (печать без окна), saved={} — только документ.
    """
    out = defaults(doc)
    out.update(clean(stored(doc) if saved is None else saved))
    return out


def stored(doc: PrimaryDocument) -> dict:
    raw = getattr(doc, "print_json", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def dump(values: dict) -> str:
    """Сохранить правки в колонку print_json (компактно и по-русски читаемо)."""
    return json.dumps(clean(values), ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ сборка HTML
def _cell(value: str, cls: str = "") -> str:
    text = escape(value) if value else ""
    return f'<td class="{cls}">{text}</td>' if cls else f"<td>{text}</td>"


def _underline(value: str) -> str:
    """Поле шапки: значение на линии, пустое — линия для заполнения от руки."""
    return f'<span class="val">{escape(value)}</span>' if value else '<span class="val empty"></span>'


def header_rows(doc: PrimaryDocument, values: dict) -> list[tuple[str, str, str]]:
    """Строки шапки формы: подпись, значение (HTML), номер строки."""
    corrected = ""
    if doc.correction_number:
        corrected = f"№ {doc.correction_number} от {doc.correction_date.strftime('%d.%m.%Y') if doc.correction_date else '—'}"
    seller = " ".join(part for part in (doc.seller_name, f"ИНН {doc.seller_inn}" if doc.seller_inn else "",
                                        f"КПП {doc.seller_kpp}" if doc.seller_kpp else "") if part)
    buyer = " ".join(part for part in (doc.buyer_name, f"ИНН {doc.buyer_inn}" if doc.buyer_inn else "",
                                       f"КПП {doc.buyer_kpp}" if doc.buyer_kpp else "") if part)
    return [
        ("Счёт-фактура №",
         f"{escape(doc.doc_number or '—')} от {escape(date_words(doc.doc_date))}", "1"),
        ("Исправление №", escape(corrected) if corrected else "— от —", "1а"),
        ("Продавец:", escape(seller), "2"),
        ("Адрес:", _underline(values.get("seller_address", "")), "2а"),
        ("ИНН/КПП продавца:", escape(" / ".join(x for x in (doc.seller_inn, doc.seller_kpp) if x) or "—"), "2б"),
        ("Грузоотправитель и его адрес:", _underline(values.get("shipper", "")), "3"),
        ("Грузополучатель и его адрес:", _underline(values.get("consignee", "")), "4"),
        ("К платёжно-расчётному документу №", _underline(values.get("payment_doc", "")), "5"),
        ("Документ об отгрузке №", _underline(values.get("shipment_doc", "")), "5а"),
        ("Покупатель:", escape(buyer), "6"),
        ("Адрес:", _underline(values.get("buyer_address", "")), "6а"),
        ("ИНН/КПП покупателя:", escape(" / ".join(x for x in (doc.buyer_inn, doc.buyer_kpp) if x) or "—"), "6б"),
        ("Валюта: наименование, код", _underline(values.get("currency", "") or currency_label(doc.currency)), "7"),
        ("Идентификатор государственного контракта, договора (соглашения) (при наличии):",
         _underline(values.get("contract_id", "")), "8"),
    ]


def line_rows(doc: PrimaryDocument) -> str:
    rows = []
    for index, line in enumerate(doc.lines or [], 1):
        cells = (
            _cell(line.item_code or "—"),
            _cell(str(line.position or index)),
            _cell(line.name),
            _cell("—"),
            _cell(line.unit_code or "—"),
            _cell(line.unit_name or "—"),
            _cell(qty(line.quantity)),
            _cell(money(line.price)),
            _cell(money(line.sum_without_vat)),
            _cell("без акциза"),
            _cell(vat_label(line.vat_rate)),
            _cell(money(line.vat_sum)),
            _cell(money(line.sum_with_vat)),
        )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="13" class="empty-row">В документе нет строк</td></tr>')
    return "".join(rows)


def totals_row(doc: PrimaryDocument) -> str:
    return (f"<tr class=\"totals\"><td colspan=\"8\">Всего к оплате (9)</td>"
            f"<td>{escape(money(doc.total_without_vat))}</td><td colspan=\"2\">X</td>"
            f"<td>{escape(money(doc.vat_total))}</td><td>{escape(money(doc.total_with_vat))}</td></tr>")


def signatures(values: dict) -> str:
    """Подписи бланка: должность слева, линия подписи, ФИО справа — как в форме № 1137."""
    rows = [
        (values.get("signer_chief_role", ""), values.get("signer_chief", "")),
        (values.get("signer_accountant_role", ""), values.get("signer_accountant", "")),
    ]
    if (values.get("signer_entrepreneur") or "").strip():
        rows.append(("Индивидуальный предприниматель", values.get("signer_entrepreneur", "")))
    out = ['<colgroup><col style="width: 46%"><col style="width: 30%"><col style="width: 24%"></colgroup>']
    for role, name in rows:
        out.append(f'<tr><td class="role">{escape(role)}</td>'
                   f'<td class="sign"></td><td class="name">{escape(name or "")}</td></tr>')
    out.append('<tr><td class="role"></td><td class="cap">(подпись)</td>'
               '<td class="cap">(расшифровка подписи)</td></tr>')
    # Строки бланка, которые заполняются от руки при передаче бумаги: печатаем линии,
    # чтобы форма читалась как оригинал, а не как выписка из реестра.
    for role in ("Товар (груз) передал / услуги, результаты работ, права сдал",
                 "Товар (груз) получил / услуги, результаты работ, права принял",
                 "Ответственный за правильность оформления факта хозяйственной жизни"):
        out.append(f'<tr><td class="role">{escape(role)}</td>'
                   f'<td class="sign"></td><td class="name"></td></tr>')
    return "".join(out)


CSS = """
@page { size: A4 landscape; margin: 8mm 8mm 10mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #f2f4f7; font-family: 'Inter', 'DejaVu Sans', Arial, sans-serif;
       font-size: 9px; color: #000; }
.sheet { width: 281mm; min-height: 190mm; margin: 10px auto; padding: 6mm; background: #fff;
         box-shadow: 0 2px 12px rgba(0,0,0,.15); }
.toolbar { display: flex; gap: 8px; align-items: center; justify-content: center; padding: 8px; }
.toolbar button { font: inherit; font-size: 13px; padding: 6px 14px; cursor: pointer; }
.toolbar .hint { font-size: 12px; color: #444; }
.top { display: flex; gap: 8px; align-items: flex-start; }
.form-name { font-weight: 700; font-size: 11px; line-height: 1.25; width: 42mm; }
.form-name small { display: block; font-weight: 400; font-size: 8px; margin-top: 4px; }
.status { margin-top: 6px; font-size: 10px; }
.status-code { border: 1px solid #000; padding: 0 4px; font-weight: 700; }
.hdr { border-collapse: collapse; width: 100%; }
.hdr td { padding: 1px 4px; vertical-align: bottom; }
.hdr .lbl { white-space: nowrap; }
.hdr .num { text-align: right; width: 26px; font-size: 8px; }
.hdr .val { display: inline-block; min-width: 60mm; border-bottom: 1px solid #000; padding: 0 2px; }
.hdr .val.empty { min-width: 60mm; }
.grid { border-collapse: collapse; width: 100%; margin-top: 6px; table-layout: fixed; }
.grid col { }
.grid th, .grid td { border: 1px solid #000; padding: 2px 3px; text-align: center; vertical-align: middle;
                     word-wrap: break-word; }
.grid th { font-weight: 400; font-size: 7.5px; line-height: 1.15; }
.grid td:nth-child(3) { text-align: left; }
.grid td { font-size: 8.5px; white-space: nowrap; }
.grid td:nth-child(3) { white-space: normal; }
.grid .totals td { font-weight: 700; }
.empty-row { text-align: left; color: #555; }
.meta { margin-top: 6px; font-size: 9px; }
.sign { border-collapse: collapse; margin-top: 6px; width: 100%; table-layout: fixed; }
.sign td { padding: 6px 0 2px; vertical-align: bottom; }
.sign .role { padding-right: 6px; line-height: 1.2; }
.sign .sign { border-bottom: 1px solid #000; }
.sign .name { border-bottom: 1px solid #000; padding-left: 6px; white-space: nowrap; overflow: hidden; }
.sign .cap { font-size: 7px; color: #333; text-align: center; }
.basis { margin-top: 8px; font-size: 9px; }
.basis .val { display: inline-block; min-width: 120mm; border-bottom: 1px solid #000; padding: 0 2px; }
.foot { margin-top: 10px; font-size: 8px; color: #444; }
@media print {
  body { background: #fff; }
  .sheet { width: auto; min-height: 0; margin: 0; padding: 0; box-shadow: none; }
  .toolbar { display: none; }
}
"""


def title(doc: PrimaryDocument) -> str:
    kind = "УПД" if (doc.function or "").upper() in ("СЧФДОП", "ДОП") else "Счёт-фактура"
    number = doc.doc_number or "б/н"
    day = doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else "—"
    return f"{kind} № {number} от {day}"


def html(doc: PrimaryDocument, values: dict | None = None, *, auto_print: bool = False) -> str:
    """Готовая страница печатной формы: A4 в альбомной ориентации, один документ."""
    data = form(doc, values)
    head = "".join(
        f'<tr><td class="lbl">{escape(label)}</td><td>{value}</td><td class="num">({escape(number)})</td></tr>'
        for label, value, number in header_rows(doc, data))
    columns = "".join(f'<th title="{escape(title)}">{escape(title)}</th>' for _, title in COLUMNS)
    numbers = "".join(f"<th>{escape(number)}</th>" for number, _ in COLUMNS)
    widths = "".join(f'<col style="width: {escape(width)}">' for width in COLUMN_WIDTHS.split(","))
    sheets = data.get("sheets") or "1"
    basis = data.get("basis", "")
    basis_html = (f'<span class="val">{escape(basis)}</span>' if basis else '<span class="val">&nbsp;</span>')
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title(doc))}</title>
<style>{CSS}</style></head>
<body data-print="{1 if auto_print else 0}">
<div class="toolbar"><button type="button" id="pp-print">🖨 Печать / сохранить в PDF</button>
  <span class="hint">В окне печати выберите «Сохранить как PDF», чтобы получить файл.</span></div>
<div class="sheet">
  <div class="top">
    <div>
      <div class="form-name">Универсальный передаточный документ<small>Приложение № 1 к постановлению
        Правительства РФ от 26.12.2011 № 1137 (в редакции постановления от 19.08.2017 № 981)</small></div>
      <div class="status">Статус: <span class="status-code">{escape(data.get("status", "1"))}</span></div>
      <div class="foot">1 — счёт-фактура и передаточный документ (акт)<br>2 — передаточный документ (акт)</div>
    </div>
    <table class="hdr">{head}</table>
  </div>
  <table class="grid">
    <colgroup>{widths}</colgroup>
    <thead><tr>{columns}</tr><tr>{numbers}</tr></thead>
    <tbody>{line_rows(doc)}{totals_row(doc)}</tbody>
  </table>
  <div class="meta">Документ составлен на {escape(sheets)} {_sheets_word(sheets)}</div>
  <table class="sign">{signatures(data)}</table>
  <div class="basis">Основание передачи (сдачи) / получения (приёмки): {basis_html} <span class="num">(8)</span></div>
  <div class="foot">Собран программой «Ямастер Чек» из данных реестра первички. Значения реквизитов
    электронного документа не меняются; заполняемые поля правятся в окне «Печатная форма».</div>
</div>
<script src="/js/primary_print_page.js" defer></script>
</body></html>"""


def _sheets_word(sheets: str) -> str:
    try:
        value = int(sheets)
    except (TypeError, ValueError):
        return "листах"
    return "листе" if value % 10 == 1 and value % 100 != 11 else "листах"
