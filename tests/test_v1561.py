# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.56.1: img display:block + компенсации,
# rem-типографика лендинга/входа, доказательство наличия land-стилей.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re


class TestVersion1561:
    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # точный пин 1.56.1 перенесён в tests/test_v1562.py (версия ушла вперёд)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 56, 1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.56.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.56.1':") < js.index("'1.56.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.56.1]") == 1
        assert ch.index("## [1.56.1]") < ch.index("## [1.56.0]")


class TestBlockImages:
    def test_img_rule_has_display_block(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "img { max-width: 100%; height: auto; display: block; }" in css

    def test_centered_logos_compensated(self):
        """Логотип входа центрировался через text-align родителя — для
        block-картинки нужен явный auto-отступ."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        blk = css.split(".login-logo img {")[1][:200]
        assert "margin-inline: auto" in blk

    def test_print_qr_compensated(self):
        """QR в печатных окнах центрировался text-align — нужен margin."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.count("margin:0 auto") >= 2
        assert 'width="320" height="320" style="margin:0 auto">' in js
        assert 'style="width:26mm;height:26mm;margin:0 auto"' in js

    def test_flex_containers_unaffected(self):
        """Сайдбар-бренд и splash — flex: block-картинка не меняет раскладку."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        brand = css.split(".sidebar-brand {")[1][:120]
        assert "display: flex" in brand


class TestLandingRem:
    def test_landing_typography_in_rem(self):
        """Типографика лендинга задана в rem на трёх breakpoint'ах.

        v1.70.35: значение 1.5625rem было МЁРТВЫМ — его глушил соседний блок с
        тем же max-width: 920px, где .land-h1 задан как 1.375rem. То есть тест
        фиксировал размер, который никогда не применялся. Два соседних блока
        одного breakpoint'а слиты в один, действующая шкала сохранена.
        """
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "font-size: 1.9375rem" in css          # .land-h1 базовый
        assert "font-size: 1.375rem" in css           # .land-h1 ≤920 (v1.55)
        assert "font-size: 1.25rem" in css            # .land-h1 ≤520
        assert "font-size: 0.96875rem" in css         # .land-lead
        assert "font-size: 1.875rem" in css           # .login-logo h1
        # мёртвый дубль не вернулся; смотрим на код без комментариев, иначе
        # проверка находит собственное объяснение правки
        code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        assert "1.5625rem" not in code
        # breakpoint лендинга объявлен ровно один раз: два соседних блока с
        # одинаковым max-width и породили мёртвое значение
        assert css.count("@media (max-width: 920px) {") == 1
        # соизмеримо: rem-объявлений на лендинге/входе достаточно много
        assert css.count("rem;") >= 22

    def test_inputs_stay_px(self):
        """Поля ввода — жёсткие 16px: если пользователь уменьшит базовый
        шрифт, rem опустился бы ниже порога зума iOS."""
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert "input, select, textarea { font-size: 16px; }" in css


class TestLandingStylesExist:
    """Аудит-инструменты, обрезающие файл (~64 КБ), не видят хвост app.css,
    где живут land-стили. Тест фиксирует их наличие целиком."""

    def test_land_rules_present(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert css.count("land-") >= 45
        for rule in ("#login-screen .land-wrap {", ".land-badge {",
                     ".land-h1 {", ".land-lead {", ".land-stats {",
                     ".land-cta {", ".land-steps {", ".land-lead-form {",
                     ".land-faq details {", ".land-note {"):
            assert rule in css, rule

    def test_land_responsive_present(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        m920 = css.split("@media (max-width: 920px)")[1]
        assert "grid-template-columns: 1fr" in m920
        m520 = css.split("@media (max-width: 520px)")[1]
        assert "flex: 1 1 100%" in m520               # CTA во всю ширину
        assert "justify-self: center" in css          # карточка по центру

    def test_landing_markup_intact(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        for cls in ("land-wrap", "land-left", "land-right", "land-badge",
                    "land-h1", "land-lead", "land-stats", "land-cta",
                    "land-steps", "land-lead-form", "land-faq", "land-note"):
            assert f'"{cls}' in idx or cls in idx, cls


class TestBlocksRegistry:
    def test_blocks_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Чек-Пул"] == "1.56.1"                # лендинг
        assert tuple(int(x) for x in
                     reg["Адаптивный интерфейс"].split(".")) >= (1, 56, 1)  # структурный   # img/rem
        vt = lambda v: tuple(int(x) for x in v.split("."))
        assert vt(reg["Обновления"]) >= (1, 55, 1)       # не задет
        assert vt(reg["Проверка чеков (ФНС и источники)"]) >= (1, 55, 0)
