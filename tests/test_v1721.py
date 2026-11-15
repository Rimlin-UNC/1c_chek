"""v1.72.1: живые обновления в компании клиента (T-91, этап 8 плана «Клиенты», ADR-54).

Бухгалтер-аутсорсер работает в компании клиента (заголовок X-Ym-Company, ADR-30), но WebSocket-подписка
до сих пор брала `users.company_id`, то есть его СВОЮ компанию: чеки и статусы по клиенту не приходили
вживую, пока не перезагрузишь экран. Теперь подписка учитывает активного клиента: сервер принимает
`company` в запросе подключения (или в первом сообщении), проверяет право по тем же правилам, что и
HTTP-запросы, и шлёт события выбранной компании; при смене клиента подписка переключается, а не молча
остаётся на прежней. Проверяется ядро рассылки (без живого сокета) и клиентская часть — окно.
Только синтетика (ADR-14).
"""
from __future__ import annotations

from uuid import uuid4

from app.models import User
from app.services import events
from tests import test_docreq as _dr
from tests import test_primary_upd as _upd

firms = _upd.firms
client_firm = _dr.client_firm


def _queues():
    return list(events._subscribers.items())


def test_subscription_can_be_pinned_to_active_client(monkeypatch):
    """Подписку можно привязать к компании клиента: событие этой компании доходит, событие своей — нет."""
    monkeypatch.setattr(events, "_loop", None)
    own, client = str(uuid4()), str(uuid4())
    q = events.subscribe_sync(client, True)                  # новая функция: без корутины — для ядра и тестов
    try:
        events._deliver({"company_id": client, "id": "r1"})
        events._deliver({"company_id": own, "id": "r2"})
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        assert len(got) == 1 and client in got[0] and "r1" in got[0]
    finally:
        events.unsubscribe(q)


def test_switch_client_moves_subscription(monkeypatch):
    """Смена клиента переключает подписку: события прежнего клиента после переключения не приходят."""
    monkeypatch.setattr(events, "_loop", None)
    first, second = str(uuid4()), str(uuid4())
    q = events.subscribe_sync(first, True)
    try:
        assert events.resubscribe(q, second) is True
        events._deliver({"company_id": first, "id": "old"})
        events._deliver({"company_id": second, "id": "new"})
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        assert len(got) == 1 and "new" in got[0]
    finally:
        events.unsubscribe(q)


def test_admin_sees_everything_regardless(monkeypatch):
    """Администратор подписан на все компании: привязка к компании клиента ему не нужна и не вредит."""
    monkeypatch.setattr(events, "_loop", None)
    a, b = str(uuid4()), str(uuid4())
    q = events.subscribe_sync(None, True)
    try:
        events._deliver({"company_id": a, "id": "a1"})
        events._deliver({"company_id": b, "id": "b1"})
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        assert len(got) == 2
    finally:
        events.unsubscribe(q)


def test_anonymous_still_gets_nothing_of_companies(monkeypatch):
    """Анонимный подписчик не получает событий компаний даже при попытке привязаться."""
    monkeypatch.setattr(events, "_loop", None)
    c = str(uuid4())
    q = events.subscribe_sync(c, False)
    try:
        events._deliver({"company_id": c, "id": "secret"})
        assert q.empty()
    finally:
        events.unsubscribe(q)


def test_server_accepts_active_client(client_firm):
    """Сервер принимает имя активного клиента и проверяет право: своя компания и назначенная — да, чужая — нет."""
    from app.clients import service as clients
    a, b = client_firm["a"], client_firm["b"]
    with __import__("app.database", fromlist=["SessionLocal"]).SessionLocal() as db:
        acc = db.query(User).filter_by(role="accountant", company_id=a["id"]).first()
        assert clients.ws_company(db, acc, a["id"]) == a["id"]        # своя
        assert clients.ws_company(db, acc, "") is None                # без выбора — своя компания, как раньше
        try:
            clients.ws_company(db, acc, b["id"])
            raise AssertionError("чужая компания не должна приниматься")
        except clients.ClientsError as exc:
            assert exc.status == 403 and "не назначена" in str(exc)
        # администратор вправе смотреть любую компанию
        assert clients.ws_company(db, db.query(User).filter_by(role="admin").first(), b["id"]) == b["id"]


def test_ws_company_of_inactive_client(client, client_firm):
    """Отключённая компания не принимается: живые обновления по архиву не нужны, и права те же, что у HTTP."""
    from app.clients import service as clients
    from app.database import SessionLocal
    admin, a = client_firm["admin"], client_firm["a"]
    client.patch(f"/api/v1/companies/{a['id']}", headers=admin, json={"is_active": False})
    try:
        with SessionLocal() as db:
            with __import__("pytest").raises(clients.ClientsError) as caught:
                clients.ws_company(db, db.get(User, db.query(User).filter_by(role="admin").first().id), a["id"])
            assert "отключена" in str(caught.value)
    finally:
        client.patch(f"/api/v1/companies/{a['id']}", headers=admin, json={"is_active": True})


def test_company_records_untouched():
    """Схема базы не меняется: подписка — в памяти процесса, никаких новых таблиц."""
    from app.database import Base
    assert "companies" in Base.metadata.tables
    assert not any("ws_" in t or "socket" in t for t in Base.metadata.tables)


def test_websocket_switch_home_keeps_company_isolation(client, firms):
    """Реальный сокет: сброс клиента не превращает бухгалтера в администратора."""
    _, a, b = firms
    token = a['hdr']['Authorization'].split()[1]
    with client.websocket_connect(f"/ws/status?token={token}") as ws:
        assert ws.receive_json()['type'] == 'connected'
        ws.send_json({'type': 'client', 'company': ''})
        reply = ws.receive_json()
        assert reply['type'] == 'client_ok'
        assert reply['payload']['company'] == a['id']
        assert (a['id'], True) in events._subscribers.values()
        assert (None, True) not in events._subscribers.values()
        ws.send_json({'type': 'client', 'company': b['id']})
        assert ws.receive_json()['type'] == 'client_error'
        assert (a['id'], True) in events._subscribers.values()
        ws.send_json({'type': 'client', 'company': ['bad']})
        assert ws.receive_json()['type'] == 'client_error'
        ws.send_json({'type': 'client', 'company': ''})
        assert ws.receive_json()['type'] == 'client_ok'


def test_websocket_revocation_stops_client_events(client, firms, monkeypatch):
    """После отзыва доступа уже открытый сокет не получает данные клиента."""
    admin, a, b = firms
    acc_id = client.get('/api/v1/auth/me', headers=a['hdr']).json()['id']
    url = f"/api/v1/clients/access/{acc_id}"
    assert client.put(url, headers=admin, json={'company_ids': [b['id']]}).status_code == 200
    token = a['hdr']['Authorization'].split()[1]
    with client.websocket_connect(f"/ws/status?token={token}&company={b['id']}") as ws:
        assert ws.receive_json()['type'] == 'connected'
        # Другие тесты создают и закрывают TestClient; используем цикл именно этого сокета.
        import asyncio
        monkeypatch.setattr(events, '_loop', ws.portal.call(asyncio.get_running_loop))
        events.broadcast('receipt_created', {'company_id': b['id'], 'id': 'allowed'})
        assert ws.receive_json()['payload']['id'] == 'allowed'
        assert client.put(url, headers=admin, json={'company_ids': []}).status_code == 200
        events.broadcast('receipt_created', {'company_id': b['id'], 'id': 'secret'})
        msg = ws.receive_json()
        assert msg['type'] == 'client_error'
        assert 'не назначена' in msg['payload']['detail']
        assert (a['id'], True) in events._subscribers.values()


def test_ws_company_rights_on_both_databases(store):
    """Права на назначенного, чужого и отключённого клиента — SQLite и PostgreSQL."""
    import pytest

    from app.clients.models import AccountantClient
    from app.clients.service import ClientsError, ws_company
    from app.models import Company

    tag = uuid4().hex
    with store.session() as db:
        home = Company(name=f'ws home {tag}')
        target = Company(name=f'ws client {tag}')
        other = Company(name=f'ws other {tag}')
        db.add_all([home, target, other])
        db.flush()
        user = User(username=f'ws_{tag}', password_hash='synthetic', role='accountant', company_id=home.id)
        db.add(user)
        db.flush()
        grant = AccountantClient(user_id=user.id, company_id=target.id)
        db.add(grant)
        db.flush()
        assert ws_company(db, user, target.id) == target.id
        with pytest.raises(ClientsError, match='не назначена'):
            ws_company(db, user, other.id)
        target.is_active = False
        db.flush()
        with pytest.raises(ClientsError, match='отключена'):
            ws_company(db, user, target.id)
        db.rollback()
