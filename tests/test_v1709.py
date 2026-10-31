# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.9: страница настроек — иерархическое меню
# (5 разделов, подменю по карточкам, deep-link #/settings/<раздел>/<id>,
# кнопка «Все настройки», адаптив ПК/планшет/телефон).
# ======================================================================
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------- версии
class TestVersions1709:
    def test_config_version(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.70.10: строгий замок версии переехал в test_v17010
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 9)

    def test_static_versions_synced(self):
        # v1.70.10: проверка синхронности версий — генерическая (по config),
        # чтобы не релаксировать её при каждом релизе
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        sw = open("app/static/sw.js", encoding="utf-8").read()
        idx = open("app/static/index.html", encoding="utf-8").read()
        mx = open("app/static/maxapp/index.html", encoding="utf-8").read()
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw
        assert f"/css/app.css?v={ver}" in sw and f"/js/app.js?v={ver}" in sw
        assert f"/css/app.css?v={ver}" in idx and f"/js/app.js?v={ver}" in idx
        assert f"/maxapp/maxapp.js?v={ver}" in mx
        assert f'"version": "{ver}"' in mf

    def test_changelog_and_whatsnew(self):
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert ch.count("## [1.70.9]") == 1
        assert ch.index("## [1.70.9]") < ch.index("## [1.70.8]")
        assert js.index("'1.70.9':") < js.index("'1.70.8':")


# ---------------------------------------------------------------- структура меню
class TestSettingsMenuStructure:
    def js(self):
        return open("app/static/js/app.js", encoding="utf-8").read()

    def osm_js(self):
        """Разметка карточки «Мониторинг OpenStack» (v1.70.51).

        У карточки четыре вкладки, графики и журнал, поэтому её разметка
        вынесена из app.js в отдельный модуль osm.js — тот же приём, что
        для графиков в charts.js и печати в printpack.js.
        """
        return open("app/static/js/osm.js", encoding="utf-8").read()

    def settings_js(self):
        """Вся разметка раздела настроек: app.js и osm.js.

        Замок обязан видеть обе части: иначе раздел считается по 30
        карточкам, а 31-я остаётся без присмотра, и молча суженный замок
        хуже сломанного — регрессию в новой карточке он уже не поймает.
        """
        return self.js() + "\n" + self.osm_js()

    def test_five_groups(self):
        js = self.js()
        for key in ("data", "checks", "integr", "personal", "device"):
            assert f"key: '{key}'" in js, key
            assert f'data-grp="${{g.key}}"' in js  # шаблон группы
        assert "SETTINGS_GROUPS" in js

    def test_all_cards_have_id_and_sec(self):
        js = self.js()
        # область шаблона настроек
        i0 = js.index("async function viewSettings(container)")
        i1 = js.index("settings-layout")
        # v1.70.51: карточка «Мониторинг OpenStack» живёт в osm.js, поэтому
        # к шаблону настроек добавлен весь модуль — иначе 31-я карточка
        # осталась бы за пределами замка.
        seg = js[i0:i0 + 60000] + "\n" + self.osm_js()
        cards = re.findall(
            r'<div class="glass card"(?: id="([\w-]+)")?'
            r'(?: data-sec="(\w+)")? style="order:(\d+)', seg)
        with_sec = [(cid, sec, order) for cid, sec, order in cards if sec]
        # v1.70.28: карточек 27 — у AdGuard VPN появился собственный блок
        # st-adguard в разделе «Устройство и служебное».
        # v1.70.37: карточек 28 — добавлен блок st-consent «Согласие 152-ФЗ:
        # данные участников из MAX» в разделе «Интеграции» (реквизиты оператора
        # для текста согласия, который показывается в мини-приложении MAX).
        # v1.70.50: карточек 30 — по требованию владельца в разделе «Данные»
        # рядом с блоком «Сервер и команды» появились st-clients «Клиенты
        # приложения» (порядок 14) и st-transfer «Передача данных и сеть»
        # (порядок 15); обе только для администратора и на одном запросе.
        # v1.70.51: карточек 31 — по ТЗ docs/Monitor.md в разделе «Чек-Пул и
        # интеграции» добавлен блок st-openstack «Мониторинг OpenStack»
        # (порядок 39, только для администратора). Подключение к внешнему
        # облаку по API с параметрами Keystone и кнопкой «Проверить
        # подключение» структурно повторяет st-checko, st-onec и
        # st-webhooks, поэтому раздел выбран integr, а не device или data.
        assert len(with_sec) == 31, with_sec
        assert ("st-consent", "integr", "38") in with_sec
        assert ("st-adguard", "device", "49") in with_sec
        assert ("st-clients", "data", "14") in with_sec
        assert ("st-transfer", "data", "15") in with_sec
        assert ("st-openstack", "integr", "39") in with_sec
        assert all(cid and cid.startswith(("st-", "wa-", "mail-"))
                   for cid, _s, _o in with_sec)
        secs = {s for _c, s, _o in with_sec}
        assert secs == {"data", "checks", "integr", "personal", "device"}

    def test_expected_distribution(self):
        # v1.70.28: device 4 -> 5 (блок AdGuard VPN); из карточки
        # Telegram-бота он удалён, но сама карточка st-tgbot осталась в integr.
        # v1.70.37: integr 6 -> 7 — карточка реквизитов оператора в согласии.
        # v1.70.50: data 4 -> 6 — «Клиенты приложения» и «Передача данных
        # и сеть» встали в раздел «Данные» рядом с «Сервер и команды».
        # v1.70.51: integr 7 -> 8 — блок «Мониторинг OpenStack». Считаем по
        # app.js и osm.js вместе: разметку новой карточки рисует osm.js.
        expect = {"data": 6, "checks": 6, "integr": 8, "personal": 6,
                  "device": 5}
        js_all = self.settings_js()
        for sec, n in expect.items():
            assert f'data-sec="{sec}"' in js_all
            assert js_all.count(f'data-sec="{sec}"') == n, sec

    def test_old_flat_sections_removed(self):
        js = self.js()
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "settings-sect" not in js
        assert "settings-sect" not in css

    def test_menu_wiring(self):
        js = self.js()
        for frag in ("settingsApply", "snav-all", "snav-collapse",
                     "history.replaceState", "settings_all",
                     "scrollIntoView", "snav-item", "snav-sub",
                     "matchMedia('(max-width: 900px)')"):
            assert frag in js, frag

    def test_deeplink_route_parses(self):
        js = self.js()
        # routeParam → {group, card}; прямая ссылка собирается из key и id
        assert "state.routeParam" in js
        assert "'#/settings/' + key + '/' + id" in js


# ---------------------------------------------------------------- CSS/адаптив
class TestSettingsMenuCss:
    def test_layout_and_responsive(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".settings-layout {" in css
        assert "grid-template-columns: 268px minmax(0, 1fr)" in css
        # v1.70.10: прячутся только группы, панель с кнопками остаётся видимой
        assert "body.settings-all .settings-nav .snav-group { display: none;" in css
        assert "body.settings-all .settings-nav { display: none;" not in css
        assert "@media (max-width: 900px)" in css
        assert ".settings-nav {" in css and "position: sticky" in css
        assert ".snav-group {" in css and ".snav-item.active" in css
        # после 900px — одна колонка (телефон: меню над карточками)
        i = css.index("@media (max-width: 900px)")
        frag = css[i:i + 400]
        assert ".settings-layout { grid-template-columns: 1fr; }" in frag, frag[:200]
        assert ".settings-nav { position: static; }" in frag

    def test_scroll_margin_for_sticky_topbar(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "scroll-margin-top: calc(var(--topbar-h) + 14px)" in css


# ---------------------------------------------------------------- SW/инструкция/статика
class TestSwAndManual:
    def test_sw_precaches_manual_image(self):
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "'/img/manual/settings.jpg'" in sw

    def test_manual_section_present(self):
        src = open("app/services/manual_content.py", encoding="utf-8").read()
        assert '"id": "settings-guide"' in src
        assert '"/img/manual/settings.jpg"' in src
        assert "Настройки: меню и порядок" in src
        # старая подсказка про «сворачиваются» обновлена
        assert "собраны в меню с разделами" in src

    def test_static_image_served(self):
        with TestClient(app) as c:
            r = c.get("/img/manual/settings.jpg")
            assert r.status_code == 200
            assert r.content[:2] == b"\xff\xd8"        # JPEG-сигнатура
            assert len(r.content) > 30000              # не заглушка
            assert r.headers["content-type"].startswith("image/jpeg")

    def test_image_aspect_16x9(self):
        # размеры проверяем структурой JPEG без Pillow: парсим SOF0/SOF2
        data = open("app/static/img/manual/settings.jpg", "rb").read()
        i = 2
        w = h = None
        while i < len(data) - 9:
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h = data[i + 5] * 256 + data[i + 6]
                w = data[i + 7] * 256 + data[i + 8]
                break
            seg = data[i + 2] * 256 + data[i + 3]
            i += 2 + seg
        assert w and h, "не удалось прочитать размеры JPEG"
        ratio = w / h
        assert 1.7 <= ratio <= 1.8, (w, h)


# ---------------------------------------------------------------- харнесс
class TestHarness1709:
    def test_harness_check_added(self):
        h = open("tests/ui_harness.js", encoding="utf-8").read()
        assert "v1.70.9: настройки — меню с разделами и подменю" in h
        assert h.index("v1.70.9") < h.index("HARNESS")
