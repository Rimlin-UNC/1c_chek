# ======================================================================
# Ямастер Чек — тесты v1.25.0: единая светлая тема.
# Переключатели оформления удалены, карточка чека/тосты/подсказки —
# светлые с тёмным текстом; «светлое на светлом» исключено.
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
CSS = "app/static/css/app.css"
JS = "app/static/js/app.js"
IDX = "app/static/index.html"


class TestLightOnly:
    def test_root_is_light_palette(self):
        css = open(CSS, encoding="utf-8").read()
        assert "color-scheme: light" in css
        i0 = css.index(":root {")
        root = css[i0:css.index("}", i0)]
        for needle in ("--bg: #f5f5f5", "--panel: #ffffff", "--text: #333333",
                       "--accent: #ff7a00"):
            assert needle in root, needle

    def test_no_theme_switching_left(self):
        css = open(CSS, encoding="utf-8").read()
        js = open(JS, encoding="utf-8").read()
        idx = open(IDX, encoding="utf-8").read()
        assert "data-theme" not in css and "data-theme" not in idx
        assert "applyTheme" not in js and "ymaster-theme" not in js
        assert "theme-seg" not in js and "data-accent" not in js
        assert "data-density" not in js and "ymaster-accent" not in js
        assert "ymaster-density" not in js
        assert "prefers-color-scheme" not in css   # авто-режима больше нет

    def test_settings_appearance_cards_removed(self):
        js = open(JS, encoding="utf-8").read()
        assert "🎨 Оформление" not in js           # карточки v1.7.0 и v1.20.0
        assert "accent-row" not in js and "density-seg" not in js
        # «Спокойный час» сохранён (управление уведомлениями)
        assert "🔕 Уведомления" in js
        assert "btn-quiet" in js and "quiet-dur" in js and "quiet-status" in js
        assert "bindAppearance" in js and "renderAppearanceState" in js

    def test_drawer_receipt_card_light(self):
        css = open(CSS, encoding="utf-8").read()
        i0 = css.index(".drawer {")
        seg = css[i0:css.index("}", i0)]
        assert "background: #ffffff" in seg        # было rgba(13,18,38,.97)

    def test_toasts_light(self):
        css = open(CSS, encoding="utf-8").read()
        i0 = css.index(".toast {")
        seg = css[i0:css.index("}", i0)]
        assert "background: #ffffff" in seg        # было rgba(17,23,48,.92)
        assert "color: #333333" in seg
        assert "border-left-color: var(--ok)" in css
        assert "border-left-color: var(--bad)" in css
        assert "border-left-color: var(--warn)" in css

    def test_popups_and_hints_light(self):
        css = open(CSS, encoding="utf-8").read()
        # подсказка графика
        i0 = css.index(".chart-tooltip {")
        assert "background: #ffffff" in css[i0:css.index("}", i0)]
        # модальное подложка светлая (мягкий сланец, не чёрный)
        assert "rgba(15, 23, 42, .45)" in css
        # поля ввода белые
        assert "background: #ffffff; color: var(--text)" in css
        # шапки таблиц светлые
        assert "background: rgba(245, 245, 245, .96)" in css
        # QR-блок и код-блоки светлые
        assert "background: #f6f6f7" in css
        # сплэш-экран в app.js — светлый
        js = open(JS, encoding="utf-8").read()
        assert "background:#f5f5f5" in js

    def test_no_light_on_light(self):
        """Скандинавский аудит: цветных «пар» светлый-на-светлом быть не должно."""
        css = open(CSS, encoding="utf-8").read()
        # белый текст допустим ТОЛЬКО на градиенте/акценте/графите/терракоте
        import re
        for m in re.finditer(r"color:\s*#fff\b", css):
            ctx = css[max(0, m.start() - 220):m.start()]
            allowed = any(k in ctx for k in (
                "--grad", "--accent", "#333333", "--bad", "btn-danger",
                "btn-primary", "nav-badge", "step-num", "toastIn"))
            assert allowed, f"белый текст на неизвестном фоне: …{ctx[-120:]!r}"

    def test_index_meta_static(self):
        idx = open(IDX, encoding="utf-8").read()
        assert '<meta name="theme-color" content="#f5f5f5">' in idx
        # скрипт предустановки темы удалён
        assert "ymaster-theme" not in idx and "data-theme" not in idx
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert '"theme_color": "#ff7a00"' in mf


class TestVersion1250:
    def test_versions_synced(self):
        import re
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # актуальный пин — в тесте текущей версии (v1.25.1)
        idx = open(IDX, encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog_manual(self):
        js = open(JS, encoding="utf-8").read()
        assert "'1.25.0':" in js
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert "## [1.25.0]" in ch
        manual = open("app/services/manual_content.py", encoding="utf-8").read()
        assert "Один стиль для всех" in manual
        assert '"title": "Внешний вид"' in manual
