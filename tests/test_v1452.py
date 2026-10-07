# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.45.2: неубиваемая главная (слоистый рендер с
# запасными вариантами), Cache-Control на оболочке, устойчивый
# SEO-пререндер (тег body с атрибутами).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1452:
    def test_versions_synced(self):
        # точный пин 1.45.2 перенесён в tests/test_v1453.py (версия ушла вперёд)
        cfg = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "' in cfg
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "?v=1.45.1" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.45.2':" in js
        assert js.index("'1.45.2':") < js.index("'1.45.1':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.45.2]") == 1
        assert ch.index("## [1.45.2]") < ch.index("## [1.45.1]")


class TestUnbreakableLanding:
    def _src(self):
        return open("app/static/js/app.js", encoding="utf-8").read()

    def test_layered_render_chain(self):
        js = self._src()
        # три слоя: богатый → упрощённый → чистая форма
        assert "function landHeroHTML()" in js
        assert "function landSimpleHTML()" in js
        i = js.index("function showPublicScreen() {")
        body = js[i:js.index("hideSeo()", i)]
        assert "root.innerHTML = landHeroHTML();" in body
        assert "root.innerHTML = landSimpleHTML()" in body
        assert "publicFormHTML() + '</div>'" in body
        # оба слоя подстрахованы, причина пишется в консоль
        assert body.count("console.error('[landing]") >= 2
        assert "богатая версия не отрисовалась" in body

    def test_bindings_guarded(self):
        js = self._src()
        i = js.index("function showPublicScreen() {")
        body = js[i:js.index("hideSeo()", i)]
        assert "try { bindPoolForm(root); } catch" in body
        assert "try { bindPoolAccount(root); } catch" in body
        # живой стат и якоря — только у богатой версии
        assert "if (rich) root.querySelectorAll('a[data-scroll]')" in body
        assert "if (rich) {" in body

    def test_fallback_keeps_form_and_auth(self):
        js = self._src()
        i = js.index("function landSimpleHTML() {")
        body = js[i:js.index("function showPublicScreen() {", i)]
        # в упрощённом слое остаются форма проверки, похвала и кабинет
        assert "${publicFormHTML()}" in body
        assert "${poolAuthHTML()}" in body
        assert 'id="pub-praise"' in body
        assert 'id="land-to-app"' in body


class TestHtmlCacheAndInject:
    def test_index_no_cache(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"

    def test_seo_inject_body_with_attrs(self):
        from app.main import _seo_inject
        raw = ("<!doctype html><html><head><title>Ямастер Чек</title>"
               '<meta name="description" content="x"></head>'
               '<body class="theme-light" data-x="1">'
               '<div id="app"></div></body></html>')
        out = _seo_inject(raw)
        assert out.count("seo-injected") == 1
        assert '<body class="theme-light" data-x="1"><!-- seo-injected -->' in out
        assert "Проверить чек онлайн по QR-коду" in out
        assert "seo-landing" in out

    def test_seo_inject_without_body_tag(self):
        from app.main import _seo_inject
        raw = "<html><head><title>Ямастер Чек</title></head></html>"
        out = _seo_inject(raw)
        assert out.count("seo-injected") == 1
        assert "</head><!-- seo-injected -->" in out

    def test_served_index_has_prerender(self, client):
        html = client.get("/").text
        assert "seo-injected" in html
        assert "Проверить чек онлайн по QR-коду" in html
