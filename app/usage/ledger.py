"""Наблюдаемый журнал: только инфраструктура, не биллинг и не лимитер.

Вызывающий код должен проверить права и компанию, передать стабильный ключ
бизнес-события, уникальный в компании и метрике для всех периодов и источников,
и записать его в ТОЙ ЖЕ транзакции, что и подтверждаемый результат. Ошибка
означает откат всей операции. Модуль не делает commit/rollback; историю не восстанавливает.
Сейчас ни один источник проверок не подключён; публичных маршрутов нет.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import uid, utcnow
from .models import UsageEvent

_IDENT = re.compile(r'[A-Za-z0-9_-]{1,36}\Z', re.ASCII)
_METRIC = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z', re.ASCII)
_PERIOD = re.compile(r'[A-Za-z0-9_.:-]{1,80}\Z', re.ASCII)


class EventConflict(ValueError):
    """Повтор использует прежний идентификатор для другого периода."""


def _scope(company_id: str, metric: str, period_key: str) -> None:
    if (not isinstance(company_id, str) or not _IDENT.fullmatch(company_id)
            or not isinstance(metric, str) or not _METRIC.fullmatch(metric)
            or not isinstance(period_key, str) or not _PERIOD.fullmatch(period_key)):
        raise ValueError('Некорректные идентификаторы компании, метрики или периода')


def record(db: Session, company_id: str, metric: str, period_key: str, event_key: str) -> bool:
    """Добавить одно событие или распознать повтор; при конфликте вернуть ошибку.

    Идентификатор источника не сохраняется в открытом виде. SHA-256 не делает
    угадываемые идентификаторы анонимными; этот внутренний журнал не экспортируется.
    Использует только текущую транзакцию вызывающего кода.
    """
    _scope(company_id, metric, period_key)
    if (not isinstance(event_key, str) or not 1 <= len(event_key) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in event_key)):
        raise ValueError('Некорректный ключ события')
    fingerprint = hashlib.sha256(event_key.encode('utf-8')).hexdigest()
    dialect = db.get_bind().dialect.name
    if dialect == 'sqlite':
        insert = sqlite_insert
    elif dialect == 'postgresql':
        insert = pg_insert
    else:
        raise ValueError('СУБД не поддерживает безопасную дедупликацию')
    stmt = (insert(UsageEvent).values(id=uid(), company_id=company_id, metric=metric, period_key=period_key,
                                      event_hash=fingerprint, created_at=utcnow())
            .on_conflict_do_nothing(index_elements=['company_id', 'metric', 'event_hash'])
            .returning(UsageEvent.id))
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        return True
    existing_period = db.scalar(select(UsageEvent.period_key).where(
        UsageEvent.company_id == company_id, UsageEvent.metric == metric, UsageEvent.event_hash == fingerprint))
    if existing_period != period_key:
        raise EventConflict('Повтор события с другим периодом')
    return False


def summary(db: Session, company_id: str, metric: str, period_key: str, *, limit: int | None = None) -> dict:
    """Посчитать события и математический остаток; не включать ограничение доступа.

    Лимит получает только доверенный вызывающий код. Он не сохраняется, не является
    ценой и не утверждает, что период или единица пригодны для тарификации.
    """
    _scope(company_id, metric, period_key)
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 2_000_000_000):
        raise ValueError('Некорректный лимит')
    used = db.scalar(select(func.count()).select_from(UsageEvent).where(
        UsageEvent.company_id == company_id, UsageEvent.metric == metric, UsageEvent.period_key == period_key)) or 0
    return {'used': used, 'limit': limit,
            'remaining': None if limit is None else max(limit - used, 0),
            'over_limit': None if limit is None else max(used - limit, 0),
            'complete': False}
