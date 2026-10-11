# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.59.4: карточка «Новое приглашение».
# 1) Выпадающий список компаний из базы (свежий, не из кэша) + вариант
#    «своя компания» с полем ввода (раньше — datalist из пустого кэша).
# 2) Список приглашённых: дублирующая колонка «Компания» убрана (8→6),
#    кнопки — единым блоком .row-actions с переносом строк, на мобильном
#    больше не наезжают друг на друга.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import re


class TestVersion1594:
    """Строгий версионный замок релиза v1.59.4."""

    def test_versions_synced(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert ver == "1.59.4"
        idx = open("app/static/index.html", encoding="utf-8").read()
        assert f"app.css?v={ver}" in idx and f"app.js?v={ver}" in idx
        assert "?v=1.59.3" not in idx
        sw = open("app/static/sw.js", encoding="utf-8").read()
        assert f"ymaster-check-v{ver}" in sw and f"?v={ver}" in sw
        mf = open("app/static/manifest.webmanifest", encoding="utf-8").read()
        assert f'"version": "{ver}"' in mf

    def test_whats_new_changelog(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert js.index("'1.59.4':") < js.index("'1.59.3':")
        ch = open("CHANGELOG.md", encoding="utf-8").read()
        assert ch.count("## [1.59.4]") == 1
        assert ch.index("## [1.59.4]") < ch.index("## [1.59.3]")


class TestInviteDialogCompanies:
    """Диалог приглашения: select существующих компаний + своя компания."""

    def test_select_and_custom_input(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "iv-company-select" in js            # честный select
        assert "iv-company-name" in js              # поле своей компании
        assert "iv-company-new-wrap" in js          # показ/скрытие поля
        assert "Другая компания — вписать" in js

    def test_no_datalist_in_dialog(self):
        """Капризный datalist из диалога убран (проверяем разметку)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        block = js.split("function inviteDialog")[1].split("// --- Экран «Компании»")[0]
        assert '<datalist' not in block
        assert 'list="iv-company-list"' not in block

    def test_companies_loaded_fresh(self):
        """Список компаний берётся из базы при открытии (кэш был пуст)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        block = js.split("async function inviteDialog")[1].split("openModal(")[0]
        assert "api.get('/api/v1/companies')" in block
        assert "Array.isArray" in block             # страховка формы ответа

    def test_save_resolution_kept(self):
        """Выбранное — берём, свою — создаём при сохранении (как было)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "company_name: companyNameVal" in js
        assert "api.post('/api/v1/companies'" in js


class TestInvitesListLayout:
    """Список приглашённых: 6 колонок, кнопки не наезжают."""

    def test_no_duplicate_company_column(self):
        """Дубль колонки «Компания» убран ИМЕННО у приглашений
        (в таблице пользователей колонка «Компания» легитимна)."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        invites = js.split('Приглашения <span class="spacer"')[1] \
                    .split('Пользователи системы')[0]
        assert "<th>Компания (группа)</th>" in invites
        assert "<th>Компания</th>" not in invites   # дубль убран
        assert 'colspan="6"' in invites             # пустое — под 6 колонок

    def test_row_actions_block(self):
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert '<div class="row-actions">' in js    # кнопки единым блоком
        assert "i-link" in js and "i-qr" in js and "i-revoke" in js

    def test_css_actions_wrap_and_token_line(self):
        css = open("app/static/css/app.css", encoding="utf-8").read()
        assert ".row-actions { display: flex; flex-wrap: wrap; gap: 6px;" in css
        assert ".token-line" in css and "flex-wrap: wrap" in css

    def test_receipts_and_users_untouched(self):
        """«Не задев то, что работает»: чеки (таблица/карточки) и
        пользователи — без изменений в этой версии."""
        js = open("app/static/js/app.js", encoding="utf-8").read()
        assert "function receiptRow(" in js
        assert "state.receiptsView === 'cards'" in js
        assert "u-add" in js and "u-reset" in js
