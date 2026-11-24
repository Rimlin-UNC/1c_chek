# -*- coding: utf-8 -*-
"""Ямастер Чек — v1.73.8: настоящий PDF авансового отчёта.

Сервер собирает HTML-бланк и превращает его в PDF через xhtml2pdf/ReportLab.
Важно: это НЕ картинка страницы. В PDF встроен DejaVuSans (ToUnicode CMap),
поэтому кириллицу можно выделять, копировать и искать; QR чеков остаются
картинками, как фискальные коды. Системных Pango/Cairo/браузера не нужно.

v1.73.9 (этап 1): серверный бланк повторяет печатную форму АО-1
(ОКУД 0302001) — лицевая сторона с расчётом и распиской, оборотная сторона
с реестром документов. Ширины колонок заданы <colgroup>, длинные фискальные
реквизиты разбиты по строкам, шапка таблицы повторяется на каждой странице:
текст больше не наезжает на соседние ячейки, пустой хвостовой страницы нет.

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
# Этап 2 снимет это ограничение и заменит QR-карточки полными копиями чеков.
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

# Пропись суммы для лицевой стороны АО-1 (как в печатной форме app.js).
_ONES_M = ["ноль", "один", "два", "три", "четыре", "пять", "шесть",
           "семь", "восемь", "девять"]
_ONES_F = ["ноль", "одна", "две", "три", "четыре", "пять", "шесть",
           "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать",
          "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
          "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят",
         "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот"]

_CSS = """
@page { size: A4 portrait; margin: 10mm 15mm 10mm 20mm; }
@font-face { font-family: DejaVu; src: url('DejaVuSans.ttf'); font-weight: normal; }
@font-face { font-family: DejaVu; src: url('DejaVuSans.ttf'); font-weight: bold; }
* { font-family: DejaVu; }
body { font-family: DejaVu; font-size: 8pt; color: #111; line-height: 1.25; }
h1 { font-size: 15pt; text-align: center; margin: 2mm 0 3mm 0; }
h2 { font-size: 11pt; margin: 4mm 0 2mm 0; }
h3 { font-size: 10pt; text-align: center; margin: 0 0 2mm 0; }
p { margin: 0 0 2mm 0; }
table { width: 100%; }
thead { display: table-header-group; }
tfoot { display: table-footer-group; }
td, th { font-family: DejaVu; border: 0.4pt solid #555; padding: 1.2mm;
         font-size: 7.6pt; vertical-align: top; overflow-wrap: anywhere; }
th { background: #f0f0f0; }
td.nb, th.nb { border: 0; }
.small { font-size: 7pt; color: #444; }
.center { text-align: center; }
.right { text-align: right; }
.num { text-align: right; white-space: nowrap; }
.total { font-weight: bold; }
.callout { border: 0.5pt solid #888; padding: 2mm; margin: 2mm 0; font-size: 8pt; }
.signs td { height: 12mm; }
.qr { width: 90px; height: 90px; }
.receipt { margin: 0 0 3mm 0; padding: 2mm; border: 0.4pt solid #777;
           page-break-inside: avoid; }
.page-break { page-break-before: always; }
.ao1-codes td { border: 0; padding: 1mm; font-size: 7pt; }
.ao1-lbl { border: 0.4pt solid #555; }
.ao1-code { border: 0.4pt solid #555; font-weight: bold; }
.ao1-org { border: 0.4pt solid #555; font-weight: bold; }
.ao1-nums td { font-size: 7.5pt; }
.ao1-approve { border: 0.4pt dashed #555; padding: 2mm; font-size: 7pt; }
.ao1-under { font-size: 6.5pt; color: #444; }
.ao1-meta td, .ao1-calc td, .ao1-calc th { font-size: 7.2pt; }
.ao1-calc th { font-size: 6.8pt; }
.ao1-line { margin: 2mm 0; font-size: 7.5pt; }
.ao1-signs td { border: 0; padding: 2mm 1mm 1mm 1mm; font-size: 7.2pt; }
.ao1-sigline { border-bottom: 0.4pt solid #333; }
.ao1-receipt { border: 0.5pt solid #333; padding: 2mm; margin-top: 3mm;
               font-size: 7.2pt; }
.foot { font-size: 6.5pt; color: #555; text-align: center; margin-top: 3mm; }
.back-table td, .back-table th { font-size: 6.3pt; padding: 0.8mm; }
.back-table th { font-size: 6.2pt; }
.back-table td.desc { font-size: 5.9pt; }
.back-table .fiscal { font-size: 5pt; }
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


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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


def _doc_dt(value) -> str:
    """Дата чека в две строки: xhtml2pdf не должен ломать длинную строку в ячейке."""
    return _e(_receipt_dt(value), 25).replace(" ", "<br>", 1)


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


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and n100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _three(n: int, feminine: bool = False) -> str:
    h, rest = divmod(n, 100)
    t, u = divmod(rest, 10)
    words = []
    if h:
        words.append(_HUNDREDS[h])
    if t == 1:
        words.append(_TEENS[u])
    else:
        if t:
            words.append(_TENS[t])
        if u:
            words.append((_ONES_F if feminine else _ONES_M)[u])
    return " ".join(words)


def _amount_in_words(value) -> str:
    """«201366,34» → «двести одна тысяча триста шестьдесят шесть рублей 34 коп.»."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    rub = int(number)
    kop = int(round((number - rub) * 100))
    if kop >= 100:
        rub, kop = rub + 1, 0
    parts = []
    rest = rub
    for unit, forms, feminine in (
        (1_000_000, ("миллион", "миллиона", "миллионов"), False),
        (1_000, ("тысяча", "тысячи", "тысяч"), True),
        (1, ("рубль", "рубля", "рублей"), False),
    ):
        group, rest = divmod(rest, unit)
        if group:
            parts.append(f"{_three(group, feminine)} {_plural(group, forms)}")
    if not parts:
        parts.append("ноль рублей")
    elif rub % 1000 == 0:
        # В печатной форме «одна тысяча рублей», а не «одна тысяча копеек».
        parts.append("рублей")
    return f"{' '.join(parts)} {kop:02d} коп."


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


def _fiscal(row: dict) -> str:
    """ФН / ФП в две строки: раньше единая 34-символьная строка вылезала
    из ячейки и наезжала на «Статус ФНС» и «Сумму»."""
    fn = _text(row.get("fn"), 30)
    fp = _text(row.get("fp"), 20)
    return f"<span class='fiscal'>ФН {_e(fn)}<br>ФП {_e(fp)}</span>"


def _fiscal_plain(row: dict) -> str:
    return _e("/".join(filter(None, [
        _text(row.get("fn"), 30), _text(row.get("fd"), 20), _text(row.get("fp"), 20),
    ])))


def _ao1_face(ctx: dict) -> str:
    """Лицевая сторона унифицированной формы АО-1 (ОКУД 0302001)."""
    return f"""
    <table class="ao1-codes">
      <colgroup><col style="width:8%"><col style="width:62%"><col style="width:30%"></colgroup>
      <tr><td class="nb"></td><td class="nb small">Форма по ОКУД</td>
          <td class="ao1-code right">0302001</td></tr>
      <tr><td class="nb"></td><td class="ao1-org">{ctx['org_full']}
          <div class="ao1-under">наименование организации</div></td>
          <td class="small">по ОКПО&nbsp;&nbsp;<b>{ctx['okpo']}</b></td></tr>
    </table>
    <table class="ao1-nums">
      <colgroup><col style="width:28%"><col style="width:24%"><col style="width:24%"><col style="width:24%"></colgroup>
      <tr><td>Структурное подразделение<br><b>{ctx['department']}</b></td>
          <td>Номер документа<br><b>{ctx['number']}</b></td>
          <td>Дата составления<br><b>{ctx['report_date']}</b></td>
          <td>Отчётный период<br><b>{ctx['month']}</b></td></tr>
    </table>
    <table style="margin-top:2mm"><colgroup><col style="width:66%"><col style="width:34%"></colgroup>
      <tr><td class="nb small">Организация: <b>{ctx['org_short']}</b> · ИНН / КПП: {ctx['inn']} / {ctx['kpp']}</td>
          <td class="ao1-approve">УТВЕРЖДАЮ<br>Руководитель {ctx['head']}<br>
          <span class="ao1-under">подпись · расшифровка подписи</span><br>«___» ____________ 20___ г.</td></tr>
    </table>
    <h1>АВАНСОВЫЙ ОТЧЁТ</h1>
    <table class="ao1-meta">
      <colgroup><col style="width:24%"><col style="width:41%"><col style="width:20%"><col style="width:15%"></colgroup>
      <tr><td>Подотчётное лицо</td><td><b>{ctx['assignee']}</b>
          <div class="ao1-under">фамилия, инициалы</div></td>
          <td>Табельный номер</td><td>{ctx['tabel']}</td></tr>
      <tr><td>Профессия (должность)</td><td>{ctx['position']}</td>
          <td>Подразделение</td><td>{ctx['department']}</td></tr>
      <tr><td>Назначение аванса</td><td colspan="3">{ctx['purpose']}</td></tr>
    </table>
    <table class="ao1-calc" style="margin-top:2mm">
      <colgroup><col style="width:34%"><col style="width:14%"><col style="width:13%"><col style="width:13%"><col style="width:13%"><col style="width:13%"></colgroup>
      <thead><tr><th rowspan="2">Наименование показателя</th><th rowspan="2">Сумма, руб. коп.</th>
        <th colspan="4">Бухгалтерская запись</th></tr>
        <tr><th>Дебет счёт</th><th>сумма</th><th>Кредит счёт</th><th>сумма</th></tr></thead>
      <tbody>
        <tr><td>Предыдущий аванс — остаток</td><td class="num">—</td><td colspan="4" class="center">—</td></tr>
        <tr><td>Предыдущий аванс — перерасход</td><td class="num">—</td><td colspan="4" class="center">—</td></tr>
        <tr><td>Получен аванс — 1. из кассы{ctx['rko']}</td><td class="num">{ctx['cash']}</td>
            <td class="center">{ctx['dt']}</td><td class="num">{ctx['cash']}</td>
            <td class="center">{ctx['kt']}</td><td class="num">{ctx['cash']}</td></tr>
        <tr><td>&nbsp;&nbsp;&nbsp;2. на банковскую карту{ctx['pay']}</td><td class="num">{ctx['card']}</td>
            <td class="center">{ctx['dt']}</td><td class="num">{ctx['card']}</td>
            <td class="center">51</td><td class="num">{ctx['card']}</td></tr>
        <tr class="total"><td>Итого получено</td><td class="num">{ctx['got']}</td><td colspan="4"></td></tr>
        <tr><td>Израсходовано</td><td class="num">{ctx['total_sum']}</td>
            <td class="center">{ctx['acc']}</td><td class="num">{ctx['total_sum']}</td>
            <td class="center">{ctx['dt']}</td><td class="num">{ctx['total_sum']}</td></tr>
        <tr><td>Остаток</td><td class="num">{ctx['rest']}</td><td colspan="4" class="center">{ctx['rest_dash']}</td></tr>
        <tr><td>Перерасход</td><td class="num">{ctx['over']}</td><td colspan="4" class="center">{ctx['over_dash']}</td></tr>
      </tbody>
    </table>
    <p class="ao1-line">Приложение <b>{ctx['count']}</b> документов на <b>{ctx['attachment_pages']}</b> лист(ах)</p>
    <p class="ao1-line">Отчёт проверен. К утверждению в сумме <b>{ctx['total_sum']}</b> руб.
      ({ctx['words']}).</p>
    <table class="ao1-signs">
      <colgroup><col style="width:24%"><col style="width:28%"><col style="width:10%"><col style="width:28%"><col style="width:10%"></colgroup>
      <tr><td>Главный бухгалтер</td><td class="ao1-sigline"></td><td class="small">подпись</td>
          <td class="ao1-sigline">{ctx['buh']}</td><td class="small">расшифровка</td></tr>
      <tr><td>Бухгалтер (принял отчёт)</td><td class="ao1-sigline"></td><td class="small">подпись</td>
          <td class="ao1-sigline">{ctx['buh']}</td><td class="small">расшифровка</td></tr>
    </table>
    <p class="ao1-line">{ctx['balance_line']}</p>
    <div class="ao1-receipt"><b>Расписка.</b> Принят к проверке от <b>{ctx['assignee']}</b>
      авансовый отчёт № <b>{ctx['short_number']}</b> от <b>{ctx['report_date']}</b>
      на сумму <b>{ctx['total_sum']}</b> руб., документов — <b>{ctx['count']}</b> на {ctx['attachment_pages']} листах.
      <div style="margin-top:2mm">Бухгалтер <span class="ao1-sigline">&nbsp;&nbsp;&nbsp;&nbsp;</span>
      подпись <span class="ao1-sigline">&nbsp;&nbsp;&nbsp;&nbsp;</span></div></div>
    <p class="foot">Сформировано в «Ямастер Чек» · ymaster.ru · лицевая сторона</p>
    """


def _ao1_back_rows(rows: list[dict], acc: str) -> str:
    out = ""
    for i, row in enumerate(rows, 1):
        money = _money(row.get("total_sum", 0))
        merchant = _e(row.get("merchant_name") or "—")
        out += ("<tr>"
                f"<td class='center'>{i}</td>"
                f"<td>{_doc_dt(row.get('receipt_date'))}</td>"
                f"<td>ФД №{_e(row.get('fd') or '—', 20)}</td>"
                f"<td>{_fiscal(row)}</td>"
                f"<td class='desc'>Кассовый чек: {merchant}</td>"
                f"<td class='num'>{money}</td><td class='num'>{money}</td>"
                f"<td class='num'>{money}</td><td class='center'>{_e(acc)}</td>"
                "</tr>")
    return out or "<tr><td colspan='9'>Чеки не найдены</td></tr>"


def _ao1_back(ctx: dict) -> str:
    rows = ctx["rows"]
    money = ctx["total_sum"]
    return f"""
    <h3>Оборотная сторона формы № АО-1</h3>
    <table class="back-table">
      <colgroup><col style="width:3%"><col style="width:9%"><col style="width:10%">
        <col style="width:18%"><col style="width:27%"><col style="width:10%">
        <col style="width:9%"><col style="width:9%"><col style="width:5%"></colgroup>
      <thead><tr><th rowspan="2">№<br>п/п</th><th colspan="3">Документ, подтверждающий расходы</th>
        <th rowspan="2">Наименование документа (расхода)</th>
        <th rowspan="2">Сумма по чеку, руб. коп.</th><th rowspan="2">По отчёту</th>
        <th rowspan="2">Принято к учёту</th><th rowspan="2">Дебет счёта</th></tr>
        <tr><th>дата</th><th>номер (ФД)</th><th>ФН / ФП</th></tr></thead>
      <tbody>{_ao1_back_rows(rows, ctx['acc'])}</tbody>
      <tfoot><tr class="total"><td colspan="5">ИТОГО</td>
        <td class="num">{money}</td><td class="num">{money}</td>
        <td class="num">{money}</td><td></td></tr></tfoot>
    </table>
    <table class="ao1-signs" style="margin-top:4mm">
      <colgroup><col style="width:30%"><col style="width:25%"><col style="width:10%"><col style="width:25%"><col style="width:10%"></colgroup>
      <tr><td>Отчёт составил(а), подотчётное лицо</td><td class="ao1-sigline"></td>
          <td class="small">подпись</td><td class="ao1-sigline">{ctx['assignee']}</td>
          <td class="small">расшифровка</td></tr>
      <tr><td colspan="5" class="small">Дата составления: <b>{ctx['report_date']}</b></td></tr>
    </table>
    <p class="foot">Сформировано в «Ямастер Чек» · ymaster.ru · оборотная сторона</p>
    """


def _summary_face(ctx: dict) -> str:
    groups = "".join(
        f"<tr><td>{_e(g.get('name'))}</td><td class='num'>{int(g.get('count') or 0)}</td>"
        f"<td class='num'>{_money(g.get('sum', 0))}</td></tr>" for g in ctx["by_assignee"]
    ) or "<tr><td colspan='3'>Нет данных</td></tr>"
    return f"""
    <h1>СВОДНАЯ ВЕДОМОСТЬ ПО АВАНСОВЫМ ОТЧЁТАМ</h1>
    <p class="center small">Сводный документ не заменяет отдельные отчёты подотчётных лиц.</p>
    <table><colgroup><col style="width:25%"><col style="width:25%"><col style="width:25%"><col style="width:25%"></colgroup>
      <tr><td class="nb">Организация</td><td class="nb">{ctx['org_short']}</td>
          <td class="nb">Номер</td><td class="nb">{ctx['number']}</td></tr>
      <tr><td class="nb">Полное наименование</td><td class="nb">{ctx['org_full']}</td>
          <td class="nb">Дата отчёта</td><td class="nb">{ctx['report_date']}</td></tr>
      <tr><td class="nb">ИНН / КПП</td><td class="nb">{ctx['inn']} / {ctx['kpp']}</td>
          <td class="nb">Период</td><td class="nb">{ctx['period_from']} — {ctx['period_to']}</td></tr>
      <tr><td class="nb">Назначение аванса</td><td colspan="3" class="nb">{ctx['purpose']}</td></tr>
    </table>
    <h2>Сводка по подотчётным лицам</h2>
    <table><colgroup><col style="width:60%"><col style="width:15%"><col style="width:25%"></colgroup>
      <thead><tr><th>Подотчётник</th><th class="right">Чеков</th><th class="right">Сумма, руб.</th></tr></thead>
      <tbody>{groups}
        <tr class="total"><td>ИТОГО</td><td class="num">{ctx['count']}</td>
        <td class="num">{ctx['total_sum']}</td></tr></tbody></table>
    <table class="signs" style="margin-top:4mm">
      <colgroup><col style="width:25%"><col style="width:25%"><col style="width:25%"><col style="width:25%"></colgroup>
      <tr><th>Руководитель</th><th>Главный бухгалтер</th><th>Подотчётник</th><th>Кассир</th></tr>
      <tr><td>{ctx['head']}<br><span class="small">подпись / расшифровка</span></td>
          <td>{ctx['buh']}<br><span class="small">подпись / расшифровка</span></td>
          <td>{ctx['assignee']}<br><span class="small">подпись / расшифровка</span></td>
          <td><br><span class="small">подпись / расшифровка</span></td></tr></table>
    <p class="foot">Сформировано в «Ямастер Чек» · ymaster.ru · сводная ведомость</p>
    """


def _summary_back(ctx: dict) -> str:
    rows = ""
    n = 0
    for group in ctx["by_assignee"]:
        name = _text(group.get("name"))
        items = [r for r in ctx["rows"] if _text(r.get("assignee")) == name]
        for r in items:
            n += 1
            rows += (f"<tr><td class='center'>{n}</td>"
                     f"<td>{_doc_dt(r.get('receipt_date'))}</td>"
                     f"<td class='desc'>{_e(r.get('assignee') or '—')}</td>"
                     f"<td class='desc'>{_e(r.get('merchant_name') or '—')}</td>"
                     f"<td class='desc'>{_e(r.get('category') or '—')}</td>"
                     f"<td>ФД {_e(r.get('fd') or '—', 20)}</td>"
                     f"<td class='num'>{_money(r.get('total_sum', 0))}</td></tr>")
        rows += (f"<tr class='total'><td colspan='6'>Подытог — {_e(name)} "
                 f"({len(items)} чек.)</td><td class='num'>{_money(group.get('sum', 0))}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='7'>Чеки не найдены</td></tr>"
    return f"""
    <h3>Реестр документов сводной ведомости</h3>
    <table class="back-table">
      <colgroup><col style="width:6%"><col style="width:13%"><col style="width:17%">
        <col style="width:29%"><col style="width:12%"><col style="width:11%">
        <col style="width:12%"></colgroup>
      <thead><tr><th>№</th><th>Дата чека</th><th>Подотчётник</th><th>Продавец</th>
        <th>Статья</th><th>Документ</th><th class="right">Сумма, руб.</th></tr></thead>
      <tbody>{rows}</tbody>
      <tfoot><tr class="total"><td colspan="6">ИТОГО</td>
        <td class="num">{ctx['total_sum']}</td></tr></tfoot></table>
    <p class="foot">Сформировано в «Ямастер Чек» · ymaster.ru · реестр документов</p>
    """


def _qr_section(rows: list[dict], qr_by_id: dict) -> str:
    """Этап 1: те же QR-карточки, но примечание ПЕРЕД ними — пустая
    хвостовая страница (была стр. 10) больше не создаётся."""
    shown = rows[:PDF_QR_LIMIT]
    cards = ""
    for i, row in enumerate(shown, 1):
        uri = _qr_uri(str((qr_by_id or {}).get(row.get("id")) or ""))
        image = (f"<img class='qr' width='90' height='90' src='{uri}' alt='QR чека {i}'>"
                 if uri else "<div class='small'>Фискальный QR не сохранён</div>")
        cards += (
            "<div class='receipt'>"
            f"<table><colgroup><col style='width:25%'><col style='width:75%'></colgroup>"
            f"<tr><td class='nb'>{image}</td><td class='nb'>"
            f"<b>Приложение № {i}</b> · {_e(_receipt_dt(row.get('receipt_date')), 25)}<br>"
            f"Продавец: {_e(row.get('merchant_name') or '—')}<br>"
            f"ИНН: {_e(row.get('merchant_inn') or '—')} · ФН/ФД/ФП: {_fiscal_plain(row)}<br>"
            f"Статус ФНС: {_e(_fns(row.get('fns_status')))} · "
            f"Сумма: <b>{_money(row.get('total_sum', 0))} руб.</b>"
            "</td></tr></table></div>")
    note = (f"<p class='small'>Показаны QR первых {len(shown)} чеков из {len(rows)}; "
            "все чеки перечислены в таблице выше. Копии не заменяют обязательные "
            "подтверждающие документы.</p>" if len(rows) > len(shown) else
            "<p class='small'>Копии чеков не заменяют обязательные подтверждающие документы.</p>")
    return (f"<h2 style='page-break-before: auto; margin-top: 5mm'>Приложения: копии чеков</h2>"
            f"{note}{cards}")


def _html(report: dict, meta: dict, qr_by_id: dict, include_receipts: bool) -> str:
    rows = list(report.get("rows") or [])
    total = report.get("total") or {}
    company = report.get("company") or {}
    org = (((report.get("requisites") or {}).get("Организация")) or {})
    person = report.get("person") or {}
    period = report.get("period") or {}
    by_assignee = list(report.get("by_assignee") or [])
    many = len(by_assignee) > 1 or (len(by_assignee) == 1 and by_assignee[0].get("name") == "—")
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
    cash = _num(_meta(meta, "ao-cash", "0"))
    card = _num(_meta(meta, "ao-card", "0"))
    got = cash + card
    spent = _num(total.get("sum"))
    rest = round(max(0.0, got - spent), 2)
    over = round(max(0.0, spent - got), 2)
    dt_account = _meta(meta, "ao-dt", "71.01")
    kt_account = _meta(meta, "ao-kt", "50.01")
    acc_account = _meta(meta, "ao-acc", "44.01")
    rko = _meta(meta, "ao-rko")
    pay = _meta(meta, "ao-pay")
    total_sum = _money(total.get("sum", 0))
    count = int(total.get("count") or len(rows))
    ctx = {
        "rows": rows, "by_assignee": by_assignee,
        "org_full": _e(org.get("НаименованиеПолное") or company.get("name") or "—"),
        "org_short": _e(_org_name(report, meta)),
        "okpo": _e(org.get("ОКПО") or "—"),
        "inn": _e(org.get("ИНН") or company.get("inn") or "—"),
        "kpp": _e(org.get("КПП") or "—"),
        "number": _e(number), "short_number": _e(_text(number).split("-")[-1] or "—"),
        "report_date": _e(report_date),
        "month": _e(_text(period.get("from"), 7)[:7] or "—"),
        "period_from": _e(_date(period.get("from"))),
        "period_to": _e(_date(period.get("to"))),
        "assignee": _e(assignee or "—"), "position": _e(position or "—"),
        "tabel": _e(person.get("ТабельныйНомер") or "—"),
        "purpose": _e(purpose or "—"), "department": _e(department or "—"),
        "head": _e(head or "—"), "buh": _e(buh or "—"),
        "cash": _money(cash), "card": _money(card), "got": _money(got),
        "rest": _money(rest) if rest else "—", "over": _money(over) if over else "—",
        "rest_dash": "" if rest else "—", "over_dash": "" if over else "—",
        "total_sum": total_sum, "count": count,
        "attachment_pages": _e(_meta(meta, "attachmentPages", "_____") or "_____"),
        "words": _e(_amount_in_words(total.get("sum", 0))),
        "dt": _e(dt_account), "kt": _e(kt_account), "acc": _e(acc_account),
        "rko": f" (РКО {_e(rko)})" if rko else "",
        "pay": f" (п/п {_e(pay)})" if pay else "",
        "balance_line": (f"Остаток внесён в кассу в сумме <b>{_money(rest)}</b> руб." if rest else
                         f"Перерасход выдан в сумме <b>{_money(over)}</b> руб." if over else
                         "Остаток/перерасход отсутствуют."),
    }
    face = _summary_face(ctx) if many else _ao1_face(ctx)
    back = _summary_back(ctx) if many else _ao1_back(ctx)
    warning = ""
    if report.get("invalid_count"):
        warning = (f"<div class='callout'>⚠ В отчёте {int(report['invalid_count'])} чек(ов) "
                   "со статусом «Недействителен» — исключите их из учёта или перепроверьте.</div>")
    qr_html = _qr_section(rows, qr_by_id) if include_receipts else ""

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Ymaster Check - Advance report PDF</title>
<style>{_CSS}</style></head><body>
{face}
{warning}
<div class="page-break"></div>
{back}
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
