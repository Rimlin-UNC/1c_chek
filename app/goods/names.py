"""Читаемое название позиции чека (T-69, v1.70.101).

Строка позиции из ФНС часто «кричит» капсом, начинается с артикула или метки маркировки и прячет
фасовку в середине: «[М] 4607001234567 ЦЕМЕНТ М500 Д0 50КГ». Здесь — чистые функции без базы:

- readable(name) → {"display": «Цемент М500 Д0, 50 кг», "pack": «50 кг», "original": как пришло};
- name_key(name) → ключ товара для памяти исправлений: «цемент» (без фасовки, размеров и регистра).

Правила консервативные: марки и размеры с цифрами («М500», «3,5х35», «АИ-95»), латиница («ROCKWOOL»)
и известные сокращения («ПВХ», «ГКЛ») не трогаются; расшифровываются только сокращения, у которых нет
рода и числа («д/» → «для», «в/с» → «высший сорт»). Исходная строка в базе не меняется.
"""
from __future__ import annotations

import re

# Сокращения, которые остаются заглавными при переводе «капса» в обычный вид
KEEP_UPPER = frozenset("""
пвх пва дсп двп мдф осб гкл гвл цсп ввг ввгнг пнд ппр пэ пп нг пвс шввп узо дт аи гост ту рф ссср вд ак пф гф
нц пц шпц сиз ушм жби пгс оц свч мфу жк ип ооо ао пао ндс двс грм ваз уаз камаз зил мтс сдэк ндфл осаго каско ру
ссд usb led lcd hdmi wifi gsm sim
""".split())

# Единицы фасовки: вес, объём, штуки — уходят в отдельное поле
_PACK_UNITS = {"кг": "кг", "г": "г", "гр": "г", "грамм": "г", "мл": "мл", "л": "л", "литр": "л", "шт": "шт",
               "штук": "шт", "пак": "пак", "пакетиков": "пак", "таб": "таб", "капс": "капс"}
# Единицы размеров: остаются в названии, только приводятся к строчным
_SIZE_UNITS = {"мм": "мм", "см": "см", "м": "м", "м2": "м²", "м²": "м²", "м3": "м³", "м³": "м³", "мкм": "мкм",
               "вт": "Вт", "квт": "кВт", "ач": "А·ч", "мач": "мА·ч", "гб": "ГБ", "тб": "ТБ"}

_NUM = r"\d+(?:[.,]\d+)?"
_UNIT_RE = re.compile(
    r"(?<![а-яa-z])(?P<mult>\d+\s*[xх*×]\s*)?(?P<num>" + _NUM + r")\s*(?P<unit>"
    + "|".join(sorted({*map(re.escape, _PACK_UNITS), *map(re.escape, _SIZE_UNITS)}, key=len, reverse=True))
    + r")\.?(?:\s*/\s*уп(?:ак)?\.?)?(?![а-яa-z0-9²³])", re.IGNORECASE)

# Метки маркировки, номера строк и артикулы в начале строки
_LEAD_RE = re.compile(
    r"^\s*(?:"
    r"[\[(]\s*(?:м|m|м\+|m\+|мп|ж)\s*[\])]"          # [М], (M+), [Ж]
    r"|\*+"                                          # *Пиво
    r"|#\s*\d*"                                      # #12
    r"|(?:№|n|no)\s*\d+[.)]?"                        # №3
    r"|\d{1,3}\s*[.)](?!\d)"                         # 1. 12)
    r"|\d{5,}"                                       # 4607001234567 — штрихкод или артикул
    r"|арт(?:икул)?\.?\s*[:№]?\s*[\w\-/.]*\d[\w\-/.]*"  # Арт. 123456
    r"|код\s*[:№]?\s*\d+"
    r")\s*", re.IGNORECASE)
_TAIL_RE = re.compile(
    r"\s*(?:"
    r"[(\[]\s*(?:арт(?:икул)?|код)\.?\s*[:№]?\s*[^)\]]*[)\]]"  # (арт. 123456)
    r"|/\s*\d{4,}"                                   # /123456
    r"|(?:арт(?:икул)?|код)\.?\s*[:№]?\s*[\w\-/.]*\d[\w\-/.]*"
    r"|\d{6,}"                                       # штрихкод в конце
    r")\s*$", re.IGNORECASE)

# Сокращения без рода и числа — расшифровываются; остальные остаются как есть
_ABBR = [
    (r"(?<![а-яa-z])д/(?=[а-яa-z])", "для "),
    (r"(?<![а-яa-z])б/г(?![а-яa-z])", "без газа"),
    (r"(?<![а-яa-z])с/г(?![а-яa-z])", "с газом"),
    (r"(?<![а-яa-z])б/к(?![а-яa-z])", "без косточки"),
    (r"(?<![а-яa-z])в/с(?![а-яa-z])", "высший сорт"),
    (r"(?<![а-яa-z])м/у(?![а-яa-z])", "мягкая упаковка"),
    (r"(?<![а-яa-z])ст/б(?![а-яa-z])", "стеклянная бутылка"),
    (r"(?<![а-яa-z])пл/б(?![а-яa-z])", "пластиковая бутылка"),
    (r"(?<![а-яa-z])в/у(?![а-яa-z])", "вакуумная упаковка"),
    (r"(?<![а-яa-z])р-р(?![а-яa-z])", "раствор"),
    (r"(?<![а-яa-z])к-т(?![а-яa-z])", "комплект"),
    (r"(?<![а-яa-z])компл\.(?![а-яa-z])", "комплект"),
    (r"(?<![а-яa-z])наб\.(?![а-яa-z])", "набор"),
    (r"(?<![а-яa-z])в ассорт\.(?![а-яa-z])", "в ассортименте"),
    (r"(?<![а-яa-z])ассорт\.(?![а-яa-z])", "ассорти"),
    (r"(?<![а-яa-z])рул\.(?![а-яa-z])", "рулон"),
    (r"(?<![а-яa-z])меш\.(?![а-яa-z])", "мешок"),
    (r"(?<![а-яa-z])фл\.(?![а-яa-z])", "флакон"),
    (r"(?<![а-яa-z])пач\.(?![а-яa-z])", "пачка"),
]
_ABBR_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in _ABBR]

_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")


def _shouting(text: str) -> bool:
    """Строка написана капсом (так кассы часто печатают названия)."""
    letters = _LETTERS.findall(text)
    if len(letters) < 4:
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters) >= 0.7


def _is_latin(token: str) -> bool:
    letters = _LETTERS.findall(token)
    return bool(letters) and all(ord(ch) < 128 for ch in letters)


def _calm_token(token: str) -> str:
    """Слово из «капса» → строчные, кроме марок, латиницы и известных сокращений."""
    if any(ch.isdigit() for ch in token):
        return token                                  # М500, Д0, АИ-95, H4 — марки и модели
    if _is_latin(token):
        return token                                  # ROCKWOOL, HEETS — латиницу не трогаем
    parts = re.split(r"([\-/])", token)
    out = []
    for part in parts:
        low = part.lower()
        if low.replace("ё", "е") in KEEP_UPPER:
            out.append("ВВГнг" if low == "ввгнг" else part.upper())
        else:
            out.append(low)
    return "".join(out)


def _decimal(num: str) -> str:
    return num.replace(".", ",")


def _unit(match: re.Match) -> tuple[str, bool]:
    """(нормализованная запись, это фасовка?)"""
    raw_unit = match.group("unit").lower()
    num = _decimal(match.group("num"))
    mult = match.group("mult")
    if raw_unit in ("г", "гр") and re.fullmatch(r"(19|20)\d\d", match.group("num")):
        return match.group(0), False                  # «2026 г.» — год, а не граммы
    if raw_unit in _PACK_UNITS:
        text = f"{num} {_PACK_UNITS[raw_unit]}"
        if mult:
            text = re.sub(r"\s*[xх*×]\s*", " × ", mult.strip()) + " " + text
            text = re.sub(r"\s+", " ", text)
        return text, True
    text = f"{num} {_SIZE_UNITS.get(raw_unit, raw_unit)}"
    return (mult + text if mult else text), False


def _strip_codes(text: str) -> str:
    prev = None
    while prev != text:                               # «[М] 1. 4607… Цемент» — несколько меток подряд
        prev = text
        text = _LEAD_RE.sub("", text, count=1)
    prev = None
    while prev != text:
        prev = text
        text = _TAIL_RE.sub("", text, count=1)
    return text


def readable(name: str) -> dict:
    """Читаемое название, фасовка и исходная строка позиции."""
    original = str(name or "")
    text = re.sub(r"\s+", " ", original).strip()
    if not text:
        return {"display": "", "pack": "", "original": original, "changed": False}
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'«":
        text = text[1:-1].strip()
    text = text.replace("«", "").replace("»", "") if text.count("«") != text.count("»") else text
    stripped = _strip_codes(text)
    text = stripped if _LETTERS.search(stripped) else text   # от названия не должен остаться пустой код
    shouting = _shouting(text)
    for pat, repl in _ABBR_RE:
        text = pat.sub(repl, text)

    packs: list[str] = []

    def unit_sub(m: re.Match) -> str:
        norm, is_pack = _unit(m)
        if not is_pack:
            return norm
        packs.append(norm)
        return f"\u0000{len(packs) - 1}\u0000"

    text = _UNIT_RE.sub(unit_sub, text)
    text = re.sub(r"(?<=\d)\s?[ХX]\s?(?=\d)", "х", text)      # 4,2Х16 → 4,2х16 (размер, а не буква)
    if shouting:
        text = " ".join(_calm_token(tok) for tok in text.split(" "))
    # фасовка — последняя найденная (обычно в конце: «… 50 кг»); остальные остаются на месте
    pack = packs[-1] if packs else ""
    text = re.sub(r"\u0000(\d+)\u0000",
                  lambda m: "" if int(m.group(1)) == len(packs) - 1 else packs[int(m.group(1))], text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    text = re.sub(r"([(])\s+", r"\1", text)
    text = re.sub(r"[\s,;/\-]+$", "", text)
    text = re.sub(r"^[\s,;/\-.]+", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if text:
        text = text[0].upper() + text[1:]
    display = f"{text}, {pack}" if text and pack else (text or pack)
    return {"display": display, "pack": pack, "original": original,
            "changed": display != re.sub(r"\s+", " ", original).strip()}


_KEY_DROP = frozenset({*_PACK_UNITS, *_SIZE_UNITS, "уп", "упак", "упаковка", "арт", "код", "шт", "в", "и", "с",
                       "по", "на", "для", "из", "без", "от", "до"})


def name_key(name: str) -> str:
    """Ключ товара для памяти исправлений: слова из букв, без фасовки, размеров, кодов и регистра.

    «ЦЕМЕНТ М500 Д0 50КГ» и «Цемент М500 Д0 25 кг» дают один ключ «цемент» — бухгалтер правит
    категорию один раз, а не для каждой фасовки.
    """
    text = _strip_codes(re.sub(r"\s+", " ", str(name or "")).strip())
    for pat, repl in _ABBR_RE:
        text = pat.sub(repl, text)
    words = []
    for tok in re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е")):
        if any(ch.isdigit() for ch in tok) or len(tok) < 2 or tok in _KEY_DROP:
            continue
        words.append(tok)
        if len(words) >= 8:
            break
    return " ".join(words)[:120]
