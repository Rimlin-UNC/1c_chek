# Ямастер Чек — тесты v1.26.1: фирменный стиль «чек-смайлик».
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
#
# Проверяем:
#  - система логотипа в brand/ (знак, горизонтальный, вертикальный, монохром, бланк А4, иконка с доменом);
#  - знак действительно «смайлик» (глаза-QR + улыбка) и фирменные цвета;
#  - иконка приложения содержит домен chek.ymaster.ru;
#  - статика приложения переведена на новый знак;
#  - версии синхронны (пин текущей версии — здесь).
import re
import struct

BRAND = [
    "brand/logo-mark.svg",
    "brand/logo-horizontal.svg",
    "brand/logo-stacked.svg",
    "brand/logo-mono.svg",
    "brand/logo-mono-white.svg",
    "brand/icon-app.svg",
    "brand/blank-a4.svg",
]


def _png_size(path: str):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path}: не PNG"
    w, h = struct.unpack(">II", head[16:24])
    return w, h


class TestBrandSystem:
    def test_brand_files_exist_and_vendor(self):
        for p in BRAND:
            s = open(p, encoding="utf-8").read()
            assert s.lstrip().startswith("<svg"), p
            assert "Ямастер" in s and "ymaster.ru" in s, p  # заголовок с разработчиком

    def test_mark_is_smiley_with_brand_colors(self):
        s = open("brand/logo-mark.svg", encoding="utf-8").read()
        # улыбка «всё сошлось» (фирменный оранжевый)
        assert "q53 56" in s and "#FF7A00" in s
        # глаза-QR (два уголка наведения)
        assert s.count('fill-rule="evenodd"') >= 2
        # плитка в фирменном градиенте
        assert "#6366F1" in s and "#8B5CF6" in s and "#22D3EE" in s

    def test_icon_app_contains_domain(self):
        s = open("brand/icon-app.svg", encoding="utf-8").read()
        assert "chek.ymaster.ru" in s
        assert "q53 56" in s  # тот же знак-смайлик

    def test_logo_versions_contain_domain(self):
        for p in ("brand/logo-horizontal.svg", "brand/logo-stacked.svg"):
            s = open(p, encoding="utf-8").read()
            assert "chek.ymaster.ru" in s, p
            assert "Ямастер Чек" in s, p

    def test_mono_versions(self):
        dark = open("brand/logo-mono.svg", encoding="utf-8").read()
        white = open("brand/logo-mono-white.svg", encoding="utf-8").read()
        assert "#0E2A47" in dark and "#FF7A00" not in dark   # ч/б тёмная
        assert white.count('#FFFFFF') >= 4 and "#FF7A00" not in white  # белая контурная

    def test_blank_a4(self):
        s = open("brand/blank-a4.svg", encoding="utf-8").read()
        assert 'viewBox="0 0 2100 2970"' in s  # пропорции A4
        assert "chek.ymaster.ru" in s and "ООО «Ямастер»" in s

    def test_png_sets_rendered(self):
        assert _png_size("brand/png/icon-app-512.png") == (512, 512)
        assert _png_size("brand/png/icon-app-192.png") == (192, 192)
        assert _png_size("brand/png/mark-1024.png") == (1024, 1024)
        assert _png_size("brand/png/favicon-32.png") == (32, 32)


class TestStaticSwitchedToNewBrand:
    def test_app_logo_is_smiley(self):
        s = open("app/static/img/logo.svg", encoding="utf-8").read()
        assert "q53 56" in s and "#FF7A00" in s
        assert "Ямастер" in s

    def test_app_icons_replaced(self):
        assert _png_size("app/static/img/icon-512.png") == (512, 512)
        assert _png_size("app/static/img/icon-192.png") == (192, 192)
        assert _png_size("app/static/img/icon-64.png") == (64, 64)

    def test_manifest_icons_point_to_files(self):
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert "/img/icon-192.png" in mf and "/img/icon-512.png" in mf


class TestVersion1261:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.26.1"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.26.1':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.26.1]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Фирменный стиль (v1.26.1)" in manual
