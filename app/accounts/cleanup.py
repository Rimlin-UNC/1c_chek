"""Удаление компании-клиента без «сирот» (T-85, v1.71.2, ADR-50).

До 1.71.2 `POST /api/v1/companies/{id}/delete` переносил только чеки, сотрудников и
приглашения: после удаления в базе оставались строки удалённой компании в шести и более
таблицах — «призрачный» доступ бухгалтера, мусор в счётчиках, брошенные банковские связи.

Здесь собрана **политика** для всех таблиц с `company_id`. Она не перечисляет имена руками:
перечень строится из метаданных SQLAlchemy, поэтому новая таблица с `company_id` попадёт
под очистку сама и не сможет «забыться». Тест `tests/test_company_delete.py` проверяет,
что каждая таблица попала в одну из политик.

Политики:

* ``TABLES_WIPE`` — данные компании: при удалении в любом режиме удаляются. Банковские
  выписки, первичка, запросы «что нужно от клиента», согласования и память товаров — это
  данные компании, а не общий справочник; переносить их к другой компании нельзя.
* ``TABLES_REASSIGN`` — настройки, которые можно отдать компании назначения при
  ``mode="move"`` и которые нельзя задвоить (уникальные ключи зависят от ``company_id``):
  настройки запросов и согласований, банковские настройки, память товаров, правила
  категорий, контрагенты. Контрагенты переносятся с объединением по ИНН — карточка
  не размножается, счета и псевдонимы переезжают в существующую.
* ``TABLES_KEEP`` — история решений: не удаляется никогда. Исходящие вебхуки, журнал
  контура B2B, аудит. Удаление компании не должно стирать, что именно с ней делали.

Отдельно: ``audit_log`` не очищается никогда — это история, ради которой журнал и ведётся.
Строки журнала остаются со ссылкой на удалённую компанию намеренно.

Чеки, единый реестр, пользователи и приглашения обрабатываются существующей логикой
`delete_company` (перенос/очистка, открепление сотрудников) — здесь они не трогаются.
"""
from __future__ import annotations

from sqlalchemy import Table, func, select
from sqlalchemy.orm import Session

from ..approvals.models import AdvanceApproval, AdvanceApprovalEvent, ApprovalSettings
from ..b2b.models import B2bApplication, B2bCheck, B2bEvent, B2bToken
from ..bank.models import (
    BankConnection,
    BankIncome,
    BankLink,
    BankOperation,
    BankReimbursement,
    BankSettings,
    BankStatement,
)
from ..counterparties.models import Counterparty
from ..docreq.models import DocRequest, DocRequestEvent, DocRequestSettings
from ..goods.models import GoodsMemory
from ..models import AdvanceReportCounter, AdvanceReportReservation, MerchantRule, WebhookLog
from ..primary.models import PrimaryDocument, PrimaryMailbox, PrimaryMailItem
from ..usage.models import UsageEvent

# --------------------------------------------------------------------------
#  политики
# --------------------------------------------------------------------------
# Данные компании: удаляются всегда.
TABLES_WIPE: tuple[Table, ...] = (
    BankLink.__table__,                  # ручные привязки «платёж ↔ документ»
    BankReimbursement.__table__,         # возмещения по чекам
    BankIncome.__table__,                # поступления
    BankOperation.__table__,             # списания
    BankStatement.__table__,             # выписки
    BankConnection.__table__,            # подключения банков
    PrimaryDocument.__table__,           # первичка (строки — каскадом)
    PrimaryMailItem.__table__,           # письма с документами
    PrimaryMailbox.__table__,            # почтовые ящики
    DocRequest.__table__,                # «что нужно от клиента»
    DocRequestEvent.__table__,           # их журнал
    AdvanceApproval.__table__,           # авансовые отчёты на согласовании
    AdvanceApprovalEvent.__table__,      # их журнал решений
    B2bToken.__table__,                  # одноразовые ссылки контура
)

# Настройки и справочники: при move — компании назначения, иначе удаляются.
TABLES_REASSIGN: tuple[Table, ...] = (
    DocRequestSettings.__table__,
    ApprovalSettings.__table__,
    BankSettings.__table__,
    GoodsMemory.__table__,
    MerchantRule.__table__,
    AdvanceReportCounter.__table__,
    AdvanceReportReservation.__table__,
    Counterparty.__table__,              # перенос с объединением по ИНН, см. move_reassignables
)

# История: не удаляется и не переносится при удалении компании; срок хранения требует отдельной политики.
TABLES_KEEP: tuple[Table, ...] = (
    WebhookLog.__table__,
    B2bEvent.__table__,
    B2bApplication.__table__,
    B2bCheck.__table__,
    UsageEvent.__table__,                 # не переносить учёт в другую компанию
)

# Журнал действий: остаётся всегда, даже при удалении компании.
TABLES_KEEP_AUDIT: tuple[str, ...] = ("audit_log",)

# Остаётся, но меняет статус: заявки на чужой чек закрываются (status=void), а не удаляются —
# история споров о принадлежности чека важнее чистоты таблицы (tests/test_unified_claims.py).
TABLES_KEEP_CLOSED: tuple[str, ...] = ("unified_company_links",)

# Обрабатывает существующая логика удаления (чеки, реестр, сотрудники, приглашения):
# перенос/очистку чеков и владения делает unified.service.retarget_company, сотрудников
# открепляет delete_company. Здесь — только объявлено, что они не забыты.
TABLES_BY_EXISTING_LOGIC: tuple[str, ...] = (
    "receipts",           # перенос или очистка в delete_company
    "unified_ownership",  # retarget_company
    "unified_company_links",  # links.close_company: заявки не удаляются, а закрываются (status=void)
                              # — история споров о принадлежности чека должна оставаться
    "users",              # открепление
    "invites",            # удаление
    "accountant_clients", # доступ бухгалтеров — снимается здесь же (detach_company)
)



# --------------------------------------------------------------------------
#  проверка и очистка
# --------------------------------------------------------------------------
def company_rows(db: Session, company_id: str) -> dict[str, int]:
    """Сколько строк удалённой компании осталось в каждой таблице с company_id.

    Перечень берётся из метаданных: новая таблица попадёт в отчёт сама.
    ``audit_log`` исключён — история по правилу остаётся.
    """
    from ..database import Base

    out: dict[str, int] = {}
    for table in Base.metadata.tables.values():
        if "company_id" not in table.c or table.name in TABLES_KEEP_AUDIT:
            continue
        out[table.name] = db.execute(
            select(func.count()).select_from(table).where(table.c.company_id == company_id)
        ).scalar() or 0
    return out


def _delete_rows(db: Session, table: Table, company_id: str) -> int:
    res = db.execute(table.delete().where(table.c.company_id == company_id))
    return int(res.rowcount or 0)


def _move_rows(db: Session, table: Table, from_id: str, to_id: str) -> int:
    """Перенести строки настроек, не задваиваясь по уникальным ключам.

    Если у компании назначения уже есть такая строка (тот же уникальный ключ, что и
    у переносимой, только с company_id = to_id), переносимую удаляем: у компании
    назначения её настройка важнее, а задвоение база не примет.
    """
    uniques = []
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            cols = [c.name for c in constraint.columns]
            if "company_id" in cols:
                uniques.append([c for c in cols if c != "company_id"])
    for index in table.indexes:
        if index.unique:
            cols = [c.name for c in index.columns]
            if "company_id" in cols:
                uniques.append([c for c in cols if c != "company_id"])

    # Ключ строки — первичный ключ таблицы: у счётчиков он составной и без id.
    pk_cols = [c.name for c in table.primary_key.columns]

    def row_key(row) -> list:
        return [table.c[name] == row[name] for name in pk_cols]

    moved = 0
    for keys in uniques:
        rows = db.execute(select(table).where(table.c.company_id == from_id)).mappings().all()
        for row in rows:
            cond = [table.c.company_id == to_id] + [table.c[key] == row[key] for key in keys]
            exists = db.execute(select(func.count()).select_from(table).where(*cond)).scalar() or 0
            if exists:
                db.execute(table.delete().where(*row_key(row)))
                moved += 1
    res = db.execute(table.update().where(table.c.company_id == from_id).values(company_id=to_id))
    return moved + int(res.rowcount or 0)


def _merge_counterparties(db: Session, from_id: str, to_id: str) -> int:
    """Перенести контрагентов, объединяя карточки с тем же ИНН.

    Дубликатов не появляется: счета и псевдонимы переезжают в существующую карточку
    компании назначения, затем лишняя карточка удаляется. Без ИНН карточка просто
    меняет компанию — объединять не по чему.
    """
    from ..counterparties.models import CounterpartyAlias, CounterpartyBankAccount

    moved = 0
    for card in list(db.scalars(select(Counterparty).where(Counterparty.company_id == from_id)).all()):
        target = None
        if card.inn:
            target = db.scalars(select(Counterparty).where(
                Counterparty.company_id == to_id, Counterparty.inn == card.inn)).first()
        if target is None:
            card.company_id = to_id
            moved += 1
            continue
        for account in db.scalars(select(CounterpartyBankAccount).where(
                CounterpartyBankAccount.counterparty_id == card.id)).all():
            account.counterparty_id = target.id
        for alias in db.scalars(select(CounterpartyAlias).where(
                CounterpartyAlias.counterparty_id == card.id)).all():
            same = db.scalars(select(CounterpartyAlias).where(
                CounterpartyAlias.counterparty_id == target.id,
                CounterpartyAlias.name == alias.name)).first()
            if same is not None:
                db.delete(alias)
            else:
                alias.counterparty_id = target.id
        db.flush()
        db.expire(card, ["accounts", "aliases"])
        db.delete(card)
        moved += 1
    return moved


def detach_company(db: Session, company_id: str, *, target_company_id: str | None = None) -> dict:
    """Убрать все строки компании из подсистем, кроме чеков и сотрудников.

    ``target_company_id`` задан — режим переноса: настройки и контрагенты доезжают до
    компании назначения. Не задан — очистка: удаляются данные и настройки.

    Возвращает сводку ``{"deleted": {...}, "moved": {...}}`` для журнала и ответа.
    """
    summary: dict[str, dict[str, int]] = {"deleted": {}, "moved": {}}
    # 1. Данные компании — всегда удаляются, и раньше дочерних справочников:
    #    строки первички и привязки ссылаются на банковские операции.
    for table in TABLES_WIPE:
        count = _delete_rows(db, table, company_id)
        if count:
            summary["deleted"][table.name] = count
    # 2. Настройки и справочники — перенос или удаление.
    if target_company_id:
        for table in TABLES_REASSIGN:
            if table.name == Counterparty.__table__.name:
                count = _merge_counterparties(db, company_id, target_company_id)
            else:
                count = _move_rows(db, table, company_id, target_company_id)
            if count:
                summary["moved"][table.name] = count
    else:
        for table in TABLES_REASSIGN:
            count = _delete_rows(db, table, company_id)
            if count:
                summary["deleted"][table.name] = count
    # 3. Доступ бухгалтеров к удалённой компании — всегда.
    from ..clients.models import AccountantClient
    res = db.execute(AccountantClient.__table__.delete().where(
        AccountantClient.__table__.c.company_id == company_id))
    if res.rowcount:
        summary["deleted"]["accountant_clients"] = int(res.rowcount)
    db.flush()
    return summary
