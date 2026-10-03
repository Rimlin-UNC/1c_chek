# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.12.2: ровная вёрстка на всех устройствах.
# РЕГРЕССИЯ: селектор компаний в шапке выдавливал заголовок на телефоне,
# а несоответствие HTML/CSS/JS после обновлений давало «кривую» вёрстку.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re

CSS = open("app/static/css/app.css", encoding="utf-8").read()
HTML = open("app/static/index.html", encoding="utf-8").read()
SW = open("app/static/sw.js", encoding="utf-8").read()


def _app_version() -> str:
    cfg = open("app/config.py", encoding="utf-8").read()
    return re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)


class TestTopbarLayout:
    def test_title_never_wraps(self):
        """Заголовок в шапке: одна строка с «…» — правый блок не выдавливается."""
        for sel in (".page-title",):
            m = re.search(re.escape(sel) + r"\s*\{[^}]*\}", CSS)
            assert m, f"{sel} не найден"
            body = m.group(0)
            assert "text-overflow: ellipsis" in body
            assert "white-space: nowrap" in body
            assert "min-width: 0" in body

    def test_company_select_compact_on_phone(self):
        m = re.search(r"@media \(max-width: 860px\)\s*\{[^@]*?\.company-select[^}]*\}",
                      CSS, re.S)
        assert m, "мобильное правило .company-select не найдено"
        assert "44vw" in m.group(0)
        # и на совсем узких — ещё компактнее
        m2 = re.search(r"@media \(max-width: 400px\)\s*\{[^@]*?\.company-select[^}]*\}",
                       CSS, re.S)
        assert m2 and "38vw" in m2.group(0)

    def test_css_balanced(self):
        nc = re.sub(r'/\*.*?\*/', '', CSS, flags=re.S)
        assert nc.count('{') == nc.count('}')
        assert nc.rstrip().endswith('}')

    def test_users_group_filter_fullwidth_on_phone(self):
        assert "#p-company-filter { flex: 1 1 100%; min-width: 0; }" in CSS


class TestAssetVersionPinning:
    def test_html_links_carry_version(self):
        ver = _app_version()
        assert f'/css/app.css?v={ver}' in HTML, "CSS без маркера версии"
        assert f'/js/app.js?v={ver}' in HTML, "точка входа JS без маркера версии"

    def test_sw_precache_matches_html(self):
        ver = _app_version()
        assert f"ymaster-check-v{ver}" in SW, "кэш SW не переименован"
        assert f"'/css/app.css?v={ver}'" in SW
        assert f"'/js/app.js?v={ver}'" in SW
