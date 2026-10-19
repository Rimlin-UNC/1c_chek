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

    #: текстовые токены темы и фоны, на которых они встречаются
    TEXT_TOKENS = ("--text", "--text-dim", "--text-faint", "--accent-text",
                   "--ok-text", "--bad-text", "--warn-text", "--info-text",
                   "--accent-2")
    BG_TOKENS = ("--panel", "--bg", "--bg-soft")

    def test_text_colors_are_readable(self):
        """Все текстовые токены темы проходят контраст WCAG AA (4.5:1).

        v1.70.32: раньше проверялась только пара --text/--panel (12.63:1), и
        потому тест не видел ни одного реального дефекта: фирменные
        --ok/--bad/--warn/--info и --accent использовались как цвет мелкого
        текста (11-13 px) с контрастом 2.36-4.25, а .form-error показывал
        причину неудачного входа при 1.69:1. Теперь пересчитывается контраст
        каждого текстового токена на каждом фоне темы — заявление «читаемость
        проверена на контраст (WCAG AA)» в шапке app.css стало проверяемым.
        """
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

        def _tok(name):
            m = re.search(re.escape(name) + r":\s*(#[0-9a-fA-F]{6})", css)
            assert m, f"в app.css нет токена {name}"
            return m.group(1)

        worst = None
        for t in self.TEXT_TOKENS:
            for b in self.BG_TOKENS:
                r = _contrast(_tok(t), _tok(b))
                if worst is None or r < worst[0]:
                    worst = (r, t, b)
                assert r >= 4.5, \
                    f"контраст {t} {_tok(t)} на {b} {_tok(b)} = {r:.2f} — ниже WCAG AA"
        # самый тёмный фон темы — --bg-soft: на нём запас меньше всего
        assert worst[2] == "--bg-soft", worst

    def test_semantic_colors_are_not_used_as_small_text(self):
        """Фирменные --ok/--bad/--warn/--info/--accent — не цвет мелкого текста.

        Они дают 2.21-4.25:1 на фонах темы: годятся для границ, подложек и
        крупного текста (порог 3:1), но не для подписей 11-13 px. Для текста
        есть отдельные токены --*-text.
        """
        css = open("app/static/css/app.css", encoding="utf-8").read()
        for base in ("--ok", "--bad", "--warn", "--info", "--accent"):
            pat = re.compile(r"(?<![-\w])color\s*:\s*var\(" +
                             re.escape(base) + r"\)")
            hits = pat.findall(css)
            assert not hits, \
                f"{base} снова использован как цвет текста — нужен {base}-text"

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
