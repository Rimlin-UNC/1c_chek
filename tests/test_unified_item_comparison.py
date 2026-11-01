"""Сверка не теряет дубликаты и не объявляет неизвестные данные совпавшими."""
import pytest

from qa.unified_item_comparison import common_positions_match, sellers_match


@pytest.mark.parametrize('registry,pool,expected', [
    ([('A', 1, 2, 2, '20', 0)], [('A', 1.0, 2.0, 2.0, '20', 0.0)], True),
    ([('A', 1, 2, 2, '20', 0), ('A', 1, 2, 2, '20', 0)], [('A', 1, 2, 2, '20', 0)], False),
    ([('A', 1, 2, 2, '20', 0)], [('A', 1, 2, 2.001, '20', 0)], False),
    ([('A', 1, 2, 2, '20', 0)], [('a', 1, 2, 2, '20', 0)], False),
    ([], [], False),
    ([('A', None, 2, 2, '20', 0)], [('A', None, 2, 2, '20', 0)], False),
    ([('A', float('nan'), 2, 2, '20', 0)], [('A', float('nan'), 2, 2, '20', 0)], False),
    ([('A', 1, 2, 2, '', 0)], [('A', 1, 2, 2, '', 0)], False),
])
def test_compare_common_positions(registry, pool, expected):
    assert common_positions_match(registry, pool) is expected


def test_compare_sellers_requires_all_fields_and_exact_values():
    assert sellers_match(('магазин', '123', 'адрес'), ('магазин', '123', 'адрес'))
    assert not sellers_match(('магазин', '123', ''), ('магазин', '123', ''))
    assert not sellers_match(('магазин', '123', 'адрес'), ('магазин', '124', 'адрес'))
