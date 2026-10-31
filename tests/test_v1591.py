# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.1: гостевые страницы Чек-Пула снова видны.
# Регресс: обёртки #public-screen и #pool-account-screen созданы в
# вёрстке с классом hidden, но showPublicScreen/showPoolScreen заполняли
# только #public-root/#pool-account-root и НИКТО не снимал hidden —
# гость видел пустую страницу вместо «Сдать чек» и кабинета.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
import re


class TestVersion1591:
    """Строгий версионный замок релиза v1.59.1."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.59.2: структурная проверка — строгий замок версии в test_v1592
        assert tuple(int(x) for x in ver.split(".")) >= (1, 59, 1)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.0" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.1':") < js.index("'1.59.0':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.1]") == 1
        assert ch.index("## [1.59.1]") < ch.index("## [1.59.0]")


class TestScreenSwitcher:
    """Единый переключатель пяти верхних экранов — все обёртки переключаются."""

    def test_wrappers_exist(self):
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert 'id="public-screen" class="login-screen hidden"' in idx
        assert 'id="pool-account-screen" class="login-screen hidden"' in idx
        assert 'id="public-root"' in idx and 'id="pool-account-root"' in idx

    def test_single_switcher_used_everywhere(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function showScreen(" in js
        # пять экранов — все через showScreen (прямых remove('hidden') больше нет)
        assert js.count("showScreen('login-screen')") == 1
        assert js.count("showScreen('register-screen')") == 1
        assert js.count("showScreen('public-screen')") == 1
        assert js.count("showScreen('pool-account-screen')") == 1
        assert js.count("showScreen('app-shell')") == 1
        # переключатель знает все пять обёрток
        for sid in ("login-screen", "register-screen", "public-screen",
                    "pool-account-screen", "app-shell"):
            assert f"'{sid}'" in js.split("function showScreen(")[1].split("}")[0]
