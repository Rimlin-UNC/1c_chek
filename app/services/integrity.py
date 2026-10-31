# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сервис «понимающего» конвейера правил (v1.61.0, Фаза 1).
# Чистые проверки над УЖЕ полученными позициями чека (доп. запросов к
# источникам не порождает): суммы, цены×количество, дубли позиций.
# Нарушение → рекомендация уводить чек на ручную проверку с понятной
# человеку причиной. Работает в обоих конвейерах: Чек-Пул (приём) и
# ядро («📥 Данные сервисов»). Правила получают «товары» (одна позиция
# не совпала) и «системные» раскладки отдельно — при нескольких
# нарушениях приоритет у более общей причины.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

from dataclasses import dataclass, field

# Допуск расхождения: копейки, скидки на чеке, округления источников.
# Больше рубля — уже не «копейки», проверяем.
TOLERANCE_RUB = 0.99

# Порог дублей: позиция встречается больше N раз подряд по имени.
DUPLICATE_LIMIT = 2


@dataclass
class Verdict:
    """Итог проверок: ок / список проблем по убыванию важности."""
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    rule: str = ""

    @property
    def message(self) -> str:
        return "; ".join(self.problems) if self.problems else ""


def _r2(x) -> float:
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def check_items(items, total_sum) -> Verdict:
    """Правила v1.61.0. items — позиции ExternalResult (name, quantity,
    price, total); total_sum — итог чека. Товарная позиция без цены
    (quantity/price = 0 при заполненном total) не считается нарушением —
    некоторые источники отдают расширенную позицию без цены."""
    v = Verdict()
    its = list(items or [])
    if not its:
        return v                                  # пусто — проверять нечего

    items_sum = _r2(sum(_r2(getattr(it, "total", 0)) for it in its))
    total = _r2(total_sum)

    # 1) ГЛАВНОЕ: сумма позиций против итога чека
    diff = round(abs(items_sum - total), 2)
    if total > 0 and diff > TOLERANCE_RUB:
        v.problems.append(
            f"Итог чека {total:.2f} ₽ не сходится с суммой позиций "
            f"{items_sum:.2f} ₽ (расхождение {diff:.2f} ₽)")
        v.rule = "items_sum_mismatch"

    # 2) цена × количество против суммы позиции
    for it in its:
        qty = _r2(getattr(it, "quantity", 0))
        price = _r2(getattr(it, "price", 0))
        tot = _r2(getattr(it, "total", 0))
        pdiff = round(abs(price * qty - tot), 2)
        if price > 0 and qty > 0 and pdiff > TOLERANCE_RUB:
            v.problems.append(
                f"Позиция «{(getattr(it, 'name', '') or '—')[:60]}»: "
                f"{price:.2f} ₽ × {qty:g} = {price * qty:.2f} ₽ ≠ {tot:.2f} ₽")
            if v.rule != "items_sum_mismatch":
                v.rule = "position_math"

    # 3) дубли позиций: одинаковый товар дважды подряд (вручную дважды
    # пробивают редко — чаще ошибка кассы или источника)
    names = [str(getattr(it, "name", "") or "").strip().lower() for it in its]
    for i in range(len(names) - 1):
        if names[i] and names[i] == names[i + 1]:
            v.problems.append(
                f"Похоже на дубли позиций: «{names[i][:60]}» идёт два раза подряд")
            v.rule = v.rule or "duplicates"
            break

    v.ok = not v.problems
    return v


# Антифрод-заготовки следующих версий (v1.62+): реестр правил
# пополняется добавляюще — порядок = приоритет сообщений.
_RULES = ("items_sum_mismatch", "position_math", "duplicates")


def reset_rules() -> None:
    """Сброс реестра (для тестов; правила статичны, состояния нет)."""
    global _RULES
    _RULES = ("items_sum_mismatch", "position_math", "duplicates")
