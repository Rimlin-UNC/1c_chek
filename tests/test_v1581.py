# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.1: адаптивная вёрстка + меню по ролям.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1581:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 58, 1)  # структурный
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.58.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.58.1':") < js.index("'1.58.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.58.1]") == 1
        assert ch.index("## [1.58.1]") < ch.index("## [1.58.0]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert tuple(int(x) for x in
                     reg["Адаптивный интерфейс"].split(".")) >= (1, 58, 1)
        # блок источников в этом релизе не трогался
        assert reg["Проверка чеков (ФНС и источники)"] == "1.58.0"
        assert reg["Чек-Пул"] == "1.56.1"
        assert reg["Обновления"] == "1.55.1"


class TestAdaptiveCss:
    """Адаптивный слой в app.css — все ключевые приёмы на месте."""

    def test_fluid_typography_and_no_ios_zoom(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "#page-title { font-size: clamp(19px, 2.6vw, 26px); line-height: 1.2; }" in css
        assert "input, select, textarea { font-size: 16px; }" in css

    def test_touch_targets(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@media (pointer: coarse)" in css
        assert ".btn { min-height: 44px; }" in css
        assert ".nav-item { min-height: 48px; }" in css

    def test_safe_area(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "env(safe-area-inset-left)" in css
        assert "env(safe-area-inset-bottom)" in css

    def test_receipts_cards_on_mobile(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@media (max-width: 720px)" in css
        assert "#receipts-table .data thead { display: none; }" in css
        assert "content: attr(data-label)" in css
        assert "border-radius: 14px" in css

    def test_bottom_sheet_modals(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@keyframes sheetUp" in css
        assert ".modal-root { display: flex; align-items: flex-end; }" in css
        assert ".modal-actions .btn { flex: 1 1 auto; }" in css

    def test_ultrawide_and_compact(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@media (min-width: 1600px)" in css
        assert "@media (max-width: 360px)" in css


class TestRoleMenu:
    """Меню по ролям: группы, пул-страницы, защита маршрутов."""

    def test_nav_groups(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert idx.count('<div class="nav-group">') == 2          # Основное, Чеки и учёт
        assert '<div class="nav-group pool-only">Чек-Пул</div>' in idx
        assert '<div class="nav-group admin-only hidden">Администрирование</div>' in idx

    def test_pool_pages_marked(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert idx.count("nav-item pool-only") == 3   # public, my, partners

    def test_pool_visibility_logic(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "state.poolEnabled = !!info.enabled;" in js
        assert "$$('.pool-only').forEach(el => el.classList.toggle('hidden', !state.poolEnabled));" in js
        # защита маршрутов: пул выключен → на дашборд
        assert ("view === 'public' || view === 'my' || view === 'partners'"
                in js) and "state.poolEnabled === false" in js

    def test_role_visibility_untouched(self):
        """Базовая ролевая механика (admin/accountant-only) не сломана."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "$$('.admin-only').forEach(el => el.classList.toggle('hidden', !isAdmin()));" in js
        assert "$$('.accountant-only').forEach(el => el.classList.toggle('hidden', !isAccountant()));" in js
        idx = open("app/static/index.html", encoding="utf-8").read()
        # настройки видимы всем, экспорт/маппинг — бухгалтер+, админ-разделы скрыты
        assert 'data-view="settings" class="nav-item"' in idx
        assert 'data-view="export" class="nav-item accountant-only"' in idx
        assert 'data-view="users" class="nav-item admin-only hidden"' in idx


class TestReceiptCards:
    def test_row_has_labels(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        for lab in ("Дата чека", "Сумма", "ФН", "ФД", "ФП",
                    "Сотрудник", "Статус", "ФНС", "1С"):
            assert f'data-label="{lab}"' in js, lab
        assert 'td class="cell-actions" style="white-space:nowrap"' in js

    def test_other_tables_not_affected(self):
        """Карточки — только у списка чеков; прочие таблицы не тронуты."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        mobile = css.split("@media (max-width: 720px)")[1].split("@media")[0]
        assert "table.data {" not in mobile      # селектор только #receipts-table
        assert "#receipts-table" in mobile
