"""Номера авансовых отчётов: атомарная помесячная нумерация по компании.

v1.70.112 (T-46): ядро вынесено из эндпоинта «Зарезервировать номер авансового отчёта», чтобы печатная
форма АО-1 и согласование авансовых отчётов брали номер из одного счётчика — номер в листе согласования
совпадает с номером печатной формы. Повтор с тем же идентификатором запроса возвращает прежний номер.
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AdvanceReportCounter, AdvanceReportReservation


class NumberError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def number_text(record: AdvanceReportReservation) -> str:
    return f"АО-{record.period}-{record.number:02d}"


def reserve(db: Session, company_id: str, report_date: str, request_id: str,
            start_after: int = 0) -> AdvanceReportReservation:
    """Зарезервировать номер на дату отчёта (ГГГГ-ММ-ДД). Фиксирует транзакцию."""
    existing = db.get(AdvanceReportReservation, request_id)
    if existing:
        return existing
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise NumberError("Нумерация не поддерживает эту базу данных", 500)
    period = report_date[:7]
    try:
        # Первая запись защищает от одновременного повторения одного запроса.
        record = AdvanceReportReservation(request_id=request_id, company_id=company_id,
                                          period=period, report_date=report_date, number=0)
        db.add(record)
        db.flush()
        counter = AdvanceReportCounter.__table__
        stmt = insert(counter).values(company_id=company_id, period=period, last_number=start_after + 1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[counter.c.company_id, counter.c.period],
            set_={"last_number": counter.c.last_number + 1},
        ).returning(counter.c.last_number)
        record.number = db.execute(stmt).scalar_one()
        db.commit()
        return record
    except IntegrityError:
        db.rollback()
        existing = db.get(AdvanceReportReservation, request_id)
        if existing:
            return existing
        raise NumberError("Не удалось зарезервировать номер; повторите запрос") from None
