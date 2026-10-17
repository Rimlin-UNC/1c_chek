# Ямастер Чек — тесты v1.28.0: фильтры и статьи расходов, карточка чека.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Проверяем:
#  - GET /receipts/categories: топ используемых статей с частотами, скоуп компании, лимит ≤20;
#  - фильтр «Сотрудник» убран из UI, «Статья расходов» — селект; категория «Своя…» ≤100 символов;
#  - карточка чека: защита от повторного открытия, подсветка строки (JS-пины);
#  - версии синхронны (пин текущей версии — здесь), WHATS_NEW/CHANGELOG/инструкция.
import re

from tests.conftest import login
from tests.test_v2520 import _mk, _scan, _set_db


class TestCategoriesEndpoint:
    def test_top_categories_with_counts(self, client):
        adm, comp, u = _mk(client, "user", "cat")
        rid1 = _scan(client, adm)["id"]
        rid2 = _scan(client, adm)["id"]
        _set_db(rid1, category="Канцелярия")
        _set_db(rid2, category="Канцелярия")
        rid3 = _scan(client, adm)["id"]
        _set_db(rid3, category="ГСМ")
        d = client.get("/api/v1/receipts/categories", headers=adm)
        assert d.status_code == 200
        cats = [c for c in d.json() if c["name"] in ("Канцелярия", "ГСМ")]
        top = max(cats, key=lambda c: c["count"])
        assert top["name"] == "Канцелярия" and top["count"] >= 2

    def test_company_scope(self, client):
        adm, comp, u = _mk(client, "user", "catsc")
        rid = _scan(client, adm)["id"]
        _set_db(rid, category="Только наша статья")
        # другая компания — чужих статей не видно
        d2 = client.post("/api/v1/companies", headers=adm, json={"name": "catsc-other"})
        comp2 = d2.json()
        d = client.get(f"/api/v1/receipts/categories?company_id={comp2['id']}", headers=adm)
        assert d.status_code == 200
        assert "Только наша статья" not in [c["name"] for c in d.json()]

    def test_limit_cap_20(self, client):
        adm, comp, u = _mk(client, "user", "catlim")
        _ = u
        d = client.get("/api/v1/receipts/categories?limit=50", headers=adm)
        assert d.status_code == 422               # больше 20 нельзя (требование ТЗ)
        d = client.get("/api/v1/receipts/categories?limit=5", headers=adm)
        assert d.status_code == 200 and len(d.json()) <= 5

    def test_requires_auth(self, client):
        client.cookies.clear()   # fallback-cookie авторизации в TestClient
        assert client.get("/api/v1/receipts/categories").status_code in (401, 403)


class TestCategoryFormRules:
    def test_patch_category_cap_100(self, client):
        adm, comp, u = _mk(client, "user", "catcap")
        rid = _scan(client, adm)["id"]
        r = client.patch(f"/api/v1/receipts/{rid}", headers=adm,
                         json={"category": "Ж" * 101})
        assert r.status_code == 422               # сервер не пропустит >100

    def test_filter_ui_updated(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # фильтр «Сотрудник» убран, чтение assignee из UI убрано
        assert 'id="f-assignee"' not in js
        assert "$('#f-assignee')" not in js
        # «Статья расходов» в фильтрах — селект из используемых статей
        assert '<select id="f-category"><option value="">все статьи</option></select>' in js
        assert "/api/v1/receipts/categories" in js
        assert "function fillCats" in js
        # фильтр «Кто добавил» остался
        assert 'id="f-creator"' in js

    def test_edit_form_category_picker(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="er-cat-pick"' in js and "__custom__" in js
        assert 'maxlength="100"' in js                      # своя статья ≤100
        assert "введено ${len} из 100" in js                # счётчик: введено
        assert "осталось ${Math.max(0, 100 - len)}" in js   # счётчик: остаток
        assert ".slice(0, 19)" in js                        # ≤19 частых + «Своя…» = 20


class TestDrawerBehaviour:
    def test_single_instance_guard_pins(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # повторный клик по тому же чеку не открывает вторую карточку
        assert "receiptDrawer._openId === id && receiptDrawer._close" in js
        # двойной клик (два клика < 500 мс) закрывает
        assert "nowTs - lastClick.ts < 500" in js and "receiptDrawer._close()" in js
        # замена чужой карточки: один drawer за раз
        assert "if (receiptDrawer._close) receiptDrawer._close();" in js
        # подсветка открытой строки + снятие при закрытии
        assert 'classList.add(\'row-open\')' in js or 'classList.add("row-open")' in js
        assert "tr.classList.remove('row-open')" in js
        # подсветка переживает перерисовку списка
        assert "receiptDrawer._openId" in js

    def test_row_open_css(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "tr.row-open" in css


class TestVersion1280:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # Пин конкретной версии перенесён в tests/test_v1290.py (тест текущей версии)
        # v1.28.0: assert ver == "1.28.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.28.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.28.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Фильтры и статьи расходов (v1.28.0)" in manual
