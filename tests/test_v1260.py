# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.26.0 → структурные (v1.57.0).
# Источник «Честный Знак» (mobile.api.crpt.ru) выведен из системы в
# v1.57.0 (сервис перестал отвечать). Универсальный парсер чека
# сохраняет совместимость с ответами любого источника.
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from tests.conftest import login, client  # noqa: F401


class TestCrptRemoved:
    def test_no_crpt_in_code(self):
        src = open('app/services/external.py', encoding='utf-8').read()
        assert 'fetch_crpt' not in src and 'CRPT_URL' not in src

    def test_crpt_not_in_chain_or_status(self, client):
        from app.services.external import ExternalFetchEngine
        eng = ExternalFetchEngine()
        st = eng.status()
        assert 'crpt' not in st
        js = open('app/static/js/app.js', encoding='utf-8').read()
        assert 'Честный Знак' not in js.split('Источники данных чека')[1][:2000]

    def test_universal_parser_still_ok(self):
        from app.services.external import parse_receipt_payload
        res = parse_receipt_payload({
            'user': 'ООО Тест', 'userInn': '1234567890',
            'dateTime': '2026-10-02T12:00:00', 'totalSum': 80000,
            'items': [{'name': 'Вода', 'price': 8000, 'sum': 8000,
                       'quantity': 1}],
        }, known_rub=800.0)
        assert res.found and res.total_sum == 800.0
        assert res.items[0].name == 'Вода'
