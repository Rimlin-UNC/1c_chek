"""Фаза 4б (T-38): единая точка записи — страж для нового кода.

Фискальные данные чека (ФН, ФД, ФП, дата, сумма, операция, продавец, ИНН, адрес,
кассир) и позиции живут в трёх копиях: реестр, Чек-Пул и единая запись. Правило:
функция, которая меняет их в копии реестра или Чек-Пула (или создаёт копию), в той
же функции передаёт изменение в единую запись — одной из функций единой точки
записи (``WRITE_POINTS``). Тогда изменение доходит до единой записи и остальных
копий в той же транзакции по правилу слияния (``service._merge_core``).

Тест разбирает код ``app/`` (AST) и падает, если появилась функция, которая пишет
фискальные поля мимо единой точки записи. Исключения перечислены явно и с причиной;
устаревшее исключение тоже роняет тест.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.unified import service

ROOT = Path(__file__).resolve().parent.parent
FISCAL = {"fn", "fd", "fp", "receipt_date", "total_sum", "operation", "merchant_name",
          "merchant_inn", "merchant_address", "cashier"}
MODELS = {"Receipt", "PoolReceipt", "ReceiptItem", "PoolItem"}
WRITE_POINTS = {"sync_registry", "sync_registry_safely", "sync_pool", "register_source",
                "_register_registry_many", "move_registry_receipts", "detach_registry"}

# (файл, функция) → почему функция пишет фискальные поля без единой точки записи
ALLOWED = {
    ("app/routers/receipts.py", "remember_merchant"):
        "правило поставщика: rule.merchant_name — это не копия чека",
    ("app/services/qr.py", "parse_qr"):
        "разбор строки QR в ParsedQR — это не копия чека",
    ("app/seed.py", "make_demo_receipts"):
        "демо-данные чистой установки: чеки без единой записи, как архив до регистрации",
    ("app/services/receipt_card.py", "apply_to_receipt"):
        "помощник данных ФНС (вместе с вложенной fill): в единую запись передают вызывающие "
        "(test_fns_helpers_callers_sync)",
}
# помощники данных ФНС: каждый вызывающий обязан передать изменение в единую запись
FNS_HELPERS = {"apply_to_receipt", "enrich_receipt"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _functions():
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("app/unified/"):
            continue                     # сама единая точка записи
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield rel, node


def _writes(fn) -> list[str]:
    literals = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    found = []
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for el in (target.elts if isinstance(target, ast.Tuple) else [target]):
                    if isinstance(el, ast.Attribute) and el.attr in FISCAL:
                        found.append(f"{ast.unparse(el)} =")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "setattr" and len(node.args) > 1:
                attr = node.args[1]
                if isinstance(attr, ast.Constant) and attr.value in FISCAL:
                    found.append(f"setattr(…, {attr.value!r})")
                elif not isinstance(attr, ast.Constant) and literals & FISCAL:
                    found.append("setattr(…, поле из списка фискальных)")
            elif name in MODELS:
                found.append(f"{name}(…)")
            elif (name in ("clear", "append") and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "items"):
                found.append(f"items.{name}()")
    return found


def _calls(fn) -> set[str]:
    return {_call_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def test_write_points_exist():
    for name in WRITE_POINTS:
        assert callable(getattr(service, name)), name


def test_fiscal_writers_go_through_the_write_point():
    offenders, seen = [], set()
    for rel, fn in _functions():
        writes = _writes(fn)
        if not writes:
            continue
        key = (rel, fn.name)
        if key in ALLOWED:
            seen.add(key)
            continue
        if not _calls(fn) & WRITE_POINTS:
            offenders.append(f"{rel}:{fn.lineno} {fn.name}: {sorted(set(writes))[:4]}")
    assert not offenders, (
        "фискальные поля чека меняются мимо единой точки записи (вызовите sync_registry / "
        "register_source в той же функции или опишите исключение в ALLOWED):\n" + "\n".join(offenders))
    stale = sorted(set(ALLOWED) - seen)
    assert not stale, f"исключения больше не нужны — удалите из ALLOWED: {stale}"


def test_fns_helpers_callers_sync():
    """Вызывающие помощников данных ФНС из receipt_card передают изменение в единую запись."""
    callers = []
    for rel, fn in _functions():
        if rel == "app/services/receipt_card.py" and fn.name in FNS_HELPERS:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or _call_name(node) not in FNS_HELPERS:
                continue
            target = node.func.value.id if (isinstance(node.func, ast.Attribute)
                                            and isinstance(node.func.value, ast.Name)) else ""
            # enrich_receipt из app/pool/geo — другая функция (метки пула, не факты)
            if _call_name(node) == "enrich_receipt" and target not in ("_card", "receipt_card"):
                continue
            callers.append((rel, fn.name, bool(_calls(fn) & WRITE_POINTS)))
    assert callers, "не найдены вызовы помощников данных ФНС — проверьте имена"
    missing = [f"{rel} {name}" for rel, name, ok in callers if not ok]
    assert not missing, f"данные ФНС меняют чек без передачи в единую запись: {missing}"
