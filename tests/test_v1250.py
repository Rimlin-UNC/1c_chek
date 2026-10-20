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
        # v1.70.35: тёмный текст тоста теперь через токен --text (тот же
        # #333333). Намерение прежнее: тост светлый, текст на нём тёмный.
        assert "color: var(--text)" in seg
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
        """Скандинавский аудит: цветных «пар» светлый-на-светлом быть не должно.

        v1.70.34: прежняя версия разрешала белый текст по СПИСКУ СОСЕДНИХ СЛОВ
        («--grad», «--accent», «#333333», «btn-primary», «nav-badge»,
        «step-num»…), то есть закрепляла ровно тот дефект, который должна была
        ловить: белый на фирменном оранжевом даёт 2.11–2.61:1, а графитовая
        .btn-primary была остатком отменённой тёмной темы. Теперь контраст
        каждой пары считается из токенов и обязан быть ≥ 4.5:1 (WCAG AA).
        """
        import re
        css = open(CSS, encoding="utf-8").read()
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        tokens = dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", body))

        def _lum(hx):
            hx = hx.lstrip("#")
            ch = []
            for i in (0, 2, 4):
                c = int(hx[i:i + 2], 16) / 255
                ch.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
            return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

        def _ratio(a, b):
            la, lb = _lum(a), _lum(b)
            hi, lo = max(la, lb), min(la, lb)
            return (hi + 0.05) / (lo + 0.05)

        grad_stops = ("#ff7a00", "#ff9a3d")   # --grad 135deg, видимая часть
        pairs = 0
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
            sel, dec = m.group(1).strip(), m.group(2)
            if not re.search(r"color:\s*#fff\b|color:\s*#ffffff\b", dec):
                continue
            bg = re.search(r"(?<![-\w])background(?:-color)?\s*:\s*([^;]+)", dec)
            assert bg, f"{sel}: белый текст без фона в том же правиле — не проверить"
            backs = list(grad_stops) if "--grad" in bg.group(1) else []
            for t in re.findall(r"var\((--[\w-]+)\)", bg.group(1)):
                assert t in tokens, f"{sel}: фон ссылается на необъявленный {t}"
                backs.append(tokens[t])
            backs += re.findall(r"#[0-9a-fA-F]{6}", bg.group(1))
            for b in backs:
                pairs += 1
                r = _ratio("#ffffff", b)
                assert r >= 4.5, f"{sel}: белый текст на {b} — {r:.2f}:1, нужно ≥ 4.5:1"
        assert pairs >= 1, "белых текстов не найдено вовсе — проверка выродилась"

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
