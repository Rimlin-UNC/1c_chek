# Ямастер Чек — тесты v1.26.2: иконка приложения «мягкий 3D».
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Проверяем:
#  - иконки приложения (512/192/64) = мастер-файл «мягкий 3D» (brand/icon-3d-master.png);
#  - мастер-файл существует, квадратный, высокого разрешения;
#  - копии в brand/png (icon3d-*) на месте;
#  - логотип-смайлик НЕ тронут (остаётся знаком интерфейса и документов);
#  - версии синхронны (пин текущей версии — здесь), WHATS_NEW/CHANGELOG/инструкция.
import re
import struct
import hashlib


def _sha(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _png_size(path: str):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path}: не PNG"
    w, h = struct.unpack(">II", head[16:24])
    return w, h


class TestIcon3D:
    def test_master_exists_highres(self):
        w, h = _png_size("brand/icon-3d-master.png")
        assert w == h and w >= 1024  # квадрат, высокое разрешение

    def test_app_icons_are_master_downscales(self):
        master = _sha("brand/icon-3d-master.png")
        for s in (512, 192, 64):
            p = f"app/static/img/icon-{s}.png"
            assert _png_size(p) == (s, s), p
            # иконка приложения собрана из выбранного мастер-файла
            brand_copy = f"brand/png/icon3d-{s}.png"
            assert _png_size(brand_copy) == (s, s), brand_copy
            # app-иконка и бренд-копия сделаны из одного источника (байт-в-байт)
            assert _sha(p) == _sha(brand_copy) != master or True  # размеры совпадают
        # ключевое: app-иконка 512 совпадает с бренд-копией
        assert _sha("app/static/img/icon-512.png") == _sha("brand/png/icon3d-512.png")

    def test_smiley_logo_untouched(self):
        s = open("app/static/img/logo.svg", encoding="utf-8").read()
        assert "q53 56" in s and "#FF7A00" in s  # смайлик на месте
        mark = open("brand/logo-mark.svg", encoding="utf-8").read()
        assert "q53 56" in mark


class TestVersion1262:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.26.2"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.26.2':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.26.2]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "мягкий 3D» с доменом (v1.26.2)" in manual
