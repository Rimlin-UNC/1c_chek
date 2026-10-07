# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.45.1: приветственная главная (форма входа на
# странице, живой стат, FAQ, иллюстрации с подстраховкой), страховка
# запуска (SEO-пререндер убирается только после успешного рендера),
# устойчивая установка сервис-воркера.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1451:
    def test_versions_synced(self):
        # точный пин 1.45.1 перенесён в tests/test_v1452.py (версия ушла вперёд)
        cfg = open("app/config.py", encoding="utf-8").read()
        assert 'APP_VERSION: str = "' in cfg
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert "?v=1.45.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert "ymaster-check-v" in sw and "?v=" in sw

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "'1.45.1':" in js
        assert js.index("'1.45.1':") < js.index("'1.45.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.45.1]") == 1
        assert ch.index("## [1.45.1]") < ch.index("## [1.45.0]")
        assert "hideSeo" in ch and "addAll" in ch


class TestBootSafety:
    def test_seo_removed_only_after_render(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        # единственное место удаления пререндера — hideSeo()
        assert js.count("function hideSeo()") == 1
        assert "if (seo) seo.remove();" in js
        # успешные экраны убирают пререндер
        for fn in ("function showPublicScreen() {", "function showPoolScreen() {",
                   "function showLogin() {", "function showRegister("):
            i = js.index(fn)
            nxt = ("function showPublicScreen() {", "function showPoolScreen() {",
                   "function showLogin() {", "function showRegister(")
            ends = [js.index(x) for x in nxt if js.index(x) > i]
            body = js[i:min(ends) if ends else len(js)]
            assert "hideSeo()" in body, fn
        # boot-маршрутизация под страховкой: при сбое — рабочий экран входа
        assert "console.error('boot:', e)" in js
        m = re.search(r"try \{[\s\S]*?showPublicScreen\(\); return; \}[\s\S]*?catch \(e\) \{", js)
        assert m, "маршрутизация boot без try/catch"
        catch_tail = js[m.end():m.end() + 200]
        assert "showLogin();" in catch_tail

    def test_sw_install_resilient(self):
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert ".addAll(" not in sw                     # падучий путь убран (в т.ч. в коде)
        assert "c.add(u).catch(() => {})" in sw         # поштучная установка
        # иллюстрации главной в предзагрузке
        assert "'/img/manual/landing-hero.jpg'" in sw
        assert "'/img/manual/landing-bonus.jpg'" in sw


class TestWelcomeLanding:
    def test_login_form_on_main(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        i = js.index("function landHeroHTML() {")
        body = js[i:js.index("// v1.31.0: тот же приём", i)]
        # форма входа/регистрации кабинета прямо на главной
        assert "${poolAuthHTML()}" in body
        assert "bindPoolAccount(root);" in body
        # pf-login/pl-email/ptab-reg живут в poolAuthHTML — проверяем глобально
        for probe in ("pf-login", "pl-email", "ptab-reg"):
            assert probe in js, probe
        for probe in ("land-auth",
                      "land-to-app", "Сотрудник компании? Войти в рабочую программу"):
            assert probe in body, probe
        # проверка чека + похвала остаются на главной
        assert "${publicFormHTML()}" in body and "pub-praise" in body

    def test_landing_rich_content(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        i = js.index("function landHeroHTML() {")
        body = js[i:js.index("// v1.31.0: тот же приём", i)]
        # живой стат базы, FAQ, футер, плавные якоря
        assert "land-stat" in body and "/api/v1/public/pool/info" in body
        assert "В базе уже" in body and "land-faq" in body
        assert "Частые вопросы" in body
        assert "land-foot" in body and "ООО «Ямастер»" in body
        assert "data-scroll" in body and "scrollIntoView" in body
        # обе иллюстрации с alt и подстраховкой onerror
        assert 'src="/img/manual/landing-hero.jpg"' in body
        assert 'src="/img/manual/landing-bonus.jpg"' in body
        assert body.count("onerror=") >= 2
        assert "alt=" in body

    def test_landing_css(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        for cls in (".land {", ".land-hero {", ".land-steps {", ".land-card {",
                    ".land-bonus {", ".land-faq details {", ".land-foot {",
                    "@media (max-width: 860px)"):
            assert cls in css, cls

    def test_images_exist(self):
        import pathlib
        d = pathlib.Path("app/static/img/manual")
        for name in ("landing-hero.jpg", "landing-bonus.jpg"):
            f = d / name
            assert f.is_file() and f.stat().st_size > 10000, name
