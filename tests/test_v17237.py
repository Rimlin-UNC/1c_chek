# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.72.37: раздел «Настройки» без переполнения.
#
# Замок на второй проход по снимкам владельца 15.11.2026 («текст выходит
# за рамки блока»):
#  • навигация — горизонтальные табы; вертикальное меню и селект удалены;
#  • контент — одна колонка max-width: 900px на любой ширине экрана;
#  • токены (MAX, 1С, ФНС) показываются маской в поле с ограничением ширины,
#    рядом «показать» и «копировать»;
#  • таблицы клиентов и ключей — в .table-scroll, даты через fmtDate;
#  • чекбокс-строка и тач-зоны 44 px, ряды кнопок переносятся;
#  • активный таб синхронизирован с адресом #/settings/<раздел>;
#  • выпуск синхронизирован (версия, sw, index, manifest, блоки, доки).
#
# Поведение раскладки проверяют tests/js/settings_layout.test.js (Vitest)
# и tests/e2e/settings_layout.spec.js (Playwright, реальные ширины экрана).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================

import re

from app.config import settings


def read(path):
    return open(path, encoding="utf-8").read()


class TestVersion17237:
    def test_config_version(self):
        # живое значение сверяет выпуск-владелец; здесь — форма и то, что
        # 1.72.37 не откатился назад
        # v1.73.0: версия ушла на следующую линию; проверяется, что она не откатилась назад
        assert re.search(r'APP_VERSION: str = "(1\.72\.(3[7-9]|[4-9]\d)|1\.7[3-9]\.\d+)"',
                         read("app/config.py")), settings.APP_VERSION

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
        # модуль раскладки и монитор сервера кэшируются вместе с приложением
        assert "'/js/settings_layout.js'" in sw
        assert f"server_monitor.js?v={ver}" in read("app/static/js/app.js")

    def test_changelog_and_whatsnew(self):
        ch = read("CHANGELOG.md")
        js = read("app/static/js/app.js")
        assert ch.count("## [1.72.37]") == 1
        assert ch.index("## [1.72.37]") < ch.index("## [1.72.36]")
        assert js.count("'1.72.37':") == 1
        assert js.index("'1.72.37':") < js.index("'1.72.36':")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        for block in ("Адаптивный интерфейс", "Настройки", "Резервные копии",
                      "Последние рабочие версии", "Обновления"):
            assert tuple(int(x) for x in reg[block].split(".")) >= (1, 72, 37), block
        # блоки, которых правка не касалась, не поднимались
        assert tuple(int(x) for x in reg["Проверка чеков (ФНС и источники)"].split(".")) < (1, 72, 37)


class TestSettingsTabsJs:
    js = read("app/static/js/app.js")
    module = read("app/static/js/settings_layout.js")

    def test_tabs_markup(self):
        # полоса табов, панели разделов и меню «⋯ Ещё» — внутри #settings-nav
        i = self.js.index('<nav class="settings-nav settings-tabs" id="settings-nav"')
        j = self.js.index("</nav>", i)
        nav = self.js[i:j]
        assert 'class="stg-tabs-bar"' in nav
        assert 'role="tablist"' in nav
        assert 'role="tab"' in nav and 'class="stg-tab" id="stg-tab-${g.key}" data-sec="${g.key}"' in nav
        assert 'class="stg-tabs-row"' in nav
        assert 'id="snav-more"' in nav and 'id="snav-more-menu"' in nav
        assert 'id="snav-all"' in nav and 'id="snav-collapse"' in nav
        assert 'class="stg-panel" id="stg-panel-${g.key}" data-sec="${g.key}" role="tabpanel"' in nav
        # таб без карточек не рисуется: пустой раздел не нужен
        assert "tab.hidden = !has" in self.js

    def test_old_vertical_menu_removed(self):
        # разметка, CSS и помощники вертикального меню удалены, а не спрятаны
        for gone in ("buildSettingsJump", "snav-jump", "jumpModel", "jumpValue",
                     "parseJumpValue", "snav-group", "snav-head", "snav-sub",
                     "data-grp=", "matchMedia('(max-width: 900px)')"):
            assert gone not in self.js, gone
            assert gone not in self.module, gone

    def test_apply_and_route_sync(self):
        assert "function settingsApply" in self.js or "const settingsApply = (nav, cur) => {" in self.js
        assert "const active = allMode ? '' : tabForRoute(model, cur.group || hashSec);" in self.js
        assert "match(/^#\\/settings\\/([a-z]+)/)" in self.js
        # один раздел за раз; в режиме «все настройки» — все подряд
        assert "sec.hidden = !allMode && !!active && sec.getAttribute('data-sec') !== active;" in self.js
        # адрес обновляется кликом по табу, без перезагрузки
        assert "history.replaceState(null, '', '#/settings/' + key)" in self.js
        # переключение таба поднимает прокручиваемую область .content
        assert "const scrollSettingsTop = () => {" in self.js
        assert "box.scrollTo({ top: 0, behavior: 'smooth' })" in self.js

    def test_section_titles_and_reapply(self):
        # после сборки разделов видимость пересчитывается (разделов ещё нет
        # в момент первой подсветки табов)
        i = self.js.index("applySettingsLayout(container);")
        tail = self.js[i:i + 400]
        assert "if (snavFinal) settingsApply(snavFinal, _cur);" in tail
        assert "class=\"stg-section-title\"" in self.js or "head.className = 'stg-section-title'" in self.js

    def test_module_exports(self):
        for fn in ("export function tabsModel", "export function tabForRoute",
                   "export function maskToken", "export function layoutPlan"):
            assert fn in self.module, fn
        assert "document." not in self.module and "window." not in self.module


class TestSettingsTokensJs:
    js = read("app/static/js/app.js")

    def test_max_token_mask_not_in_label(self):
        start = self.js.index('id="st-max"')
        card = self.js[start:start + 6000]
        # маска — отдельным полем, а не внутри подписи <label class="field">
        assert '<code class="token-value"' in card
        assert "maxSet.token_masked" in card
        assert "(задан: " not in card
        assert 'class="token-field"' in card

    def test_copy_buttons(self):
        assert 'data-copy-target="#onec-token"' in self.js
        assert "container.querySelectorAll('[data-copy-target]')" in self.js
        assert "copyCmd(el.value || el.textContent || '', btn)" in self.js
        # «показать» у токена 1С остался
        assert 'id="onec-reveal"' in self.js

    def test_clients_tables_have_scroll_and_dates(self):
        # таблицы клиентов и ключей живут в .table-scroll и печатают дату через fmtDate
        assert '<div class="table-scroll">' in self.js
        assert "const dt = (v) => (v ? fmtDate(v) : '—');" in self.js
        start = self.js.index("const tbl = (head, rows) =>")
        seg = self.js[start:start + 4000]
        assert "esc(dt(p.created_at))" in seg
        assert "esc(dt(l.at))" in seg
        assert "esc(dt(k.last_used_at))" in seg
        assert 'class="td-k"' in seg and 'class="td-v"' in seg

    def test_checkbox_rows(self):
        # инлайновый flex у чекбокса заменён классом: подпись не наезжает
        assert 'class="check-row"' in self.js
        assert 'style="display:flex;gap:10px;align-items:center;cursor:pointer"' not in self.js
        # двойного атрибута class в разметке нет
        assert 'class="field full" class=' not in self.js


class TestSettingsTabsCss:
    css = read("app/static/css/app.css")

    def test_single_column(self):
        assert "max-width: 900px" in self.css
        assert ".settings-grid.settings-grouped {" in self.css
        i = self.css.index(".settings-grid.settings-grouped {")
        block = self.css[i:i + 220]
        assert "flex-direction: column" in block
        assert "grid-template-columns" not in block
        # раздел не шире сетки: min-width: 0 обязателен (таблица тянула на 1609 px)
        assert ".stg-section,\n.stg-section-grid {\n  min-width: 0; width: 100%;\n}" in self.css
        assert "@container (min-width: 1200px)" not in self.css

    def test_tabs_styles(self):
        assert ".stg-tabs-bar {" in self.css and "flex-wrap: wrap" in self.css
        assert ".stg-tabs-row {" in self.css and "overflow-x: auto" in self.css
        assert "scrollbar-width: none;" in self.css
        assert "-webkit-overflow-scrolling: touch;" in self.css
        assert ".stg-tab.active {" in self.css and "border-bottom-color: var(--accent);" in self.css
        assert ".stg-tab.active .stg-tab-ico { display: inline; }" in self.css
        assert ".stg-tab-ico { display: none;" in self.css
        assert ".stg-panel {" in self.css and "position: absolute;" in self.css
        assert "#settings-nav { container-type: inline-size;" in self.css
        # липнет к верху прокручиваемой области, а не к --topbar-h
        assert "position: sticky; top: 0;" in self.css
        assert "@media (max-width: 768px)" in self.css

    def test_tokens_and_overflow_rules(self):
        assert ".token-field {" in self.css and ".token-value {" in self.css
        i = self.css.index(".token-value {")
        block = self.css[i:i + 400]
        for frag in ("overflow: hidden;", "text-overflow: ellipsis;", "white-space: nowrap;", "min-width: 0;"):
            assert frag in block, frag
        assert ".check-row {" in self.css and "min-height: 44px" in self.css
        assert ".field.check-row {" in self.css
        assert ".settings-layout :is(.btn, .btn-sm) { min-height: 44px; }" in self.css
        assert ".settings-layout .btn-icon { width: 44px; height: 44px; }" in self.css
        assert "scroll-margin-top: calc(var(--topbar-h) + 64px);" in self.css

    def test_no_duplicate_or_broken_declarations(self):
        # опечатка из черновика: два объявления подряд, одно битое
        assert "gird-template-columns" not in self.css
        assert ".snav-group" not in self.css
        assert ".snav-jump" not in self.css
        # палитра не тронута
        assert "--accent:" in self.css


class TestDocsUpdated:
    def test_styleguide(self):
        doc = read("docs/styleguide.md")
        assert "### 8.2 Сетка настроек (v1.72.37)" in doc
        assert "### 8.4 Табы настроек и токены (v1.72.37)" in doc
        assert ".stg-tab[data-sec]" in doc and ".token-value" in doc

    def test_readme_and_kb(self):
        assert "v1.72.37" in read("README.md")
        assert "1.72.37" in read("docs/kb/README.md")
        csv = read("docs/kb/09_releases.csv")
        assert "1.72.37," in csv

    def test_ui_journal(self):
        j = read("docs/ui_journal.md")
        assert "## v1.72.37" in j
        # исторические инварианты журнала
        assert "1.4.11" in j and "pointer: coarse" in j
