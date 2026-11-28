# -*- coding: utf-8 -*-
# ======================================================================
# DocVer — тесты v1.70.10: меню настроек не пропадает.
# Корень бага: режим «Все настройки» прятал весь nav вместе с кнопкой
# возврата. Замок: прячутся только группы, панель кнопок всегда видна.
# ======================================================================
import re

from app.config import settings


class TestVersions17010:
    def test_config_version(self):
        # v1.70.11: строгий замок версии переехал в test_v17011
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 70, 10)

    def test_changelog(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.70.10]") == 1
        assert ch.index("## [1.70.10]") < ch.index("## [1.70.9]")

    def test_whatsnew(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.70.10':") < js.index("'1.70.9':")


class TestAllModeExitAlwaysVisible:
    css = open("app/static/css/app.css", encoding="utf-8").read()
    js = open("app/static/js/app.js", encoding="utf-8").read()

    def test_groups_hidden_not_nav(self):
        # v1.72.37: разделы больше не прячутся по одному — экран показывает
        # один раздел табом либо все подряд. Инвариант v1.70.10 прежний:
        # полоса навигации в режиме «все настройки» остаётся на месте.
        assert "body.settings-all .settings-nav { display: none;" not in self.css
        assert "body.settings-all .stg-tab.active" in self.css
        assert "body.settings-all .settings-grid" not in self.css
        # таб в режиме «все настройки» остаётся кликабельным выходом
        assert ".stg-tab {" in self.css and "cursor: pointer" in self.css

    def test_collapse_hidden_in_all_mode(self):
        # «Свернуть» живёт в меню «⋯ Ещё» и доступна в любом режиме
        assert 'id="snav-collapse"' in self.js
        assert "const bCollapse = $('#snav-collapse');" in self.js
        assert "#snav-collapse" in self.css or ".stg-more-item" in self.css

    def test_toolbar_still_inside_sticky_nav(self):
        # кнопки «⋯ Ещё» / «Все настройки» / «Свернуть» — внутри #settings-nav,
        # а сам nav липкий: при прокрутке выход из режима остаётся под рукой
        assert ".settings-nav {" in self.css and "position: sticky" in self.css
        i = self.js.index('<nav class="settings-nav settings-tabs" id="settings-nav"')
        j = self.js.index('</nav>', i)
        frag = self.js[i:j]
        assert 'id="snav-more"' in frag
        assert 'id="snav-all"' in frag
        assert 'id="snav-collapse"' in frag

    def test_deeplink_resets_stored_mode(self):
        assert "localStorage.setItem('settings_all', '')" in self.js

    def test_first_group_auto_opens(self):
        # первый доступный раздел открыт по умолчанию: таб подсвечен,
        # его раздел — единственный видимый
        assert "const first = tabs[0];" in self.js
        assert "settingsApply(snav, { group: first.getAttribute('data-sec'), card: '' })" in self.js
        assert "первый доступный раздел" in self.js

    def test_collapse_returns_view_to_menu(self):
        assert "snav.scrollIntoView({ behavior: 'smooth', block: 'nearest' })" in self.js


class TestHarness17010:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.10: возврат из «Все настройки» всегда виден" in h
