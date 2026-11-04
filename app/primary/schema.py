"""Проверка электронного УПД по официальной XSD-схеме ФНС формата 5.03 (T-61, v1.70.95).

Схемы лежат в app/primary/xsd/ (происхождение и хеши — там же, README.md). Проверка — через lxml
(libxml2): компиляция схемы 191 КБ занимает около 30 мс и около 10 МБ памяти один раз на процесс.
Документ соответствует формату, если его принимает хотя бы одна из схем каталога; первой
проверяется самая новая редакция.

Несоответствие схеме — замечание, а не ошибка. Операторы ЭДО сами проверяют файлы, а редакции схемы
меняются (так, ставку 22 % внесли в редакцию 05). Решение о вычете остаётся за бухгалтером.

Сюда попадает только XML, который уже прочитал upd.parse через defusedxml (DTD и сущности
запрещены). Парсер lxml дополнительно не раскрывает сущности и не ходит в сеть. Если lxml
не установлен или схем нет, проверка пропускается с понятной причиной: работа не останавливается.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

XSD_DIR = Path(__file__).with_name("xsd")
_PATTERN = "ON_NSCHFDOPPR_1_997_01_05_03_*.xsd"
MAX_DETAILS = 10

_lock = threading.Lock()
_cache: list[tuple[str, object]] | None = None
_cache_error = ""


def _revision(path: Path) -> str:
    return path.stem.rsplit("_", 1)[-1]


def _load() -> tuple[list[tuple[str, object]], str]:
    """Скомпилировать схемы один раз: [(редакция, XMLSchema)], причина недоступности."""
    global _cache, _cache_error
    with _lock:
        if _cache is not None:
            return _cache, _cache_error
        try:
            from lxml import etree
        except ImportError:
            _cache, _cache_error = [], "не установлен модуль lxml"
            return _cache, _cache_error
        files = sorted(XSD_DIR.glob(_PATTERN), key=_revision, reverse=True)
        compiled = []
        for path in files:
            try:
                compiled.append((_revision(path), etree.XMLSchema(etree.parse(str(path)))))
            except (etree.XMLSchemaParseError, etree.XMLSyntaxError, OSError):
                continue
        _cache = compiled
        _cache_error = "" if compiled else "нет файлов схемы ФНС в app/primary/xsd"
        return _cache, _cache_error


def reset_cache() -> None:
    """Для тестов: забыть скомпилированные схемы."""
    global _cache, _cache_error
    with _lock:
        _cache, _cache_error = None, ""


def available() -> dict:
    schemas, reason = _load()
    return {"available": bool(schemas), "revisions": [r for r, _ in schemas], "reason": reason}


# ------------------------------------------------------------------ сообщения по-русски
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"The attribute '(?P<a>[^']+)' is required but missing"), "нет обязательного атрибута «{a}»"),
    (re.compile(r"The attribute '(?P<a>[^']+)' is not allowed"), "атрибут «{a}» здесь не предусмотрен"),
    (re.compile(r"Missing child element\(s\)\. Expected is one of \( (?P<e>[^)]+) \)"),
     "не хватает вложенного элемента — нужен один из: {e}"),
    (re.compile(r"Missing child element\(s\)\. Expected is \( (?P<e>[^)]+) \)"),
     "не хватает вложенного элемента «{e}»"),
    (re.compile(r"This element is not expected\. Expected is one of \( (?P<e>[^)]+) \)"),
     "элемент здесь не ожидается — на этом месте нужен один из: {e}"),
    (re.compile(r"This element is not expected\. Expected is \( (?P<e>[^)]+) \)"),
     "элемент здесь не ожидается — на этом месте нужен «{e}»"),
    (re.compile(r"This element is not expected"), "элемент здесь не ожидается"),
    (re.compile(r"\[facet 'enumeration'\] The value '(?P<v>[^']*)' is not an element of the set"),
     "недопустимое значение «{v}»"),
    (re.compile(r"\[facet 'pattern'\] The value '(?P<v>[^']*)' is not accepted by the pattern"),
     "значение «{v}» не соответствует формату"),
    (re.compile(r"\[facet 'maxLength'\] The value has a length of '(?P<n>\d+)'; this exceeds the allowed maximum "
                r"length of '(?P<m>\d+)'"), "длина {n} — больше допустимой ({m})"),
    (re.compile(r"\[facet 'minLength'\] The value has a length of '(?P<n>\d+)'; this underruns the allowed minimum "
                r"length of '(?P<m>\d+)'"), "длина {n} — меньше допустимой ({m})"),
    (re.compile(r"\[facet 'length'\] The value '(?P<v>[^']*)' has a length of '(?P<n>\d+)'; this differs from the "
                r"allowed length of '(?P<m>\d+)'"), "значение «{v}»: длина должна быть {m}, а не {n}"),
    (re.compile(r"\[facet 'totalDigits'\] The value '(?P<v>[^']*)'"), "в числе «{v}» слишком много цифр"),
    (re.compile(r"\[facet 'fractionDigits'\] The value '(?P<v>[^']*)'"), "в числе «{v}» слишком много знаков после точки"),
    (re.compile(r"\[facet '(?:minInclusive|maxInclusive|minExclusive|maxExclusive)'\] The value '(?P<v>[^']*)'"),
     "число «{v}» вне допустимых границ"),
    (re.compile(r"'(?P<v>[^']*)' is not a valid value of the atomic type 'xs:(?:decimal|integer)'"), "«{v}» — не число"),
    (re.compile(r"'(?P<v>[^']*)' is not a valid value of the atomic type"), "«{v}» — недопустимое значение"),
    (re.compile(r"No matching global declaration available for the validation root"),
     "корневой элемент не соответствует формату 5.03"),
    (re.compile(r"Character content other than whitespace is not allowed"), "внутри элемента лишний текст"),
]
_ELEMENT = re.compile(r"^Element '(?P<el>[^']+)'(?:, attribute '(?P<at>[^']+)')?: ")


def message_ru(message: str) -> str:
    """Сообщение libxml2 → понятная фраза по-русски (неизвестное — как есть, с пометкой)."""
    text = (message or "").strip()
    where = ""
    head = _ELEMENT.match(text)
    if head:
        where = f"«{head['el']}»" + (f", атрибут «{head['at']}»" if head["at"] else "")
        text = text[head.end():]
    for pattern, template in _RULES:
        found = pattern.search(text)
        if found:
            phrase = template.format(**{k: (v or "") for k, v in found.groupdict().items()})
            return f"{where}: {phrase}" if where else phrase[:1].upper() + phrase[1:]
    return f"{where}: {text}" if where else text


def _path_ru(path: str | None) -> str:
    parts = [p for p in (path or "").split("/") if p]
    return " › ".join(parts)


def validate(data: bytes, format_version: str = "") -> dict:
    """Проверить XML по схеме. Ответ: checked, valid, revision, errors[{path, message}], reason."""
    if format_version and format_version != "5.03":
        return {"checked": False, "valid": None, "revision": "", "errors": [],
                "reason": f"схема проверяется только для формата 5.03, а файл — {format_version}"}
    schemas, reason = _load()
    if not schemas:
        return {"checked": False, "valid": None, "revision": "", "errors": [], "reason": reason}
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        return {"checked": True, "valid": False, "revision": schemas[0][0], "reason": "",
                "errors": [{"path": "", "message": f"XML не читается: {exc.msg}"}]}
    first_errors: list[dict] = []
    for revision, schema in schemas:
        if schema.validate(root):
            return {"checked": True, "valid": True, "revision": revision, "errors": [], "reason": ""}
        if not first_errors:     # замечания самой новой редакции — они актуальнее
            first_errors = [{"path": _path_ru(e.path), "message": message_ru(e.message)}
                            for e in list(schema.error_log)[:MAX_DETAILS]]
    return {"checked": True, "valid": False, "revision": schemas[0][0], "errors": first_errors, "reason": ""}


def issues(result: dict) -> list[dict]:
    """Результат validate → замечания в общем формате проверки (upd.check)."""
    if not result.get("checked"):
        if not result.get("reason") or "только для формата" in result["reason"]:
            return []
        return [{"level": "info", "code": "xsd_skipped", "message": f"Проверка по схеме ФНС не выполнена: {result['reason']}"}]
    if result.get("valid"):
        return [{"level": "info", "code": "xsd_ok",
                 "message": f"Файл соответствует схеме ФНС 5.03 (редакция {result['revision']})"}]
    errors = result.get("errors") or []
    total = len(errors)
    more = "" if total < MAX_DETAILS else " (показаны первые)"
    first = "; ".join(e["message"] for e in errors[:2])
    return [{"level": "warning", "code": "xsd",
             "message": f"Файл не соответствует схеме ФНС 5.03: замечаний {total}{more}. {first}",
             "details": [f"{e['path']}: {e['message']}" if e["path"] else e["message"] for e in errors]}]
