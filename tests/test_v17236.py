# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.72.36: раздел «Настройки».
#
# Замок на адаптивный рефакторинг страницы (без изменения функций):
#  • карточки собраны в разделы, «широкие» карточки занимают всю строку;
#  • таблицы копий и версий живут в .table-scroll с фиксированными ширинами;
#  • «Резервные копии» разделены на зоны «настройка → действия → данные»,
#    длинный абзац больше не является flex-элементом ряда кнопок;
#  • меню настроек — список с активным пунктом; на узких экранах — селект;
#  • кнопки имеют состояния aria-busy / is-ok / is-err;
#  • выпуск синхронизирован (версия, sw, index, manifest, блоки, CHANGELOG).
#
# Поведение раскладки проверяют tests/js/settings_layout.test.js (Vitest) и
# tests/e2e/settings_layout.spec.js (Playwright, реальные ширины экрана).
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================

from app.config import settings


def read(path):
    return open(path, encoding="utf-8").read()


class TestVersion17236:
    def test_config_version(self):
        # v1.72.37: строгий пин ушёл вперёд — здесь остаётся «не ниже»
        assert tuple(int(x) for x in settings.APP_VERSION.split(".")) >= (1, 72, 36)

    def test_static_versions_synced(self):
        ver = settings.APP_VERSION
        sw = read("app/static/sw.js")
        idx = read("app/static/index.html")
        mx = read("app/static/maxapp/index.html")
        mf = read("app/static/manifest.webmanifest")
        assert f"ymaster-check-v{ver}" in sw
        assert f"/css/app.css?v={ver}" in sw and f"/js/app.js?v={ver}" in sw
        assert "'/js/settings_layout.js'" in sw
        assert f"/css/app.css?v={ver}" in idx and f"/js/app.js?v={ver}" in idx
        assert f"/maxapp/maxapp.js?v={ver}" in mx
        assert f'"version": "{ver}"' in mf
        # модуль раскладки обновляется вместе с кэшем SW (как api.js/ui.js —
        # без ?v=: версия задаётся именем кэша)
        assert "from './settings_layout.js';" in read("app/static/js/app.js")

    def test_changelog_and_whatsnew(self):
        ch = read("CHANGELOG.md")
        js = read("app/static/js/app.js")
        assert ch.count("## [1.72.36]") == 1
        assert ch.index("## [1.72.36]") < ch.index("## [1.72.35]")
        assert js.count("'1.72.36':") == 1
        assert js.index("'1.72.36':") < js.index("'1.72.35':")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        for block in ("Адаптивный интерфейс", "Настройки", "Резервные копии",
                      "Последние рабочие версии", "Обновления"):
            assert tuple(int(x) for x in reg[block].split(".")) >= (1, 72, 36), block
        # блоки, которых правка не касалась, не поднимались
        assert tuple(int(x) for x in reg["Проверка чеков (ФНС и источники)"].split(".")) < (1, 72, 36)


class TestSettingsLayoutJs:
    js = read("app/static/js/app.js")
    module = read("app/static/js/settings_layout.js")
    osm = read("app/static/js/server_monitor.js")

    def test_layout_module_is_pure_and_wired(self):
        # v1.72.37: селект-навигация (jumpModel/jumpValue/parseJumpValue) удалена,
        # её место заняли горизонтальные табы и маска токена
        for fn in ("export function layoutPlan", "export function tabsModel",
                   "export function tabForRoute", "export function maskToken",
                   "export function pageSlice", "export function orderOf"):
            assert fn in self.module, fn
        # модуль без DOM и без побочных эффектов: только экспорт и константы
        assert "document." not in self.module
        assert "window." not in self.module

    def test_apply_layout_runs_for_settings(self):
        assert "applySettingsLayout(container);" in self.js
        assert "makeSettingsCollapsible(container);" in self.js
        # вызов раскладки идёт ПОСЛЕ сворачиваемых блоков: к этому моменту
        # тело карточки уже обёрнуто в .fold-body
        assert self.js.index("makeSettingsCollapsible(container);") < \
            self.js.index("applySettingsLayout(container);")
        # карточки собираются в разделы, а не пересоздаются
        assert "createElement('section')" in self.js
        assert "stg-section-grid" in self.js
        assert "grid.classList.add('settings-grouped')" in self.js

    def test_wide_cards_marked(self):
        assert 'id="st-backups" data-sec="data" style="order:10" data-wide' in self.js
        assert 'id="st-releases" data-sec="data" style="order:12" data-wide' in self.js
        assert 'id="st-server-monitor" data-sec="integr" style="order:38" data-wide' in self.osm

    def test_tabs_nav_for_narrow_screens(self):
        # v1.72.37: вместо селекта «Перейти к настройке…» — горизонтальные табы.
        # Селект и его помощник удалены целиком (не спрятаны в CSS).
        assert "buildSettingsJump" not in self.js
        assert 'id="snav-jump"' not in self.js
        assert "parseJumpValue" not in self.js
        # таб = раздел; активный подсвечен и назван вспомогательным технологиям
        assert 'role="tab"' in self.js and 'class="stg-tab"' in self.js
        assert "tab.setAttribute('aria-selected', on ? 'true' : 'false')" in self.js
        # пункт панели таба помечается aria-current
        assert "b.setAttribute('aria-current', 'true')" in self.js
        # адрес синхронизирован с табом, глубокий переход открывает раздел
        assert "history.replaceState(null, '', '#/settings/' + key)" in self.js
        assert "tabForRoute(model," in self.js and "maskToken" not in self.js

    def test_backups_card_zones(self):
        start = self.js.index('id="st-backups"')
        card = self.js[start:start + 2600]
        for frag in ('class="card-section"', 'class="card-section-title"',
                     'class="card-actions"', 'class="card-note"',
                     'id="bk-kind"', 'id="btn-bk-create"', 'id="btn-bk-refresh"',
                     'id="btn-bk-import"', 'id="bk-list"'):
            assert frag in card, frag
        # порядок зон: настройка → действия → подпись → данные
        assert card.index("card-section") < card.index("card-actions")
        assert card.index("card-actions") < card.index("card-note")
        assert card.index("card-note") < card.index('id="bk-list"')
        # прежний дефект: длинный абзац был flex-элементом ряда кнопок
        assert 'style="margin:8px 0 0">Автоматически' not in self.js
        # длинное пояснение убрано в сворачиваемый блок
        assert "<details class=\"card-note\">" in card
        # главное действие — единственный Primary-акцент карточки
        assert 'class="btn btn-sm btn-primary" id="btn-bk-create"' in card
        assert "btn-primary\" data-restore" not in self.js

    def test_backups_table_in_scroll_container(self):
        assert '<div class="table-scroll bk-table-scroll">' in self.js
        assert 'class="data settings-table settings-cards-table bk-table"' in self.js
        assert '<colgroup>' in self.js
        assert 'data-label="Копия"' in self.js
        assert 'data-label="Размер"' in self.js
        # длинное имя не рвёт строку: полное имя — в подсказке
        assert 'title="${esc(b.name)}"' in self.js
        # порция строк в карточке и весь архив — в отдельном окне
        assert "pageSlice(visible, bkLimit())" in self.js
        assert "id=\"bk-more\"" in self.js
        assert "modal.slot.classList.add('modal-wide')" in self.js

    def test_release_table_in_scroll_container(self):
        assert "'<div class=\"table-scroll\">'" in self.js
        assert 'class="data settings-table settings-cards-table rb-table"' in self.js
        assert 'data-label="Версия"' in self.js
        # старая таблица без контейнера и с инлайновым min-width удалена
        assert '<table class="data rb-table" style="min-width:0">' not in self.js

    def test_button_states(self):
        assert "function setBtnBusy" in self.js
        assert "function flashBtn" in self.js
        assert "setBtnBusy(bcr, true)" in self.js          # создать копию
        assert "setBtnBusy(btn, true)" in self.js          # проверить / восстановить
        assert "setBtnBusy(rf, true)" in self.js           # обновить список версий
        assert "setBtnBusy(go, true)" in self.js           # откат к коммиту
        assert "setBtnBusy(bc, true)" in self.js           # проверить обновления
        # повторное нажатие блокируется на время работы, подпись не подменяется
        assert "btn.setAttribute('aria-busy', 'true')" in self.js
        assert "btn.removeAttribute('aria-busy')" in self.js


class TestSettingsLayoutCss:
    css = read("app/static/css/app.css")

    def test_sections_and_spans(self):
        # v1.72.37: многоколоночных сеток нет вообще — раздел и его карточки
        # идут одной колонкой на любой ширине окна (ТЗ владельца).
        assert ".stg-section-grid {" in self.css
        assert ".settings-section-grid" not in self.css        # переименовано: замок v1709
        assert ".settings-grid.settings-grouped {" in self.css
        assert "display: flex; flex-direction: column;" in self.css
        assert "grid-auto-flow: row dense;" not in self.css
        # ни двух-, ни трёхколоночной сетки именно в каркасе настроек
        start = self.css.index(".settings-grid.settings-grouped {")
        assert "grid-template-columns" not in self.css[start:start + 220]
        assert "@container (min-width: 1200px)" not in self.css
        # раздел не может быть шире сетки (иначе таблица тянула его на 1609 px)
        assert ".stg-section," in self.css and "min-width: 0; width: 100%;" in self.css
        # широкие карточки помечены — пригодится для рамок/полей
        assert ".stg-section-grid > .card-wide" in self.css
        assert ".stg-section-grid > .glass.card[data-wide]" in self.css

    def test_old_locks_kept(self):
        # правила, на которые опираются тесты прошлых выпусков, не удалены
        # v1.72.37: колонка меню стала полосой табов, но каркас страницы тот же
        assert ".settings-layout {" in self.css
        assert "flex-direction: column" in self.css
        assert "max-width: 1160px" in self.css      # полоса табов шире контента
        assert "body.settings-all .settings-nav { display: none;" not in self.css
        assert "@media (max-width: 900px)" in self.css
        assert "@media (max-width: 768px)" in self.css
        assert "@media (max-width: 640px)" in self.css

    def test_tables(self):
        assert ".table-scroll {" in self.css and "overflow: auto;" in self.css
        assert "table.data.settings-table { width: 100%; table-layout: fixed; min-width: 0; }" in self.css
        assert ".bk-table-scroll { max-height: 360px; }" in self.css
        assert "text-overflow: ellipsis;" in self.css
        # телефон: строки-карточки с подписями полей — реальный .td-k,
        # псевдоэлементы content: attr(data-label) проектом запрещены
        assert "table.data.settings-cards-table .td-k {" in self.css
        assert "content: attr(data-label)" not in self.css
        assert '<span class="td-k">Копия</span>' in read("app/static/js/app.js")
        assert '.settings-table .td-k { display: none; }' in self.css
        # мобильное правило таблиц (display:block, min-width 880) не должно
        # действовать на настроечные таблицы
        assert "table.data.settings-cards-table {\n    display: block;\n    min-width: 0;" in self.css

    def test_nav_and_select(self):
        # положение: липкая полоса табов, активный таб — акцентная линия снизу
        assert ".settings-nav {" in self.css and "position: sticky;" in self.css
        assert ".stg-tab.active {" in self.css and "border-bottom-color: var(--accent);" in self.css
        # панель таба — абсолютный блок с той же тенью, что у карточек
        assert ".stg-panel {" in self.css and "position: absolute;" in self.css
        assert "box-shadow: var(--shadow);" in self.css
        # селект «Перейти к настройке…» и вертикальные группы удалены
        assert ".snav-jump" not in self.css
        assert ".snav-group" not in self.css
        assert ".snav-head" not in self.css

    def test_button_states(self):
        assert '.btn[aria-busy="true"] {' in self.css
        assert '.btn[aria-busy="true"]::after {' in self.css
        assert ".btn.is-ok {" in self.css and ".btn.is-err {" in self.css
        assert ".modal-slot.modal-wide {" in self.css

    def test_card_contract(self):
        for frag in (".card-section-title {", ".card-actions {", ".card-note {",
                     ".card-foot {", "--card-pad: 24px;"):
            assert frag in self.css, frag


class TestStyleguideUpdated:
    def test_ui_kit_section(self):
        doc = read("docs/styleguide.md")
        assert "## 8. Карточка, таблица, кнопки и состояния (v1.72.36)" in doc
        assert "Figma" in doc and "Storybook" in doc
        assert "aria-busy" in doc
        # v1.72.37: одноколоночный вид — новая таблица ширин (§8.2)
        assert "### 8.2 Сетка настроек (v1.72.37)" in doc
