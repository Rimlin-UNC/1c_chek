# Ямастер Чек — тесты v1.64.0: дашборд руководителя + роль «руководитель».
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерии приёмки (docs/concept_roadmap_v160.md, Фаза 1 п.4):
#  - новая роль manager: видит ВСЕ чеки своей компании, ТОЛЬКО просмотр
#    (ФНС/выгрузка/правки/настройки — бухгалтеру; 403);
#  - администратор видит всё и входит в любую роль (impersonate);
#  - «Отчёт руководителя»: /dashboard/breakdown (суммы к учёту по
#    сотрудникам и статьям; личные вычтены; возвраты/failed мимо;
#    изоляция компании; админ — фильтр по компании) + drill-down;
#  - роль выдаётся: приглашением, созданием пользователя, «↕ Роль»;
#  - инструкция: раздел руководителя (аудитория manager), админ — вкладки;
#  - МИГРАЦИЙ НЕТ (users.role — строка); замки ФНС/пула/1С не тронуты.
import re
from datetime import datetime

import pytest

_SEQ = {"n": 64000}


# ------------------------------------------------------------------ версии
class TestVersion1640:
    """Строгий версионный замок релиза v1.64.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.65.0: структурная проверка — строгий замок версии в test_v1650
        assert tuple(int(x) for x in ver.split(".")) >= (1, 64, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.63.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.64.0':") < js.index("'1.63.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.64.0]") == 1
        assert ch.index("## [1.64.0]") < ch.index("## [1.63.0]")

    def test_roles_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in reg["Роли и доступ"].split(".")) >= (1, 64, 0)


# ------------------------------------------------------------------ роль
def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _mk_receipt(db, cid, total, assignee="", cat="", personal=0.0,
                status="verified", op=1, date=None):
    from app.models import Receipt
    _SEQ["n"] += 1
    k = _SEQ["n"]
    r = Receipt(qr_data=f"t&s&fn=64{k}&i={k}&fp={k}&n=1",
                fn=f"64{k}", fd=f"{k}", fp=f"{k}",
                receipt_date=date or datetime(2026, 10, 8, 12, 0),
                total_sum=total, personal_sum=personal, operation=op,
                status=status, company_id=cid,
                assignee=assignee, assignee_lc=assignee.casefold(),
                category=cat, category_lc=cat.casefold())
    db.add(r)
    db.flush()
    return r


class TestManagerRole:
    """Роль manager: вся компания на просмотр, правки — мимо."""

    def test_invite_register_scope_and_forbidden(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c1 = client.post("/api/v1/companies", headers=H,
                           json={"name": f"Руковод-Компания {_SEQ['n']}"})
        r_c2 = client.post("/api/v1/companies", headers=H,
                           json={"name": f"Чужая-Компания {_SEQ['n']}"})
        assert r_c1.status_code == 201 and r_c2.status_code == 201
        cid, other = r_c1.json()["id"], r_c2.json()["id"]
        # чеки в двух компаниях
        db = _db()
        try:
            mine = _mk_receipt(db, cid, 1500.0, assignee="Иванов И.И.",
                               cat="Канцелярия")
            _mk_receipt(db, cid, 800.0, assignee="Петров П.П.")
            foreign = _mk_receipt(db, other, 999.0, assignee="Чужой")
        finally:
            db.commit()
            db.close()
        # приглашение руководителя в первую компанию
        r_i = client.post("/api/v1/invites", headers=H, json={
            "role": "manager", "max_uses": 1, "company_id": cid,
            "note": "Директор"})
        assert r_i.status_code == 200, r_i.text
        inv = r_i.json()
        assert inv["role"] == "manager"
        r_r = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"boss{_SEQ['n']}",
            "password": "boss2026!", "full_name": "Директор Директоров"})
        assert r_r.status_code == 200, r_r.text
        assert r_r.json()["user"]["role"] == "manager"

        M = login(client, f"boss{_SEQ['n']}", "boss2026!")
        me = client.get("/api/v1/auth/me", headers=M).json()
        assert me["role"] == "manager" and me["company_id"] == cid

        # ВИДИТ: все чеки своей компании (в т.ч. чужие), не видит чужую
        rows = client.get("/api/v1/receipts", headers=M).json()["items"]
        ids = {x["id"] for x in rows}
        assert mine.id in ids and foreign.id not in ids
        # отдельный чужой чек — «не найден»
        assert client.get(f"/api/v1/receipts/{foreign.id}",
                          headers=M).status_code == 404
        # свой-company чек открыт целиком (drill-down)
        one = client.get(f"/api/v1/receipts/{mine.id}", headers=M)
        assert one.status_code == 200

        # ТОЛЬКО ПРОСМОТР: правки/проверки/выгрузки/настройки — 403
        assert client.post("/api/v1/receipts/verify", headers=M,
                           json={"receipt_ids": []}).status_code == 403
        assert client.post("/api/v1/receipts/export", headers=M,
                           json={"receipt_ids": []}).status_code == 403
        assert client.post(f"/api/v1/receipts/{mine.id}/fetch-details",
                           headers=M).status_code == 403
        assert client.patch(f"/api/v1/receipts/{mine.id}", headers=M,
                            json={"comment": "хочу"}).status_code == 403
        assert client.delete(f"/api/v1/receipts/{mine.id}",
                             headers=M).status_code == 403
        assert client.get("/api/v1/settings/mapping", headers=M).status_code == 403
        assert client.get("/api/v1/settings/policy", headers=M).status_code == 403
        assert client.get("/api/v1/users", headers=M).status_code == 403
        assert client.get("/api/v1/invites", headers=M).status_code == 403
        # сводка руководителю доступна (по его компании)
        st = client.get("/api/v1/dashboard/stats", headers=M).json()
        assert st["total"] >= 2 and isinstance(st.get("by_assignee"), list)

    def test_user_cannot_delete_others_receipt(self, client):
        """Регресс v1.64.0: раньше удалить чек мог ЛЮБОЙ, кто его видит.
        Теперь — только бухгалтер/админ/автор (руководитель и сосед — нет)."""
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c = client.post("/api/v1/companies", headers=H,
                          json={"name": f"Удаление {_SEQ['n']}"})
        cid = r_c.json()["id"]
        db = _db()
        try:
            r1 = _mk_receipt(db, cid, 1200.0, assignee="Коллега")   # автор — админ
        finally:
            db.commit()
            db.close()
        r_i = client.post("/api/v1/invites", headers=H, json={
            "role": "user", "max_uses": 1, "company_id": cid})
        inv = r_i.json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"neigh{_SEQ['n']}",
            "password": "plain2026!", "full_name": "Сосед По Компании"})
        U = login(client, f"neigh{_SEQ['n']}", "plain2026!")
        # чужой чек пользователь даже не ВИДИТ (изоляция «только свои»)
        assert client.get(f"/api/v1/receipts/{r1.id}", headers=U).status_code == 404
        assert client.delete(f"/api/v1/receipts/{r1.id}",
                             headers=U).status_code == 404
        # руководитель ВИДИТ чек компании, но удалить не может (только просмотр)
        r_i2 = client.post("/api/v1/invites", headers=H, json={
            "role": "manager", "max_uses": 1, "company_id": cid})
        inv2 = r_i2.json()
        client.post("/api/v1/auth/register", json={
            "token": inv2["token"], "username": f"bossdel{_SEQ['n']}",
            "password": "boss2026!", "full_name": "Руковод Удаления"})
        M = login(client, f"bossdel{_SEQ['n']}", "boss2026!")
        assert client.get(f"/api/v1/receipts/{r1.id}", headers=M).status_code == 200
        assert client.delete(f"/api/v1/receipts/{r1.id}",
                             headers=M).status_code == 403
        # бухгалтер может (не выгружен)
        r_i3 = client.post("/api/v1/invites", headers=H, json={
            "role": "accountant", "max_uses": 1, "company_id": cid})
        inv3 = r_i3.json()
        client.post("/api/v1/auth/register", json={
            "token": inv3["token"], "username": f"accdel{_SEQ['n']}",
            "password": "acc2026!!", "full_name": "Бухгалт Удаления"})
        A = login(client, f"accdel{_SEQ['n']}", "acc2026!!")
        assert client.delete(f"/api/v1/receipts/{r1.id}",
                             headers=A).status_code == 200

    def test_change_role_to_manager(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        uname = f"futureboss{_SEQ['n']}"
        r_u = client.post("/api/v1/users", headers=H, json={
            "username": uname, "password": "start2026!",
            "full_name": "Будущий Руководитель", "role": "user"})
        assert r_u.status_code in (200, 201), r_u.text
        uid = r_u.json()["id"]
        # смена роли через админ-эндпоинт (кнопка «↕ Роль»)
        r_role = client.patch(f"/api/v1/admin/users/{uid}/role",
                              headers=H, json={"role": "manager"})
        assert r_role.status_code == 200, r_role.text
        U = login(client, uname, "start2026!")
        assert client.get("/api/v1/auth/me", headers=U).json()["role"] == "manager"
        # несуществующая роль — 422; роль admin — 422
        assert client.patch(f"/api/v1/admin/users/{uid}/role", headers=H,
                            json={"role": "admin"}).status_code == 422
        assert client.patch(f"/api/v1/admin/users/{uid}/role", headers=H,
                            json={"role": "chief"}).status_code == 422

    def test_admin_can_impersonate_manager(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_i = client.post("/api/v1/invites", headers=H, json={
            "role": "manager", "max_uses": 1})
        inv = r_i.json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"viewboss{_SEQ['n']}",
            "password": "boss2026!", "full_name": "Просмотр Руководителя"})
        target = [u for u in client.get("/api/v1/users", headers=H).json()
                  if u["username"] == f"viewboss{_SEQ['n']}"][0]
        r_v = client.post(f"/api/v1/admin/impersonate/{target['id']}",
                          headers=H)
        assert r_v.status_code == 200, r_v.text
        assert r_v.json()["user"]["role"] == "manager"


class TestBreakdown:
    """Разрезы отчёта: суммы к учёту, фильтры, изоляция."""

    def test_breakdown_sums_and_scope(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c = client.post("/api/v1/companies", headers=H,
                          json={"name": f"Разрезы {_SEQ['n']}"})
        cid = r_c.json()["id"]
        db = _db()
        try:
            _mk_receipt(db, cid, 700.0, assignee="Иванов", cat="Транспорт",
                        personal=200.0)          # к учёту 500
            _mk_receipt(db, cid, 300.0, assignee="Иванов", cat="Транспорт")
            _mk_receipt(db, cid, 150.0, assignee="Петров", status="failed")
            _mk_receipt(db, cid, 250.0, assignee="Петров", op=2)   # возврат
            _mk_receipt(db, cid, 100.0, assignee="", cat="Канцелярия")
        finally:
            db.commit()
            db.close()
        bd = client.get("/api/v1/dashboard/breakdown", headers=H,
                        params={"days": 30, "company_id": cid}).json()
        iv = [x for x in bd["by_assignee"] if x["name"] == "Иванов"]
        assert iv and iv[0]["count"] == 2 and iv[0]["sum"] == 800.0
        # failed и возвраты не считаются; без сотрудника — не в сотрудниках
        assert not any(x["name"] == "Петров" for x in bd["by_assignee"])
        cat_names = {x["name"]: x for x in bd["by_category"]}
        assert cat_names["Транспорт"]["sum"] == 800.0   # 500 + 300
        assert cat_names["Канцелярия"]["sum"] == 100.0

    def test_breakdown_company_isolation(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_c1 = client.post("/api/v1/companies", headers=H,
                           json={"name": f"Изоляция-А {_SEQ['n']}"})
        r_c2 = client.post("/api/v1/companies", headers=H,
                           json={"name": f"Изоляция-Б {_SEQ['n']}"})
        cid1, cid2 = r_c1.json()["id"], r_c2.json()["id"]
        db = _db()
        try:
            _mk_receipt(db, cid1, 500.0, assignee="А-Сотрудник")
            _mk_receipt(db, cid2, 777.0, assignee="Б-Сотрудник")
        finally:
            db.commit()
            db.close()
        names_all = [x["name"] for x in client.get(
            "/api/v1/dashboard/breakdown", headers=H).json()["by_assignee"]]
        assert "А-Сотрудник" in names_all and "Б-Сотрудник" in names_all
        names_c1 = [x["name"] for x in client.get(
            "/api/v1/dashboard/breakdown", headers=H,
            params={"company_id": cid1}).json()["by_assignee"]]
        assert "А-Сотрудник" in names_c1 and "Б-Сотрудник" not in names_c1


class TestManualRoles:
    """Инструкция: раздел руководителя — только руководителю (и админу)."""

    def test_manager_gets_boss_section(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_i = client.post("/api/v1/invites", headers=H, json={
            "role": "manager", "max_uses": 1})
        inv = r_i.json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"manboss{_SEQ['n']}",
            "password": "boss2026!", "full_name": "Инструк Руководитель"})
        M = login(client, f"manboss{_SEQ['n']}", "boss2026!")
        d = client.get("/api/v1/manual", headers=M).json()
        auds = {s["audience"] for s in d["sections"]}
        titles = {s["title"] for s in d["sections"]}
        assert "Рабочее место руководителя" in titles
        assert "manager" in auds and "accountant" not in auds
        assert d["your_role"] == "manager" and d["show_tabs"] is False
        # разделу прилагается фирменная картинка
        sec = [s for s in d["sections"]
               if s["title"] == "Рабочее место руководителя"][0]
        assert sec["image"] == "/img/manual/boss.jpg"

    def test_user_does_not_see_boss_admin_sections(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        _SEQ["n"] += 1
        r_i = client.post("/api/v1/invites", headers=H, json={
            "role": "user", "max_uses": 1})
        inv = r_i.json()
        client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": f"plain{_SEQ['n']}",
            "password": "plain2026!", "full_name": "Простой Сотрудник"})
        U = login(client, f"plain{_SEQ['n']}", "plain2026!")
        d = client.get("/api/v1/manual", headers=U).json()
        auds = {s["audience"] for s in d["sections"]}
        titles = {s["title"] for s in d["sections"]}
        assert "Рабочее место руководителя" not in titles
        assert "manager" not in auds and "accountant" not in auds

    def test_admin_sees_all_tabs(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        d = client.get("/api/v1/manual", headers=H).json()
        assert d["show_tabs"] is True
        auds = {s["audience"] for s in d["sections"]}
        assert {"manager", "accountant", "admin", "user"} <= auds | {"all"} \
            or "manager" in auds


class TestBossUI:
    """Интерфейс: меню, отчёт, роль — маркеры в статике."""

    def test_static_markers(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "async function viewBoss" in js
        assert "boss-only" in js and "isManager" in js
        assert "Отчёт руководителя" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert '#/boss' in idx and "boss-only hidden" in idx
        icons = open("app/static/js/icons.js", encoding="utf-8").read()
        assert "boss:" in icons
        ui = open("app/static/js/ui.js", encoding="utf-8").read()
        assert "manager: 'Руководитель'" in ui
        img = open("app/static/img/manual/boss.jpg", "rb").read()
        assert img[:2] == b"\xff\xd8"          # настоящий JPEG, не заглушка
