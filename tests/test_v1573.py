# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.57.3: в карточке чека всегда есть путь к
# данным — блок «Полные данные» ИЛИ кнопка получения, плюс обновление.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1573:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.57.3 перенесён в tests/test_v1574.py (версия ушла вперёд)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 57, 3)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.57.2" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.57.3':") < js.index("'1.57.2':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.57.3]") == 1
        assert ch.index("## [1.57.3]") < ch.index("## [1.57.2]")


class TestDataZoneAlwaysAvailable:
    def test_no_dead_end_without_ext(self):
        """Нет ext → в карточке кнопка «Получить данные» (не пустота)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "Полные данные чека ещё не получены" in js
        assert 'id="er-fetch"' in js

    def test_refetch_with_ext(self):
        """Есть ext → кнопка «Обновить данные из источника»."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="er-refetch"' in js
        assert "ваши правки не затрутся" in js

    def test_fetch_calls_per_receipt_endpoint(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "api.post(`/api/v1/receipts/${r.id}/fetch-details?force=1`)" in js
        # после успеха карточка переоткрывается свежими данными
        assert "openEditReceipt(fresh, onSaved)" in js
        assert "Запрашиваю (пауза 2–7 с)" in js

    def test_ext_block_still_first_choice(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "${extBlockHTML(r.ext) ||" in js
        assert "function extBlockHTML(ext)" in js

    def test_list_fetch_hidden_for_full_data_is_fine(self):
        """В списке 📥 по-прежнему скрыт у full_data — тупик закрыт
        кнопкой в карточке (это осознанное решение, не регресс)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "r.full_data ? '' :" in js


class TestEndpointExists:
    def test_fetch_details_endpoint_present(self):
        src = open("app/routers/receipts.py", encoding="utf-8").read()
        assert '"/{receipt_id}/fetch-details"' in src
        assert "receipt.ext_json = json.dumps(res.extra" in src


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        # точный пин 1.57.3 перенесён в tests/test_v1574.py
        assert tuple(int(x) for x in
                     reg["Проверка чеков (ФНС и источники)"].split(".")) >= (1, 57, 3)
        assert reg["Чек-Пул"] == "1.56.1"                 # не задет
        assert reg["Сканирование чеков"] == "1.56.2"      # не задет
