# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.27.0 → структурные (v1.57.0).
# Источник «Приложение ФНС» (irkkt-mobile, fns_app) выведен из системы
# в v1.57.0 (сервис перестал отвечать). Инвариант порядка сохранён:
# proverkacheka — единственный рабочий источник, mock всегда в конце.
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
from tests.conftest import login, client  # noqa: F401


class TestFnsAppRemoved:
    def test_no_fns_app_in_code(self):
        src = open('app/services/external.py', encoding='utf-8').read()
        for gone in ('fetch_fns_app', 'FNS_APP_URL', 'FNS_APP_HEADERS',
                     '_fns_app_session', 'irkkt'):
            assert gone not in src, gone

    def test_default_chain_is_fns_only(self):
        """v1.70.70: цепочка источников — только официальное API ФНС."""
        src = open('app/services/external.py', encoding='utf-8').read()
        chain = src.split("def provider_chain")[1].split("def is_available")[0]
        assert 'return ["fns_api"]' in chain
        for gone in ("proverkacheka", "mock", "fns_app", "ofd_ru", "crpt"):
            assert gone not in chain, gone

    def test_status_has_no_dead_sources(self):
        from app.services.external import ExternalFetchEngine
        st = ExternalFetchEngine().status()
        assert 'fns_app' not in st and 'ofd_ru' not in st and 'crpt' not in st

    def test_settings_ui_only_fns(self):
        """v1.70.70: в настройках — только мастер-токен и AppId ФНС."""
        js = open('app/static/js/app.js', encoding='utf-8').read()
        assert 'ext-fns-inn' not in js and 'ext-ofd' not in js
        assert 'id="fns-token"' in js and 'id="fns-appid"' in js
        assert 'Токен proverkacheka.com' not in js
