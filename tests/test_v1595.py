# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.5: выпадающий список компаний в приглашении —
# в КРАТКОЙ форме. Приоритет — short_name из карточки компании (ЕГРЮЛ/
# ЕГРИП, как на странице «Компании»); без карточки ОПФ сокращается:
# «Общество с ограниченной ответственностью» → «ООО»,
# «Индивидуальный предприниматель» → «ИП»,
# самозанятые → «Самозанятый» (сокращение на будущее), АО/ПАО/НАО/КФХ.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1595:
    """Строгий версионный замок релиза v1.59.5."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        # v1.59.6: структурная проверка — строгий замок версии в test_v1596
        assert tuple(int(x) for x in ver.split(".")) >= (1, 59, 5)
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.4" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.5':") < js.index("'1.59.4':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.5]") == 1
        assert ch.index("## [1.59.5]") < ch.index("## [1.59.4]")

    def test_roles_block_bumped(self):
        from app.services import updater
        reg = updater.read_blocks("app/services/blocks.py")
        assert reg["Роли и доступ"] == "1.59.5"


class TestShortOpf:
    """Краткая форма ОПФ: карточка компании в приоритете, фолбэк — замена."""

    def test_helper_present(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function shortOpfName(" in js
        # карточка компании (ЕГРЮЛ) — первичный источник короткого имени
        assert "shortOpfName(compName(c))" in js

    def test_invite_select_uses_short(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        block = js.split("function inviteDialog")[1].split("// --- Экран «Компании»")[0]
        assert "shortOpfName(compName(c))" in block
        # значение опции — ПОЛНОЕ имя (резолюция компании не ломается)
        assert '<option value="${esc(c.name)}"' in block

    def test_opf_variants_in_helper(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        opf_list = js.split("const OPF_SHORT = [")[1].split("];")[0]
        for opf in ("ООО", "ИП", "Самозанятый", "АО", "ПАО", "НАО", "КФХ"):
            assert opf in opf_list, opf

    def test_selection_resolution_untouched(self):
        """Выбор из списка — по полному имени; своя компания создаётся."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "company_name: companyNameVal" in js
        assert "api.post('/api/v1/companies'" in js
