# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.4: меню команд и надёжный запуск мини-приложения.
#  1) /start без кода → приветствие с кнопкой open_app (штатный запуск
#     мини-приложения из сообщения, по документации MAX Bot API);
#  2) при отказе API на open_app — откат на диплинк-кнопку;
#  3) /start КОД (6 цифр) по-прежнему привязывает уведомления;
#  4) прочий текст — прежняя подсказка про QR.
# Ноль сети: send_rich/send_message — monkeypatch.
# ======================================================================
import re

import pytest


def _bot_update(text: str, user_id: int = 63001, username: str = "starter"):
    from app.database import SessionLocal
    from app.services import max_bot
    s = SessionLocal()
    try:
        max_bot.handle_update(s, {"update": {"message": {
            "body": {"text": text},
            "sender": {"user_id": user_id, "username": username,
                       "first_name": "Тест"}}}})
    finally:
        s.rollback(); s.close()


class TestStartMenu1704:
    def test_plain_start_sends_welcome_with_open_app(self, client, monkeypatch):
        from app.services import max_bot
        rich, plain = [], []
        monkeypatch.setattr(max_bot, "send_rich",
                            lambda t, uid, text, buttons:
                            rich.append(buttons) or True)
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: plain.append(text) or True)
        _bot_update("/start")
        assert len(rich) == 1 and not plain
        btns = rich[0]
        assert btns[0]["type"] == "open_app"
        assert btns[1]["type"] == "link" and \
            btns[1]["url"].startswith("https://chek.ymaster.ru")

    def test_start_with_garbage_arg_welcome(self, client, monkeypatch):
        from app.services import max_bot
        rich, plain = [], []
        monkeypatch.setattr(max_bot, "send_rich",
                            lambda t, uid, text, b: rich.append(b) or True)
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: plain.append(text) or True)
        _bot_update("/start привет")
        assert len(rich) == 1 and not plain

    def test_app_command(self, client, monkeypatch):
        from app.services import max_bot
        rich, plain = [], []
        monkeypatch.setattr(max_bot, "send_rich",
                            lambda t, uid, text, b: rich.append(b) or True)
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: plain.append(text) or True)
        _bot_update("/app")
        assert len(rich) == 1

    def test_bind_code_still_works(self, client, monkeypatch):
        from app.services import max_bot
        rich, plain = [], []
        monkeypatch.setattr(max_bot, "send_rich",
                            lambda t, uid, text, b: rich.append(b) or True)
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: plain.append(text) or True)
        from app.database import SessionLocal
        from app.services import max_login
        s = SessionLocal()
        try:
            code = max_login.create_code(s)["code"]
        finally:
            s.rollback(); s.close()
        _bot_update("/start " + code, user_id=63002)
        assert not rich and plain and "✅" in plain[0]

    def test_other_text_qr_hint_unchanged(self, client, monkeypatch):
        from app.services import max_bot
        rich, plain = [], []
        monkeypatch.setattr(max_bot, "send_rich",
                            lambda t, uid, text, b: rich.append(b) or True)
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: plain.append(text) or True)
        _bot_update("привет")
        assert not rich and plain and "QR" in plain[0]

    def test_open_app_fallback_to_link(self, client, monkeypatch):
        """MAX API отверг open_app → повтор с диплинк-кнопкой, затем текст."""
        from app.services import max_bot
        calls, plain = [], []

        def fake_rich(t, uid, text, buttons):
            calls.append([b["type"] for b in buttons])
            if len(calls) == 1:
                raise max_bot.MaxError("bad button")
            return True

        monkeypatch.setattr(max_bot, "send_rich", fake_rich)
        monkeypatch.setattr(max_bot, "send_message",
                            lambda t, uid, text: plain.append(text) or True)
        _bot_update("/start")
        assert calls[0][0] == "open_app" and calls[1] == ["link"]
        assert not plain


class TestVersions1704:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.5: строгий замок версии переехал в test_v1705
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 4)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.js?v={ver}" in idx and '?v=1.70.3"' not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and "-v1.70.3'" not in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_changelog_whats_new(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.4]") == 1
        assert ch.index("## [1.70.4]") < ch.index("## [1.70.3]")
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.4':") < js.index("'1.70.3':")

    def test_manual_mentions_start_menu(self):
        mc = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Меню команд" in mc and "open_app" in mc


class TestNoRegression1704:
    def test_login_flow_alive(self, client):
        r = client.post("/api/v1/auth/max/start", json={})
        assert r.status_code == 200
        p = client.post("/api/v1/auth/max/poll",
                        json={"code": r.json()["code"]})
        assert p.json()["status"] == "pending"

    def test_diag_fields_alive(self, client):
        from app.services import max_bot
        st = max_bot.bot_settings(SessionLocal := __import__(
            "app.database", fromlist=["SessionLocal"]).SessionLocal())
        assert "token_bot" in st and "last_update_ago" in st
