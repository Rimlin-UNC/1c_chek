# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.27.0 → структурные (v1.57.0).
# Источник «Приложение ФНС» (irkkt-mobile, fns_app) выведен из системы
# в v1.57.0 (сервис перестал отвечать). Инвариант порядка сохранён:
# proverkacheka — единственный рабочий источник, mock всегда в конце.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login, client  # noqa: F401


class TestFnsAppRemoved:
    def test_no_fns_app_in_code(self):
        src = open('app/services/external.py', encoding='utf-8').read()
        for gone in ('fetch_fns_app', 'FNS_APP_URL', 'FNS_APP_HEADERS',
                     '_fns_app_session', 'irkkt'):
            assert gone not in src, gone

    def test_default_order_is_proverkacheka(self):
        src = open('app/services/external.py', encoding='utf-8').read()
        chain = src.split("def provider_chain")[1].split("def is_available")[0]
        # v1.57.6: порядок задан системой — proverkacheka.com первый, напрямую
        assert 'chain.append("proverkacheka")' in chain
        # мёртвые источники в цепочке не упоминаются вовсе
        assert 'fns_app' not in chain and 'ofd_ru' not in chain and 'crpt' not in chain

    def test_status_has_no_dead_sources(self):
        from app.services.external import ExternalFetchEngine
        st = ExternalFetchEngine().status()
        assert 'fns_app' not in st and 'ofd_ru' not in st and 'crpt' not in st

    def test_settings_ui_only_proverkacheka(self):
        js = open('app/static/js/app.js', encoding='utf-8').read()
        assert 'ext-fns-inn' not in js and 'ext-ofd' not in js
        assert 'Токен proverkacheka.com' in js
