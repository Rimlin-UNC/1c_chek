"""Печатная форма УПД — бланк постановления № 1137 один в один с формой из ЭДО (T-62, ADR-45).

Владелец (06.11.2026, #47): «программа должна собрать электронный УПД в печатный формат
максимально как оригинал, дать распечатать в PDF и поправить те поля, которые можно
заполнять». 19.11.2026 владелец прислал две настоящие печатные формы из ЭДО (УПД № 32
от 06.04.2026 — ИП без НДС, и УПД № 118050913 от 07.09.2026 — организация с НДС 22 %) и
попросил сделать шаблон «один в один»: до этого бланк был упрощённый — 13 граф вместо 16,
без граф 10, 10а, 11, без строки (5б), без блока передачи товара, банковских реквизитов и
блока электронной подписи.

Как сделано:

  • геометрия листа взята с исходных PDF: A4 в альбомной ориентации 841,92 × 594,96 pt;
    каждая строка шапки, шапка таблицы (А, 1, 1а, 1б, 2, 2а, 3 … 10, 10а, 11), подписи,
    блок «Товар (груз) передал… получил», банковские реквизиты, наименование составителя
    и блок «ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ» стоят там же, где в оригинале;
  • реквизиты берутся из записи реестра и из XML (адреса сторон, грузоотправитель и
    грузополучатель, документ об отгрузке, основание передачи, подписант, ОГРНИП ИП) —
    той же записи, что уходит в 1С, поэтому печатная форма и файл для 1С не расходятся;
  • заполняемые поля — то, чего в электронном документе нет или что зависит от бумаги:
    адреса, номера платёжных и отгрузочных документов, основание передачи, даты отгрузки
    и приёмки, должности и подписанты обеих сторон, банковские реквизиты, данные ЭДО.
    Их правят в окне «Печатная форма», правки запоминаются к документу (колонка print_json);
  • PDF — печатью браузера («Печать → Сохранить как PDF»): сервер не тянет рендерер PDF на
    2 ГБ (ADR-25), а вид на бумаге и на экране — один и тот же HTML (ADR-45).

Чистые функции: документ + правки на входе, HTML на выходе. База здесь не трогается.
"""
from __future__ import annotations

import json
from datetime import date

from .models import PrimaryDocument

# Порядок и подписи полей — один список для модели запроса, базы и интерфейса.
# Группа нужна окну «Печатная форма»: 48 полей подряд читать невозможно, они разложены
# по тем же разделам, что и сам бланк.
FIELD_SPECS: tuple[tuple[str, str, str, int], ...] = (
    # ключ, подпись в окне, раздел, предел длины
    ("status", "Статус документа (1 или 2)", "Шапка", 1),
    ("correction", "Исправление № и дата (строка под заголовком)", "Шапка", 60),
    ("seller_address", "Адрес продавца (2а)", "Шапка", 300),
    ("buyer_address", "Адрес покупателя (6а)", "Шапка", 300),
    ("shipper", "Грузоотправитель и его адрес (3)", "Шапка", 300),
    ("consignee", "Грузополучатель и его адрес (4)", "Шапка", 300),
    ("payment_doc", "К платёжно-расчётному документу (5)", "Шапка", 200),
    ("payment_advance", "К счёту-фактуре при оплате, частичной оплате (5б)", "Шапка", 300),
    ("shipment_doc", "Документ об отгрузке (5а)", "Шапка", 200),
    ("contract_id", "Идентификатор госконтракта, договора (8)", "Шапка", 100),
    ("currency", "Валюта: наименование, код (7)", "Шапка", 100),
    ("basis", "Основание передачи (сдачи) / получения (приёмки)", "Основание и передача", 300),
    ("sheets", "Документ составлен на … листах", "Основание и передача", 3),
    ("transfer_position", "Товар передал: должность", "Основание и передача", 200),
    ("transfer_sign", "Товар передал: подпись (в ЭДО — «электронно»)", "Основание и передача", 60),
    ("transfer_name", "Товар передал: ФИО", "Основание и передача", 200),
    ("accept_position", "Товар получил: должность", "Основание и передача", 200),
    ("accept_sign", "Товар получил: подпись (в ЭДО — «электронно»)", "Основание и передача", 60),
    ("accept_name", "Товар получил: ФИО", "Основание и передача", 200),
    ("transfer_date", "Дата отгрузки, передачи (сдачи)", "Основание и передача", 20),
    ("accept_date", "Дата получения (приёмки)", "Основание и передача", 20),
    ("transfer_other", "Иные сведения об отгрузке, передаче", "Основание и передача", 300),
    ("accept_other", "Иные сведения о получении, приёмке", "Основание и передача", 300),
    ("seller_bank", "Банковские реквизиты продавца", "Основание и передача", 300),
    ("buyer_bank", "Банковские реквизиты покупателя", "Основание и передача", 300),
    ("seller_subject", "Экономический субъект — составитель документа (продавец)", "Подписи", 300),
    ("buyer_subject", "Экономический субъект — составитель документа (покупатель)", "Подписи", 300),
    ("signer_chief_role", "Руководитель: должность", "Подписи", 200),
    ("signer_chief", "Руководитель: ФИО", "Подписи", 200),
    ("signer_accountant_role", "Главный бухгалтер: должность", "Подписи", 200),
    ("signer_accountant", "Главный бухгалтер: ФИО", "Подписи", 200),
    ("signer_entrepreneur", "Индивидуальный предприниматель: ФИО", "Подписи", 200),
    ("seller_ogrnip", "ОГРНИП и дата присвоения (ИП)", "Подписи", 60),
    ("resp_left_position", "Ответственный (передал): должность", "Подписи", 200),
    ("resp_left_sign", "Ответственный (передал): подпись", "Подписи", 60),
    ("resp_left_name", "Ответственный (передал): ФИО", "Подписи", 200),
    ("resp_right_position", "Ответственный (получил): должность", "Подписи", 200),
    ("resp_right_sign", "Ответственный (получил): подпись", "Подписи", 60),
    ("resp_right_name", "Ответственный (получил): ФИО", "Подписи", 200),
    ("edo_operator", "Оператор ЭДО", "ЭДО", 200),
    ("edo_id", "Идентификатор документа (ЭДО)", "ЭДО", 60),
    ("edo_sent_name", "Отправлено: кто подписал", "ЭДО", 300),
    ("edo_sent_time", "Отправлено: дата и время", "ЭДО", 60),
    ("edo_sent_cert", "Отправлено: сертификат", "ЭДО", 60),
    ("edo_approved_name", "Утверждено: кто подписал", "ЭДО", 300),
    ("edo_approved_time", "Утверждено: дата и время", "ЭДО", 60),
    ("edo_approved_cert", "Утверждено: сертификат", "ЭДО", 60),
    ("edo_approved_role", "Утверждено: должность", "ЭДО", 200),
)
FIELDS = tuple(key for key, _, _, _ in FIELD_SPECS)
LABELS = {key: label for key, label, _, _ in FIELD_SPECS}
GROUPS = {key: group for key, _, group, _ in FIELD_SPECS}
LIMITS = {key: limit for key, _, _, limit in FIELD_SPECS}

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")
CURRENCIES = {"643": "Российский рубль", "978": "Евро", "840": "Доллар США", "398": "Тенге",
              "933": "Белорусский рубль", "980": "Гривна", "156": "Юань"}
# как fns_refs/upd: ставка в записи — код, на бумаге — проценты словами
VAT_LABELS = {"none": "без НДС", "agent": "НДС исчисляется налоговым агентом",
              "22": "22%", "20": "20%", "18": "18%", "10": "10%", "7": "7%", "5": "5%", "0": "0%",
              # v1.73.6: у расчётных ставок знака процента на бумаге нет — в PDF из ЭДО «20/120»;
              # VAT_LABELS заодно печатает пояснение к графе 7, поэтому там же и слово «НДС».
              "22/122": "22/122", "20/120": "20/120", "18/118": "18/118", "10/110": "10/110",
              "7/107": "7/107", "5/105": "5/105"}
# Графы таблицы бланка — 16 номеров, как в утверждённой форме (включая 10, 10а и 11).
COLUMNS = ("А", "1", "1а", "1б", "2", "2а", "3", "4", "5", "6", "7", "8", "9", "10", "10а", "11")
# Ширины граф в пунктах — сняты с PDF владельца (14 … 827,2 pt), поэтому таблица совпадает
# с оригиналом и по границам, и по переносам длинных названий.
# v1.73.6: ширины сняты с PDF из ЭДО (шаг 0.1 pt по вертикальным линиям таблицы товаров):
# код товара 65.2, № п/п 16.6, наименование 138.8, вид товара 36.7, единица 20.3 | 44.2,
# количество 42.8, цена 38.3, стоимость без налога 58.5, акциз 40.5, ставка 30.8,
# сумма налога 55.5, стоимость с налогом 57.0, страна 39.8 | 39.7, номер декларации 80.3.
COLUMN_WIDTHS = (65.2, 16.6, 138.8, 36.7, 20.3, 44.2, 42.8, 38.3, 58.5, 40.5, 30.8, 55.5,
                 57.0, 39.8, 39.7, 80.3)


class PrintFormError(ValueError):
    """Документ нельзя напечатать — сообщение для человека."""


# ------------------------------------------------------------------ формат значений
def money(value: float | None) -> str:
    """16400.5 → «16 400.50»; пусто → «—» (в бумажной форме пустая графа — прочерк).

    v1.73.6: разделитель дробной части — точка, как в PDF из ЭДО (там «155.74»,
    «35 500.00»), а не запятая: бланк должен совпадать с эталоном один в один.
    """
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ")


def qty(value: float | None) -> str:
    """Количество до трёх знаков без хвостовых нулей: 1.25 → «1.25», 10 → «10»."""
    if value is None:
        return "—"
    text = f"{value:,.3f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    return text.replace(",", " ")


def date_words(day: date | None) -> str:
    """14.10.2026 → «14 октября 2026 г.» (так печатает 1С)."""
    if day is None:
        return "—"
    return f"{day.day} {MONTHS[day.month - 1]} {day.year} г."


def date_short(day: date | None) -> str:
    return day.strftime("%d.%m.%Y") if day else ""


def vat_label(rate: str | None) -> str:
    code = (rate or "").strip().lower().replace(" ", "")
    if not code:
        return "без НДС"
    return VAT_LABELS.get(code, code)


def currency_label(code: str | None) -> str:
    value = (code or "643").strip() or "643"
    return f"{CURRENCIES.get(value, 'Валюта')}, код - {value}"


def status_title(function: str) -> str:
    """Статус документа: 2 — только передаточный документ (акт), 1 — счёт-фактура и передаточный."""
    return "2" if (function or "").upper() == "ДОП" else "1"


# ------------------------------------------------------------------ значения по умолчанию
def auto_from(parsed: dict) -> dict:
    """Реквизиты печатной формы из разобранного XML (upd.parse) — их не нужно вводить руками.

    Это не правки бухгалтера, а то, что уже есть в электронном документе: адреса сторон,
    грузоотправитель и грузополучатель, документ об отгрузке, основание передачи, подписант.
    Хранится рядом с правками в print_json (ключ «auto»), поэтому форма не зависит от того,
    остался ли исходный XML под рукой.
    """
    seller = parsed.get("seller") or {}
    buyer = parsed.get("buyer") or {}
    seller_print = parsed.get("seller_print") or {}
    buyer_print = parsed.get("buyer_print") or {}
    signer = parsed.get("signer") or {}
    # v1.73.6: у УПД подписантов два — продавец («Товар передал») и покупатель («Товар получил»).
    # В PDF из ЭДО правая половина блока подписей заполнена подписью получателя; берём второго
    # подписанта, если он есть в файле, иначе поля остаются под рукописную подпись.
    signers = [s for s in (parsed.get("signers") or []) if s.get("name")]
    receiver = signers[1] if len(signers) > 1 else {}
    ogrnip, ogrnip_date = (seller_print.get("ogrnip") or ""), (seller_print.get("ogrnip_date") or "")
    subject_seller = ", ".join(p for p in (seller.get("name"), f"ИНН {seller['inn']}" if seller.get("inn") else "") if p)
    subject_buyer = ", ".join(p for p in (buyer.get("name"), f"ИНН {buyer['inn']}" if buyer.get("inn") else "") if p)
    out = {
        "seller_address": seller_print.get("address") or "",
        "buyer_address": buyer_print.get("address") or "",
        "shipper": parsed.get("shipper") or "",
        "consignee": parsed.get("consignee") or buyer.get("name") or "",
        "shipment_doc": parsed.get("shipment_doc") or "",
        "basis": parsed.get("basis") or "",
        "seller_ogrnip": ", ".join(p for p in (ogrnip, ogrnip_date) if p),
        # строка «составителя документа»: полное наименование из Документ/@НаимЭконСубСост,
        # а если его нет — краткое имя и ИНН продавца
        "seller_subject": parsed.get("subject") or subject_seller,
        "buyer_subject": subject_buyer,
        "transfer_position": signer.get("role") or "",
        "transfer_name": signer.get("name") or "",
        "transfer_sign": "электронно" if signer.get("name") else "",
        # v1.73.4: в PDF из ЭДО подпись руководителя в блоке под таблицей тоже электронная —
        # там «электронно» и ФИО подписанта, а не пустые линии под рукописную подпись.
        "signer_chief": signer.get("name") or "",
        "signer_chief_role": signer.get("role") or "",
        "transfer_date": date_short(parsed.get("date")),
        "accept_date": date_short(parsed.get("date")),
    }
    if receiver:
        out["accept_position"] = receiver.get("role") or ""
        out["accept_name"] = receiver.get("name") or ""
        out["accept_sign"] = "электронно"
    if seller.get("kind") == "ИП":
        out["signer_entrepreneur"] = signer.get("name") or seller.get("name", "").replace("ИП ", "", 1)
    return {k: v for k, v in out.items() if v}


def defaults(doc: PrimaryDocument) -> dict:
    """Значения по умолчанию: сначала то, что программа знает из документа, потом — из XML."""
    count = len(doc.lines or [])
    shipment = ""
    if doc.doc_number:
        shipment = f"№ п/п 1–{max(count, 1)} № {doc.doc_number} от {date_words(doc.doc_date)}"
    buyer = (doc.buyer_name or "").strip()
    values = {
        "status": status_title(doc.function),
        "correction": "",
        "seller_address": "",
        "buyer_address": "",
        "shipper": "он же" if buyer else "",
        "consignee": buyer,
        "payment_doc": "—",
        "payment_advance": "",
        "shipment_doc": shipment or "—",
        "contract_id": "",
        "currency": currency_label(doc.currency),
        "basis": "",
        "sheets": "1",
        "transfer_position": "",
        "transfer_sign": "",
        "transfer_name": "",
        "accept_position": "",
        "accept_sign": "",
        "accept_name": "",
        "transfer_date": date_short(doc.doc_date),
        "accept_date": date_short(doc.doc_date),
        "transfer_other": "",
        "accept_other": "",
        "seller_bank": "",
        "buyer_bank": "",
        "seller_subject": "",
        "buyer_subject": "",
        "signer_chief_role": "",
        "signer_chief": "",
        "signer_accountant_role": "",
        "signer_accountant": "",
        "signer_entrepreneur": "",
        "seller_ogrnip": "",
        "resp_left_position": "",
        "resp_left_sign": "",
        "resp_left_name": "",
        "resp_right_position": "",
        "resp_right_sign": "",
        "resp_right_name": "",
        "edo_operator": "",
        "edo_id": "",
        "edo_sent_name": "",
        "edo_sent_time": "",
        "edo_sent_cert": "",
        "edo_approved_name": "",
        "edo_approved_time": "",
        "edo_approved_cert": "",
        "edo_approved_role": "",
    }
    values.update(auto(doc))
    return values


def auto(doc: PrimaryDocument) -> dict:
    """Реквизиты из XML, сохранённые вместе с документом (ключ «auto» в print_json)."""
    raw = stored_raw(doc).get("auto")
    if not isinstance(raw, dict):
        return {}
    return {k: str(v)[:LIMITS[k]] for k, v in raw.items() if k in LIMITS and v not in (None, "")}


def stored_raw(doc: PrimaryDocument) -> dict:
    raw = getattr(doc, "print_json", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def stored(doc: PrimaryDocument) -> dict:
    """Правки бухгалтера из колонки print_json (без служебного ключа «auto»)."""
    return {k: v for k, v in stored_raw(doc).items() if k in LIMITS}


def clean(raw: dict | None) -> dict:
    """Правки из базы или запроса: только известные поля, строки, без хвостов, в пределах длины."""
    out: dict = {}
    for key, value in (raw or {}).items():
        if key not in LIMITS:
            continue
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        out[key] = " ".join(value.split())[:LIMITS[key]]
    if out.get("status") not in (None, "1", "2"):
        out.pop("status", None)
    if "sheets" in out and not out["sheets"].isdigit():
        out.pop("sheets", None)
    return out


def keep(doc: PrimaryDocument, values: dict, explicit: set[str] | None = None) -> dict:
    """Склеить присланные правки с уже сохранёнными: непереданное поле не стирает заполненное.

    Окно печати отправляет все поля формы, поэтому явно очищенное поле сохраняется пустым.
    Боты и скрипты зовут PUT частично (одно-два поля) — остальное остаётся как было, вместе
    с реквизитами из XML. Раньше такой запрос затирал всё остальное.
    """
    out = dict(stored(doc))
    sent = clean(values)
    if explicit is None:
        out.update(sent)
        return out
    for key in explicit:
        if key not in LIMITS:
            continue
        if key in sent:
            out[key] = sent[key]
        else:
            # явно очищено (пустая строка, не число листов) — правка снимается,
            # и печать берёт значение по умолчанию из документа
            out.pop(key, None)
    return out


def form(doc: PrimaryDocument, saved: dict | None = None) -> dict:
    """Поля печатной формы: значения документа, поверх — правки.

    saved=None — берём сохранённые к документу правки (печать без окна), saved={} — только документ.
    """
    out = defaults(doc)
    out.update(clean(stored(doc) if saved is None else saved))
    return out


def dump(values: dict, auto_values: dict | None = None) -> str:
    """Сохранить правки в колонку print_json (компактно и по-русски читаемо).

    Реквизиты из XML лежат рядом под ключом «auto»: печать без окна не теряет адреса и
    подписанта, даже если правок нет вовсе.
    """
    payload: dict = clean(values)
    auto_clean = {k: str(v)[:LIMITS[k]] for k, v in (auto_values or {}).items() if k in LIMITS and v}
    if auto_clean:
        payload["auto"] = auto_clean
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ сборка HTML
# Геометрия листа: 841,92 × 594,96 pt (A4 landscape) — размер исходного PDF владельца.
# Ширины граф таблицы ниже сняты с того же PDF, поэтому границы граф совпадают с оригиналом.
PAGE_W, PAGE_H = 841.92, 594.96
HDR_LABEL_W = 146.3
HDR_NUM_W = 20.0


def _esc(value) -> str:
    from html import escape
    return escape("" if value is None else str(value))


def header_rows(doc: PrimaryDocument, values: dict) -> str:
    """Строки шапки (2)–(8): подпись слева, значение на линии, номер строки справа.

    Таблица прижата низом к таблице товаров (как в бланке): если адрес или название
    длиннее строки, добавляется вторая строка, а верх шапки уходит вверх — таблица
    товаров от этого не сдвигается.
    """
    rows = [
        ("Продавец", doc.seller_name or "—", "2", True),
        ("Адрес", values.get("seller_address", ""), "2а", False),
        ("ИНН/КПП продавца", " / ".join(x for x in (doc.seller_inn, doc.seller_kpp) if x) or "—", "2б", False),
        ("Грузоотправитель и его адрес:", values.get("shipper", ""), "3", False),
        ("Грузополучатель и его адрес:", values.get("consignee", ""), "4", False),
        # Подписи строк (5) и (5б) пишутся с «ё»: это правило репозитория
        # (тест «ё там, где требует норма»), даже если в PDF из ЭДО её нет.
        ("К платежно-расчётному документу", values.get("payment_doc", ""), "5", False),
    ]
    # v1.73.4: строка (5б) есть в бланке всегда — после (5а) и перед (6); пустая печатается
    # прочерком, как в PDF из ЭДО («-»), иначе нумерация строк шапки «прыгает».
    rows += [
        ("Документ об отгрузке:", values.get("shipment_doc", ""), "5а", False),
        ("К счёту-фактуре (счетам-фактурам), выставленному (выставленным) при получении оплаты, "
         "частичной оплаты или иных платежей в счёт предстоящих поставок товаров (выполнения работ, "
         "оказания услуг), передачи имущественных прав",
         values.get("payment_advance", "") or "-", "5б", False),
        ("Покупатель", doc.buyer_name or "—", "6", True),
        ("Адрес", values.get("buyer_address", ""), "6а", False),
        ("ИНН/КПП покупателя", " / ".join(x for x in (doc.buyer_inn, doc.buyer_kpp) if x) or "—", "6б", False),
        ("Валюта: наименование, код", values.get("currency", ""), "7", False),
        ("Идентификатор государственного контракта, договора (соглашения) (при наличии)",
         values.get("contract_id", ""), "8", False),
    ]
    def row(label: str, value: str, number: str, bold: bool) -> str:
        # v1.73.4: длинная подпись (5б) в бланке занимает всю ширину шапки, а значение
        # печатается справа у номера строки — поэтому у неё своя разметка, иначе строка
        # либо уезжает за правый край, либо ломается на десять строк в узкой колонке.
        if len(label) > 60:
            return (f'<tr class="wide"><td class="hl hl-wrap" colspan="2">{_esc(label)}'
                    f'<span class="hv-r{" b" if bold else ""}">{_esc(value)}</span></td>'
                    f'<td class="hn">({_esc(number)})</td></tr>')
        return (f'<tr><td class="hl">{_esc(label)}</td>'
                f'<td class="hv{" b" if bold else ""}">{_esc(value)}</td>'
                f'<td class="hn">({_esc(number)})</td></tr>')

    body = "".join(row(*item) for item in rows)
    # v1.73.6 (аудит): строка-распорка — прижимает шапку низом к таблице товаров, как в PDF,
    # где между строкой (8) и шапкой таблицы ровно 10.5 pt белого поля.
    filler = '<tr class="filler"><td colspan="3"></td></tr>'
    return f'<table class="hdr"><colgroup><col style="width:{HDR_LABEL_W}pt"><col>' \
           f'<col style="width:{HDR_NUM_W}pt"></colgroup><tbody>{body}{filler}</tbody></table>'


def table_head() -> str:
    """Шапка таблицы: три яруса, как в бланке — названия граф, уточнения и номера граф."""
    def cell(title: str, span: int = 1, rowspan: int = 1) -> str:
        extra = (f' colspan="{span}"' if span > 1 else "") + (f' rowspan="{rowspan}"' if rowspan > 1 else "")
        return f"<th{extra}>{title}</th>"

    first = "".join((
        cell("Код<br>товара/<br>работ,<br>услуг", 1, 2),
        cell("№<br>п/<br>п", 1, 2),
        cell("Наименование товара<br>(описание выполненных работ,<br>оказанных услуг),<br>"
             "имущественного права", 1, 2),
        cell("Код вида<br>товара", 1, 2),
        cell("Единица<br>измерения", 2),
        cell("Количество<br>(объем)", 1, 2),
        cell("Цена<br>(тариф)<br>за<br>единицу<br>измерения", 1, 2),
        cell("Стоимость<br>товаров<br>(работ,услуг),<br>имущественных<br>прав без<br>налога - всего", 1, 2),
        cell("В том<br>числе<br>сумма<br>акциза", 1, 2),
        cell("Налого-<br>вая<br>ставка", 1, 2),
        cell("Сумма налога,<br>предъявляемая<br>покупателю", 1, 2),
        cell("Стоимость<br>товаров<br>(работ, услуг),<br>имущественных<br>прав с<br>налогом - всего", 1, 2),
        cell("Страна происхож-<br>дения товара", 2),
        cell("Регистрационный<br>номер декларации на<br>товары или<br>регистрационный<br>номер партии товара,<br>"
             "подлежащего<br>прослеживаемости", 1, 2),
    ))
    second = "".join((
        cell("код"), cell("условное<br>обозначе-<br>ние (нацио-<br>нальное)"),
        cell("Цифро-<br>вой код"), cell("Краткое<br>наимено-<br>вание"),
    ))
    numbers = "".join(f"<th>{_esc(number)}</th>" for number in COLUMNS)
    return f'<thead><tr>{first}</tr><tr>{second}</tr><tr class="letters">{numbers}</tr></thead>'


def _line_cells(line, index: int) -> list[str]:
    """Значения граф А…11 одной строки товаров."""
    rate = vat_label(line.vat_rate)
    no_vat = rate == "без НДС"
    return [
        line.item_code or "—",
        str(line.position or index),
        line.name or "—",
        "—",
        line.unit_code or "—",
        line.unit_name or "—",
        qty(line.quantity),
        money(line.price),
        money(line.sum_without_vat),
        "без акциза",
        rate,
        "без НДС" if no_vat else money(line.vat_sum),
        money(line.sum_with_vat),
        "—",
        "—",
        "—",
    ]


def line_rows(doc: PrimaryDocument) -> str:
    rows = []
    for index, line in enumerate(doc.lines or [], 1):
        cells = "".join(
            f'<td class="{"l" if i == 2 else "c"}">{_esc(value)}</td>' for i, value in enumerate(_line_cells(line, index)))
        rows.append(f"<tr>{cells}</tr>")
    if not rows:
        rows.append(f'<tr><td class="l empty-row" colspan="16">В документе нет строк</td></tr>')
    return "".join(rows)


def totals_row(doc: PrimaryDocument, values: dict) -> str:
    """«Всего к оплате»: графы 5, 6 («Х»), 8 и 9 — как в бланке; графа 7 не заполняется."""
    vat_empty = all((line.vat_rate or "none") == "none" for line in (doc.lines or []))
    vat_cell = "без НДС" if vat_empty else money(doc.vat_total)
    # v1.73.6: в PDF из ЭДО строка «Всего к оплате» обрывается на графе 9 — справа от неё
    # рамки нет (графы 10, 10а и 11 остаются только у строк товаров). Поэтому последняя
    # ячейка занимает оставшиеся три графы (39.8 + 39.7 + 80.3 = 159.8 pt).
    return (f'<tr class="totals"><td class="l" colspan="8">Всего к оплате</td>'
            f'<td class="c">{_esc(money(doc.total_without_vat))}</td><td class="c">Х</td>'
            f'<td class="c"></td><td class="c">{_esc(vat_cell)}</td>'
            f'<td class="c">{_esc(money(doc.total_with_vat))}</td>'
            f'<td class="c" colspan="3"></td></tr>')


def _triple(values: dict, pos_key: str, sign_key: str, name_key: str, sign_default: str = "электронно") -> str:
    """Три поля в строке: должность | подпись | ф.и.о. — отдельные линии и подписи под ними."""
    return (
        '<table class="triple"><colgroup><col style="width:44%"><col style="width:22%"><col></colgroup><tbody>'
        "<tr><td>" + _esc(values.get(pos_key, "")) + "</td>"
        '<td class="c">' + _esc(values.get(sign_key, "") or sign_default) + "</td>"
        '<td class="c">' + _esc(values.get(name_key, "")) + "</td></tr>"
        '<tr class="cap"><td>(должность)</td><td class="c">(подпись)</td><td class="c">(ф.и.о.)</td></tr>'
        "</tbody></table>")


def signatures(values: dict) -> str:
    """Блок под таблицей товаров: листы, подписи, ОГРНИП ИП.

    Геометрия — как в утверждённом бланке: слева вне рамки «Документ составлен на … листах»,
    справа в рамке две колонки подписей (руководитель / главный бухгалтер), под ними
    индивидуальный предприниматель и ОГРНИП, разделённые линией.
    """
    def lines(sign_value: str, name_value: str, sign_default: str = "") -> str:
        return ('<table class="sgn"><colgroup><col style="width:34%"><col></colgroup><tbody>'
                f'<tr><td class="c">{_esc(sign_value or sign_default)}</td><td class="c">{_esc(name_value)}</td></tr>'
                '<tr class="cap"><td class="c">(подпись)</td><td class="c">(ф.и.о.)</td></tr>'
                "</tbody></table>")

    # подпись руководителя в электронном документе ставится словами «электронно» — так же,
    # как в PDF из ЭДО; если ФИО не заполнено, остаётся пустая линия под рукописную подпись
    chief = values.get("signer_chief", "")
    # v1.73.6: в PDF из ЭДО «электронно» и ФИО стоят НА ОДНОЙ строке с должностью подписанта
    # («Руководитель группы  электронно  Мельникова Анна Валерьевна»), а не ниже неё.
    role = values.get("signer_chief_role", "")
    left = (
        '<table class="roleline"><colgroup><col style="width:44%"><col style="width:22%"><col></colgroup>'
        '<tbody><tr>'
        '<td class="r"><div class="role">Руководитель организации<br>или иное уполномоченное лицо</div>'
        f'<div class="pos">{_esc(role)}</div></td>'
        f'<td class="c sgn">{_esc("электронно" if chief else "")}</td>'
        f'<td class="c sgn">{_esc(chief)}</td>'
        "</tr>"
        '<tr class="cap"><td class="r"></td><td class="c">(подпись)</td><td class="c">(ф.и.о.)</td></tr>'
        "</tbody></table>"
        '<div class="role ip">Индивидуальный предприниматель<br>или иное уполномоченное лицо</div>'
        + lines("", values.get("signer_entrepreneur", ""), "электронно")
    )
    accountant_role = values.get("signer_accountant_role", "")
    right = (
        '<div class="role">Главный бухгалтер<br>или иное уполномоченное лицо</div>'
        f'<div class="pos">{_esc(accountant_role)}</div>'
        + lines("", values.get("signer_accountant", ""))
        + f'<div class="ogrnip">{_esc(values.get("seller_ogrnip", ""))}</div>'
        + '<div class="hint1">(основной государственный регистрационный номер индивидуального '
          "предпринимателя и дата присвоения такого номера)</div>"
    )
    return (
        '<table class="signers"><colgroup><col style="width:66.8pt"><col style="width:65.5pt"><col></colgroup>'
        "<tbody><tr>"
        f'<td class="sheets"><div>Документ</div><div>составлен на</div>'
        f'<div><span class="fill">{_esc(values.get("sheets") or "1")}</span> листах</div></td>'
        '<td class="boxed" colspan="2"><table class="signgrid">'
        '<colgroup><col style="width:50%"><col></colgroup><tbody>'
        f'<tr><td>{left}</td><td class="vr">{right}</td></tr>'
        "</tbody></table></td></tr></tbody></table>")


def _date_words(raw: str) -> str:
    """Дата из поля «дата отгрузки/приёмки» (ДД.ММ.ГГГГ) → «« 06 » апреля 20 26 г.» — как в бланке."""
    parts = (raw or "").strip().replace("-", ".").split(".")
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        return ""
    day, month, year = (p.strip() for p in parts)
    month_name = MONTHS[int(month) - 1] if 1 <= int(month) <= 12 else ""
    return f"« {day} » {month_name} 20 {year[2:]} г."


def tail(values: dict) -> str:
    """Нижняя половина бланка: основание передачи, транспортировка, передал/получил, реквизиты, ЭДО."""
    def halves(left_html: str, right_html: str, cls: str = "") -> str:
        """Пара половин одной полосы; cls задаёт высоту полосы по PDF из ЭДО."""
        return (f'<table class="halves {cls}"><colgroup><col style="width:50%"><col></colgroup>'
                f'<tr><td class="hl">{left_html}</td><td class="hr">{right_html}</td></tr></table>')

    bank_extra = bool((values.get("seller_bank") or "").strip() or (values.get("buyer_bank") or "").strip())
    transfer_date = _date_words(values.get("transfer_date", ""))
    accept_date = _date_words(values.get("accept_date", ""))
    return "".join((
        '<div class="basisline">'
        '<span class="lbl">Основание передачи (сдачи)/получения приемки</span>'
        f'<span class="fillline">{_esc(values.get("basis", ""))}</span></div>',
        '<div class="hint1 center">(договор; доверенность и др.)</div>',
        '<div class="transportline">'
        '<span class="lbl">Данные о транспортировке и грузе</span>'
        '<span class="fillline"></span></div>',
        '<div class="hint1 center one-line">(транспортная накладная, поручение экспедитору, '
        "экспедиторская/складская расписка и др./масса нетто/брутто груза, если не приведены ссылки "
        "на транспортные документы, содержащие эти сведения)</div>",
        halves('<div class="lbl">Товар (груз) передал/услуги, результаты работ, права сдал</div>'
               + _triple(values, "transfer_position", "transfer_sign", "transfer_name"),
               '<div class="lbl">Товар (груз) получил/услуги, результаты работ, права принял</div>'
               + _triple(values, "accept_position", "accept_sign", "accept_name"), "b-transfer"),
        halves('<div class="dateline"><span class="lbl">Дата отгрузки, передачи (сдачи)</span>'
               f'<span class="date">{_esc(transfer_date)}</span></div>',
               '<div class="dateline"><span class="lbl">Дата получения (приемки)</span>'
               f'<span class="date">{_esc(accept_date)}</span></div>', "b-date"),
        # v1.73.6 (аудит): раскладка нижней половины снята с PDF из ЭДО строка за строкой.
        # Полоса «Иные сведения» (445.9…467.1): слева подпись+линия, справа ещё и
        # «Товары … приняты без расхождений (претензий)» (458.6) — отдельной полосы
        # во всю ширину нет. Подсказки (468.6) открывают следующую полосу.
        halves('<div class="lbl">Иные сведения об отгрузке, передаче</div>'
               f'<div class="fillline">{_esc(values.get("transfer_other", ""))}</div>',
               '<div class="lbl">Иные сведения о получении, приемке</div>'
               f'<div class="fillline">{_esc(values.get("accept_other", ""))}</div>'
               '<div class="lbl acc">Товары (работы, услуги, права) приняты без расхождений (претензий)</div>',
               "b-other"),
        halves('<div class="hint1">(ссылки на неотъемлемые приложения, сопутствующие документы, '
               'иные документы и т. п.)</div>',
               '<div class="hint1">(информация о наличии/отсутствии претензии; ссылки на неотъемлемые '
               'приложения и другие документы и т. п.)</div>', "b-hints"),
        halves('<div class="lbl">Ответственный за правильность оформления факта хозяйственной жизни</div>'
               + _triple(values, "resp_left_position", "resp_left_sign", "resp_left_name"),
               '<div class="lbl">Ответственный за правильность оформления факта хозяйственной жизни</div>'
               + _triple(values, "resp_right_position", "resp_right_sign", "resp_right_name"), "b-resp"),
        halves('<div class="lbl">Наименование экономического субъекта — составителя документа '
               "(в т. ч. комиссионера/агента)</div>"
               f'<div class="fillline">{_esc(values.get("seller_subject", ""))}</div>'
               '<div class="hint1">(может не заполняться при проставлении печати в М. П., '
               "может быть указан ИНН/КПП)</div>",
               '<div class="lbl">Наименование экономического субъекта — составителя документа</div>'
               f'<div class="fillline">{_esc(values.get("buyer_subject", ""))}</div>'
               '<div class="hint1">(может не заполняться при проставлении печати в М. П., '
               "может быть указан ИНН/КПП)</div>", "b-subject"),
        # v1.73.6 (аудит): в PDF из ЭДО (бланк 1137 в ред. 23.01.2026) строки банковских
        # реквизитов НЕТ — полоса печатается только если поля заполнены в форме.
        halves(f'<div class="fillline">{_esc(values.get("seller_bank", ""))}</div>'
               '<div class="hint1 center">(банковские реквизиты продавца)</div>',
               f'<div class="fillline">{_esc(values.get("buyer_bank", ""))}</div>'
               '<div class="hint1 center">(банковские реквизиты покупателя)</div>', "b-bank") if bank_extra else "",
        # v1.73.5: блок «ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ» (сертификаты, «ОТПРАВЛЕНО»,
        # «УТВЕРЖДЕНО», идентификатор и оператор ЭДО) в печатный бланк не выводится — по
        # указанию владельца: печатается сама форма УПД, а не протокол подписания.
        '<div class="mp">М.П.</div>',
    ))


CSS = """
/* v1.73.6: размер страницы задан точно в пунктах, а не «A4 landscape» (595,28 pt):
   лист 594,96 pt плюс рамка не влезали в округлённый A4 и «М.П.» уезжало на вторую
   страницу. Теперь один документ — ровно одна страница. */
@page { size: 841.92pt 594.96pt; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: #eef1f5; color: #000; font-family: Tahoma, 'PT Sans', 'DejaVu Sans', Arial, sans-serif;
       font-size: 7pt; line-height: 1.2; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.toolbar { display: flex; gap: 10px; align-items: center; justify-content: center; padding: 10px; font-size: 12px; }
.toolbar button { font: inherit; font-size: 13px; padding: 6px 14px; cursor: pointer; }
.toolbar .hint { color: #444; }
/* лист: A4 landscape 841,92 × 594,96 pt, поля 14 pt — как в PDF владельца */
table.page { width: 841.92pt; height: 594.96pt; margin: 10px auto; background: #fff; border-collapse: collapse;
             box-shadow: 0 2px 14px rgba(0,0,0,.18); table-layout: fixed; }
table.page > tbody > tr > td { padding: 14pt; vertical-align: top; }
.top { display: table; width: 100%; table-layout: fixed; }
.stub { display: table-cell; width: 71.3pt; vertical-align: top; padding-right: 0; }
.head { display: table-cell; vertical-align: top; }
.formname { font-size: 7pt; line-height: 8.2pt; }
.statusline { margin-top: 2.4pt; }
.status-code { display: inline-block; min-width: 21pt; border: .8pt solid #000; text-align: center;
               padding: 0 2pt; font-weight: 700; }
.legend { margin-top: 4.4pt; font-size: 7.5pt; line-height: 9pt; }
.appendix { text-align: right; font-size: 7pt; line-height: 8.3pt; }
table.headtop { width: 100%; border-collapse: collapse; table-layout: fixed; }
table.headtop td { vertical-align: top; padding: 0; }
table.headtop td.apx { text-align: right; }
.title { font-size: 9pt; font-weight: 700; margin-top: 1.4pt; }
.correction { font-size: 7pt; margin-top: 1.6pt; }
/* --- шапка (2)–(8): 14 строк, как в бланке --- */
table.hdr { width: 100%; border-collapse: collapse; table-layout: fixed; margin-top: .8pt; }
.hdr td { border-bottom: .6pt solid #000; padding: 0 2pt 1.1pt 0; vertical-align: bottom; }
.hdr .hl { padding-left: 5pt; }   /* снято с PDF: подписи шапки начинаются на 5 pt от рамки */
/* v1.73.4: длинная подпись строки (5б — «К счёту-фактуре…») переносится по словам,
   как в бланке; остальные подписи остаются в одну строку */
.hdr .hl-wrap { white-space: normal; line-height: 1.05; }
.hdr .hv-r { float: right; padding-left: 6pt; }
.hdr .hn { text-align: right; font-size: 6.8pt; padding-right: 1pt; border-bottom: 0; }
.hdr tr:last-child td { border-bottom: 0; }
/* распорка: белое поле между шапкой (8) и таблицей товаров — ровно как в PDF */
.hdr tr.filler td { border-bottom: 0; padding: 0; height: 8.5pt; }
.b { font-weight: 700; }
/* --- таблица товаров: 16 граф, ширины сняты с оригинала --- */
/* v1.73.6 (аудит): в PDF между строкой (8) и шапкой таблицы 10.5 pt белого поля */
table.grid { width: 805pt; border-collapse: collapse; table-layout: fixed; margin-top: 4.2pt; }
.grid th, .grid td { border: .6pt solid #000; padding: .3pt 1.8pt; vertical-align: middle; font-weight: 400; }
/* v1.73.6 (аудит): в PDF текст шапки прижат к ВЕРХУ ячейки (первая строка на 2 pt ниже
   линии), у нас он центрировался — из-за этого «Код товара» уезжал на 12.6 pt вниз */
.grid th { text-align: center; vertical-align: top; font-size: 6.7pt; line-height: 1.05; overflow-wrap: anywhere;
           word-break: break-word; }
.grid td { font-size: 7pt; }
.grid td.c { text-align: center; }
.grid td.l { text-align: left; }
.grid tr.totals td { font-weight: 700; }
.grid tr.totals td:first-child { text-align: left; font-size: 8pt; }
.empty-row { text-align: left; }
/* --- подписи: слева «на … листах», справа рамка с двумя колонками --- */
/* v1.73.6: высоты подогнаны под PDF из ЭДО — блок подписей там 65.3 pt
   (301.8 … 367.1), иначе нижняя половина уезжала за нижний край листа. */
table.signers { width: 813.2pt; border-collapse: collapse; table-layout: fixed; }
table.signers td { vertical-align: top; padding: 0; }
.signers .sheets { padding-top: 1.2pt; font-size: 7pt; line-height: 8.8pt; }
.signers .fill { display: inline-block; min-width: 16pt; border-bottom: .6pt solid #000; text-align: center; }
.signers .boxed { border: .6pt solid #000; padding: .8pt 3pt 0; }
table.signgrid { width: 100%; border-collapse: collapse; table-layout: fixed; }
table.signgrid td { vertical-align: top; padding: 0 5pt 0 2pt; }
table.signgrid td.vr { border-left: .6pt solid #000; padding-left: 6pt; }
.signers .role { line-height: 7.9pt; }
.signers .ip { margin-top: .8pt; }
.signers .pos { font-size: 6.9pt; min-height: 6.4pt; line-height: 7.6pt; }
table.sgn { width: 100%; border-collapse: collapse; table-layout: fixed; margin-top: 1pt; }
table.sgn td { border-bottom: .6pt solid #000; height: 7.4pt; vertical-align: bottom; font-size: 6.9pt;
               padding: 0 1pt; }
table.sgn tr.cap td { border-bottom: 0; height: 5.4pt; font-size: 6pt; vertical-align: top; }
table.sgn td.c { text-align: center; }
/* v1.73.6: строка подписи руководителя — должность слева, «электронно» и ФИО на одной
   линии с ней, как в PDF из ЭДО; линия подписи проходит под всей строкой. */
table.roleline { width: 100%; border-collapse: collapse; table-layout: fixed; margin-top: 1pt; }
table.roleline td { vertical-align: bottom; padding: 0 1pt; }
table.roleline td.r { text-align: left; }
table.roleline td.c { text-align: center; }
table.roleline td.sgn { border-bottom: .6pt solid #000; height: 7.4pt; font-size: 6.9pt; }
table.roleline tr.cap td { border-bottom: 0; height: 5.4pt; font-size: 6pt; padding-top: 0; }
.signers .ogrnip { margin-top: 4pt; border-bottom: .6pt solid #000; font-size: 6.9pt; min-height: 6.6pt; }
.signers .hint1 { font-size: 6pt; line-height: 1.05; }
/* v1.73.6 (аудит): в PDF подсказка под ОГРНИП — одна строка; у нас она ломалась на две
   и растягивала рамку подписей на 5.6 pt, сдвигая вниз всю нижнюю половину */
.signers .hint1 { white-space: nowrap; font-size: 5.6pt; letter-spacing: -.1pt; }
/* подсказка ОГРНИП должна остаться в одну строку и ВНУТРИ рамки: при 6 pt
   Tahoma делает её на 390 pt и она вылезала за правый край листа (842.4 при
   ширине 841.92), в PDF — 346 pt при 6 pt */
/* --- нижняя половина: основание, передача товара, реквизиты, ЭДО --- */
/* v1.73.6: интервалы и высоты строк нижней половины — по PDF из ЭДО
   (Основание 367.1, Данные 385.1, Товар передал 404.6, Дата 435.4, Ответственный 477.4,
   Наименование 508.2, М.П. 535.2; нижняя граница листа 594.96). */
/* v1.73.6 (аудит): в PDF нижняя половина идёт шагами — основание 367.1, данные 385.1,
   передал 404.6, дата 435.4, иные 445.9, приняты 458.6, ответственный 477.4,
   наименование 508.2, М.П. 535.2. Интервалы заданы явно. */
.tail { font-size: 7pt; }
/* v1.73.6 (аудит): в PDF «Основание передачи» начинается сразу под рамкой подписей
   (367.1) — отдельного отступа нет */
.basisline, .transportline { display: flex; align-items: baseline; border-bottom: .6pt solid #000;
                             margin-top: .2pt; padding-bottom: .4pt; }
.transportline { margin-top: 1pt; }
.basisline .lbl, .transportline .lbl { flex: none; padding-right: 4pt; }
.basisline .fillline, .transportline .fillline { flex: 1 1 auto; min-height: 7pt; }
.hint1 { font-size: 6pt; line-height: 1.1; }
/* v1.73.6 (аудит): в PDF подсказка к «Данным о транспортировке» — ОДНА строка
   (6 pt, 828 pt ширины); у нас она ломалась на две и добавляла 6.6 pt */
.hint1.center { text-align: center; margin: 0; }
.hint1.one-line { white-space: nowrap; }   /* 6 pt, одна строка — как в PDF */
table.halves { width: 813.2pt; border-collapse: collapse; table-layout: fixed; }
table.halves td { vertical-align: top; padding: 0 2pt 0 2.3pt; }
/* Высоты полос сняты с PDF из ЭДО: полоса «передал» 404.6→435.4 (30.8), дата 10.5,
   иные сведения/приняты 31.5, ответственный 30.8, наименование 27.0 */
table.halves.b-transfer { height: 30.2pt; }
table.halves.b-date { height: 10.5pt; }
table.halves.b-other { height: 17.4pt; }
table.halves.b-bank { height: 22.4pt; }
table.halves.b-hints { height: 8.8pt; }
.dateline { display: flex; align-items: baseline; }
.dateline .date { padding-left: 24pt; }
table.halves.b-resp { height: 30.8pt; }
table.halves.b-subject { height: 26.4pt; }
table.halves td.hr { padding-left: 6pt; }
.lbl { font-size: 7pt; line-height: 8.4pt; }
.fillline { border-bottom: .6pt solid #000; min-height: 7.6pt; }
.date { padding-left: 54pt; }
.lbl.acc { padding-top: .6pt; }
table.triple { width: 100%; border-collapse: collapse; table-layout: fixed; margin-top: .4pt; }
table.triple td { padding: 0 1pt; border-bottom: .6pt solid #000; font-size: 7pt; height: 8.6pt;
                  vertical-align: bottom; }
table.triple tr.cap td { border-bottom: 0; font-size: 6pt; height: 7pt; vertical-align: top; }
table.triple td.c { text-align: center; }
.mp { font-size: 7pt; padding: 1pt 2pt 0; }
/* --- печать и просмотр на узком экране --- */
@media print {
  body { background: #fff; }
  .toolbar { display: none; }
  /* 593,6 pt вместо 594,96 — запас под рамку и округления браузера, чтобы
     «М.П.» гарантированно оставалось на первой странице */
  table.page { margin: 0; box-shadow: none; height: 593.6pt; }
}
@media screen and (max-width: 1100px) { table.page { zoom: .78; } }
@media screen and (max-width: 820px) { table.page { zoom: .52; } }
/* v1.73.5 (Android): бланк — альбомный лист, на телефоне вместе с масштабированием
   это 400+ px в ширину. Узкий экран прокручивается вбок, но не ломает раскладку листа. */
body { overflow-x: auto; -webkit-text-size-adjust: 100%; }
/* Телефон: бланк шире экрана — текст остаётся читаемым, а лист прокручивается вбок.
   Дальше можно приблизить двумя пальцами: maximum-scale не ограничен. */
@media screen and (max-width: 560px) {
  table.page { zoom: .52; margin: 6px auto; }
  .toolbar { padding: 8px 6px; display: block; text-align: center; }
  .toolbar button { width: 100%; margin-bottom: 6px; }
}
/* v1.73.6 (аудит): кнопка на сенсорном экране была 32 px по высоте при каноне
   проекта 44 px (app.css, блок @media (pointer: coarse)). Палец по ней попадал
   мимо, а печать с телефона — основной сценарий. Правило по указателю, а не по
   ширине: планшет и телефон одинаково. */
@media (pointer: coarse) {
  .toolbar button { min-height: 44px; font-size: 15px; }
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
    if doc.correction_number:
        when = doc.correction_date.strftime("%d.%m.%Y") if doc.correction_date else "---"
        correction = f"№ {doc.correction_number} от {when}"
    else:
        correction = data.get("correction") or "№ --- от ---"
    heading = f"Счёт-фактура № {doc.doc_number or 'б/н'} от {date_words(doc.doc_date)}"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- v1.73.6 (аудит): адрес бланка может нести билет печати (?ticket=…). Без этого
     заголовка браузер отправлял его в Referer при загрузке /js/primary_print_page.js,
     и одноразовая ссылка оседала в журналах доступа. -->
<meta name="referrer" content="no-referrer">
<title>{_esc(title(doc))}</title>
<style>{CSS}</style></head>
<body data-print="{1 if auto_print else 0}">
<div class="toolbar"><button type="button" id="pp-print">🖨 Печать / сохранить в PDF</button>
  <span class="hint" id="pp-hint">В окне печати выберите «Сохранить как PDF», чтобы получить файл.</span></div>
<table class="page"><tbody><tr><td>
  <div class="top">
    <div class="stub">
      <div class="formname">Универсальный передаточный документ</div>
      <div class="statusline">Статус: <span class="status-code">{_esc(data.get("status") or "1")}</span></div>
      <div class="legend">1 – счёт-фактура<br>и передаточный<br>документ (акт)<br>2 – передаточный<br>документ (акт)</div>
    </div>
    <div class="head">
      <!-- v1.73.6 (аудит): заголовок и «Исправление» — слева, приложение — справа в тех же
           двух строках, как в PDF из ЭДО (заголовок 19.2, приложение 20.5/28.8). Раньше три
           блока шли друг под другом и вся шапка уезжала вниз на 13.5 pt. -->
      <table class="headtop"><colgroup><col><col style="width:322pt"></colgroup><tbody><tr>
        <td class="hl"><div class="title">{_esc(heading)}</div>
          <div class="correction">Исправление {_esc(correction)}</div></td>
        <td class="apx"><div class="appendix">Приложение N 1 к постановлению Правительства Российской Федерации от 26.12.2011 № 1137
          <br>(в ред. Постановления Правительства РФ от 23.01.2026 № 26)</div></td>
      </tr></tbody></table>
      {header_rows(doc, data)}
    </div>
  </div>
  <table class="grid">
    <colgroup>{"".join(f'<col style="width:{w}pt">' for w in COLUMN_WIDTHS)}</colgroup>
    {table_head()}
    <tbody>{line_rows(doc)}{totals_row(doc, data)}</tbody>
  </table>
  {signatures(data)}
  <div class="tail">{tail(data)}</div>
</td></tr></tbody></table>
<script src="/js/primary_print_page.js" defer></script>
</body></html>"""


def _sheets_word(sheets: str) -> str:
    try:
        value = int(sheets)
    except (TypeError, ValueError):
        return "листах"
    return "листе" if value % 10 == 1 and value % 100 != 11 else "листах"
