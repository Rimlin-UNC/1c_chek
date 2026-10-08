# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Pydantic-схемы для валидации запросов и ответов API.
"""
from __future__ import annotations

from datetime import datetime
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
    company_id: Optional[str] = Field(default=None, max_length=36,
                                      description="v1.11.0: компания сотрудника")


class UserPatch(BaseModel):
    full_name: Optional[str] = None
    organization: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=200)
    role: Optional[Literal["accountant", "user"]] = None
    is_active: Optional[bool] = None
    company_id: Optional[str] = Field(default=None, max_length=36,
                                      description="v1.11.0: перевод в другую компанию")


# --- Чеки ------------------------------------------------------------------
class ManualReceipt(BaseModel):
    """Ручной ввод реквизитов чека (резервный режим)."""
    date_time: str = Field(min_length=8, max_length=25, description="ДД.ММ.ГГГГ ЧЧ:ММ или ISO")
    total_sum: float = Field(gt=0, description="Сумма чека, ₽")
    fn: str = Field(min_length=8, max_length=20, description="ФН")
    fd: str = Field(min_length=1, max_length=20, description="ФД")
    company_id: Optional[str] = Field(default=None, max_length=36,
                                      description="v1.11.0: компания чека (админ)")
    fp: str = Field(min_length=4, max_length=20, description="ФП/ФПД")
    operation: int = Field(default=1, ge=1, le=2, description="1 приход, 2 возврат")


class ScanRequest(BaseModel):
    """Данные сканирования QR (строка целиком)."""
    qr_data: str = Field(min_length=10, max_length=4000)
    source: str = Field(default="web", max_length=20)
    verify: bool = Field(default=True, description="Запустить проверку в ФНС")
    company_id: Optional[str] = Field(default=None, max_length=36,
                                      description="v1.11.0: компания чека (админ)")


class VerifyRequest(BaseModel):
    receipt_ids: list[str] = Field(default_factory=list, max_length=500)


class AdvanceReportBody(BaseModel):
    """v1.14.0: сборка авансового отчёта из чеков за период."""
    company_id: Optional[str] = Field(default=None, max_length=36,
                                      description="Компания (только админ)")
    date_from: str = Field(min_length=10, max_length=10, description="ГГГГ-ММ-ДД")
    date_to: str = Field(min_length=10, max_length=10, description="ГГГГ-ММ-ДД")
    assignee: Optional[str] = Field(default=None, max_length=200,
                                    description="Один сотрудник (пусто — все)")
    receipt_ids: list[str] = Field(default_factory=list, max_length=1000,
                                   description="Только выбранные чеки")
    only_valid: bool = Field(default=False,
                             description="Только чеки, подтверждённые ФНС")


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
    company_id: Optional[str] = Field(default=None, max_length=36,
                                      description="v1.11.0: компания приглашённого")
    company_name: Optional[str] = Field(default=None, max_length=200,
                                        description="v1.12.0: название компании — "
                                                    "найдётся или будет создана")


class AssignBulk(BaseModel):
    receipt_ids: list[str] = Field(default_factory=list, max_length=1000)
    assignee: str = Field(min_length=1, max_length=200)


class ReceiptPatch(BaseModel):
    """v1.2.0: расширенное редактирование чека.
    Права: сотрудник — только notified/comment (свой чек);
    бухгалтер/админ — все поля + позиции (items)."""
    assignee: Optional[str] = Field(default=None, max_length=200)
    comment: Optional[str] = Field(default=None, max_length=2000)
    notified: Optional[bool] = None
    category: Optional[str] = Field(default=None, max_length=100)
    # --- поля для бухгалтера/админа ---
    fn: Optional[str] = Field(default=None, max_length=20)
    fd: Optional[str] = Field(default=None, max_length=20)
    fp: Optional[str] = Field(default=None, max_length=20)
    receipt_date: Optional[datetime] = None
    total_sum: Optional[float] = Field(default=None, ge=0)
    personal_sum: Optional[float] = Field(default=None, ge=0)   # v1.8.0: личные
    operation: Optional[int] = None          # 1 приход / 2 возврат
    merchant_name: Optional[str] = Field(default=None, max_length=500)
    merchant_inn: Optional[str] = Field(default=None, max_length=20)
    merchant_address: Optional[str] = Field(default=None, max_length=500)
    cashier: Optional[str] = Field(default=None, max_length=200)
    items: Optional[list["ReceiptItemPatch"]] = None


class ReceiptItemPatch(BaseModel):
    name: str = Field(min_length=1, max_length=1000)
    quantity: float = Field(default=1.0, gt=0)
    price: float = Field(default=0.0, ge=0)
    total: float = Field(default=0.0, ge=0)
    vat_rate: Optional[str] = Field(default="none", max_length=10)
    vat_sum: Optional[float] = Field(default=None, ge=0)


class FetchDetailsRequest(BaseModel):
    """Массовое получение данных чеков из внешних источников (v1.2.0)."""
    receipt_ids: list[str] = Field(min_length=1, max_length=200)


class ExternalSettingsPatch(BaseModel):
    """Настройки источников данных о чеке (только администратор)."""
    fns_master_token: Optional[str] = Field(default=None, max_length=500)
    proverkacheka_token: Optional[str] = Field(default=None, max_length=500)
    ofd_ru_token: Optional[str] = Field(default=None, max_length=500)  # tokenSecret ofd.ru
    fns_app_inn: Optional[str] = Field(default=None, max_length=20)      # v1.27.0: ИНН ЛК ФНС
    fns_app_password: Optional[str] = Field(default=None, max_length=200)  # v1.27.0: пароль ЛК
    fns_app_secret: Optional[str] = Field(default=None, max_length=200)  # v1.27.0: clientSecret (своя)
    external_order: Optional[str] = Field(default=None, max_length=100)
    external_auto: Optional[bool] = None


class ExternalTestRequest(BaseModel):
    provider: str = "chain"  # chain (весь порядок) | fns_api | fns_app | crpt | ofd_ru | proverkacheka
    qrraw: Optional[str] = None


class UpdateApplyBody(BaseModel):
    """v1.10.0: пароль sudo сервера — ТОЛЬКО на время обновления.
    Не сохраняется и не логируется; используется исключительно для
    `sudo -S systemctl restart`, если sudoers-правило ещё не установлено."""
    sudo_password: Optional[str] = Field(default=None, max_length=256)


class AppSettingsPatch(BaseModel):
    auto_verify: Optional[bool] = None
    # v1.8.0: срок сдачи авансового отчёта от даты чека (дней; приказ руководителя,
    # п. 6.3 Указания ЦБ 3210-У — не более 3 рабочих дней после израсходования)
    advance_deadline_days: Optional[int] = Field(default=None, ge=1, le=365)


# --- v1.11.0: мультикомпанийность -------------------------------------------
class CompanyCreate(BaseModel):
    """Новая компания-клиент (ООО, ИП) — пространство аутсорсинга."""
    name: str = Field(min_length=2, max_length=200)
    inn: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=500)


class CompanyPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    inn: Optional[str] = Field(default=None, max_length=20)
    note: Optional[str] = Field(default=None, max_length=500)
    is_active: Optional[bool] = None


class ReceiptMoveBody(BaseModel):
    """Перемещение чеков между компаниями (только администратор платформы)."""
    receipt_ids: list[str] = Field(max_length=1000)
    company_id: str = Field(max_length=36)
    assignee: Optional[str] = Field(default=None, max_length=200,
                                    description="Переназначить подотчётное лицо")


class CompanyDeleteBody(BaseModel):
    """v1.13.0: удаление компании с обработкой её данных.

    mode="move" — чеки (и при желании сотрудники) переезжают в target_company_id;
    mode="wipe" — чеки компании удаляются безвозвратно, сотрудники открепляются."""
    mode: Literal["move", "wipe"]
    target_company_id: Optional[str] = Field(default=None, max_length=36)
    move_users: bool = True


class CheckoLookup(BaseModel):
    """v1.13.0: предпросмотр карточки по ИНН через Checko (без сохранения)."""
    inn: str = Field(min_length=10, max_length=12)


class CheckoKeyBody(BaseModel):
    """v1.13.0: сохранение API-ключа Checko (администратор)."""
    api_key: str = Field(min_length=4, max_length=200)
