"""Кандидат v1.72.2: согласованные точки версии и предостережения T-88."""
from pathlib import Path

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
VERSION = '1.72.2'


def read(path):
    return (ROOT / path).read_text(encoding='utf-8')


def test_candidate_version_and_cache_agree():
    current = settings.APP_VERSION
    assert tuple(map(int, current.split("."))) >= tuple(map(int, VERSION.split(".")))
    for path in ('app/static/index.html', 'app/static/sw.js', 'app/static/manifest.webmanifest',
                 'app/static/maxapp/index.html', 'app/static/b2b/index.html'):
        assert current in read(path), path
    assert read('app/static/index.html').count(f'?v={current}') == 3
    assert read('app/static/sw.js').count(f'?v={current}') == 5
    assert f'ymaster-check-v{current}' in read('app/static/sw.js')


def test_candidate_documents_contract_limitations():
    entry = read('CHANGELOG.md').split('## [1.72.2]', 1)[1].split('\n## [', 1)[0]
    assert len(entry) < 4000
    for phrase in ('пролонгац', 'внешние каналы', 'откат', 'резервн'):
        assert phrase in entry.lower()
    assert "'1.72.2': [" in read('app/static/js/app.js')
    assert 'Договор и тариф клиента' in read('docs/USER_GUIDE.md')
    assert 'не юридически значимое уведомление' in read('app/services/manual_content.py')
