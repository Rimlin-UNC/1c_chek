# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.10: меню настроек не пропадает.
# Корень бага: режим «Все настройки» прятал весь nav вместе с кнопкой
# возврата. Замок: прячутся только группы, панель кнопок всегда видна.
# ======================================================================
import re

from app.config import settings


class TestVersions17010:
    def test_config_version(self):
        assert settings.APP_VERSION == "1.70.10"

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
        # прячутся только группы; сам nav с кнопками — НИКОГДА
        assert "body.settings-all .settings-nav .snav-group { display: none; }" in self.css
        assert "body.settings-all .settings-nav { display: none;" not in self.css
        assert "body.settings-all .settings-layout { grid-template-columns: auto minmax(0, 1fr); }" in self.css

    def test_collapse_hidden_in_all_mode(self):
        assert "body.settings-all #snav-collapse { display: none; }" in self.css

    def test_toolbar_still_inside_sticky_nav(self):
        # панель кнопок остаётся в sticky-nav → доступна и при прокрутке
        assert "snav.insertAdjacentHTML('afterbegin'" in self.js
        assert ".settings-nav {" in self.css and "position: sticky" in self.css

    def test_deeplink_resets_stored_mode(self):
        assert "localStorage.setItem('settings_all', '')" in self.js

    def test_first_group_auto_opens(self):
        assert "snav-group.open')) {" in self.js
        assert "первый видимый непустой раздел" in self.js

    def test_collapse_returns_view_to_menu(self):
        assert "snav.scrollIntoView({ behavior: 'smooth', block: 'nearest' })" in self.js


class TestHarness17010:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.10: возврат из «Все настройки» всегда виден" in h
