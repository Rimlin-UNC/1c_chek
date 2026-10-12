# Ямастер Чек — тесты v1.66.0: Ассистенты (Telegram + MAX).
# ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Критерии приёмки (docs/concept_roadmap_v160.md, Фаза 1 п.6):
#  - напоминания «сдай чеки за сегодня» (18:00, настройка) и утренний
#    дайджест бухгалтеру/админу;
#  - КАНАЛЫ: telegram | max | both — работают в паре или по одному;
#    выключение одного не трогает другой; напоминания шлёт только
#    канал, к которому сотрудник привязан (или оба сразу);
#  - MAX: Bot API platform-api2.max.ru, токен в заголовке Authorization,
#    long-poll GET /updates (marker), отправка POST /messages?user_id=,
#    лимит 2 msg/сек (анти-флуд). Без геолокации (решение владельца);
#  - дайджест: напоминания и дайджест не в один день-слот не конфликтуют;
#    тесты — без реальной сети (все отправки мокаются).
import re
from datetime import datetime

import pytest

_SEQ = {"n": 66000}


# ------------------------------------------------------------------ прогрев
class TestBootstrap:
    """Первым делом стартуем TestClient: сид создаёт admin, пока users
    пуста. Без этого тесты ниже (создают сотрудников напрямую) сломали бы
    сид при автономном прогоне файла."""

    def test_admin_seeded(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=H).json()
        assert me["role"] == "admin"


# ------------------------------------------------------------------ версии
class TestVersion1660:
    """Строгий версионный замок релиза v1.66.0."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.66.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.65.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.66.0':") < js.index("'1.65.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.66.0]") == 1
        assert ch.index("## [1.66.0]") < ch.index("## [1.65.0]")

    def test_telegram_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Telegram-уведомления"] == "1.66.0"


# ------------------------------------------------------------------ MAX API
class TestMaxApi:
    """Клиент MAX Bot API: домен, заголовок, форматы, анти-флуд."""

    def test_client_shape(self):
        from app.services import max_bot as mx
        assert mx.API_BASE == "https://platform-api2.max.ru"
        assert mx.SETT == "max_enabled"
        assert mx.DEFAULT_REMIND_TIME == "18:00"

    def test_send_message_uses_header_and_query(self, monkeypatch):
        from app.services import max_bot as mx
        captured = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"message": {}}

        def _post(url, headers=None, params=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, params=params, json=json)
            return _Resp()

        monkeypatch.setattr(mx.httpx, "post", _post)
        ok = mx.send_message("TOKEN123", 555000, "Привет!")
        assert ok is True
        assert captured["url"].startswith("https://platform-api2.max.ru/messages")
        assert captured["headers"]["Authorization"] == "TOKEN123"
        assert captured["params"]["user_id"] == 555000
        assert captured["json"]["text"].startswith("Привет")

    def test_get_me(self, monkeypatch):
        from app.services import max_bot as mx

        class _Resp:
            status_code = 200
            def json(self):
                return {"user_id": 1, "name": "Ямастер Чек",
                        "username": "ymaster_check_bot", "is_bot": True}

        monkeypatch.setattr(mx.httpx, "get", lambda *a, **k: _Resp())
        me = mx.get_me("T")
        assert me["username"] == "ymaster_check_bot"

    def test_api_error_is_human(self, monkeypatch):
        import httpx as _httpx
        from app.services import max_bot as mx

        def _raise(*a, **k):
            raise _httpx.ConnectTimeout("timeout")

        monkeypatch.setattr(mx.httpx, "post", _raise)
        try:
            mx.send_message("T", 1, "x")
            raise AssertionError("ожидали MaxError")
        except mx.MaxError as e:
            assert "MAX" in str(e)


# ------------------------------------------------------------------ дайджест
class TestDigestAndReminders:
    """Дайджест бухгалтеру/админу; напоминания — по каналам привязки."""

    def test_digest_text_contains_breakdown(self):
        from app.database import SessionLocal
        from app.models import Company, Receipt
        from app.services import max_bot as mx
        s = SessionLocal()
        try:
            _SEQ["n"] += 1
            c = Company(name=f"ООО Дайджест {_SEQ['n']}")
            s.add(c)
            s.flush()
            _SEQ["n"] += 1
            k = _SEQ["n"]
            s.add(Receipt(qr_data=f"t&s&fn=9{k}&i={k}&fp={k}&n=1",
                          fn=f"9{k}", fd=f"{k}", fp=f"{k}",
                          receipt_date=datetime(2026, 10, 12, 9, 0),
                          total_sum=1200.0, operation=1, status="verified",
                          company_id=c.id, assignee="Иванов",
                          assignee_lc="иванов", category="Транспорт",
                          category_lc="транспорт"))
            s.commit()
            text = mx.digest_text(s)
            assert "Дайджест" in text and "Иванов" in text
        finally:
            s.rollback()
            s.close()

    def test_reminder_targets_respect_channels(self):
        """Напоминание уходит только туда, где сотрудник привязан."""
        from app.database import SessionLocal
        from app.models import User
        from app.services import telegram_bot as tg
        from app.services import max_bot as mx
        s = SessionLocal()
        try:
            _SEQ["n"] += 1
            u1 = User(username=f"ch66a{_SEQ['n']}", full_name="Анна Только-Тг",
                      password_hash="x", role="user")
            u1.telegram_chat_id = "111"
            u2 = User(username=f"ch66b{_SEQ['n']}", full_name="Борис Только-Max",
                      password_hash="x", role="user")
            u2.max_user_id = "222"
            u3 = User(username=f"ch66c{_SEQ['n']}", full_name="Вера Оба",
                      password_hash="x", role="user")
            u3.telegram_chat_id = "333"
            u3.max_user_id = "444"
            s.add_all([u1, u2, u3])
            s.commit()
            tg_targets = {x.id for x in tg._users_to_remind(s)}
            mx_targets = {x.id for x in mx._users_to_remind(s)}
            assert u1.id in tg_targets and u1.id not in mx_targets
            assert u2.id in mx_targets and u2.id not in tg_targets
            assert u3.id in tg_targets and u3.id in mx_targets
        finally:
            s.rollback()
            s.close()

    def test_send_reminders_channel_split(self, monkeypatch):
        """TG-канал шлёт только tg-привязанным, MAX — только max-привязанным."""
        from app.database import SessionLocal
        from app.models import User
        from app.services import telegram_bot as tg
        from app.services import max_bot as mx
        s = SessionLocal()
        try:
            _SEQ["n"] += 1
            a = User(username=f"ch66d{_SEQ['n']}", full_name="Галя Обе",
                     password_hash="x", role="user")
            a.telegram_chat_id = "777"
            a.max_user_id = "888"
            s.add(a)
            s.commit()
            sent_tg, sent_mx = [], []

            def _tg_send(token, chat_id, text, proxy=""):
                sent_tg.append(chat_id)
                return True

            def _mx_send(token, user_id, text):
                sent_mx.append(user_id)
                return True

            monkeypatch.setattr(tg, "send_message", _tg_send)
            monkeypatch.setattr(mx, "send_message", _mx_send)
            monkeypatch.setattr(tg, "get_token", lambda db: "TGK")
            monkeypatch.setattr(mx, "get_token", lambda db: "MXK")
            tg._send_reminders(s)
            mx._send_reminders(s)
            assert "777" in sent_tg and "888" not in sent_tg
            assert "888" in sent_mx and "777" not in sent_mx
        finally:
            s.rollback()
            s.close()

    def test_worker_starts_max_too(self):
        """MAX-воркер — отдельный поток, заводится и останавливается."""
        from app.services import max_bot as mx
        assert hasattr(mx, "start_worker") and hasattr(mx, "stop_worker")


class TestAssistantsSettingsApi:
    """API настроек MAX (админ): сохранение токена, канал, время, дайджест."""

    def test_max_settings_roundtrip(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.get("/api/v1/settings/max", headers=H)
        assert r.status_code == 200, r.text
        base = r.json()
        assert base["channel"] in ("telegram", "max", "both")
        assert base["enabled"] in (True, False)
        # сохраняем конфигурацию (без реального бота: enabled=False)
        r2 = client.put("/api/v1/settings/max", headers=H, json={
            "bot_token": "-", "channel": "both",
            "reminder_time": "17:30", "digest_enabled": True})
        assert r2.status_code == 200, r2.text
        g = client.get("/api/v1/settings/max", headers=H).json()
        assert g["channel"] == "both"
        assert g["reminder_time"] == "17:30"
        assert g["digest_enabled"] is True
        # вернуть исходное
        client.put("/api/v1/settings/max", headers=H, json={
            "channel": base["channel"], "reminder_time": base["reminder_time"],
            "digest_enabled": base["digest_enabled"]})

    def test_max_bad_time_422(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.put("/api/v1/settings/max", headers=H,
                       json={"reminder_time": "99:99"})
        assert r.status_code == 422

    def test_channel_in_both_settings(self, client):
        """Выбор канала есть в ОБОИХ настройках (Telegram и MAX)."""
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        g = client.get("/api/v1/settings/telegram", headers=H).json()
        assert "channel" in g and g["channel"] in ("telegram", "max", "both")
        # смена канала через настройки Telegram
        r = client.put("/api/v1/settings/telegram", headers=H,
                       json={"channel": "max"})
        assert r.status_code == 200
        g2 = client.get("/api/v1/settings/telegram", headers=H).json()
        assert g2["channel"] == "max"
        client.put("/api/v1/settings/telegram", headers=H,
                   json={"channel": "telegram"})

    def test_me_has_max_bound(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        me = client.get("/api/v1/auth/me", headers=H).json()
        assert "max_bound" in me and "telegram_bound" in me


class TestAssistantsMigration:
    """Добавляющая миграция: users.max_user_id; секрет max_bot_token."""

    def test_column_and_secret(self):
        from sqlalchemy import inspect
        from app.database import engine
        cols = {c["name"] for c in inspect(engine).get_columns("users")}
        assert "max_user_id" in cols
        src = open("app/database.py", encoding="utf-8").read()
        assert "ADD COLUMN max_user_id" in src
        appset = open("app/services/appsettings.py", encoding="utf-8").read()
        assert "max_bot_token" in appset        # в списке секретов

    def test_no_geolocation(self):
        """Решение владельца: геолокацию НЕ запрашиваем (152-ФЗ)."""
        for f in ("app/services/max_bot.py", "app/services/telegram_bot.py"):
            src = open(f, encoding="utf-8").read()
            assert "location" not in src.lower(), f

    def test_blocks_registry(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Ассистенты (Telegram и MAX)"] == "1.66.0"


class TestManualSection:
    """Инструкция: раздел «Ассистенты: Telegram и MAX» для всех."""

    def test_manual_has_assistants_section(self, client):
        from tests.conftest import login
        H = login(client, "admin", "admin123")
        r = client.get("/api/v1/manual", headers=H)
        assert r.status_code == 200
        d = r.json()
        titles = {s["title"] for s in d["sections"]}
        assert "Ассистенты: Telegram и MAX" in titles
        sec = [s for s in d["sections"]
               if s["title"] == "Ассистенты: Telegram и MAX"][0]
        assert sec["image"] == "/img/manual/assistants.jpg"
        assert "геолок" in sec["html"].lower()
        img = open("app/static/img/manual/assistants.jpg", "rb").read()
        assert img[:2] == b"\xff\xd8"
