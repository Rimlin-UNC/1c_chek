# -*- coding: utf-8 -*-
"""Ямастер Чек — v1.73.8: настоящий PDF авансового отчёта.

Сервер собирает HTML-бланк и превращает его в PDF через xhtml2pdf/ReportLab.
Важно: это НЕ картинка страницы. В PDF встроен DejaVuSans (ToUnicode CMap),
поэтому кириллицу можно выделять, копировать и искать; QR чеков остаются
картинками, как фискальные коды. Системных Pango/Cairo/браузера не нужно.

Данные приходят из того же `_build_advance_report`, что и JSON/CSV: клиент не
может подсунуть серверу произвольные суммы или чужую компанию.
"""
from __future__ import annotations

import base64
import html
import io
import re
from pathlib import Path

import qrcode
from xhtml2pdf import pisa
from xhtml2pdf.config.resources import ResourceAccessPolicy

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
FONT_PATH = FONT_DIR / "DejaVuSans.ttf"
PDF_QR_LIMIT = 40
MAX_TEXT = 200

_FNS = {
    "valid": "Действителен",
    "invalid": "Недействителен",
    "not_found": "Не найден",
    "unknown": "Не проверен",
    "pending": "Ожидает проверки",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "№": "N",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "Kh", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh", "Щ": "Sch",
    "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "Yu", "Я": "Ya",
}

_CSS = """
@page { size: A4 portrait; margin: 10mm 15mm 10mm 20mm; }
@font-face { font-family: DejaVu; src: url('DejaVuSans.ttf'); font-weight: normal; }
@font-face { font-family: DejaVu; src: url('DejaVuSans.ttf'); font-weight: bold; }
body { font-family: DejaVu; font-size: 9pt; color: #111; line-height: 1.35; }
h1 { font-size: 15pt; text-align: center; margin: 0 0 2mm 0; }
h2 { font-size: 11pt; margin: 5mm 0 2mm 0; }
p { margin: 0 0 2mm 0; }
table { width: 100%; }
td, th { border: 0.4pt solid #555; padding: 1.5mm; font-size: 8pt; vertical-align: top; }
th { background: #f0f0f0; }
td.nb, th.nb { border: 0; }
.small { font-size: 7.5pt; color: #444; }
.center { text-align: center; }
.right { text-align: right; }
.num { text-align: right; white-space: nowrap; }
.total { font-weight: bold; }
.callout { border: 0.5pt solid #888; padding: 2mm; margin: 2mm 0; font-size: 8pt; }
.signs td { height: 13mm; }
.qr { width: 90px; height: 90px; }
.receipt { margin: 0 0 3mm 0; padding: 2mm; border: 0.4pt solid #777; page-break-inside: avoid; }
.page-break { page-break-before: always; }
"""


def _text(value, limit: int = MAX_TEXT) -> str:
    """Строка без управляющих символов и с предсказуемой длиной."""
    out = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value if value is not None else ""))
    return out[:limit]


def _e(value, limit: int = MAX_TEXT) -> str:
    return html.escape(_text(value, limit), quote=True)


def _meta(meta: dict, key: str, default: str = "") -> str:
    value = (meta or {}).get(key, default)
    return _text(default if value is None else value)


def _money(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.2f}".replace(".", ",")


def _date(value) -> str:
    s = _text(value, 10)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"{s[8:10]}.{s[5:7]}.{s[:4]}"
    return s


def _receipt_dt(value) -> str:
    s = _text(value, 25).replace("T", " ")
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        d = _date(s[:10])
        return f"{d} {s[11:16]}" if len(s) >= 16 else d
    return s


def _fns(value) -> str:
    return _FNS.get(str(value or ""), str(value or "—"))


def _safe_filename(name: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', " ", _text(name, 300))
    text = re.sub(r"\s+", " ", text).strip().strip(". ")
    return text[:180].rstrip(". ") or "avansovy-otchet"


def _ascii_filename(name: str) -> str:
    name = _safe_filename(name)
    stem, _, ext = name.rpartition(".")
    if not stem:
        stem, ext = name, ""
    out = []
    for ch in stem:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        else:
            out.append("_")
    safe = re.sub(r"_+", "_", "".join(out)).strip("._-")[:120].rstrip("._-")
    return f"{safe or 'avansovy-otchet'}.{ext.lower() if ext else 'pdf'}"


def _pdf_base(report: dict, meta: dict) -> str:
    org = (((report.get("requisites") or {}).get("Организация")) or {})
    company = report.get("company") or {}
    name = (org.get("НаименованиеСокращенное") or company.get("short_name")
            or company.get("name") or _meta(meta, "ao-org", "Организация"))
    number = _meta(meta, "ao-num") or _meta(meta, "report_number", "АО")
    date = _meta(meta, "ao-date") or (report.get("period") or {}).get("to", "")
    amount = _money((report.get("total") or {}).get("sum", 0)).replace(",", "-")
    return _safe_filename("-".join([number, name, date, amount]))


def ao_pdf_unicode_filename(report: dict, meta: dict) -> str:
    """Кириллическое имя для `filename*=UTF-8''…`."""
    return _safe_filename(f"{_pdf_base(report, meta)}.pdf")


def ao_pdf_filename(report: dict, meta: dict) -> str:
    """ASCII-имя для `filename=…`: Chromium иначе теряет кириллицу."""
    return _ascii_filename(f"{_pdf_base(report, meta)}.pdf")


def _qr_uri(qr_data: str) -> str:
    text = _text(qr_data, 1000)
    if not text:
        return ""
    try:
        image = qrcode.make(text, box_size=4, border=2)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def _org_name(report: dict, meta: dict) -> str:
    org = (((report.get("requisites") or {}).get("Организация")) or {})
    company = report.get("company") or {}
    return (org.get("НаименованиеСокращенное") or company.get("short_name")
            or company.get("name") or _meta(meta, "ao-org", "Организация"))


def _html(report: dict, meta: dict, qr_by_id: dict, include_receipts: bool) -> str:
    rows = list(report.get("rows") or [])
    total = report.get("total") or {}
    company = report.get("company") or {}
    org = (((report.get("requisites") or {}).get("Организация")) or {})
    person = report.get("person") or {}
    period = report.get("period") or {}
    by_assignee = list(report.get("by_assignee") or [])
    many = len(by_assignee) > 1 or (len(by_assignee) == 1 and by_assignee[0].get("name") == "—")
    title = "СВОДНАЯ ВЕДОМОСТЬ ПО АВАНСОВЫМ ОТЧЁТАМ" if many else "АВАНСОВЫЙ ОТЧЁТ (АО-1)"
    number = _meta(meta, "ao-num") or _meta(meta, "report_number", "б/н")
    report_date = _date(_meta(meta, "ao-date") or period.get("to", ""))
    assignee = _meta(meta, "assignee") or _text(person.get("ФИО"))
    if not assignee and len(by_assignee) == 1:
        assignee = _text(by_assignee[0].get("name"))
    position = _meta(meta, "ao-post") or _text(person.get("Должность"))
    purpose = _meta(meta, "ao-purpose")
    department = _meta(meta, "ao-dept")
    head = _meta(meta, "ao-head") or _text(org.get("Руководитель"))
    buh = _meta(meta, "ao-buh")
    cash = _money(_meta(meta, "ao-cash", "0"))
    card = _money(_meta(meta, "ao-card", "0"))
    dt_account = _meta(meta, "ao-dt", "71.01")
    kt_account = _meta(meta, "ao-kt", "50.01")
    acc_account = _meta(meta, "ao-acc", "44.01")
    rko = _meta(meta, "ao-rko")
    pay = _meta(meta, "ao-pay")
    total_sum = _money(total.get("sum", 0))
    count = int(total.get("count") or len(rows))

    head_rows = "".join([
        f"<tr><td class='nb' width='28%'>Организация</td><td class='nb'>{_e(_org_name(report, meta))}</td>"
        f"<td class='nb' width='18%'>Номер</td><td class='nb'>{_e(number)}</td></tr>",
        f"<tr><td class='nb'>Полное наименование</td><td class='nb'>{_e(org.get('НаименованиеПолное') or company.get('name'))}</td>"
        f"<td class='nb'>Дата отчёта</td><td class='nb'>{_e(report_date)}</td></tr>",
        f"<tr><td class='nb'>ИНН / КПП</td><td class='nb'>{_e(org.get('ИНН') or company.get('inn'))} / {_e(org.get('КПП'))}</td>"
        f"<td class='nb'>Период</td><td class='nb'>{_e(_date(period.get('from')))} — {_e(_date(period.get('to')))}</td></tr>",
        f"<tr><td class='nb'>Подотчётное лицо</td><td class='nb'>{_e(assignee or '—')}</td>"
        f"<td class='nb'>Должность</td><td class='nb'>{_e(position or '—')}</td></tr>",
        f"<tr><td class='nb'>Подразделение</td><td class='nb'>{_e(department or '—')}</td>"
        f"<td class='nb'>Назначение аванса</td><td class='nb'>{_e(purpose or '—')}</td></tr>",
    ])

    group_rows = ""
    for group in by_assignee:
        group_rows += (f"<tr><td>{_e(group.get('name'))}</td>"
                       f"<td class='num'>{int(group.get('count') or 0)}</td>"
                       f"<td class='num'>{_money(group.get('sum', 0))}</td></tr>")

    row_lines = ""
    for i, row in enumerate(rows, 1):
        row_lines += (
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_e(_receipt_dt(row.get('receipt_date')), 25)}</td>"
            f"<td>{_e(row.get('assignee') or '—')}</td>"
            f"<td>{_e(row.get('merchant_name') or '—')}</td>"
            f"<td>{_e(row.get('merchant_inn') or '—')}</td>"
            f"<td>{_e('/'.join(filter(None, [_text(row.get('fn'), 30), _text(row.get('fd'), 20), _text(row.get('fp'), 20)])))}</td>"
            f"<td>{_e(_fns(row.get('fns_status')))}</td>"
            f"<td class='num'>{_money(row.get('total_sum', 0))}</td>"
            "</tr>")

    qr_html = ""
    if include_receipts:
        shown = rows[:PDF_QR_LIMIT]
        cards = ""
        for i, row in enumerate(shown, 1):
            uri = _qr_uri(str((qr_by_id or {}).get(row.get("id")) or ""))
            image = (f"<img class='qr' width='90' height='90' src='{uri}' alt='QR чека {i}'>"
                     if uri else "<div class='small'>Фискальный QR не сохранён</div>")
            cards += (
                "<div class='receipt'>"
                f"<table><tr><td class='nb' width='25%'>{image}</td><td class='nb'>"
                f"<b>Приложение № {i}</b> · {_e(_receipt_dt(row.get('receipt_date')), 25)}<br>"
                f"Продавец: {_e(row.get('merchant_name') or '—')}<br>"
                f"ИНН: {_e(row.get('merchant_inn') or '—')} · ФН/ФД/ФП: "
                f"{_e('/'.join(filter(None, [_text(row.get('fn'), 30), _text(row.get('fd'), 20), _text(row.get('fp'), 20)])))}<br>"
                f"Статус ФНС: {_e(_fns(row.get('fns_status')))} · Сумма: <b>{_money(row.get('total_sum', 0))} ₽</b>"
                "</td></tr></table></div>")
        note = (f"<p class='small'>Показаны QR первых {len(shown)} чеков из {len(rows)}; "
                "все чеки перечислены в таблице выше. Копии не заменяют обязательные "
                "подтверждающие документы.</p>" if len(rows) > len(shown) else
                "<p class='small'>Копии чеков не заменяют обязательные подтверждающие документы.</p>")
        qr_html = (f"<div class='page-break'></div><h2>Приложения: копии чеков</h2>{cards}{note}")

    warning = ""
    if report.get("invalid_count"):
        warning = (f"<div class='callout'>⚠ В отчёте {int(report['invalid_count'])} чек(ов) "
                   "со статусом «Недействителен» — исключите их из учёта или перепроверьте.</div>")

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Ymaster Check - Advance report PDF</title>
<style>{_CSS}</style></head><body>
<h1>{_e(title)}</h1>
<p class="center small">Проект документа. Проверьте реквизиты, операции возврата, личные расходы и подписи в бухгалтерской системе.</p>
<table>{head_rows}</table>
{warning}
<h2>Сводка по подотчётным лицам</h2>
<table><tr><th>Подотчётник</th><th class="right">Чеков</th><th class="right">Сумма, ₽</th></tr>
{group_rows or '<tr><td colspan="3">Нет данных</td></tr>'}
<tr class="total"><td>ИТОГО</td><td class="num">{count}</td><td class="num">{total_sum}</td></tr></table>
<h2>Реестр чеков</h2>
<table><tr><th>№</th><th>Дата</th><th>Подотчётник</th><th>Продавец</th><th>ИНН</th>
<th>ФН/ФД/ФП</th><th>Статус ФНС</th><th class="right">Сумма, ₽</th></tr>
{row_lines or '<tr><td colspan="8">Чеки не найдены</td></tr>'}
<tr class="total"><td colspan="7">ИТОГО</td><td class="num">{total_sum}</td></tr></table>
<h2>Расчёт и подписи</h2>
<table><tr><td>Выдано наличными, ₽</td><td class="num">{cash}</td>
<td>Перечислено на карту, ₽</td><td class="num">{card}</td></tr>
<tr><td>Дебет</td><td>{_e(dt_account)}</td><td>Кредит</td><td>{_e(kt_account)}</td></tr>
<tr><td>Счёт затрат</td><td>{_e(acc_account)}</td><td>РКО / платёжное поручение</td>
<td>{_e('/'.join(filter(None, [rko, pay])) or '—')}</td></tr></table>
<table class="signs" style="margin-top:4mm"><tr><th>Руководитель</th><th>Главный бухгалтер</th><th>Подотчётник</th><th>Кассир</th></tr>
<tr><td>{_e(head or '—')}<br><span class="small">подпись / расшифровка</span></td>
<td>{_e(buh or '—')}<br><span class="small">подпись / расшифровка</span></td>
<td>{_e(assignee or '—')}<br><span class="small">подпись / расшифровка</span></td>
<td><br><span class="small">подпись / расшифровка</span></td></tr></table>
<p class="small" style="margin-top:3mm">Сформировано в «Ямастер Чек». Текст PDF выделяется и копируется; фискальные QR встроены как изображения.</p>
{qr_html}
</body></html>"""


def _link_callback(uri, _basepath=None):
    """Пропускаем только встроенный TTF и data:-QR; всё внешнее запрещено."""
    value = str(uri or "")
    if not value or value.startswith("data:"):
        return None
    if value in ("DejaVuSans.ttf", str(FONT_PATH)):
        return str(FONT_PATH)
    raise ValueError(f"В PDF запрещён внешний ресурс: {value[:120]}")


def _resource_policy():
    """xhtml2pdf не должен читать ни URL, ни файлы вне каталога шрифта."""
    return ResourceAccessPolicy.server(
        base_dir=FONT_DIR,
        allow_remote=False,
        allow_private_networks=False,
        allow_local_outside_base=False,
        max_resources=PDF_QR_LIMIT + 2,
        max_render_seconds=90,
    )


def build_ao_pdf(report: dict, meta: dict, qr_by_id: dict | None = None,
                 include_receipts: bool = False) -> bytes:
    """Собрать PDF. Бросает RuntimeError, если шрифт не встроился."""
    if not FONT_PATH.is_file():
        raise RuntimeError("Не найден встроенный шрифт DejaVuSans.ttf")
    source = _html(report, meta, qr_by_id or {}, include_receipts)
    buf = io.BytesIO()
    result = pisa.CreatePDF(source, dest=buf, path=str(FONT_PATH),
                            link_callback=_link_callback,
                            resource_policy=_resource_policy(),
                            encoding="utf-8", capacity=8 * 1024 * 1024)
    pdf = buf.getvalue()
    if result.err or not pdf.startswith(b"%PDF") or b"/FontFile" not in pdf:
        raise RuntimeError("xhtml2pdf не смог встроить кириллический шрифт в PDF")
    return bytes(pdf)
