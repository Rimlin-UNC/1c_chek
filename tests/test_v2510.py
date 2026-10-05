# ======================================================================
# Ямастер Чек — тесты v1.25.1: аудит страницы «Чеки».
# Все фильтры работают вместе (status/fns/exported/assignee/creator/
# full_data/category/notified/даты/q/ids), поиск без учёта регистра,
# фильтры не конфликтуют со scope компании.
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

from tests.conftest import login, client  # noqa: F401

QR1 = "t=20260901T1000&s=100.00&fn=9999078902001407&i=62001&fp=777001001&n=1"
QR2 = "t=20260902T1100&s=200.00&fn=9999078902001414&i=62002&fp=777001002&n=1"
QR3 = "t=20261001T1200&s=300.00&fn=9999078902001421&i=62003&fp=777001003&n=1"

# валидные ИНН, не занятые другими тестами (контрольная сумма пройдена)
_FREE_INN = ["5002000005", "5002000012", "5002000020", "5002000037"]
_INN_IDX = {"n": 0}


def _mk_company(client, adm):
    """Уникальная компания на тест (иначе 409 «дубликат» в общем прогоне)."""
    inn = _FREE_INN[_INN_IDX["n"] % len(_FREE_INN)]
    _INN_IDX["n"] += 1
    name = f"ООО Фильтры-{inn[-4:]}"
    comp = client.post("/api/v1/companies", json={
        "name": name, "inn": inn}, headers=adm).json()
    assert "id" in comp, comp
    return comp


def _ids(data):
    return sorted(x["id"] for x in (data.get("items") or []))


_QR_SEQ = {"n": 0}


def _qrs():
    """Уникальные QR на тест: дедупликация ФН+ФД+ФП иначе вернёт чужие чеки."""
    _QR_SEQ["n"] += 1
    k = _QR_SEQ["n"]
    def mk(day, s, i, fp):
        return (f"t=2026{day}T1000&s={s}&fn=9999078902004{k:02d}"
                f"&i=63{i:02d}&fp=778{k:02d}{fp}&n=1")
    return (mk("0901", "100.00", 1, "1"), mk("0902", "200.00", 2, "2"),
            mk("1001", "300.00", 3, "3"))


class TestReceiptFiltersFull:
    def _setup(self, client):
        adm = login(client, "admin", "admin123")
        comp = _mk_company(client, adm)
        inv1 = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=adm).json()
        u1 = client.post("/api/v1/auth/register", json={
            "token": inv1["token"], "username": "flt_ivan_" + comp["id"][:6],
            "password": "parol123", "full_name": "Иванов Фильтр"}).json()
        inv2 = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=adm).json()
        u2 = client.post("/api/v1/auth/register", json={
            "token": inv2["token"], "username": "flt_petr_" + comp["id"][:6],
            "password": "parol123", "full_name": "Петров Отыск"}).json()
        U1 = {"Authorization": "Bearer " + u1["access_token"]}
        U2 = {"Authorization": "Bearer " + u2["access_token"]}
        qr1, qr2, qr3 = _qrs()
        r1 = client.post("/api/v1/receipts/scan", headers=U1, json={
            "qr_data": qr1, "source": "manual", "verify": False}).json()["receipt"]
        r2 = client.post("/api/v1/receipts/scan", headers=U2, json={
            "qr_data": qr2, "source": "manual", "verify": False}).json()["receipt"]
        r3 = client.post("/api/v1/receipts/scan", headers=U1, json={
            "qr_data": qr3, "source": "manual", "verify": False}).json()["receipt"]
        return adm, comp, U1, U2, r1, r2, r3

    def _tune(self, client, adm, r1, r2, r3):
        """Разводим чеки по свойствам: категории и сотрудники (PATCH),
        статусы/выгрузку/уведомления/полноту — напрямую в БД (авто-проверка
        при скане сама меняет статус, поэтому задаём детерминированно)."""
        client.patch(f"/api/v1/receipts/{r1['id']}", headers=adm, json={
            "category": "Канцелярия", "assignee": "Иванов Фильтр"})
        client.patch(f"/api/v1/receipts/{r2['id']}", headers=adm, json={
            "category": "Топливо", "assignee": "Петров Отыск"})
        client.patch(f"/api/v1/receipts/{r3['id']}", headers=adm, json={
            "category": "канцелярия отдел", "assignee": "Иванов Фильтр"})
        from app.database import SessionLocal
        from app.models import Receipt
        db = SessionLocal()
        try:
            flags = {
                r1["id"]: dict(status="new", fns_status="unknown", exported=False,
                               notified=False, full_data=False),
                r2["id"]: dict(status="verified", fns_status="valid", exported=True,
                               notified=True, full_data=True),
                r3["id"]: dict(status="failed", fns_status="invalid", exported=False,
                               notified=False, full_data=False),
            }
            for rid, f in flags.items():
                db.query(Receipt).filter(Receipt.id == rid).update(f)
            db.commit()
        finally:
            db.close()

    def test_filters_creator_full_data_and_combined(self, client):
        adm, comp, U1, U2, r1, r2, r3 = self._setup(client)
        self._tune(client, adm, r1, r2, r3)
        H = adm
        # creator: два создателя
        ids1 = _ids(client.get("/api/v1/receipts?creator=" + U1["Authorization"][7:], headers=H).json()) \
            if False else None
        # (creator фильтруется по id пользователя — берём из /creators)
        cr = client.get("/api/v1/receipts/creators", headers=H).json()
        # матч по уникальному username (в общем прогоне есть «Ивановы» других тестов)
        ivan = next(x["id"] for x in cr if x["username"].startswith("flt_ivan"))
        petr = next(x["id"] for x in cr if x["username"].startswith("flt_petr"))
        got_ivan = _ids(client.get(
            f"/api/v1/receipts?creator={ivan}&company_id={comp['id']}", headers=H).json())
        got_petr = _ids(client.get(
            f"/api/v1/receipts?creator={petr}&company_id={comp['id']}", headers=H).json())
        assert r1["id"] in got_ivan and r3["id"] in got_ivan
        assert r2["id"] in got_petr and r1["id"] not in got_petr
        # creator + company_id (совместно со scope)
        got = _ids(client.get(
            f"/api/v1/receipts?creator={petr}&company_id={comp['id']}", headers=H).json())
        assert got == [r2["id"]]
        # full_data: r2 — полные, r1/r3 — ждут
        got = _ids(client.get(
            f"/api/v1/receipts?full_data=false&company_id={comp['id']}", headers=H).json())
        assert r1["id"] in got and r3["id"] in got and r2["id"] not in got
        got = _ids(client.get(
            f"/api/v1/receipts?full_data=true&company_id={comp['id']}", headers=H).json())
        assert got == [r2["id"]]
        # комбинация creator + full_data + статус (r1: new/не полные; r2: verified/полные)
        got = _ids(client.get(
            f"/api/v1/receipts?creator={ivan}&full_data=false&status=new"
            f"&company_id={comp['id']}", headers=H).json())
        assert r1["id"] in got and r2["id"] not in got
        got = _ids(client.get(
            f"/api/v1/receipts?status=verified&exported=true&company_id={comp['id']}",
            headers=H).json())
        assert got == [r2["id"]]
        got = _ids(client.get(
            f"/api/v1/receipts?status=failed&company_id={comp['id']}", headers=H).json())
        assert got == [r3["id"]]
        # фильтр проверки ФНС
        got = _ids(client.get(
            f"/api/v1/receipts?fns_status=valid&company_id={comp['id']}", headers=H).json())
        assert got == [r2["id"]]
        got = _ids(client.get(
            f"/api/v1/receipts?fns_status=invalid&company_id={comp['id']}", headers=H).json())
        assert got == [r3["id"]]
        got = _ids(client.get(
            f"/api/v1/receipts?fns_status=unknown&company_id={comp['id']}", headers=H).json())
        assert got == [r1["id"]]

    def test_filters_assignee_category_notified_dates_q(self, client):
        adm, comp, U1, U2, r1, r2, r3 = self._setup(client)
        self._tune(client, adm, r1, r2, r3)
        H = adm
        C = f"company_id={comp['id']}"
        # assignee: частично и без регистра (v1.25.1: assignee_lc — кириллица)
        got = _ids(client.get(f"/api/v1/receipts?assignee=петров&{C}", headers=H).json())
        assert got == [r2["id"]]
        # category: без учёта регистра (category_lc)
        got = _ids(client.get(f"/api/v1/receipts?category=КАНЦЕЛЯРИЯ&{C}", headers=H).json())
        assert r1["id"] in got and r3["id"] in got and r2["id"] not in got
        # notified: только r2
        got = _ids(client.get(f"/api/v1/receipts?notified=true&{C}", headers=H).json())
        assert got == [r2["id"]]
        # даты: узкий диапазон — только r2 (02.09)
        got = _ids(client.get(
            f"/api/v1/receipts?date_from=2026-09-02&date_to=2026-09-02&{C}", headers=H).json())
        assert got == [r2["id"]]
        # q: по номеру ФД чека r2
        got = _ids(client.get(f"/api/v1/receipts?q=6302&{C}", headers=H).json())
        assert got == [r2["id"]]
        # q: по сотруднику БЕЗ регистра (v1.25.1: assignee_lc)
        got = _ids(client.get(f"/api/v1/receipts?q=ИВАНОВ&{C}", headers=H).json())
        assert r1["id"] in got and r3["id"] in got and r2["id"] not in got
        # статус: r2 verified
        got = _ids(client.get(f"/api/v1/receipts?status=verified&{C}", headers=H).json())
        assert got == [r2["id"]]
        # exported=false — r1 и r3 (r2 выгружен)
        got = _ids(client.get(f"/api/v1/receipts?exported=false&{C}", headers=H).json())
        assert r1["id"] in got and r3["id"] in got and r2["id"] not in got
        # ids: выборка для печати — два конкретных
        got = _ids(client.get(
            f"/api/v1/receipts?ids={r1['id']},{r2['id']}", headers=H).json())
        assert sorted([r1["id"], r2["id"]]) == got

    def test_filter_scope_isolation(self, client):
        adm, comp, U1, U2, r1, r2, r3 = self._setup(client)
        self._tune(client, adm, r1, r2, r3)
        H = adm
        # пользователь видит только свои чеки своего пространства: u2 — свой r2
        got = _ids(client.get("/api/v1/receipts?status=verified", headers=U2).json())
        assert got == [r2["id"]]
        got = _ids(client.get("/api/v1/receipts?status=new", headers=U2).json())
        assert r1["id"] not in got and r3["id"] not in got
        # бухгалтер/пользователь не может поднять чужой company_id — фильтр игнорируется
        got = _ids(client.get(
            f"/api/v1/receipts?company_id={comp['id']}", headers=U1).json())
        assert {r1["id"], r3["id"]} <= set(got)

    def test_ui_state_persist_and_refresh_exposed(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # живое обновление вместо route() — список не пропадает
        assert "viewReceipts._refresh = load" in js
        assert "state.view === 'receipts' && typeof viewReceipts._refresh === 'function'" in js
        # фильтры переживают перестроение вида
        assert "viewReceipts._filters = { ...filters }" in js
        # сигнатурный кэш не глушит первый рендер после перестроения
        assert "viewReceipts._mounted = false" in js
        assert "viewReceipts._sig === rcptSig && viewReceipts._mounted" in js
        # ошибка загрузки — состояние с «Повторить»
        assert "Не удалось загрузить чеки" in js and "rc-retry" in js
        # создатели: кэш + восстановление + сброс при смене компании
        assert "viewReceipts._creators" in js and "fillCreators" in js
        assert "viewReceipts._creatorsCompany !== cCompany" in js

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.25.1"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf
