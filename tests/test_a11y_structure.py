# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.58.5 (часть 3): доступность и структура UI.
# Практики WCAG/верстальщиков: язык страницы, viewport, подписи
# иконочных кнопок (aria-label), альтернативы изображениям, поддержка
# prefers-reduced-motion, читаемый базовый контраст темы.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestDocumentStructure:
    def test_lang_and_viewport(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert re.search(r'<html[^>]*lang="ru"', idx), "нет lang=ru у <html>"
        assert re.search(r'<meta name="viewport"[^>]*width=device-width', idx), \
            "нет viewport для мобильных"

    def test_icon_buttons_have_accessible_names(self):
        """Иконочные кнопки обязаны иметь подпись для скринридера."""
        idx = open("app/static/index.html", encoding="utf-8").read()
        for btn_id in ("sb-close", "btn-sidebar"):
            m = re.search(rf'<button[^>]*id="{btn_id}"[^>]*>', idx)
            assert m, f"кнопка {btn_id} не найдена"
            assert "aria-label=" in m.group(0), f"{btn_id}: нет aria-label"

    def test_images_have_alt(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        for m in re.finditer(r"<img[^>]*>", idx):
            assert "alt=" in m.group(0), f"img без alt: {m.group(0)[:80]}"


class TestMotionAndContrast:
    def test_reduced_motion_supported(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "@media (prefers-reduced-motion: reduce)" in css

    def test_text_colors_are_readable(self):
        """Базовые цвета темы проходят контраст WCAG AA для обычного текста."""
        css = open("app/static/css/app.css", encoding="utf-8").read()

        def _lum(hexc):
            hexc = hexc.lstrip("#")
            r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))
            lin = lambda c: c / 12.92 if c <= 0.03928 else \
                ((c + 0.055) / 1.055) ** 2.4
            return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

        def _contrast(a, b):
            l1, l2 = sorted((_lum(a), _lum(b)), reverse=True)
            return (l1 + 0.05) / (l2 + 0.05)

        text = re.search(r"--text:\s*(#[0-9a-fA-F]{6})", css).group(1)
        bg = re.search(r"--panel:\s*(#[0-9a-fA-F]{6})", css).group(1)
        assert _contrast(text, bg) >= 4.5, \
            f"контраст текста {text} на {bg} ниже WCAG AA"

    def test_touch_targets_previous_release(self):
        """Тач-цели ≥44px не регрессируют (закрепление v1.58.1)."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".btn { min-height: 44px; }" in css


class TestSemantics:
    def test_theme_color_pwa(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert re.search(r'<meta name="theme-color"', idx), \
            "PWA без theme-color — адресная строка не в фирменном цвете"
