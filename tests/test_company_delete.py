"""T-85 (v1.71.2): удаление компании-клиента не оставляет «сирот» (К-2, ADR-50).

Что проверяется (критерии приёмки):
1. после `mode="wipe"` ни в одной таблице с `company_id` нет строк удалённой компании;
   перечень таблиц берётся из метаданных — новая таблица попадёт под страж автоматически;
2. после `mode="move"` — то же самое: строк с company_id удалённой компании не остаётся,
   а переносимые данные (контрагенты, память товаров, настройки) доезжают до компании назначения;
3. доступ бухгалтеров к удалённой компании не остаётся ни в каком виде;
4. при `move` контрагент с тем же ИНН у компании назначения не дублируется — счета и
   псевдонимы переезжают в существующую карточку (объединение без потери истории);
5. при `wipe` контрагенты, банковские связи, первичка, согласования и память товаров
   удаляются вместе с компанией, а не висят мусором;
6. журнал действий (`audit_log`) и замок `audit_log` не удаляются — история решений остаётся.

Данные синтетические (ADR-14). Набор общий для SQLite и PostgreSQL.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select

import tests.test_primary_upd as _upd
from app.accounts.cleanup import TABLES_BY_EXISTING_LOGIC, TABLES_KEEP_AUDIT, company_rows
from app.database import Base, SessionLocal
from app.models import AuditLog, Company, User
from tests.test_v1140 import _mk_user

firms = _upd.firms


# --------------------------------------------------------------------------
#  вспомогательное: наполнить компанию данными во всех подсистемах
# --------------------------------------------------------------------------
def _fill_company(company_id: str) -> dict[str, str]:
    """Создать по строке в каждой подсистеме, где есть company_id.

    Возвращает {имя таблицы: id строки} — по нему потом проверяем, что данные
    действительно были, а не что проверка «пустая».
    """
    from app.approvals.models import AdvanceApproval, ApprovalSettings
    from app.b2b.models import B2bApplication, B2bCheck, B2bEvent
    from app.bank.models import (
        BankConnection,
        BankIncome,
        BankLink,
        BankOperation,
        BankSettings,
        BankStatement,
    )
    from app.counterparties.models import Counterparty
    from app.docreq.models import DocRequest, DocRequestEvent, DocRequestSettings
    from app.goods.models import GoodsMemory
    from app.models import Invite, MerchantRule, WebhookLog
    from app.primary.models import PrimaryDocument, PrimaryMailbox, PrimaryMailItem

    made: dict[str, str] = {}
    tag = uuid4().hex[:8]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with SessionLocal() as db:
        def add(name: str, row):
            db.add(row)
            db.flush()
            made[name] = getattr(row, "id", getattr(row, "company_id", ""))
            return row

        cp = add("counterparties", Counterparty(
            company_id=company_id, inn=f"77{uuid4().int % 10**8:08d}", name_full=f"ООО «Контрагент {tag}»"))
        from app.counterparties.models import CounterpartyAlias, CounterpartyBankAccount
        add("counterparty_bank_accounts", CounterpartyBankAccount(
            counterparty_id=cp.id, account=f"4070281090{uuid4().int % 10**10:010d}", bank_name="Банк", bik="044525225"))
        add("counterparty_aliases", CounterpartyAlias(counterparty_id=cp.id,
                                                      name=f"ООО «К {tag}»", source="manual"))
        add("goods_memory", GoodsMemory(company_id=company_id, name_key=f"товар {tag}", category="food",
                                               sample_name=f"Товар {tag}"))
        add("doc_request_settings", DocRequestSettings(company_id=company_id))
        dr = add("doc_requests", DocRequest(company_id=company_id, period="2026-10", kind="upd",
                                            title=f"УПД за {tag}", status="open", created_by=""))
        add("doc_request_events", DocRequestEvent(company_id=company_id, period="2026-10", request_id=dr.id,
                                                  kind="created", actor_id=""))
        add("bank_settings", BankSettings(company_id=company_id))
        add("bank_connections", BankConnection(company_id=company_id, bank="tbank", status="ok"))
        st = add("bank_statements", BankStatement(company_id=company_id, file_sha256=f"sha{tag}",
                                                  account_tail="0001"))
        op = add("bank_operations", BankOperation(company_id=company_id, statement_id=st.id,
                                                  op_hash=f"op{tag}", op_date=date(2026, 10, 1),
                                                  amount=100.0))
        add("bank_incomes", BankIncome(company_id=company_id, statement_id=st.id, op_hash=f"in{tag}",
                                       op_date=date(2026, 10, 1), amount=200.0))
        doc = add("primary_documents", PrimaryDocument(company_id=company_id, file_name=f"u{tag}.xml",
                                                       kind="upd", status="ok"))
        add("bank_links", BankLink(company_id=company_id, side="expense", operation_id=op.id,
                                   document_id=doc.id))
        add("primary_mailboxes", PrimaryMailbox(company_id=company_id, host="imap.example.ru",
                                                username=f"{tag}@example.ru"))
        add("primary_mail_items", PrimaryMailItem(company_id=company_id,
                                                  mailbox_id=made["primary_mailboxes"],
                                                  message_uid=f"uid{tag}", subject=f"УПД {tag}"))
        add("advance_approvals", AdvanceApproval(company_id=company_id, number="1", employee_id="",
                                                 date_from=date(2026, 10, 1), date_to=date(2026, 10, 31),
                                                 status="draft", created_by=""))
        add("approval_settings", ApprovalSettings(company_id=company_id))
        add("merchant_rules", MerchantRule(company_id=company_id,
                                           merchant_inn=f"77{uuid4().int % 10**8:08d}",
                                           field="category", value="Прочее"))
        add("webhook_logs", WebhookLog(company_id=company_id, url="https://example.com/hook",
                                       payload="{}", created_at=now))
        add("invites", Invite(company_id=company_id, role="user", token=f"tok-{tag}"))
        add("b2b_applications", B2bApplication(company_id=company_id, tariff="start", task="проверка"))
        add("b2b_checks", B2bCheck(company_id=company_id, kind="inn", ok=True))
        add("b2b_events", B2bEvent(company_id=company_id, action="check", details="{}"))
        # unified_company_links требует настоящей записи единого реестра — её
        # создаёт существующий retarget_company, отдельная строка здесь ничего не проверяет.
        from app.accounts.cleanup import TABLES_KEEP
        for table in TABLES_KEEP:
            if table.name != "webhook_logs":
                check = db.execute(select(func.count()).select_from(table).where(
                    table.c.company_id == company_id)).scalar() or 0
                assert check >= 1, f"{table.name}: данные для проверки не созданы"
        db.commit()
    return made


def _rows_with_company(company_id: str) -> dict[str, int]:
    """Сколько строк удалённой компании осталось в каждой таблице с company_id.

    История (TABLES_KEEP: вебхуки, журнал контура B2B) остаётся намеренно — ADR-50.
    Здесь она не считается «сиротами»: это след того, что с компанией работали.
    """
    from app.accounts.cleanup import TABLES_KEEP

    # История остаётся намеренно: вебхуки и журнал B2B (TABLES_KEEP), заявки на чужой
    # чек — закрываются статусом void, а не удаляются (история споров, unified_claims).
    keep = {t.name for t in TABLES_KEEP} | {"unified_company_links"}
    out: dict[str, int] = {}
    with SessionLocal() as db:
        for name, count in company_rows(db, company_id).items():
            if count and name not in keep:
                out[name] = count
    return out


def _delete(client, admin, company_id: str, **body) -> dict:
    res = client.post(f"/api/v1/companies/{company_id}/delete", headers=admin, json=body)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------
#  1. Перечень таблиц — из метаданных, а не руками
# --------------------------------------------------------------------------
def test_guard_covers_every_table_with_company_id():
    """Страж обязан видеть каждую таблицу с company_id: новая таблица попадёт сама."""
    from app.accounts.cleanup import TABLES_KEEP, TABLES_REASSIGN, TABLES_WIPE

    declared = {t.name for t in Base.metadata.tables.values() if "company_id" in t.c}
    covered = ({t.name for t in TABLES_WIPE} | {t.name for t in TABLES_REASSIGN}
               | {t.name for t in TABLES_KEEP} | set(TABLES_KEEP_AUDIT)
               | set(TABLES_BY_EXISTING_LOGIC))
    missing = declared - covered
    assert not missing, f"таблицы с company_id вне политики удаления: {sorted(missing)}"
    both = ({t.name for t in TABLES_WIPE} & {t.name for t in TABLES_REASSIGN})
    assert not both, f"таблица одновременно в двух политиках: {sorted(both)}"
    existing = set(TABLES_BY_EXISTING_LOGIC)
    assert {"receipts", "users", "invites"} <= existing, "логика удаления чеков и сотрудников не забыта"
    assert not ({t.name for t in TABLES_WIPE} & existing), "двойная обработка таблицы"


# --------------------------------------------------------------------------
#  2. wipe: ничего не остаётся
# --------------------------------------------------------------------------
def test_wipe_leaves_no_rows_with_company_id(client, firms):
    admin, a, b = firms
    made = _fill_company(b["id"])
    payload = _delete(client, admin, b["id"], mode="wipe", move_users=True)
    assert payload["ok"] is True and payload["mode"] == "wipe"
    leftovers = _rows_with_company(b["id"])
    assert leftovers == {}, f"после удаления остались строки: {leftovers}"
    with SessionLocal() as db:
        assert db.get(Company, b["id"]) is None
        # данные действительно были — проверка не пустая
        assert db.execute(select(func.count()).select_from(AuditLog)).scalar() >= 1
    assert made, "наполнение компании не сработало"


# --------------------------------------------------------------------------
#  3. move: ничего не остаётся, переносимое доезжает
# --------------------------------------------------------------------------
def test_move_leaves_no_rows_and_carries_settings(client, firms):
    admin, a, b = firms
    _fill_company(b["id"])
    payload = _delete(client, admin, b["id"], mode="move", target_company_id=a["id"], move_users=True)
    assert payload["ok"] is True and payload["mode"] == "move"
    leftovers = _rows_with_company(b["id"])
    assert leftovers == {}, f"после переноса остались строки: {leftovers}"

    from app.approvals.models import ApprovalSettings
    from app.counterparties.models import Counterparty
    from app.docreq.models import DocRequestSettings
    from app.goods.models import GoodsMemory
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(
            Counterparty).where(Counterparty.company_id == a["id"])).scalar() >= 1
        assert db.execute(select(func.count()).select_from(
            GoodsMemory).where(GoodsMemory.company_id == a["id"])).scalar() >= 1
        assert db.execute(select(func.count()).select_from(
            DocRequestSettings).where(DocRequestSettings.company_id == a["id"])).scalar() == 1
        assert db.execute(select(func.count()).select_from(
            ApprovalSettings).where(ApprovalSettings.company_id == a["id"])).scalar() == 1


# --------------------------------------------------------------------------
#  4. доступ бухгалтеров не остаётся
# --------------------------------------------------------------------------
def test_accountant_access_is_gone(client, firms):
    admin, a, b = firms
    _fill_company(b["id"])
    acc = _mk_user(client, admin, b["id"], f"acc_del_{uuid4().hex[:6]}", role="accountant")
    admin_id = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    with SessionLocal() as db:
        from app.clients.models import AccountantClient
        acc_id = db.query(User).filter(User.username.like("acc_del_%")).first().id
        db.add(AccountantClient(user_id=acc_id, company_id=b["id"], granted_by=admin_id))
        db.commit()
    _delete(client, admin, b["id"], mode="move", target_company_id=a["id"], move_users=False)
    with SessionLocal() as db:
        from app.clients.models import AccountantClient
        assert db.query(AccountantClient).filter(AccountantClient.company_id == b["id"]).count() == 0
    # сотрудники не переезжали (move_users=False) — они откреплены, но аккаунты живы
    me = client.get("/api/v1/auth/me", headers=acc)
    assert me.status_code == 200, "аккаунт сотрудника пропал вместе с компанией"
    assert me.json()["company_id"] is None, "сотрудник остался в удалённой компании"
    with SessionLocal() as db:
        assert db.query(User).filter(User.company_id == b["id"]).count() == 0


# --------------------------------------------------------------------------
#  5. move: контрагент с тем же ИНН не дублируется
# --------------------------------------------------------------------------
def test_move_merges_counterparty_with_same_inn(client, firms):
    admin, a, b = firms
    inn = f"77{uuid4().int % 10**8:08d}"
    from app.counterparties.models import Counterparty, CounterpartyAlias, CounterpartyBankAccount
    with SessionLocal() as db:
        keep = Counterparty(company_id=a["id"], inn=inn, name_full="ООО «Общий»")
        move = Counterparty(company_id=b["id"], inn=inn, name_full="ООО «Общий (дубль)»")
        db.add_all([keep, move])
        db.flush()
        db.add(CounterpartyBankAccount(counterparty_id=move.id, account=f"4070281090{uuid4().int % 10**10:010d}",
                                       bank_name="Банк", bik="044525225"))
        db.add(CounterpartyAlias(counterparty_id=move.id, name="ООО «Общий»", source="upd"))
        db.commit()
        keep_id, move_id = keep.id, move.id

    _delete(client, admin, b["id"], mode="move", target_company_id=a["id"], move_users=True)

    with SessionLocal() as db:
        assert db.get(Counterparty, move_id) is None
        kept = db.execute(select(Counterparty).where(
            Counterparty.company_id == a["id"], Counterparty.inn == inn)).scalars().all()
        assert len(kept) == 1, "контрагент размножился при переносе"
        assert kept[0].id == keep_id
        accounts = db.execute(select(CounterpartyBankAccount).where(
            CounterpartyBankAccount.counterparty_id == keep_id)).scalars().all()
        assert len(accounts) == 1, "счёт перенесённого контрагента потерялся"
        aliases = db.execute(select(CounterpartyAlias).where(
            CounterpartyAlias.counterparty_id == keep_id)).scalars().all()
        assert aliases, "псевдоним перенесённого контрагента потерялся"


# --------------------------------------------------------------------------
#  6. история решений остаётся
# --------------------------------------------------------------------------
def test_audit_history_is_kept(client, firms):
    admin, a, b = firms
    _fill_company(b["id"])
    before = None
    with SessionLocal() as db:
        db.add(AuditLog(user_id=None, username="admin", company_id=b["id"], action="test_history",
                        entity_type="company", entity_id=b["id"], details="{}"))
        db.commit()
        before = db.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.company_id == b["id"])).scalar()
    assert before and before >= 1
    _delete(client, admin, b["id"], mode="wipe", move_users=True)
    with SessionLocal() as db:
        after = db.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.company_id == b["id"])).scalar()
    assert after == before, "журнал решений удалён вместе с компанией"
