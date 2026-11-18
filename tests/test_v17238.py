# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.72.38: общая вёрстка под все устройства.
#
# Сплошной обход 18 экранов на 1600 / 1280 / 1024 / 768 / 390 px дал три
# класса дефектов вне «Настроек»:
#  • шапка на телефоне: заголовок получал 62–146 px и рвался по слогам,
#    рядом теснились селектор компании, аватар и кнопка выхода;
#  • формы: двухколоночные .filter-bar на 390 px обрезали значения селектов,
#    шрифт полей 13–14 px (iOS зумит страницу на фокусе);
#  • кнопки: .btn-sm 30 px, .btn-icon 38 px, на касание — меньше 44 px.
#
# Замок проверяет правила CSS и разметку. Поведение на реальных ширинах
# проверяет tests/e2e/responsive_layout.spec.js (Playwright).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================

import glob
import re

from app.config import settings


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


CSS = read("app/static/css/app.css")
HOME_CSS = read("app/static/css/home.css")
JS = read("app/static/js/app.js")
STYLEGUIDE = read("docs/styleguide.md")


def media_block(css, header, last=False):
    """Тело media-блока по его заголовку (последнее вхождение при last=True)."""
    idx = css.rindex(header) if last else css.index(header)
    depth, start = 0, css.index("{", idx)
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:i]
    raise AssertionError("незакрытый блок: " + header)


class TestVersion17238:
    def test_config_version(self):
        # v1.73.3: версия ушла вперёд; структурная проверка не даёт откатиться ниже 1.72.38
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 72, 38)

    def test_static_versions_synced(self):
        ver = settings.APP_VERSION
        sw = read("app/static/sw.js")
        idx = read("app/static/index.html")
        mx = read("app/static/maxapp/index.html")
        mf = read("app/static/manifest.webmanifest")
        b2b = read("app/static/b2b/index.html")
        assert f"ymaster-check-v{ver}" in sw
        assert sw.count(f"?v={ver}") == 5
        assert idx.count(f"?v={ver}") == 3
        assert b2b.count(f"?v={ver}") == 2
        assert f"/maxapp/maxapp.js?v={ver}" in mx
        assert f'"version": "{ver}"' in mf
        assert f"server_monitor.js?v={ver}" in read("app/static/js/osm.js")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        # v1.73.0: «Настройки» ушли вперёд вместе с приёмной папкой; здесь — закреплённые этим выпуском блоки
        for block in ("Адаптивный интерфейс", "Резервные копии",
                      "Обновления", "Последние рабочие версии"):
            assert reg[block] == "1.72.38", block

    def test_changelog_and_whatsnew(self):
        ch = read("CHANGELOG.md")
        assert ch.count("## [1.72.38]") == 1
        assert ch.index("## [1.72.38]") < ch.index("## [1.72.37]")
        assert JS.count("'1.72.38':") == 1
        assert JS.index("'1.72.38':") < JS.index("'1.72.37':")


class TestMobileHeader:
    """Шапка: заголовок в две строки, компания — отдельной строкой."""

    def test_title_clamped_without_word_split(self):
        block = media_block(CSS, "@media (max-width: 767px) {")
        rule = re.search(r"#page-title \{[^}]*\}", block).group(0)
        assert "-webkit-line-clamp: 2" in rule
        assert "overflow-wrap: normal" in rule
        assert "word-break: normal" in rule
        assert "font-size: 16px" in rule

    def test_page_title_wrap_rule_stays_last(self):
        # контракт v1.70.58: последний @media (max-width: 640px) — про перенос
        tail = CSS.rsplit("@media (max-width: 640px) {", 1)[1]
        assert ".page-title {" in tail
        assert "font-size: 17px" in tail.split(".page-title {", 1)[1].split("}", 1)[0]

    def test_company_selector_moves_to_second_row(self):
        block = media_block(CSS, "@media (max-width: 480px) {")
        assert "grid-template-areas" in block
        assert '"burger title user"' in block
        assert '"company company company"' in block
        # на телефоне панель профиля — часть сетки шапки
        assert ".topbar-right { display: contents; }" in block
        assert "grid-area: company" in block

    def test_drawer_keeps_width_and_locks_background(self):
        block = media_block(CSS, "@media (max-width: 767px) {")
        # ширину панели менять нельзя: её же использует margin-left закрытой панели
        assert ".sidebar { width: min(86vw, 300px)" not in CSS
        assert "body.sb-open .content { overflow: hidden; }" in block
        assert ":is(.nav-item) { min-height: 44px; }" in block

    def test_footer_compact(self):
        block = media_block(CSS, "@media (max-width: 767px) {")
        rule = re.search(r"\.app-footer \{[^}]*\}", block).group(0)
        assert "font-size: 11px" in rule


class TestMobileForms:
    def test_filters_single_column(self):
        block = media_block(CSS, "@media (max-width: 767px) {")
        assert ".filter-bar .field { flex: 1 1 100%; min-width: 0; }" in block

    def test_inputs_sixteen_px_to_avoid_ios_zoom(self):
        block = media_block(CSS, "@media (max-width: 767px) {")
        rule = re.search(r"\.content :is\(input:not\(\[type=\"checkbox\"\]\)", block)
        assert rule
        assert "font-size: 16px;" in block

    def test_buttons_touch_targets(self):
        # на устройствах с мышью — 40/32 (внутри медиа, чтобы не глушить
        # блок тач-целей), на телефоне — 44
        assert "@media (min-width: 768px) and (pointer: fine) {" in CSS
        assert ".btn { min-height: 40px; }" in CSS
        assert ":is(.btn-sm) { min-height: 32px; }" in CSS
        block = media_block(CSS, "@media (max-width: 767px) {")
        assert ".content :is(.btn, .btn-sm), .modal-slot :is(.btn, .btn-sm) { min-height: 44px; }" in block


class TestTables:
    def test_scroll_shadow_is_conditional(self):
        # тень видна только там, где есть что прокручивать
        assert "background-attachment: local, local, scroll, scroll;" in CSS
        assert "radial-gradient(farthest-side at 100% 50%" in CSS

    def test_row_wrapping_fix(self):
        assert "table.data td { text-wrap: pretty; }" in CSS
        assert "padding-left: 12px; padding-right: 12px;" in CSS

    def test_empty_state_table_is_not_stretched(self):
        assert "table.data:has(td .empty-state) { min-width: 0; width: 100%; }" in CSS
        assert "table.data:has(td .empty-state) thead { display: none; }" in CSS

    def test_backup_table_columns(self):
        assert "table.data.bk-table .bk-name { white-space: normal; overflow-wrap: anywhere; }" in CSS
        widths = {k: int(v) for k, v in re.findall(r"\.bk-col-([a-z]+) \{ width: (\d+)%; \}", CSS)}
        assert sum(widths.values()) == 100, widths
        assert widths["name"] >= 22 and widths["size"] >= 14


class TestSettingsTabsWrap:
    def test_tabs_wrap_before_scrolling(self):
        assert "@container stgnav (max-width: 1140px) {" in CSS
        assert ".stg-tabs-row { flex-wrap: wrap; overflow-x: visible; row-gap: 0; }" in CSS

    def test_mobile_panel_is_inline_and_never_auto_opens(self):
        block = media_block(CSS, "@media (max-width: 767px) {")
        assert "position: static; margin-top: 6px; grid-template-columns: 1fr;" in block
        assert "phoneTabs" in JS
        assert "if (phoneTabs || wasOpen) closeSettingsPanels(); else openSettingsPanel(key);" in JS


class TestLandingNav:
    def test_links_wrap_and_are_touch_targets(self):
        block = media_block(HOME_CSS, "@media (max-width: 960px) {", last=True)
        assert "flex-wrap: wrap;" in block
        assert "overflow-x: visible;" in block
        assert "min-height: 44px;" in block

    def test_no_horizontal_scroll_strip(self):
        # .home-nav остаётся с overflow-x: auto только в базовом правиле
        assert HOME_CSS.count("overflow-x: auto;") == 1


class TestStyleguideAndPalette:
    def test_styleguide_section(self):
        assert "### 8.5 Оболочка и адаптив всей программы (v1.72.38)" in STYLEGUIDE
        assert "### 8.6 Статусы" in STYLEGUIDE

    def test_palette_untouched(self):
        # акцент — токен выпуска 1.25.0; новых цветов этот выпуск не вводил
        assert "--accent: #ff7a00;" in CSS
        new_block = CSS[CSS.index("v1.72.38 — общая вёрстка под все устройства"):]
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", new_block), "hex-цвет в блоке v1.72.38"

    def test_no_important_in_new_block(self):
        new_block = CSS[CSS.index("v1.72.38 — общая вёрстка под все устройства"):]
        assert "!important" not in new_block


class TestBrowserLock:
    def test_e2e_spec_exists(self):
        spec = read("tests/e2e/responsive_layout.spec.js")
        for width in ("1440", "1024", "768", "390"):
            assert width in spec
        assert "scrollWidth" in spec
        assert "min-height" in spec or "getBoundingClientRect" in spec

    def test_e2e_specs_do_not_pin_old_versions(self):
        """Сценарии не держат закреплённую версию в localStorage.

        v1.72.38: в девяти файлах стояло '1.72.35'. После выпуска 1.72.36
        окно «Что нового» открывалось у каждого такого сценария, перекрывало
        заставку — и прогон падал по таймауту 120 с. Теперь версия берётся
        у сервера (tests/e2e/seen_version.js), а этот замок не даёт вернуть
        захардкоженную строку.
        """
        helper = read("tests/e2e/seen_version.js")
        assert "export async function seenVersion" in helper
        assert "/api/v1/about" in helper
        for path in sorted(glob.glob("tests/e2e/*.js")):
            text = read(path)
            assert "ymaster_seen_version', '1." not in text, path
            if "ymaster_seen_version" in text:
                assert "seenVersion" in text, path

