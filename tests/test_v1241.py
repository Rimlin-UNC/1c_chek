# ======================================================================
# Ямастер Чек — тесты v1.24.1: чеки печатаются С фискальным QR-кодом.
# Дожидаемся загрузки изображений до диалога печати, QR без лимита 60,
# прогресс загрузки, пометка «фискальный QR не получен».
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
JS = "app/static/js/app.js"


class TestQRPrint:
    def _fn_segment(self, js: str) -> str:
        i0 = js.index("async function printReceiptsPDF")
        return js[i0:js.index("\nfunction ", i0 + 10)]

    def test_waits_for_images_before_print(self):
        js = open(JS, encoding="utf-8").read()
        assert "function whenImagesReady(root)" in js
        seg = self._fn_segment(js)
        # ожидание картинок — ДО window.print() и до тоста
        assert "await whenImagesReady(root);" in seg
        assert seg.index("whenImagesReady(root);") < seg.index("window.print();")
        # страховка по таймауту внутри хелпера
        assert "setTimeout(done" in js

    def test_qr_without_60_limit(self):
        js = open(JS, encoding="utf-8").read()
        assert "slice(0, 60)" not in js                     # старый лимит убран
        seg = self._fn_segment(js)
        assert "qrList = data.items.slice(0, 300)" in seg   # страховка от гигантских выборок
        assert "Загружаю фискальные QR: ${i + 1} из ${qrList.length}" in seg

    def test_qr_fail_marker(self):
        js = open(JS, encoding="utf-8").read()
        assert "фискальный QR не получен" in js
        assert "rcptBlocks(r, qrUrl, qrFail)" in js.replace('(r, qrUrl, qrFail) {', '(r, qrUrl, qrFail)')
        # пометка только когда реквизиты есть, а код не получен
        assert "qrFail && r.fn" in js

    def test_qr_count_in_toast_and_status(self):
        js = open(JS, encoding="utf-8").read()
        seg = self._fn_segment(js)
        assert "QR на ${withQr}" in seg
        assert 'id="pp-status"' in js

    def test_modal_title_renamed(self):
        js = open(JS, encoding="utf-8").read()
        assert '<div class="modal-title">🖨 Напечатать чек</div>' in js
        assert 'modal-title">🖨 Печать чеков' not in js


class TestVersion1241:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # актуальный пин — в тесте текущей версии (v1.25.0)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_and_changelog(self):
        js = open(JS, encoding="utf-8").read()
        assert "'1.24.1':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.24.1]" in ch
