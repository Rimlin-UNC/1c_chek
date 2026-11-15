"""Сводка и печать проверяют границы месяца до вычислений."""
from tests import test_primary_upd as _upd

firms = _upd.firms


def test_extreme_months_return_422(client, firms):
    admin, _, _ = firms
    for endpoint in ('/api/v1/clients', '/api/v1/clients/report'):
        for period in ('0000-01', '9999-12'):
            response = client.get(endpoint, headers=admin, params={'period': period})
            assert response.status_code == 422, (endpoint, period, response.text)
        for period in ('0001-01', '9999-11', '2026-10'):
            response = client.get(endpoint, headers=admin, params={'period': period})
            assert response.status_code == 200, (endpoint, period, response.text)
