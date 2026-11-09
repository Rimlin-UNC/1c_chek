"""Договор и тариф клиента (T-88): суммы без двоичного округления, права и сроки."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from ..models import Company, User
from .service import ClientsError, allowed_company_ids


class ContractIn(BaseModel):
    number: str = Field(min_length=1, max_length=200)
    date: date
    service_since: date
    service_until: date | None = None
    fee: str = Field(max_length=30)
    frequency: Literal['month', 'quarter', 'year', 'once']
    currency: str = Field(min_length=3, max_length=3)
    manager: str = Field(default='', max_length=200)

    @field_validator('number')
    @classmethod
    def valid_number(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('Укажите номер договора')
        return value

    @field_validator('fee')
    @classmethod
    def valid_fee(cls, value: str) -> str:
        if not re.fullmatch(r'(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?', value):
            raise ValueError('Укажите неотрицательную сумму с точностью до копеек')
        return format(Decimal(value), '.2f')

    @field_validator('currency')
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.upper()
        if not re.fullmatch(r'[A-Z]{3}', value):
            raise ValueError('Укажите трёхбуквенный код валюты')
        return value

    @model_validator(mode='after')
    def valid_interval(self):
        if self.service_until and self.service_until < self.service_since:
            raise ValueError('Окончание обслуживания раньше начала')
        return self


def company_for(db: Session, user: User, company_id: str) -> Company:
    allowed = allowed_company_ids(db, user)
    if allowed is not None and company_id not in allowed:
        raise ClientsError('Компания не назначена этому бухгалтеру', 403)
    company = (db.query(Company).filter(Company.id == company_id).with_for_update().first())
    if company is None:
        raise ClientsError('Компания не найдена', 404)
    if not company.is_active:
        raise ClientsError('Компания отключена', 403)
    return company


def reminder(until: date | None, today: date) -> dict | None:
    if until is None:
        return None
    days = (until - today).days
    if days > 30:
        return None
    return {'days': days, 'level': 'expired' if days < 0 else ('urgent' if days <= 7 else 'soon')}


def as_dict(company: Company) -> dict | None:
    if company.contract_no is None:
        return None
    return {'number': company.contract_no, 'date': company.contract_date, 'service_since': company.service_since,
            'service_until': company.service_until, 'fee': company.service_fee, 'frequency': company.service_frequency,
            'currency': company.service_currency, 'manager': company.service_manager or ''}


def save(company: Company, body: ContractIn) -> None:
    company.contract_no = body.number
    company.contract_date = body.date.isoformat()
    company.service_since = body.service_since.isoformat()
    company.service_until = body.service_until.isoformat() if body.service_until else None
    company.service_fee = body.fee
    company.service_frequency = body.frequency
    company.service_currency = body.currency
    company.service_manager = body.manager.strip() or None
