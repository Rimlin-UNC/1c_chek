# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Pydantic-схемы для валидации запросов и ответов API.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- Аутентификация ------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=4, max_length=200)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=6, max_length=200)
    full_name: str = ""
    organization: str = ""
    role: Literal["accountant", "user"] = "user"


class UserPatch(BaseModel):
    full_name: Optional[str] = None
    organization: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=200)
    role: Optional[Literal["accountant", "user"]] = None
    is_active: Optional[bool] = None


# --- Чеки ------------------------------------------------------------------
class ManualReceipt(BaseModel):
    """Ручной ввод реквизитов чека (резервный режим)."""
    date_time: str = Field(min_length=8, max_length=25, description="ДД.ММ.ГГГГ ЧЧ:ММ или ISO")
    total_sum: float = Field(gt=0, description="Сумма чека, ₽")
    fn: str = Field(min_length=8, max_length=20, description="ФН")
    fd: str = Field(min_length=1, max_length=20, description="ФД")
    fp: str = Field(min_length=4, max_length=20, description="ФП/ФПД")
    operation: int = Field(default=1, ge=1, le=2, description="1 приход, 2 возврат")


class ScanRequest(BaseModel):
    """Данные сканирования QR (строка целиком)."""
    qr_data: str = Field(min_length=10, max_length=4000)
    source: str = Field(default="web", max_length=20)
    verify: bool = Field(default=True, description="Запустить проверку в ФНС")


class VerifyRequest(BaseModel):
    receipt_ids: list[str] = Field(default_factory=list, max_length=500)


class ExportRequest(BaseModel):
    receipt_ids: list[str] = Field(default_factory=list, max_length=1000)
    format: Literal["json", "xml"] = "json"
    target_object: str = Field(default="ПоступлениеТоваровУслуг", max_length=100)


# --- Маппинг ---------------------------------------------------------------
class MappingItem(BaseModel):
    id: Optional[str] = None
    source_field: str = Field(max_length=100)
    target_object: str = Field(max_length=100)
    target_field: str = Field(max_length=100)
    transform: str = Field(default="direct", max_length=50)
    transform_param: str = Field(default="", max_length=200)
    is_active: bool = True
    position: int = 0


class MappingSave(BaseModel):
    items: list[MappingItem] = Field(default_factory=list, max_length=200)


# --- Настройки ---------------------------------------------------------------
class FnsSettingsPatch(BaseModel):
    provider: Literal["mock", "fns"]
    api_base: Optional[str] = None
    master_token: Optional[str] = None
    client_app_id: Optional[str] = None


class OnecSettingsPatch(BaseModel):
    api_token: Optional[str] = None
    regen_token: bool = False


class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=200)

class RegisterRequest(BaseModel):
    """Регистрация по приглашению: роль приходит из приглашения."""
    token: str = Field(min_length=8, max_length=64)
    username: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=6, max_length=200)
    full_name: str = Field(default="", max_length=200)


class InviteCreate(BaseModel):
    role: Literal["accountant", "user"] = "user"
    max_uses: int = Field(default=1, ge=1, le=200)
    expires_hours: int = Field(default=72, ge=1, le=8760)
    note: str = Field(default="", max_length=200)


class AssignBulk(BaseModel):
    receipt_ids: list[str] = Field(default_factory=list, max_length=1000)
    assignee: str = Field(min_length=1, max_length=200)


class ReceiptPatch(BaseModel):
    assignee: Optional[str] = Field(default=None, max_length=200)
    comment: Optional[str] = Field(default=None, max_length=2000)


class AppSettingsPatch(BaseModel):
    auto_verify: Optional[bool] = None
