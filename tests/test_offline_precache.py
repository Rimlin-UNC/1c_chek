"""Все статические модули оболочки должны быть доступны после первой установки PWA."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'app/static'


def _dependencies():
    from urllib.parse import urlsplit

    visited, urls = set(), set()
    def visit(path):
        path = path.resolve()
        if path in visited:
            return
        assert path.is_file(), path
        visited.add(path)
        text = path.read_text(encoding='utf-8')
        pattern = r"""(?:^\s*import\s+(?:[^'"\n]*?\s+from\s+)?|^\s*export\s+[^'"\n]*?\s+from\s+)["'](\.[^"']+)["']"""
        for spec in re.findall(pattern, text, re.M):
            parsed = urlsplit(spec)
            dependency = (path.parent / parsed.path).resolve()
            url = '/' + str(dependency.relative_to(STATIC))
            if parsed.query:
                url += '?' + parsed.query
            urls.add(url)
            visit(dependency)
    visit(STATIC / 'js/app.js')
    return urls


def test_critical_osm_import_is_versioned_and_precached():
    from app.config import settings

    app = (STATIC / 'js/app.js').read_text(encoding='utf-8')
    sw = (STATIC / 'sw.js').read_text(encoding='utf-8')
    expected = f'./osm.js?v={settings.APP_VERSION}'
    assert f"from '{expected}'" in app
    assert f"'/js/osm.js?v={settings.APP_VERSION}'" in sw


def test_every_static_module_and_chart_is_precached_with_exact_url():
    sw = (STATIC / 'sw.js').read_text(encoding='utf-8')
    assets = sw.split('const ASSETS = [', 1)[1].split('];', 1)[0]
    urls = set(re.findall(r"'([^']+)'", assets))
    assert _dependencies() | {'/js/vendor/chart.umd.js'} <= urls
