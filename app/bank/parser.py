"""Разбор выписки в формате обмена 1С с банком «1CClientBankExchange» (T-45, v1.70.98).

Формат — текст «ключ=значение»: заголовок файла, секции «СекцияРасчСчет … КонецРасчСчет» (свой счёт и
период), документы «СекцияДокумент=<вид> … КонецДокумента», в конце «КонецФайла». Кодировка — по строке
«Кодировка=»: Windows (cp1251) или DOS (cp866); встречается и UTF-8. Своя сторона операции — та, чей счёт
указан в «РасчСчет»: плательщик — списание, получатель — поступление.

Хранится минимум (ADR-28): дата, номер, сумма, ИНН и наименование контрагента, назначение (до 210 знаков —
предел поля в платёжном поручении), последние 4 цифры карты. Полные номера счетов, БИК и банки не хранятся.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

MAX_BYTES = 5 * 1024 * 1024                 # выписка за год у малой компании — сотни килобайт
MAX_DOCUMENTS = 20000
HEADER = "1CClientBankExchange"
PURPOSE_MAX = 210

# Виды операций: каким нужны документы, а какие — нет (налоги, зарплата, комиссии банка, свои счета)
NEEDS_DOCUMENTS = ("card", "transfer")
KIND_TITLES = {"card": "покупка по карте", "transfer": "оплата поставщику", "tax": "налоги и взносы",
               "salary": "зарплата и выплаты людям", "fee": "комиссия банка", "own": "перевод между своими счетами",
               "advance": "выдача под отчёт", "income": "поступление", "loan": "кредиты и займы"}

# маска номера карты: звёздочки, латинские x X или русские х Х (\u0445 \u0425 — чтобы буквы не смешивались)
_CARD_MASK = re.compile(r"\b\d{4,6}[*xX\u0445\u0425•]{2,}\d{4}\b")
_CARD_WORD = re.compile(r"карт\w*\s*(?:№|n|#|\*|x{2,}|х{2,})?\s*\**\s*(\d{4})\b", re.I)
_CARD_HINT = re.compile(r"(?:по\s+карт|корпоративн\w*\s+карт|с\s+использованием\s+(?:корпоративной\s+|банковской\s+)?карт"
                        r"|операци\w*\s+по\s+карт|покупк\w*\s+по\s+карт|оплат\w*\s+товар\w*.*карт)", re.I)
# «НДС» в назначении — не признак налога: «в т.ч. НДС 22 %» и «Без налога (НДС)» есть почти в каждой оплате
# поставщику. Налог узнаём по счёту казначейства, реквизитам бюджетного платежа и устойчивым словам.
_TAX = re.compile(r"(?:(?<!\w)енп(?!\w)|единый\s+налогов\w*\s+плат\w*|(?<!\w)ндфл(?!\w)|страхов\w*\s+взнос\w*|"
                  r"налог\w*\s+(?:на\s+)?(?:доход|прибыл|имуществ|землю|транспорт|упрощ|по\s+усн)|(?<!\w)усн(?!\w)|"
                  r"(?<!\w)кбк(?!\w)|уплат\w*\s+налог\w*|штраф\w*\s+(?:фнс|ифнс|сфр|пфр|фсс))", re.I)
_SALARY = re.compile(r"(?:заработн\w*\s+плат|зарплат|з/п\b|оплат\w*\s+труда|отпускн|пособи|больничн|"
                     r"премия\s+сотрудник|выплат\w*\s+(?:сотрудник|работник))", re.I)
_ADVANCE = re.compile(r"(?:под\s*отч[её]т|подотч[её]т|на\s+хозяйственн\w*\s+нужд|командировочн|"
                      r"выдач\w*\s+наличн|сняти\w*\s+наличн|получени\w*\s+наличн)", re.I)
_FEE = re.compile(r"(?:комисси\w*|плат\w*\s+за\s+(?:обслуживан|ведение|смс|пакет)|абонентск\w*\s+плат\w*\s+банк|"
                  r"обслуживани\w*\s+сч[её]т)", re.I)
_LOAN = re.compile(r"(?:погашени\w*\s+(?:кредит|займ|ссуд)|процент\w*\s+по\s+(?:кредит|займ|договору\s+кредит)|"
                   r"выдач\w*\s+займ|возврат\w*\s+займ)", re.I)
_INN_PREFIX = re.compile(r"^\s*ИНН\s*\d{10,12}\s*", re.I)
_TREASURY_ACCOUNTS = ("03100", "03212", "03221", "03222", "40101", "40102")   # казначейские счета — бюджет
_BANK_ACCOUNTS = ("70601", "47423", "30232", "30233", "47422")                 # доходы и расчёты банка


class StatementError(ValueError):
    """Файл нельзя принять — сообщение для бухгалтера."""


def decode(raw: bytes) -> str:
    """Текст выписки: UTF-8 (с меткой или без), иначе по строке «Кодировка=»: DOS — cp866, Windows — cp1251."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    try:
        text = raw.decode("utf-8")
        if HEADER in text[:200]:
            return text
    except UnicodeDecodeError:
        pass
    # «Кодировка=DOS» в cp866 и в cp1251 выглядит по-разному — ищем обе записи
    if "Кодировка=DOS".encode("cp866") in raw[:2000] or "Кодировка=DOS".encode("cp1251") in raw[:2000]:
        return raw.decode("cp866", errors="replace")
    return raw.decode("cp1251", errors="replace")


def _date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _amount(value: str) -> float | None:
    value = (value or "").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        number = round(float(value), 2)
    except ValueError:
        return None
    return number if 0 < number < 1e13 else None


def _digits(value: str, limit: int) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())[:limit]


def _name(raw_name: str, fallback: str = "") -> str:
    name = " ".join((raw_name or fallback or "").split())
    return _INN_PREFIX.sub("", name)[:200]


def parse(raw: bytes) -> dict:
    """Разобрать файл: свои счета, период и документы (словари с исходными полями). Ошибка — StatementError."""
    if not raw:
        raise StatementError("Файл пустой")
    if len(raw) > MAX_BYTES:
        raise StatementError("Файл больше 5 МБ — выгрузите выписку за меньший период")
    text = decode(raw)
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    first = next((line for line in lines if line), "")
    if first.lstrip("\ufeff") != HEADER:
        raise StatementError("Это не выписка в формате обмена с 1С: первая строка должна быть «1CClientBankExchange». "
                             "В интернет-банке выберите выгрузку «в 1С» (файл kl_to_1c.txt)")
    header: dict[str, str] = {}
    accounts: list[str] = []
    sections: list[dict] = []
    documents: list[dict] = []
    current: dict | None = None
    section: dict | None = None
    for line in lines[1:]:
        if not line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "КонецФайла":
            break
        if key == "СекцияДокумент":
            current = {"_kind": value, "_purpose": []}
            continue
        if key == "КонецДокумента":
            if current is not None:
                documents.append(current)
                if len(documents) > MAX_DOCUMENTS:
                    raise StatementError(f"В файле больше {MAX_DOCUMENTS} операций — выгрузите меньший период")
            current = None
            continue
        if key == "СекцияРасчСчет":
            section = {}
            continue
        if key == "КонецРасчСчет":
            if section is not None:
                sections.append(section)
            section = None
            continue
        if current is not None:
            if key == "НазначениеПлатежа" or re.fullmatch(r"НазначениеПлатежа\d", key):
                if value:
                    current["_purpose"].append(value)
            elif key not in current:
                current[key] = value
        elif section is not None:
            section.setdefault(key, value)
        else:
            if key == "РасчСчет" and value:
                accounts.append(_digits(value, 20))
            header.setdefault(key, value)
    for sec in sections:
        acc = _digits(sec.get("РасчСчет", ""), 20)
        if acc and acc not in accounts:
            accounts.append(acc)
    date_from = _date(header.get("ДатаНачала", "")) or min((d for s in sections if (d := _date(s.get("ДатаНачала", "")))),
                                                            default=None)
    date_to = _date(header.get("ДатаКонца", "")) or max((d for s in sections if (d := _date(s.get("ДатаКонца", "")))),
                                                         default=None)
    if not documents and not sections:
        raise StatementError("В выписке нет ни одной операции")
    return {"accounts": [a for a in accounts if a], "date_from": date_from, "date_to": date_to,
            "documents": documents, "sender": header.get("Отправитель", "")[:100]}


def card_tail(purpose: str) -> str:
    """Последние 4 цифры карты из назначения платежа (маска 220220******1234 или «карта *1234»)."""
    mask = _CARD_MASK.search(purpose or "")
    if mask:
        return mask.group(0)[-4:]
    word = _CARD_WORD.search(purpose or "")
    return word.group(1) if word else ""


def classify(doc: dict, accounts: list[str], own_inn: str = "") -> dict | None:
    """Своя операция в удобном виде; None — документ не по нашим счетам (не наша строка выписки)."""
    payer_acc = _digits(doc.get("ПлательщикСчет") or doc.get("ПлательщикРасчСчет", ""), 20)
    payee_acc = _digits(doc.get("ПолучательСчет") or doc.get("ПолучательРасчСчет", ""), 20)
    if payer_acc and payer_acc in accounts:
        direction = "out"
    elif payee_acc and payee_acc in accounts:
        direction = "in"
    elif doc.get("ДатаСписано"):
        direction = "out"
    elif doc.get("ДатаПоступило"):
        direction = "in"
    else:
        return None
    amount = _amount(doc.get("Сумма", ""))
    if amount is None:
        return None
    side = "Получатель" if direction == "out" else "Плательщик"
    other_acc = payee_acc if direction == "out" else payer_acc
    own_side = "Плательщик" if direction == "out" else "Получатель"
    when = _date(doc.get("ДатаСписано" if direction == "out" else "ДатаПоступило", "")) or _date(doc.get("Дата", ""))
    if when is None:
        return None
    purpose = " ".join(" ".join(doc.get("_purpose") or []).split())[:PURPOSE_MAX]
    inn = _digits(doc.get(f"{side}ИНН", ""), 12)
    if inn in ("0", "00", "000000000000"):
        inn = ""
    name = _name(doc.get(f"{side}1", ""), doc.get(side, ""))
    tail = card_tail(purpose)
    own = _digits(doc.get(f"{own_side}ИНН", ""), 12)
    mine = own_inn or own
    budget = bool((doc.get("СтатусСоставителя") or "").strip() or (doc.get("ПоказательКБК") or "").strip(" 0"))
    if direction == "in":
        kind = "income"
    elif (mine and inn and inn == mine) or (other_acc and other_acc in accounts):
        kind = "own"
    elif other_acc.startswith(_TREASURY_ACCOUNTS) or budget or _TAX.search(purpose):
        kind = "tax"
    elif _SALARY.search(purpose):
        kind = "salary"
    elif _ADVANCE.search(purpose):
        kind = "advance"
    elif _FEE.search(purpose) and (not inn or other_acc.startswith(_BANK_ACCOUNTS)):
        kind = "fee"                         # комиссия банка, в том числе «за обслуживание карты *1234»
    elif tail or _CARD_HINT.search(purpose):
        kind = "card"
    elif _LOAN.search(purpose):
        kind = "loan"
    elif other_acc.startswith(_BANK_ACCOUNTS) and not inn:
        kind = "fee"
    else:
        kind = "transfer"
    return {"direction": direction, "kind": kind, "op_date": when, "number": (doc.get("Номер") or "")[:20],
            "amount": amount, "counterparty_inn": inn, "counterparty_name": name, "purpose": purpose,
            "card_tail": tail, "own_inn": own, "account_tail": (payer_acc if direction == "out" else payee_acc)[-4:]}


def op_hash(company_id: str, op: dict) -> str:
    """Отпечаток операции: та же операция из пересекающихся выписок не задвоится."""
    key = "|".join([company_id, op["account_tail"], op["op_date"].isoformat(), op["number"], f"{op['amount']:.2f}",
                    op["direction"], op["counterparty_inn"], op["purpose"][:120]])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
