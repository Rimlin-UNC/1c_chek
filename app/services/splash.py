"""Заставка загрузки (v1.70.100): длительность задаёт администратор — 0…60 секунд, по умолчанию 5.

Значение отдаёт публичный /api/v1/about (заставка идёт до входа), меняет — только администратор
(PUT /api/v1/settings/splash). 0 — без заставки. Если программа загружается дольше заданного времени,
заставка ждёт её (прогресс держится на 90–94 %).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import appsettings

KEY = "splash_seconds"
DEFAULT = 5
MIN_SECONDS = 0
MAX_SECONDS = 60


def seconds(db: Session) -> int:
    """Длительность заставки, секунд; неверное значение в базе — по умолчанию."""
    raw = appsettings.get_setting(db, KEY, str(DEFAULT))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT
    return max(MIN_SECONDS, min(MAX_SECONDS, value))


def save(db: Session, value: int) -> int:
    if not MIN_SECONDS <= int(value) <= MAX_SECONDS:
        raise ValueError(f"Длительность заставки — от {MIN_SECONDS} до {MAX_SECONDS} секунд")
    appsettings.set_setting(db, KEY, str(int(value)))
    return int(value)


def mode(value: int) -> str:
    """Как выглядит заставка при заданном времени (для журнала и подсказки в настройках)."""
    if value <= 0:
        return "без заставки"
    if value <= 2:
        return "короткая — прогресс без списка этапов"
    if value >= 8:
        return "длинная — этапы, прогресс и подсказки о возможностях программы"
    return "обычная — этапы загрузки и прогресс"
