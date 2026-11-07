# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.3: «Список» чеков снова в одну строку.
# Решение владельца (11.10.2026): мобильное превращение таблицы списка
# в строки-карточки с подписями полей (v1.58.1, ≤720px) убрано — в
# режиме «Список» каждый чек одна строка таблицы; «Карточки» не тронуты.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re


class TestVersion1593:
    """Строгий версионный замок релиза v1.59.3."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.59.4: структурная проверка — строгий замок версии в test_v1594
        assert tuple(int(x) for x in ver.split(".")) >= (1, 59, 3)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.2" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.3':") < js.index("'1.59.2':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.3]") == 1
        assert ch.index("## [1.59.3]") < ch.index("## [1.59.2]")


class TestListViewTable:
    """Режим «Список» — настоящая таблица на экранах любой ширины."""

    def test_pseudo_cards_css_removed(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "строки-КАРТОЧКИ" not in css
        assert "#receipts-table .data thead { display: none; }" not in css
        assert "content: attr(data-label)" not in css
        # оба медиаблока ≤720px для списка чеков убраны
        assert "max-width: 720px" not in css

    def test_table_scrolls_inside_block(self):
        """На телефоне таблица прокручивается внутри .table-wrap —
        строка на чек остаётся одной строкой."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".table-wrap { overflow-x: auto; border-radius: var(--radius); }" in css
        assert "min-width: 880px" in css          # компактная таблица ≤640px

    def test_cards_mode_styles_intact(self):
        """«Карточки» не тронуты: свои стили .rc-* на месте."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".rc-cards {" in css and ".rc-card {" in css
        assert ".rc-actions {" in css and ".seg .btn.active" in css

    def test_list_renders_table(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'el.innerHTML = `<table class="data"><thead><tr>' in js
        assert "function receiptRow(" in js       # одна строка на чек
        assert "state.receiptsView === 'cards'" in js
