# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.2: вид «Список/Карточки» на странице «Чеки»,
# кнопки внизу карточек, московское время журнала, перенос текста кнопок,
# оранжевые кнопки «Компаний», ровная сетка «Настроек».
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import logging
import re


class TestVersion1582:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.58.2"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.58.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.58.2':") < js.index("'1.58.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.58.2]") == 1
        assert ch.index("## [1.58.2]") < ch.index("## [1.58.1]")

    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Адаптивный интерфейс"] == "1.58.2"
        assert reg["Проверка чеков (ФНС и источники)"] == "1.58.0"  # не задет
        assert reg["Чек-Пул"] == "1.56.1"
        assert reg["Обновления"] == "1.55.1"


class TestListCardsToggle:
    """Переключатель «Список / Карточки» на странице «Чеки»."""

    def test_toggle_buttons_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="rc-view-list"' in js and 'id="rc-view-cards"' in js
        assert "📋 Список" in js and "🗂 Карточки" in js

    def test_choice_persisted(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "localStorage.getItem('ym_rc_view')" in js
        assert "localStorage.setItem('ym_rc_view'" in js
        assert "receiptsView: 'list'," in js

    def test_cards_render_branch(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "state.receiptsView === 'cards'" in js
        assert 'rc-cards' in js and "receiptCard(r, acc)" in js
        # клик по карточке открывает чек, клик по кнопке — не мешает
        assert "if (e.target.closest('button')) return;" in js

    def test_card_actions_at_bottom(self):
        """Кнопки запроса данных и ручного заполнения — ВНИЗУ карточки."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        i = js.find("function receiptCard")
        assert i > 0
        body = js[i:js.find("}", js.find("rc-actions", i))]
        # в карточке есть обе кнопки/действия
        assert "r-fetch" in body and "data-act=\"edit\"" in body
        # rc-actions — последняя секция карточки (внизу)
        assert body.index("rc-actions") > body.index("rc-chips")
        assert "rc-actions" in body
        # «Заполнить» подписан текстом, а не только иконкой
        assert "Заполнить</button>" in body

    def test_css_card_actions_bottom(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".rc-actions { display: flex; flex-wrap: wrap; gap: 8px;" in css
        assert "margin-top: auto; }" in css          # прижаты к низу карточки
        # мобильные карточки: кнопки внизу с разделителем
        assert "#receipts-table .data td.cell-actions {" in css
        assert "border-top: 1px dashed var(--border);" in css

    def test_toggle_style(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".seg { display: inline-flex;" in css
        assert ".seg .btn.active { background: var(--accent); color: #fff; }" in css


class TestMskTime:
    """Журнал и процесс — по Московскому времени."""

    def test_msk_formatter_installed(self):
        src = open("app/main.py", encoding="utf-8").read()
        assert "_MskFormatter" in src
        assert 'ZoneInfo("Europe/Moscow")' in src
        assert 'os.environ["TZ"] = "Europe/Moscow"' in src
        assert "tzset()" in src
        # форматтер установлен на корневые обработчики
        assert "for _h in logging.getLogger().handlers:" in src

    def test_formatter_outputs_msk_offset(self):
        import datetime
        from app.main import _MSK, _MskFormatter
        rec = logging.LogRecord("ymaster", logging.INFO, __file__, 1,
                                "тест", None, None)
        s = _MskFormatter().formatTime(rec)
        assert s.endswith("+0300"), s
        msk_now = datetime.datetime.now(_MSK)
        assert s.startswith(msk_now.strftime("%Y-%m-%d %H:")), s

    def test_process_tz_is_msk(self):
        import time
        import datetime
        from zoneinfo import ZoneInfo
        # процесс переведён в МСК: локальное время совпадает с MSK
        assert time.tzname is not None
        loc = datetime.datetime.now()
        msk = datetime.datetime.now(
            ZoneInfo("Europe/Moscow")).replace(tzinfo=None)  # настенные часы
        assert abs((loc - msk).total_seconds()) < 2


class TestButtonAndOverflow:
    """Текст кнопок не выходит за границы; оранжевые кнопки «Компаний»."""

    def test_btn_text_wraps(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "white-space: normal; overflow-wrap: anywhere; max-width: 100%;" \
            in css
        assert "user-select: none; white-space: nowrap;" not in css

    def test_mapping_and_similar_safe(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="m-add">+ Добавить правило</button>' in js  # кнопка на месте

    def test_companies_primary_orange(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'class="btn btn-sm btn-primary" id="cp-bulk-refresh"' in js
        assert 'class="btn btn-sm btn-primary" id="cp-ao-all"' in js
        assert 'class="btn btn-primary btn-sm" id="cp-add"' in js   # был оранжевой

    def test_overflow_guards(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".glass.card { min-width: 0; max-width: 100%; }" in css
        assert ".card-title, .modal-title, h1, h2, h3 { overflow-wrap: anywhere; }" in css
        assert ".form-hint { overflow-wrap: anywhere; }" in css

    def test_settings_grid_even(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ("repeat(auto-fit, minmax(min(340px, 100%), 1fr))" in css)
        assert "@media (max-width: 640px) { .settings-grid { grid-template-columns: 1fr; } }" in css


class TestRowModeUntouched:
    def test_table_mode_intact(self):
        """Режим «Список» сохранён целиком (строки, выбор, pager)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert '<table class="data"><thead><tr>' in js
        assert 'id="sel-all"' in js and "receiptRow(r)" in js
        assert 'id="pg-prev"' in js
        # подписи полей мобильных карточек не потеряны
        assert 'data-label="Дата чека"' in js
