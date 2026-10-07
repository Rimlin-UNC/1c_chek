# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.52.0: полный набор способов сдачи чека везде
# (камера, фото, перетаскивание, строка QR + новый резервный ручной ввод).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1520:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.52.0"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.51.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.52.0':" in js
        assert js.index("'1.52.0':") < js.index("'1.51.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.52.0]") == 1
        assert ch.index("## [1.52.0]") < ch.index("## [1.51.0]")


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Сканирование чеков"] == "1.52.0"
        assert reg["Чек-Пул"] == "1.52.0"
        assert reg["Обновления"] == "1.50.0"     # не задеты


class TestFullScanCoverage:
    def test_manual_dialog_helper(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function poolManualDialog(onQr)" in js
        for fid in ("pm-date", "pm-sum", "pm-op", "pm-fn", "pm-fd", "pm-fp"):
            assert f'"{fid}"' in js, fid
        # собирает стандартную строку QR (формат 54-ФЗ, как у ФНС)
        assert "t=${m[3]}${m[2]}${m[1]}T${m[4]}${m[5]}" in js
        assert "&s=${sum.toFixed(2)}" in js
        assert "&fn=${fn}&i=${fd}&fp=${fp}&n=" in js
        # валидация форматов до сборки
        assert "/^(\\d{2})\\.(\\d{2})\\.(\\d{4})\\s+(\\d{2}):(\\d{2})$/" in js
        assert "/^\\d{8,20}$/.test(fn)" in js

    def test_manual_wired_into_pool_form(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert 'id="pub-manual"' in js and "Ввести вручную" in js
        assert "poolManualDialog((qr)" in js
        # результат попадает в поле QR формы — дальше обычная отправка
        chunk = js.split("poolManualDialog((qr)")[1][:200]
        assert "$p('pub-qr').value = qr;" in chunk

    def test_coverage_map(self):
        """Каждое место ввода чека — полный набор способов."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # 1) раздел «Сканирование» (корпоративные, все роли)
        assert "id=\"btn-camera-start\"" in js and "id=\"btn-manual\"" in js
        assert "id=\"dropzone\"" in js and "id=\"btn-paste\"" in js
        assert "new CameraScanner(video, (text) => handleScannedText" in js
        # 2) страница «Сдать чек» (гости + приложение)
        assert 'id="pub-cam"' in js and 'id="pub-manual"' in js
        assert 'id="pub-photo"' in js and 'id="pub-qr"' in js
        assert "root.addEventListener('drop'" in js
        assert "function openPoolCameraScan(onText)" in js
        assert "function poolManualDialog(onQr)" in js
        # 3) кабинет «Мой Чек-Пул» — та же форма в модалке
        assert 'id="pool-submit-open"' in js
        assert "bindPoolForm(box, { onDone:" in js

    def test_scan_view_manual_unaffected(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # корпоративный ручной ввод по-прежнему идёт в свой эндпоинт
        assert "function manualDialog()" in js
        assert "'/api/v1/receipts/manual'" in js
        # пуловый ручной ввод НЕ ходит в корпоративный эндпоинт
        chunk = js.split("function poolManualDialog")[1].split("function publicFormHTML")[0]
        assert "receipts/manual" not in chunk
        assert "poolApi" not in chunk
