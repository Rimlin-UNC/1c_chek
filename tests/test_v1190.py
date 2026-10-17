# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.19.0: человекочитаемый журнал действий
# (фразы + комментарии в кавычках) и стиль «Ямастер» (дизайн-система).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login


class TestAuditHuman:
    def test_receipt_created(self):
        from app.services.audit_human import humanize_audit
        h, c = humanize_audit("receipt_created",
                              {"fd": "55667", "sum": 1250.0, "source": "image"})
        assert h == "отсканирован чек на 1 250,00 ₽"
        assert c == "ФД 55667, источник: фото"

    def test_login_failed_comment(self):
        from app.services.audit_human import humanize_audit
        h, c = humanize_audit("login_failed",
                              {"username": "buh", "ip": "10.0.0.5"})
        assert "неверный логин или пароль" in h
        assert "buh" in c and "10.0.0.5" in c

    def test_role_change_in_russian(self):
        from app.services.audit_human import humanize_audit
        h, c = humanize_audit("user_role_changed", {"from": "user", "to": "accountant"})
        assert "роль" in h
        assert "сотрудник" in c and "бухгалтер" in c and "→" in c

    def test_company_and_impersonate_quotes(self):
        from app.services.audit_human import humanize_audit
        _, c1 = humanize_audit("company_created", {"name": "ООО Ромашка"})
        assert "ООО Ромашка" in c1
        _, c2 = humanize_audit("impersonate_start",
                               {"target": "Мария", "target_role": "accountant"})
        assert "Мария" in c2 and "бухгалтер" in c2
        # вложенные кавычки — „…“ (снаружи клиент добавит «…»)
        assert "«" not in c1 and "«" not in c2

    def test_settings_and_updates(self):
        from app.services.audit_human import humanize_audit
        _, c1 = humanize_audit("app_settings_updated",
                               {"auto_verify": True, "advance_deadline_days": 10})
        assert "автопроверка: вкл" in c1 and "10 дн." in c1
        h2, c2 = humanize_audit("update_apply",
                                {"target": "1.19.0", "branch": "main"})
        assert "обновление" in h2 and "1.19.0" in c2

    def test_unknown_action_fallback(self):
        from app.services.audit_human import humanize_audit
        h, c = humanize_audit("some_future_action", None)
        assert h == "some_future_action" and c == ""

    def test_recent_endpoint_human_fields(self, client):
        hdr = login(client, "admin", "admin123")   # создаёт событие login
        r = client.get("/api/v1/dashboard/recent?limit=60", headers=hdr)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert rows, "журнал не должен быть пуст"
        login_row = next(x for x in rows if x["action"] == "login")
        assert login_row["human"] == "вход в систему"
        assert "human" in rows[0] and "comment" in rows[0]


class TestAuditUI:
    def test_journal_view_redesign(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for needle in ("AUDIT_KIND", "auditDayLabel", "audit-day", "audit-row",
                       "audit-comment", "«${esc(f.comment)}»", "Журнал действий",
                       "limit=60", "f.human"):
            assert needle in js, needle
        # журнал в рукописном акценте
        assert 'card-title font-accent">Журнал действий' in js

    def test_feed_uses_server_human(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "if (f.human) return f.human" in js


class TestYamasterStyle:
    def test_palette_tokens(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        # v1.25.0: тёмные токены (#1e1e1e, #e06d00, #e0e0e0, #3cb371, #c94c4c)
        # удалены вместе с тёмной темой — остались светлые «Ямастер»
        for needle in ("#ff7a00", "#4b0082", "#2e8b57", "#d9534f",
                       "#333333", "#f5f5f5",
                       "--indigo", "--font-mono", "--font-accent"):
            assert needle in css.lower() or needle in css, needle
        for gone in ("#1e1e1e", "#e06d00", "#e0e0e0", "#3cb371", "#c94c4c"):
            assert gone not in css.lower(), gone

    def test_typography(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "tabular-nums" in css            # табличные цифры
        assert "font-size: 15px" in css         # база ≥14px
        assert "line-height: 1.5" in css
        assert "letter-spacing: 0.015em" in css
        assert ".font-accent" in css            # рукописные заголовки
        assert "#page-title" in css

    def test_buttons_and_audit_css(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".btn-accent" in css             # оранжевая secondary
        assert ".btn-primary" in css and "#333333" in css
        for needle in (".audit-day", ".audit-row", ".audit-comment",
                       ".audit-time", "kind-ok", "kind-bad"):
            assert needle in css, needle

    def test_fonts_connected(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "fonts.googleapis.com" in idx
        assert "Inter" in idx and "JetBrains+Mono" in idx and "Caveat" in idx

    def test_light_theme_default(self):
        # v1.25.0: светлая — единственная (переключателей нет)
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "--bg: #f5f5f5" in css and "--accent: #ff7a00" in css
        assert "#1e1e1e" not in open("app/static/js/app.js", encoding="utf-8").read()

    def test_styleguide_doc_exists(self):
        md = open("docs/styleguide.md", encoding="utf-8").read()
        for needle in ("#FF7A00", "#4B0082", "#2E8B57", "#D9534F",
                       "JetBrains Mono", "Caveat", "tabular-nums"):
            assert needle in md, needle


class TestVersion1190:
    def test_versions_synced(self):
        import re
        # синхронизация ?v= с APP_VERSION (актуальность пина — в тесте текущей
        # версии, test_v1200; здесь только согласованность — v1.20.0)
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw

    def test_whats_new(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.19.0':" in js
        assert "Журнал действий — человеческим языком" in js
