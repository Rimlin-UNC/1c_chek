# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.56.0: адаптив по ТЗ (320px → 1440px+).
# Fluid-изображения, без горизонтальной прокрутки, таблицы прокручиваются
# внутри блока, touch-цели 44px, поля 16px, aria сайдбара.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1560:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.56.0 перенесён в tests/test_v1561.py (версия ушла вперёд)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 56, 0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.55.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.56.0':") < js.index("'1.55.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.56.0]") == 1
        assert ch.index("## [1.56.0]") < ch.index("## [1.55.1]")


class TestNoHorizontalScroll:
    def test_body_clips_stray_overflow(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        body = css.split("body {")[1][:300]
        assert "overflow-x: hidden;" in body

    def test_fluid_images(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "img { max-width: 100%; height: auto; display: block; }" in css

    def test_tables_scroll_inside_block(self):
        """Таблицы прокручиваются внутри блока даже без .table-wrap
        (список чеков и компании рендерятся без обёртки)."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        m640 = css.split("@media (max-width: 640px)")[1]
        assert "table.data { display: block; max-width: 100%; overflow-x: auto;" in m640
        # существующая обёртка на месте (двойная защита)
        assert ".table-wrap { overflow-x: auto;" in css

    def test_unwrapped_tables_exist_so_rule_needed(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert '<table class="data">' in js     # минимум одна без обёртки
        assert 'table-wrap"><table class="data"' in js


class TestTouchAndInputs:
    def test_touch_targets_44px(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        coarse = css.split("@media (pointer: coarse)")[1][:500]
        assert ".btn { min-height: 44px; }" in coarse
        assert ".btn-icon { width: 44px; height: 44px; }" in coarse
        assert ".nav-item { min-height: 44px; }" in coarse
        assert ".segmented button { min-height: 44px; }" in coarse
        # компактные кнопки в таблицах — 40px, строки не раздуваются
        assert ".btn-sm { min-height: 40px; }" in coarse

    def test_inputs_16px_on_phone(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        m520 = css.split("@media (max-width: 520px)")[1]
        assert "input, select, textarea { font-size: 16px; }" in m520


class TestSidebarA11y:
    def test_hamburger_and_aria_in_markup(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="btn-sidebar"' in idx and "aria-expanded" in idx
        assert 'aria-controls="sidebar"' in idx
        assert 'id="sidebar-backdrop"' in idx

    def test_aria_hidden_sync_and_desktop_reset(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # aria-hidden синхронно с открытием/закрытием
        assert "$('#sidebar').setAttribute('aria-hidden', String(!opened));" in js
        assert "if (_sbMobile()) $('#sidebar').setAttribute('aria-hidden', 'true');" in js
        # на десктопе служебное состояние сбрасывается (и на старте тоже)
        assert "const _sbSyncDesktop = () => {" in js
        assert "window.addEventListener('resize', _sbSyncDesktop);" in js
        assert js.count("_sbSyncDesktop();") == 1
        assert "(max-width: 860px)" in js

    def test_close_paths_intact(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "$('#sidebar-backdrop').onclick = closeSidebar;" in js
        assert "if (e.key === 'Escape') closeSidebar();" in js


class TestBaseIntact:
    def test_viewport_and_landing(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'content="width=device-width, initial-scale=1.0' in idx
        for cls in ("land-wrap", "land-h1", "land-stats", "login-card"):
            assert cls in idx

    def test_shell_adaptive_kept(self):
        """Off-canvas сайдбар (v1.10.x) и узкие экраны не сломаны."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        m860 = css.split("@media (max-width: 860px)")[1]
        m860 = m860[:m860.find("@media")] if "@media" in m860 else m860
        assert "position: fixed" in m860 and ".hamburger { display: inline-flex; }" in m860
        assert ".sidebar.open { margin-left: 0;" in css
        assert ".content { padding: 12px; }" in css.split("@media (max-width: 640px)")[1]


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        vt = lambda v: tuple(int(x) for x in v.split("."))
        # точные пины 1.56.0 перенесены в tests/test_v1561.py
        assert vt(reg["Адаптивный интерфейс"]) >= (1, 56, 0)
        assert vt(reg["Чек-Пул"]) >= (1, 56, 0)
        assert vt(reg["Обновления"]) >= (1, 55, 1)       # не задет
        assert vt(reg["Проверка чеков (ФНС и источники)"]) >= (1, 55, 0)
