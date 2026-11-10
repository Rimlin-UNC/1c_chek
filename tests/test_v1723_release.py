"""Кандидат v1.72.3: безопасный печатный отчёт и согласованная версия."""
from pathlib import Path

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
VERSION = '1.72.3'


def read(path):
    return (ROOT / path).read_text(encoding='utf-8')


def test_candidate_version_and_cache():
    current = settings.APP_VERSION
    assert tuple(map(int, current.split('.'))) >= tuple(map(int, VERSION.split('.')))
    for path in ('app/static/index.html', 'app/static/sw.js', 'app/static/manifest.webmanifest',
                 'app/static/maxapp/index.html', 'app/static/b2b/index.html'):
        assert current in read(path), path
    assert read('app/static/index.html').count(f'?v={current}') == 3
    assert read('app/static/sw.js').count(f'?v={current}') == 4
    assert f'ymaster-check-v{current}' in read('app/static/sw.js')


def test_candidate_documents_report_limits():
    entry = read('CHANGELOG.md').split(f'## [{VERSION}]', 1)[1].split('\n## [', 1)[0]
    assert len(entry) < 4000
    for phrase in ('на момент формирования', 'не исторический', 'не отправляется', 'резервн'):
        assert phrase in entry.lower()
    assert f"'{VERSION}': [" in read('app/static/js/app.js')
    assert 'Сводный отчёт по клиентам' in read('docs/USER_GUIDE.md')
