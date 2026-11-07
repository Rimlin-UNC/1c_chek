"""Синтетическая печатная форма УПД (HTML) для тестов распознавания PDF и сканов (T-61).

Раскладка повторяет типовую печатную форму УПД из 1С: шапка счёта-фактуры со строками (1)–(8),
таблица товаров с графами А–13, строка «Всего к оплате (9)» и подписи. Реквизиты выдуманы (ADR-14):
ИНН берутся из генератора с правильной контрольной суммой (tests/primary_samples.py).

Из этого HTML через Chromium получены образцы в tests/fixtures/primary/:
  • upd_print_text.pdf — PDF с текстовым слоем (как «Печать → Сохранить в PDF» из 1С);
  • upd_print_scan.png — та же страница картинкой (как скан, 200 точек на дюйм).
Повторная сборка образцов: python3 tests/primary_print.py <каталог> (печать — вручную в Chromium).
"""
from __future__ import annotations

import sys
from html import escape
from pathlib import Path

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")

# Фиксированные выдуманные реквизиты образцов: контрольные суммы ИНН верные (см. primary_samples.inn10).
SELLER = {"name": "ООО «СтройБаза»", "inn": "5001007329", "kpp": "500101001",
          "address": "141400, Московская обл., г. Тестовск, ул. Синтетическая, д. 1"}
BUYER = {"name": "ООО «Ромашка-Строй»", "inn": "7704123450", "kpp": "770401001",
         "address": "119000, г. Москва, ул. Проверочная, д. 2, оф. 3"}
LINES = [
    {"code": "CEM-500", "name": "Цемент М500, мешок 50 кг", "okei": "796", "unit": "шт", "qty": "10",
     "price": "500,00", "base": "5 000,00", "rate": "22%", "vat": "1 100,00", "total": "6 100,00"},
    {"code": "ARM-12", "name": "Арматура А500С d12 мм", "okei": "168", "unit": "т", "qty": "1,250",
     "price": "64 000,00", "base": "80 000,00", "rate": "22%", "vat": "17 600,00", "total": "97 600,00"},
    {"code": "", "name": "Доставка до объекта", "okei": "796", "unit": "шт", "qty": "1",
     "price": "1 500,00", "base": "1 500,00", "rate": "22%", "vat": "330,00", "total": "1 830,00"},
]
TOTALS = {"base": "86 500,00", "vat": "19 030,00", "total": "105 530,00"}


def _date_words(day: str) -> str:
    d, m, y = day.split(".")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y} г."


def upd_print_html(*, number: str = "УТ-215", day: str = "14.10.2026", status: str = "1",
                   seller: dict | None = None, buyer: dict | None = None, lines: list[dict] | None = None,
                   totals: dict | None = None) -> str:
    """HTML печатной формы. Значения сумм — строками в «бумажном» виде (пробелы в тысячах, запятая)."""
    s, b = seller or SELLER, buyer or BUYER
    rows = lines if lines is not None else LINES
    t = totals or TOTALS
    nbsp = lambda v: escape(v).replace(" ", "&nbsp;")  # noqa: E731 — как в 1С: неразрывные пробелы в числах
    head = [
        ("Счет-фактура №", f"{escape(number)} от {_date_words(day)}", "(1)"),
        ("Исправление №", "-- от --", "(1а)"),
        ("Продавец:", escape(s["name"]), "(2)"),
        ("Адрес:", escape(s["address"]), "(2а)"),
        ("ИНН/КПП продавца:", f'{s["inn"]}/{s["kpp"]}', "(2б)"),
        ("Грузоотправитель и его адрес:", "он же", "(3)"),
        ("Грузополучатель и его адрес:", f'{escape(b["name"])}, {escape(b["address"])}', "(4)"),
        ("К платежно-расчетному документу №", "-- от --", "(5)"),
        ("Документ об отгрузке", f"№ п/п 1-{len(rows)} № {escape(number)} от {day}", "(5а)"),
        ("Покупатель:", escape(b["name"]), "(6)"),
        ("Адрес:", escape(b["address"]), "(6а)"),
        ("ИНН/КПП покупателя:", f'{b["inn"]}/{b["kpp"]}', "(6б)"),
        ("Валюта: наименование, код", "Российский рубль, 643", "(7)"),
        ("Идентификатор государственного контракта, договора (соглашения) (при наличии):", "", "(8)"),
    ]
    head_html = "".join(f"<tr><td class='lbl'>{a}</td><td class='val'>{v}</td><td class='num'>{n}</td></tr>"
                        for a, v, n in head)
    cols = ["Код товара/ работ, услуг", "№ п/п", "Наименование товара (описание выполненных работ, оказанных "
            "услуг), имущественного права", "Код вида товара", "Единица измерения: код",
            "условное обозначение (национальное)", "Количество (объем)", "Цена (тариф) за единицу измерения",
            "Стоимость товаров (работ, услуг), имущественных прав без налога — всего",
            "В том числе сумма акциза", "Налоговая ставка", "Сумма налога, предъявляемая покупателю",
            "Стоимость товаров (работ, услуг), имущественных прав с налогом — всего"]
    nums = ["А", "1", "1а", "1б", "2", "2а", "3", "4", "5", "6", "7", "8", "9"]
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in (
            escape(r["code"]) or "--", str(i), escape(r["name"]), "--", r["okei"], escape(r["unit"]),
            nbsp(r["qty"]), nbsp(r["price"]), nbsp(r["base"]), "без акциза", escape(r["rate"]),
            nbsp(r["vat"]), nbsp(r["total"]))) + "</tr>"
        for i, r in enumerate(rows, 1))
    total_row = (f"<tr class='tot'><td colspan='8'>Всего к оплате (9)</td><td>{nbsp(t['base'])}</td>"
                 f"<td colspan='2'>X</td><td>{nbsp(t['vat'])}</td><td>{nbsp(t['total'])}</td></tr>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>УПД {escape(number)}</title>
<style>
 @page {{ size: A4 landscape; margin: 8mm; }}
 body {{ font-family: 'DejaVu Sans', Arial, sans-serif; font-size: 9px; color: #000; margin: 0; }}
 .wrap {{ display: flex; gap: 6px; }}
 .side {{ width: 110px; border-right: 1px solid #000; padding-right: 4px; }}
 .side b {{ font-size: 11px; }}
 table {{ border-collapse: collapse; }}
 .hdr td {{ padding: 1px 4px; vertical-align: top; }}
 .hdr .lbl {{ white-space: nowrap; }} .hdr .val {{ border-bottom: 1px solid #000; min-width: 380px; }}
 .grid {{ width: 100%; margin-top: 6px; }}
 .grid th, .grid td {{ border: 1px solid #000; padding: 2px 3px; text-align: center; }}
 .grid td:nth-child(3) {{ text-align: left; }}
 .tot td {{ font-weight: bold; }}
 .sign {{ margin-top: 10px; display: flex; gap: 40px; }}
</style></head><body>
<div class="wrap"><div class="side"><b>Универсальный передаточный документ</b><br><br>Статус: {status}<br><br>
1 — счет-фактура и передаточный документ (акт)<br>2 — передаточный документ (акт)</div>
<div><table class="hdr">{head_html}</table></div></div>
<table class="grid"><thead><tr>{"".join(f"<th>{escape(c)}</th>" for c in cols)}</tr>
<tr>{"".join(f"<th>{n}</th>" for n in nums)}</tr></thead><tbody>{body}{total_row}</tbody></table>
<div>Документ составлен на 1 листе</div>
<div class="sign"><div>Руководитель организации или иное уполномоченное лицо ____________ Тестов Т. Т.</div>
<div>Главный бухгалтер или иное уполномоченное лицо ____________ Проверкина П. П.</div></div>
<div>Основание передачи (сдачи) / получения (приемки): Договор поставки № 12 от 01.02.2026 [8]</div>
</body></html>"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "upd_print.html").write_text(upd_print_html(), encoding="utf-8")
    print(out / "upd_print.html")
