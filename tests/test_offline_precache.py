"""Все статические модули оболочки должны быть доступны после первой установки PWA."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'app/static'


def _dependencies():
    visited = set()
    def visit(path):
        path = path.resolve()
        if path in visited:
            return
        assert path.is_file(), path
        visited.add(path)
        text = path.read_text(encoding='utf-8')
        for spec in re.findall(r'''(?:^\s*import\s+(?:[^'"\n]*?\s+from\s+)?|^\s*export\s+[^'"\n]*?\s+from\s+)["'](\.[^"']+)["']''', text, re.M):
            visit(path.parent / spec)
    visit(STATIC / 'js/app.js')
    return {'/' + str(p.relative_to(STATIC)) for p in visited if p.name != 'app.js'}


def test_every_static_module_and_chart_is_precached_with_exact_url():
    sw = (STATIC / 'sw.js').read_text(encoding='utf-8')
    assets = sw.split('const ASSETS = [', 1)[1].split('];', 1)[0]
    urls = set(re.findall(r"'([^']+)'", assets))
    assert _dependencies() | {'/js/vendor/chart.umd.js'} <= urls
