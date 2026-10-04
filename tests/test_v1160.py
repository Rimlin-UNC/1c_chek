# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.16.0: самовосстановление апдейтера (обновление
# из приложения без .git), Telegram-бот (настройки/привязка/напоминания),
# светофор контрагента, массовое обновление ЕГРЮЛ.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import itertools
import os
import re
import subprocess

from tests.conftest import login

_SEQ = itertools.count(700, 10)


class TestUpdaterSelfHeal:
    def test_git_ready_and_ensure(self, tmp_path, monkeypatch):
        """Без .git апдейтер обязан сам создать репозиторий и забрать ветку.
        GitHub не нужен: origin — локальный bare-клон."""
        import app.services.updater as up
        src = os.getcwd()
        bare = str(tmp_path / "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", "--no-hardlinks",
                        src, bare], check=True)
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=bare,
            capture_output=True, text=True).stdout.strip() or "main"
        appcopy = tmp_path / "appcopy"
        appcopy.mkdir()
        (appcopy / "app").mkdir()
        (appcopy / "app" / "config.py").write_text(
            'APP_VERSION: str = "0.0.1"\n', encoding="utf-8")
        monkeypatch.setattr(up, "APP_DIR", str(appcopy))
        assert up._git_ready() is False
        ok, msg = up._ensure_git_repo("x/y", branch, remote_url=bare)
        assert ok, msg
        assert up._git_ready() is True
        cfg = (appcopy / "app" / "config.py").read_text(encoding="utf-8")
        assert "APP_VERSION" in cfg          # ветка забрана (checkout FETCH_HEAD)

    def test_pre_flight_has_git_flag(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/admin/update/preflight", headers=hdr)
        assert r.status_code == 200
        assert "git_ok" in r.json()

    def test_branch_without_app_message(self, monkeypatch):
        """Ветка без app/config.py → внятное сообщение, а не «GitHub недоступен»."""
        import app.services.updater as up

        def fail_404(repo, branch, path):
            raise FileNotFoundError("HTTP 404")

        monkeypatch.setattr(up, "_gh_api_raw", fail_404)
        monkeypatch.setattr(up, "_gh_raw", fail_404)
        monkeypatch.setattr(up, "_gh_git", fail_404)
        try:
            up._remote_version("o/r", "main")
            assert False, "должна быть ошибка"
        except RuntimeError as e:
            assert "нет файла" in str(e) and "main" in str(e)


class TestTelegramSettings:
    def test_roundtrip_and_secret(self, client):
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/settings/telegram", headers=hdr)
        assert r.status_code == 200
        assert r.json()["has_token"] is False
        # плохой токен
        r = client.put("/api/v1/settings/telegram",
                       json={"bot_token": "how-do-i-bot"}, headers=hdr)
        assert r.status_code == 422
        # несуществующий бот: API Telegram вернёт 401 — формат валиден,
        # но get_me падает → 422 с понятным текстом (в песочнице может не быть
        # сети к Telegram — допускаем и 200, если сеть есть)
        r = client.put("/api/v1/settings/telegram",
                       json={"bot_token": "123456789:AA" + "x" * 33,
                             "enabled": True, "reminder_time": "09:30"},
                       headers=hdr)
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            from app.database import SessionLocal
            from app.models import AppSetting
            s = SessionLocal()
            try:
                row = s.get(AppSetting, "telegram_bot_token")
                assert row.value.startswith("enc:v1:")
            finally:
                s.close()
        # время в неправильном формате
        r = client.put("/api/v1/settings/telegram",
                       json={"reminder_time": "25:61"}, headers=hdr)
        assert r.status_code == 422

    def test_forbidden_for_user(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": f"ООО Тг {next(_SEQ)}"}, headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "tg_user",
            "password": "parol123", "full_name": "Тимур Телеграмов"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        assert client.get("/api/v1/settings/telegram", headers=hu).status_code == 403
        # личная привязка доступна пользователю
        r = client.post("/api/v1/users/me/telegram/bind", headers=hu)
        assert r.status_code == 200
        code = r.json()["code"]
        assert re.fullmatch(r"\d{6}", code)


class TestTelegramLogic:
    def test_bind_code_lifecycle(self):
        from app.services import telegram_bot as tg
        code = tg.new_bind_code(None, "user-xyz")
        assert re.fullmatch(r"\d{6}", code)
        assert tg.consume_bind_code(code) == "user-xyz"
        assert tg.consume_bind_code(code) is None          # одноразовость
        assert tg.consume_bind_code("000000") is None

    def test_handle_update_binds_chat(self, client, monkeypatch):
        """Полный цикл: код → «/start КОД» → chat_id в профиле."""
        hdr = login(client, "admin", "admin123")
        r = client.post("/api/v1/users/me/telegram/bind", headers=hdr).json()
        from app.database import SessionLocal
        from app.models import User
        s = SessionLocal()
        try:
            admin = s.query(User).filter(User.username == "admin").first()
            uid = admin.id
        finally:
            s.close()
        # стаб отправки (сеть не нужна)
        sent = []
        from app.services import telegram_bot as tg
        monkeypatch.setattr(tg, "send_message",
                            lambda token, chat_id, text: sent.append((chat_id, text)) or True)
        monkeypatch.setattr(tg, "get_token", lambda db: "fake-token")
        s = SessionLocal()
        try:
            tg.handle_update(s, {"message": {
                "chat": {"id": 555000111},
                "text": f"/start {r['code']}"}})
            admin = s.get(User, uid)
            assert admin.telegram_chat_id == "555000111"
        finally:
            s.close()
        assert sent and "Привязано" in sent[0][1]

    def test_reminders_selection(self, client):
        """Напоминание — только привязанным активным пользователям без чеков сегодня."""
        from app.database import SessionLocal
        from app.models import User
        from app.services.telegram_bot import _users_to_remind
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": f"ООО Напоминания {next(_SEQ)}"},
                           headers=hdr).json()
        inv = client.post("/api/v1/invites", json={
            "role": "user", "company_id": comp["id"]}, headers=hdr).json()
        reg = client.post("/api/v1/auth/register", json={
            "token": inv["token"], "username": "rem_user",
            "password": "parol123", "full_name": "Рома Напомин"})
        hu = {"Authorization": "Bearer " + reg.json()["access_token"]}
        s = SessionLocal()
        try:
            u = s.query(User).filter(User.username == "rem_user").first()
            u.telegram_chat_id = "111222333"
            s.commit()
            targets = _users_to_remind(s)
            assert any(x.id == u.id for x in targets)
            # админ (роль admin) не получает напоминания — даже привязанный
            adm = s.query(User).filter(User.username == "admin").first()
            assert not any(x.id == adm.id for x in targets)
        finally:
            s.close()
        _ = hu

    def test_notify_user_safe_without_binding(self):
        """notify_user не падает и не шлёт ничего, если чат не привязан."""
        from app.services import telegram_bot as tg
        tg.notify_user("nonexistent-user-id", "тест")   # не должно поднять


class TestRiskTrafficLight:
    def test_rules(self):
        from app.services.companies_util import risk_assessment as r
        assert r(None, "")["level"] == "yellow"
        assert r(None, "7801234564")["level"] == "none"
        red = {"name_full": "ООО Х", "status": "Ликвидирована",
               "reg_date": "2010-01-01", "capital": "100 000 ₽"}
        assert r(red, "7801234564")["level"] == "red"
        young = {"name_full": "ООО Y", "status": "Действует",
                 "reg_date": "2026-09-01", "capital": "10 000 ₽"}
        assert r(young, "7801234564")["level"] == "yellow"
        good = {"name_full": "ООО Z", "status": "Действует",
                "reg_date": "2015-01-01", "capital": "50 000 ₽"}
        assert r(good, "7801234564")["level"] == "green"

    def test_card_endpoint_returns_risk(self, client):
        hdr = login(client, "admin", "admin123")
        comp = client.post("/api/v1/companies",
                           json={"name": f"ООО Светофор {next(_SEQ)}"},
                           headers=hdr).json()
        d = client.get(f"/api/v1/companies/{comp['id']}/card", headers=hdr).json()
        assert "risk" in d and d["risk"]["level"] in ("green", "yellow", "red", "none")


class TestBulkRefresh:
    def test_requires_key_and_counts(self, client, monkeypatch):
        hdr = login(client, "admin", "admin123")
        # без ключа — 422 (стартуем с чистого состояния настройки)
        from app.database import SessionLocal
        from app.models import AppSetting
        s = SessionLocal()
        try:
            row = s.get(AppSetting, "checko_api_key")
            if row:
                s.delete(row)
                s.commit()
        finally:
            s.close()
        r = client.post("/api/v1/companies/bulk-refresh",
                        json={"limit": 10}, headers=hdr)
        assert r.status_code == 422
        # задаём валидный ключ и стабим Checko
        client.put("/api/v1/settings/checko",
                   json={"api_key": "bulkkey1234567890"}, headers=hdr)
        from app.services import checko as ck
        calls = {"n": 0}

        def fake_fetch(key, inn):
            calls["n"] += 1
            if inn == _inn2:
                raise ck.CheckoError("Компания с таким ИНН не найдена")
            return {"card": {"kind": "legal", "name_full": f"ООО {inn}",
                             "inn": inn, "status": "Действует"}, "raw": {}}

        monkeypatch.setattr(ck, "fetch_card", fake_fetch)
        comp = client.post("/api/v1/companies", json={
            "name": f"ООО Массовое {next(_SEQ)}", "inn": "7707083893"},
            headers=hdr).json()
        # уникальный валидный ИНН (контрольная цифра по коэффициентам ИФНС)
        _d = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        _base = f"77{next(_SEQ):07d}"[:9]
        _cs = (sum(a * b for a, b in zip(_d, [int(c) for c in _base])) % 11) % 10
        _inn2 = _base + str(_cs)
        client.post("/api/v1/companies", json={
            "name": f"ООО Массовое {next(_SEQ)}", "inn": _inn2},
            headers=hdr)
        r = client.post("/api/v1/companies/bulk-refresh",
                        json={"limit": 40}, headers=hdr)
        assert r.status_code == 200, r.text
        d = r.json()
        # в общей БД есть компании из других тестов: считаем относительно
        assert d["total_with_inn"] >= 2
        assert d["updated"] == d["total_with_inn"] - len(d["errors"])
        assert any(_inn2 in e for e in d["errors"])
        # карточка реально записана
        d2 = client.get(f"/api/v1/companies/{comp['id']}/card", headers=hdr).json()
        assert d2["card"]["name_full"] == "ООО 7707083893"


class TestVersion1160:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        from app.config import settings
        assert settings.APP_VERSION == ver
        assert ver == "1.16.0"

    def test_ui_and_docs(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.16.0':" in js
        assert "tg-token" in js and "tg-code" in js and "cp-bulk-refresh" in js
        assert "Светофор" not in js or "riskBlock" in js
        plan = open("docs/unique_features.md", encoding="utf-8").read()
        assert "сделано в 1.16.0" in plan
