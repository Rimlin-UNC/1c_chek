"""Консервативное сопоставление общих реквизитов позиций на SQLite-копии.

Совпадение общих полей не доказывает совпадение полного ответа ФНС: поля,
отсутствующие в пуле, остаются только в исходной записи реестра.
"""
from collections import Counter
import math


def common_positions_match(registry_rows, pool_rows) -> bool:
    """Сравнить мультисеты без округления и без раскрытия содержимого."""
    def valid(row):
        if len(row) != 6:
            return False
        name, quantity, price, total, vat_rate, vat_sum = row
        if not isinstance(name, str) or not name.strip() or not isinstance(vat_rate, str) or not vat_rate.strip():
            return False
        return all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
                   for n in (quantity, price, total, vat_sum))
    registry_rows, pool_rows = [tuple(r) for r in registry_rows], [tuple(r) for r in pool_rows]
    return (bool(registry_rows) and bool(pool_rows)
            and all(valid(r) for r in registry_rows + pool_rows)
            and Counter(registry_rows) == Counter(pool_rows))


def sellers_match(registry_row, pool_row) -> bool:
    """Не выбираем источник, если обязательные реквизиты продавца неизвестны."""
    return (bool(registry_row) and bool(pool_row) and len(registry_row) == len(pool_row) == 3
            and all(isinstance(value, str) and value.strip() for value in (*registry_row, *pool_row))
            and tuple(registry_row) == tuple(pool_row))
