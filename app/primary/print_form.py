"""Печатная форма УПД или счёта-фактуры: текст из PDF или OCR → черновик реквизитов (T-61, v1.70.95).

Подход — правила, как в шаблонах invoice2data (MIT): у печатной формы УПД один утверждённый вид
(постановление Правительства № 1137, письмо ФНС ММВ-20-3/96@), поэтому один встроенный шаблон
покрывает выгрузки из 1С, Диадока, СБИС и сканы бумажных документов. Нейросети и облака не нужны.

  • Шапка — по подписям строк (1)–(7). Правила терпят типичный шум OCR: «ИННИКПП» вместо «ИНН/КПП»,
    «Счет-Фоктура», «500 «…»» вместо «ООО «…»».
  • Строки таблицы — по ставке НДС: слева количество, цена и стоимость без налога, справа налог и
    стоимость с налогом. Разряды в числах отделяются пробелами, поэтому «10 500,00» может означать
    и «10 шт. по 500,00», и «10 500,00»; разбивка выбирается по арифметике «количество × цена =
    стоимость». В PDF-тексте с раскладкой колонки разделены двумя и более пробелами — там это
    не нужно.
  • «Всего к оплате (9)» — итоги; сходятся ли «без НДС + НДС = с НДС», проверяется.

Результат — словарь того же вида, что upd.parse, плюс found (что найдено) и uncertain (что
проверить глазами). Дальше его проверяет общий upd.check, и бухгалтер подтверждает черновик.
Чистые функции: текст на входе, словарь на выходе.
"""
from __future__ import annotations

import io
import re
from datetime import date

from . import upd

MAX_PDF_PAGES = 20
MAX_TEXT = 400_000            # символов текста на документ: защита от «бесконечного» PDF

_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8,
           "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_SPACES = str.maketrans({"\u202f": "\u00a0", "\u2009": "\u00a0", "\u2007": "\u00a0", "\t": " "})
# OCR с языками rus+eng путает похожие буквы: «Cчет», «OT», «Bсего». Для поиска подписей латинские
# двойники заменяются кириллическими — буква в букву, поэтому позиции совпадают и значения берутся
# из исходного текста (коды вроде CEM-500 не портятся).
_LATIN_TWINS = "AaBCcEeHKkMOoPpTXxy"
_CYRILLIC_TWINS = "АаВСсЕеНКкМОоРрТХху"
_FOLD = str.maketrans(_LATIN_TWINS, _CYRILLIC_TWINS)
_QUOTES = str.maketrans({"„": "«", "“": "»", "”": "»"})
_DASH = r"[-‐–—]"

# Число в «бумажном» виде: разряды через пробел (обычный или неразрывный), запятая перед копейками
AMOUNT = re.compile(r"(?<![\d,.])(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)[.,]\d{2}(?![\d])")
_SF = re.compile(r"Сч[её]т[\s\-‐–—]*ф[ао]ктура\s*№?\s*(?P<num>.+?)\s+(?:от|0т)\s+"
                 r"(?P<date>\d{1,2}[\s.]+[А-Яа-яЁё]{3,}[\s.]+\d{4}|\d{1,2}\.\d{1,2}\.\d{2,4})", re.I)
_UPD = re.compile(r"Универсальн\w*\s+передаточн\w*", re.I)
_STATUS = re.compile(r"Статус\s*[:.]?\s*(?P<s>[12])(?!\d)", re.I)
_CORR = re.compile(r"Исправлени[ея]\s*№\s*(?P<n>\S+)\s+(?:от|0т)\s+(?P<d>\d{1,2}[\s.]+[А-Яа-яЁё]{3,}[\s.]+\d{4}|"
                   r"\d{1,2}\.\d{1,2}\.\d{2,4}|" + _DASH + r"{1,2})", re.I)
# между «ИНН» и «КПП» OCR вместо косой черты видит «I», «l», «1», «И» — допускаем любой один знак
_INN_KPP = r"ИНН\s*\S?\s*КПП\s+{role}\w*\s*[:.]?\s*[`'‘’\"«]?\s*(?P<inn>\d{{12}}|\d{{10}})(?!\d)" \
           r"(?:\s*[^\w\s]?\s*(?P<kpp>\d{{7,11}}))?"
_SELLER_INN = re.compile(_INN_KPP.format(role="продав"), re.I)
_BUYER_INN = re.compile(_INN_KPP.format(role="покупат"), re.I)
_SELLER = re.compile(r"Продавец[ \t]*[:.]?[ \t]*(?P<name>.+?)\s*(?:\(2\)|$)", re.I | re.M)
_BUYER = re.compile(r"Покупатель[ \t]*[:.]?[ \t]*(?P<name>.+?)\s*(?:\(6\)|$)", re.I | re.M)
_CURRENCY = re.compile(r"Валюта[^\n]*?(?P<code>\d{3})(?!\d)", re.I)
_TOTAL = re.compile(r"Всего\s+к\s+оплат\w*(?P<rest>[^\n]*)", re.I)
_ORG_NOISE = re.compile(r"^[0О5]{3}(?=\s*[«\"])")      # OCR путает «ООО» с «000» и «500» (после свёртки)
# Национальные условные обозначения единиц (ОКЕИ), что встречаются в строительной первичке
_UNITS = re.compile(r"(?i)(?:шт|т|тн|кг|г|м|мм|см|км|м2|м3|м²|м³|кв\.?\s?м|куб\.?\s?м|п\.?\s?м|пог\.?\s?м|л|мл|"
                    r"компл|к-т|упак|уп|усл\.?\s?ед|ед|рул|лист|пар[аы]?|ч|час|сут|мес|тыс\.?\s?шт|ящ|меш|рейс|"
                    r"маш\.?-?ч)\.?")


class PrintFormError(ValueError):
    """Текст не похож на печатную форму УПД или счёта-фактуры — сообщение для пользователя."""


# ------------------------------------------------------------------ PDF → текст
def pdf_text(data: bytes) -> tuple[str, str, int]:
    """Текстовый слой PDF: (простой текст, текст с раскладкой колонок, страниц). Скан — пустые строки."""
    if not data.startswith(b"%PDF"):
        raise PrintFormError("Это не PDF-файл")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception as exc:                            # noqa: BLE001 — pypdf бросает разное
        raise PrintFormError("PDF не читается: файл повреждён или защищён паролем") from exc
    if len(pages) > MAX_PDF_PAGES:
        raise PrintFormError(f"В PDF больше {MAX_PDF_PAGES} страниц — это не похоже на один документ")
    plain, layout = [], []
    for page in pages:
        try:
            plain.append(page.extract_text() or "")
            layout.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:                               # noqa: BLE001 — страница-картинка или битый шрифт
            plain.append("")
            layout.append("")
        if sum(map(len, plain)) > MAX_TEXT:
            break
    return "\n".join(plain)[:MAX_TEXT], "\n".join(layout)[:MAX_TEXT], len(pages)


def has_text(plain: str) -> bool:
    """Есть ли в PDF осмысленный текстовый слой (а не пара служебных символов скана)."""
    return len(re.findall(r"[А-Яа-яЁё]{3,}", plain or "")) >= 8


# ------------------------------------------------------------------ мелкие разборщики
def parse_date(raw: str) -> date | None:
    """«14 октября 2026 г.», «14.10.2026», «14.10.26» → дата; шум OCR в названии месяца терпим."""
    text = (raw or "").strip().lower()
    found = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", text)
    try:
        if found:
            day, month, year = (int(x) for x in found.groups())
            return date(year + 2000 if year < 100 else year, month, day)
        found = re.match(r"(\d{1,2})[\s.]+([а-яё]{3,})[\s.]+(\d{4})", text)
        if found:
            month = _MONTHS.get(found.group(2)[:3])
            return date(int(found.group(3)), month, int(found.group(1))) if month else None
    except ValueError:
        return None
    return None


def to_number(raw: str) -> float | None:
    """«1 100,00», «5\u00a0000,00», «1,250» → число; не число — None."""
    text = re.sub(r"[ \u00a0]", "", raw or "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    return float(text)


def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").translate(_QUOTES).split())
    name = re.sub(r"\s*\(\s*\d[абв]?\s*\)\s*$", "", name)           # хвост «(2)» / «(6)»
    if _ORG_NOISE.match(name.translate(_FOLD)):
        name = "ООО" + name[3:]
    if "»" in name:                                                    # мусор OCR после закрывающей кавычки
        head, _, tail = name.rpartition("»")
        if len(tail.strip()) <= 2:
            name = head + "»"
    return name.strip(" ,;:|`'")


def _party(text: str, name_re: re.Pattern, inn_re: re.Pattern, found: list, uncertain: list, role: str) -> dict:
    """Ищем по свёрнутому тексту, значения берём из исходного (позиции совпадают)."""
    party = {"kind": "", "name": "", "inn": "", "kpp": ""}
    folded = text.translate(_FOLD)
    m = name_re.search(folded)
    if m:
        party["name"] = _clean_name(text[m.start("name"):m.end("name")])
        if party["name"]:
            found.append(f"{role}_name")
    m = inn_re.search(folded)
    if m:
        party["inn"] = m.group("inn")
        found.append(f"{role}_inn")
        if not upd.inn_is_valid(party["inn"]):
            uncertain.append(f"{role}_inn")
        kpp = m.group("kpp") or ""
        if kpp:
            party["kpp"] = kpp
            found.append(f"{role}_kpp")
            if len(kpp) != 9:
                uncertain.append(f"{role}_kpp")
    if len(party["inn"]) == 12 or party["name"].upper().startswith("ИП "):
        party["kind"] = "ИП"
    elif party["inn"] or party["name"]:
        party["kind"] = "ЮЛ"
    return party


# ------------------------------------------------------------------ числа строки
def _groupings(tokens: list[str], joinable: bool) -> list[tuple[int, int, float, bool]]:
    """Все числа из подряд идущих токенов: (начало, конец, значение, есть ли ровно две цифры копеек)."""
    out = []
    for start in range(len(tokens)):
        for end in range(start + 1, len(tokens) + 1 if joinable else start + 2):
            part = tokens[start:end]
            if len(part) > 1 and not (re.fullmatch(r"\d{1,3}", part[0])
                                      and all(re.fullmatch(r"\d{3}", p) for p in part[1:-1])
                                      and re.fullmatch(r"\d{3}(?:[.,]\d+)?", part[-1])):
                continue
            if len(part) == 1 and not re.fullmatch(r"\d+(?:[.,]\d+)?", part[0]):
                continue
            if re.match(r"0\d", part[0]):                # «000,00» само по себе — хвост разряда, не число
                continue
            value = to_number(" ".join(part))
            if value is not None:
                out.append((start, end, value, bool(re.search(r"[.,]\d{2}$", part[-1]))))
    return out


def solve_left(tokens: list[str], joinable: bool = True) -> tuple[float | None, float | None, float | None, bool]:
    """Хвост строки до ставки → (количество, цена, стоимость без НДС, сошлось ли кол-во × цена).

    Перебираются разбивки конца токенов на три числа. Порядок предпочтений: произведение
    совпадает со стоимостью; у цены и стоимости по две цифры копеек; использованы все числа;
    меньше расхождение. Если чисел меньше трёх — у услуги может не быть количества и цены."""
    ends: dict[int, list[tuple[int, float, bool]]] = {}
    for start, end, value, cents in _groupings(tokens, joinable):
        ends.setdefault(end, []).append((start, value, cents))
    n = len(tokens)
    best = None
    for base_start, base, base_cents in ends.get(n, []):
        for price_start, price, price_cents in ends.get(base_start, []):
            for qty_start, qty, _cents in ends.get(price_start, []):
                err = abs(qty * price - base)
                exact = err <= max(0.011, abs(qty) * 0.005)
                score = (not exact, not (price_cents and base_cents), qty_start, err)
                if best is None or score < best[0]:
                    best = (score, qty, price, base)
    if best is not None:
        (bad, _plausible, _start, _err), qty, price, base = best
        return qty, price, base, not bad
    tail = ends.get(n, [])
    if tail:
        base = min(tail, key=lambda item: item[0])[1]        # самое длинное число в конце
        return None, None, base, False
    return None, None, None, False


def _split_cells(line: str) -> tuple[list[str], bool]:
    """Строка → ячейки. Раскладка PDF (2+ пробела между колонками) — числа уже целые."""
    if re.search(r" {2,}", line):
        cells = [c.strip() for c in re.split(r" {2,}", line.strip()) if c.strip()]
        return [re.sub(r"(?<=\d)[ \u00a0](?=\d{3}\b)", "", c) for c in cells], False
    return line.replace("\u00a0", " ").split(), True


def _strip_excise(tokens: list[str]) -> list[str]:
    """Убрать графу 6 «В том числе акциз»: «без акциза», «без», «X», прочерк, мусор OCR без цифр."""
    out = list(tokens)
    while out and not re.search(r"\d", out[-1]):
        out.pop()
    return out


def _head(cells: list[str]) -> dict:
    """«[код] № наименование [--] [ОКЕИ] [ед.]» → позиция, код, наименование, единица.

    Единица и ОКЕИ снимаются с конца по ячейкам (в раскладке «усл. ед» — одна ячейка), прочерки
    графы «Код вида товара» — до и после них. Номер строки и код ищутся уже по словам: в раскладке
    номер может слипнуться с наименованием («3 | Доставка»)."""
    parts = [c.strip("|").strip() for c in cells]
    parts = [c for c in parts if c]

    def drop_dashes() -> None:
        while parts and re.fullmatch(_DASH + r"{1,2}", parts[-1]):
            parts.pop()

    drop_dashes()
    unit = okei = ""
    if len(parts) > 1:
        # единица — известное обозначение или любое короткое слово сразу после кода ОКЕИ;
        # иначе последнее слово наименования («Песок речной») не путается с единицей
        after_okei = len(parts) > 2 and re.fullmatch(r"\d{3,4}", parts[-2])
        word = re.fullmatch(r"[^\W\d_][\w.\-/ ]{0,11}", parts[-1])
        if _UNITS.fullmatch(parts[-1]) or (word and after_okei):
            unit = parts.pop()
    if len(parts) > 2 and re.fullmatch(r"\d{3,4}", parts[-1]):
        okei = parts.pop()
    drop_dashes()
    tokens = [t.strip("|") for t in " ".join(parts).split()]
    tokens = [t for t in tokens if t]
    if unit and not okei and len(tokens) > 2 and re.fullmatch(r"\d{3}", tokens[-1]):
        okei = tokens.pop()                      # в раскладке ОКЕИ слипся с наименованием в одну ячейку
    while tokens and not re.search(r"[0-9A-Za-zА-Яа-яЁё]", tokens[-1]):
        tokens.pop()                             # хвостовой мусор OCR: «”», «=.», прочерки
    position, code = None, ""
    for i, token in enumerate(tokens[:3]):
        if re.fullmatch(r"\d{1,4}", token):
            position = int(token)
            code = " ".join(tokens[:i])
            tokens = tokens[i + 1:]
            break
    if re.fullmatch(_DASH + r"{1,2}", code or ""):
        code = ""
    name = " ".join(tokens).strip(" |—–-")
    return {"position": position, "item_code": code[:100], "name": name, "unit_code": okei, "unit_name": unit}


def _rate_at(cells: list[str], i: int) -> tuple[int, str]:
    """Ставка НДС с ячейки i: (сколько ячеек занимает, текст ставки); 0 — здесь не ставка."""
    cell = cells[i].translate(_FOLD)
    nxt = cells[i + 1].translate(_FOLD) if i + 1 < len(cells) else ""
    if re.fullmatch(r"\d{1,2}(?:[.,]\d{1,2})?%", cell) or re.fullmatch(r"\d{1,2}/\d{2,3}", cell):
        return 1, cell
    if re.fullmatch(r"без\s*ндс", cell, re.I):
        return 1, "без НДС"
    if cell.lower() == "без" and nxt.upper().startswith("НДС"):
        return 2, "без НДС"
    if re.fullmatch(r"\d{1,2}(?:[.,]\d{1,2})?", cell) and nxt == "%":
        return 2, cell + "%"
    return 0, ""


def parse_line(line: str) -> dict | None:
    """Строка таблицы товаров → словарь строки (как upd._line) или None, если это не строка товара."""
    if re.search(r"Всего\s+к\s+оплат", line.translate(_FOLD), re.I):
        return None
    cells, joinable = _split_cells(line)
    for i in range(len(cells)):
        width, raw_rate = _rate_at(cells, i)
        if not width:
            continue
        left, right = cells[:i], cells[i + width:]
        amounts = [to_number(a) for a in AMOUNT.findall(" ".join(right))]
        no_vat = raw_rate == "без НДС"
        if len(amounts) >= (1 if no_vat else 2) and AMOUNT.search(" ".join(left)):
            break
    else:
        return None
    left = _strip_excise(left)
    tail: list[str] = []
    for cell in reversed(left):
        if not re.fullmatch(r"\d[\d.,]*", cell):
            break
        tail.insert(0, cell)
    qty, price, base, exact = solve_left(tail, joinable)
    head = _head(left[:len(left) - len(tail)])
    if base is None or not head["name"]:
        return None
    vat, total = (0.0, amounts[0]) if no_vat else (amounts[0], amounts[1])
    consistent = abs(base + vat - total) <= 0.011
    return {
        "position": head["position"], "name": head["name"], "unit_code": head["unit_code"],
        "unit_name": head["unit_name"], "quantity": qty, "price": price, "sum_without_vat": base,
        "vat_rate": upd.vat_rate(raw_rate), "vat_raw": raw_rate, "vat_sum": vat, "sum_with_vat": total,
        "item_code": head["item_code"], "uncertain": not (exact or qty is None) or not consistent,
    }


def parse_totals(text: str) -> tuple[dict, bool]:
    """«Всего к оплате (9)» → итоги и признак «без НДС + НДС = с НДС»."""
    totals = {"without_vat": None, "vat": None, "with_vat": None}
    m = None
    folded = text.translate(_FOLD)
    for m in _TOTAL.finditer(folded):
        pass                                     # берём последнее вхождение: итог в конце документа
    if m is None:
        return totals, False
    rest = re.sub(r"\(\s*9\s*\)", " ", folded[m.start("rest"):m.end("rest")])
    amounts = [to_number(a) for a in AMOUNT.findall(rest)]
    if re.search(r"без\s*НДС", rest, re.I) and len(amounts) >= 2:
        totals.update(without_vat=amounts[0], vat=0.0, with_vat=amounts[1])
    elif len(amounts) >= 3:
        # если OCR добавил лишнее число, ищем тройку подряд, где сходится сумма
        for i in range(len(amounts) - 2):
            a, b, c = amounts[i:i + 3]
            if abs(a + b - c) <= 0.011:
                totals.update(without_vat=a, vat=b, with_vat=c)
                return totals, True
        totals.update(without_vat=amounts[-3], vat=amounts[-2], with_vat=amounts[-1])
        return totals, False
    elif len(amounts) == 2:
        totals.update(without_vat=amounts[0], with_vat=amounts[1])
    a, b, c = totals["without_vat"], totals["vat"], totals["with_vat"]
    return totals, None not in (a, b, c) and abs(a + b - c) <= 0.011


# ------------------------------------------------------------------ документ целиком
def _unsupported(text: str) -> str:
    low = text.translate(_FOLD).lower()
    if "торг-12" in low or "товарная накладная" in low:
        return "Это товарная накладная (ТОРГ-12) — пока распознаются только УПД и счета-фактуры"
    if re.search(r"(?<![а-яё])акт(?![а-яё])", low) and "выполненных работ" in low:
        return "Это акт — пока распознаются только УПД и счета-фактуры"
    if re.search(r"сч[её]т на оплату", low):
        return "Это счёт на оплату — он не первичный документ; загрузите УПД или счёт-фактуру"
    return "Не похоже на УПД или счёт-фактуру: не найдена строка «Счёт-фактура № … от …»"


# v1.70.109 (T-77): латинские буквы, которые на скане не отличить от русских
_TWINS = frozenset("ABCEHKMOPTXYaceopxy")


def latin_twins(number: str) -> bool:
    """В номере есть буквы, и все они — латинские двойники русских: «YT-215» вместо «УТ-215».
    Такой номер Tesseract читает уверенно, а ошибку не видно глазами — помечаем «проверьте»."""
    letters = [ch for ch in number if ch.isalpha()]
    return bool(letters) and all(ch in _TWINS for ch in letters)


def parse_text(plain: str, layout: str = "") -> dict:
    """Текст печатной формы → черновик в формате upd.parse + found и uncertain."""
    text = (plain or "").translate(_SPACES)[:MAX_TEXT]
    table_text = (layout or "").translate(_SPACES)[:MAX_TEXT] or text
    folded = text.translate(_FOLD)
    found: list[str] = []
    uncertain: list[str] = []
    m = _SF.search(folded)
    # Заголовок УПД на скане разбит по строкам боковой колонки; «Статус: 1|2» есть только у УПД
    is_upd = bool(_UPD.search(folded) or (re.search(r"Универсальн", folded, re.I)
                                          and re.search(r"передаточн", folded, re.I))
                  or _STATUS.search(folded))
    if not m and not is_upd:
        raise PrintFormError(_unsupported(text))
    number = re.sub(r"\s*\(\s*1\s*\)\s*$", "", text[m.start("num"):m.end("num")]).strip(" .,:;|") if m else ""
    day = parse_date(folded[m.start("date"):m.end("date")]) if m else None
    if number:
        found.append("number")
        if latin_twins(number):
            uncertain.append("number")
    if day:
        found.append("date")
    status = _STATUS.search(folded)
    if is_upd:
        function = "ДОП" if status and status.group("s") == "2" else "СЧФДОП"
        if not status:
            uncertain.append("function")
    else:
        function = "СЧФ"
    correction_number, correction_date = "", None
    corr = _CORR.search(folded)
    if corr and not re.fullmatch(_DASH + r"{1,2}", corr.group("n")):
        correction_number = text[corr.start("n"):corr.end("n")].strip(" .,")
        correction_date = parse_date(corr.group("d"))
    seller = _party(text, _SELLER, _SELLER_INN, found, uncertain, "seller")
    buyer = _party(text, _BUYER, _BUYER_INN, found, uncertain, "buyer")
    cur = _CURRENCY.search(folded)
    currency = cur.group("code") if cur else "643"
    lines = [ln for ln in (parse_line(row) for row in table_text.splitlines()) if ln]
    if not lines and table_text is not text:
        lines = [ln for ln in (parse_line(row) for row in text.splitlines()) if ln]
    for i, ln in enumerate(lines, 1):
        if ln["position"] is None:
            ln["position"] = i
    if lines:
        found.append("lines")
    if any(ln.pop("uncertain") for ln in lines):
        uncertain.append("lines")
    totals, totals_ok = parse_totals(table_text)
    if totals["with_vat"] is None and table_text is not text:
        totals, totals_ok = parse_totals(text)
    if totals["with_vat"] is not None:
        found.append("totals")
        if not totals_ok:
            uncertain.append("totals")
    return {
        "file_id": "", "format_version": "", "function": function, "kind": upd.KINDS.get(function, "УПД"),
        "number": number[:100], "date": day, "currency": currency,
        "correction_number": correction_number[:20], "correction_date": correction_date,
        "seller": seller, "buyer": buyer, "lines": lines, "totals": totals,
        "found": found, "uncertain": sorted(set(uncertain)),
    }


# ------------------------------------------------------------------ несколько вариантов текста
_FIELDS = {
    "number": lambda d: d["number"], "date": lambda d: d["date"],
    "seller_name": lambda d: d["seller"]["name"], "seller_inn": lambda d: d["seller"]["inn"],
    "seller_kpp": lambda d: d["seller"]["kpp"], "buyer_name": lambda d: d["buyer"]["name"],
    "buyer_inn": lambda d: d["buyer"]["inn"], "buyer_kpp": lambda d: d["buyer"]["kpp"],
}


def _set(doc: dict, field: str, value) -> None:
    if field in ("number", "date"):
        doc[field] = value
    else:
        role, key = field.split("_", 1)
        doc[role][key] = value


def _lines_fit(doc: dict) -> tuple:
    """Насколько строки годятся: сходятся с итогом, их больше, меньше сомнений."""
    lines, total = doc["lines"], doc["totals"]["with_vat"]
    fits = bool(lines) and total is not None and abs(sum(ln["sum_with_vat"] or 0 for ln in lines) - total) <= 0.05
    return (fits, "lines" not in doc["uncertain"], len(lines))


def parse_variants(variants: list[tuple[str, str]]) -> dict:
    """Несколько прочтений одной страницы (простой текст OCR, строки по координатам) → лучшее по полям.

    Для каждого поля берётся прочтение, где поле найдено и не вызывает сомнений (ИНН с верной
    контрольной суммой, КПП из 9 цифр); строки — те, что сходятся с «Всего к оплате»."""
    results, errors = [], []
    for plain, layout in variants:
        try:
            results.append(parse_text(plain, layout))
        except PrintFormError as exc:
            errors.append(str(exc))
    if not results:
        raise PrintFormError(errors[0] if errors else "Текст документа не распознан")
    best = max(results, key=lambda d: (len(d["found"]) - len(d["uncertain"]), _lines_fit(d)))
    merged = {**best, "seller": dict(best["seller"]), "buyer": dict(best["buyer"]),
              "found": list(best["found"]), "uncertain": list(best["uncertain"])}
    for field, get in _FIELDS.items():
        if field in merged["found"] and field not in merged["uncertain"]:
            continue
        for other in results:
            if other is best or not get(other):
                continue
            if field in other["found"] and field not in other["uncertain"]:
                _set(merged, field, get(other))
                merged["found"] = sorted(set(merged["found"]) | {field})
                merged["uncertain"] = [u for u in merged["uncertain"] if u != field]
                break
            if field not in merged["found"]:
                _set(merged, field, get(other))
                merged["found"] = sorted(set(merged["found"]) | {field})
                merged["uncertain"] = sorted(set(merged["uncertain"]) | {field})
    lines_from = max(results, key=_lines_fit)
    if _lines_fit(lines_from) > _lines_fit(merged):
        merged["lines"] = lines_from["lines"]
        merged["uncertain"] = sorted((set(merged["uncertain"]) - {"lines"}) | (set(lines_from["uncertain"]) & {"lines"}))
    totals_from = [d for d in results if "totals" in d["found"] and "totals" not in d["uncertain"]]
    if totals_from and ("totals" not in merged["found"] or "totals" in merged["uncertain"]):
        merged["totals"] = totals_from[0]["totals"]
        merged["found"] = sorted(set(merged["found"]) | {"totals"})
        merged["uncertain"] = [u for u in merged["uncertain"] if u != "totals"]
    if any(d["function"] in ("СЧФДОП", "ДОП") for d in results) and merged["function"] == "СЧФ":
        upd_doc = next(d for d in results if d["function"] in ("СЧФДОП", "ДОП"))
        merged["function"], merged["kind"] = upd_doc["function"], upd_doc["kind"]
    if merged["lines"]:
        merged["found"] = sorted(set(merged["found"]) | {"lines"})
    return merged
