# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.51.0: скан-механизм чека во всех местах сдачи
# (камера/фото/перетаскивание на «Сдать чек», сдача из кабинета пула).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1510:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.51.0 перенесён в tests/test_v1520.py (версия ушла вперёд)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.50.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.51.0':" in js
        assert js.index("'1.51.0':") < js.index("'1.50.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.51.0]") == 1
        assert ch.index("## [1.51.0]") < ch.index("## [1.50.0]")


class TestBlocksRegistry:
    def test_scanner_and_pool_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        # структурные пины: блоки не старее этих версий (точные перенесены в test_v1520)
        vt = lambda v: tuple(int(x) for x in v.split("."))
        assert vt(reg["Сканирование чеков"]) >= (1, 51, 0)
        assert vt(reg["Чек-Пул"]) >= (1, 51, 0)
        assert vt(reg["Обновления"]) >= (1, 50, 0)
        assert vt(reg["Резервные копии"]) >= (1, 49, 0)


class TestScannerEverywhere:
    def test_pool_camera_scanner_helper(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # единый хелпер живой камеры на базе scanner.js
        assert "function openPoolCameraScan(onText)" in js
        assert "poolcam-video" in js and "poolcam-start" in js
        assert "new CameraScanner(" in js.split("function openPoolCameraScan")[1]
        # чужой QR (не чек) игнорируется
        chunk = js.split("function openPoolCameraScan")[1].split("function publicFormHTML")[0]
        assert "parseQrClient(" in chunk and "if (!p) return;" in chunk
        # камера останавливается при закрытии окна
        assert "cam.stop()" in chunk

    def test_pool_form_has_all_scan_ways(self):
        js = open("app/static/js/app.js").read()
        # кнопка камеры в форме сдачи
        assert 'id="pub-cam"' in js
        # общий декодер фото + перетаскивание снимка на форму
        assert "const decodeToQr = async (f)" in js
        assert "root.addEventListener('drop'" in js
        assert "root.addEventListener('dragover'" in js
        # распознанный QR попадает в форму с подсказкой реквизитов
        assert "QR отсканирован камерой: ФН " in js
        # сигнатура с колбэком завершения
        assert "function bindPoolForm(root, opts)" in js
        assert "opts.onDone" in js

    def test_pool_dashboard_submit(self):
        js = open("app/static/js/app.js").read()
        # кнопка сдачи прямо в кабинете участника
        assert 'id="pool-submit-open"' in js
        assert "Сдать чек сканером" in js
        # открывается та же форма и тот же сканер
        assert "bindPoolForm(box, { onDone:" in js
        # после сдачи кабинет обновляет баллы и список чеков
        chunk = js.split("bindPoolForm(box, { onDone:")[1][:400]
        assert "poolLoadSummary(root)" in chunk
        assert "poolLoadReceipts(root, 1)" in chunk
        # в модалке кабинета лишние блоки формы скрыты
        assert "leaders.remove()" in js and "mine.remove()" in js

    def test_guest_screen_uses_same_form(self):
        js = open("app/static/js/app.js").read()
        # гостевой экран и раздел приложения рендерят одну и ту же форму
        assert js.count("publicFormHTML()") >= 3   # helper/guest/app/cabinet
        assert "bindPoolForm(root)" in js or "bindPoolForm(root);" in js

    def test_scan_view_unaffected(self):
        js = open("app/static/js/app.js").read()
        # основной раздел «Сканирование» не тронут: камера, очередь, недавние
        assert "new CameraScanner(video, (text) => handleScannedText" in js
        assert "id=\"scan-video\"" in js or "'scan-video'" in js
        assert "renderRecents()" in js
