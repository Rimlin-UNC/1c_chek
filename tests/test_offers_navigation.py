"""Раздел оферт не меняет действующую редакцию и не попадает в поиск."""
from app.legal import offerta
from app.services import seo


def test_offers_catalog_and_draft_are_public_but_not_indexed(client):
    for path in ("/oferty", "/oferta-proekt", "/oferta", "/oferta.txt", "/api/v1/legal/offerta"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        if response.headers["content-type"].startswith("text/html"):
            assert '<meta name="robots" content="noindex, nofollow">' in response.text
    catalog = client.get('/oferty').text
    assert 'href="/oferta"' in catalog and 'href="/oferta-proekt"' in catalog
    draft = client.get('/oferta-proekt').text
    assert 'ПРОЕКТ' in draft and 'не является офертой' in draft
    assert client.get('/api/v1/legal/offerta').json()['edition'] == offerta.EDITION
    assert client.get('/oferta.txt').text == offerta.full_text(None)


def test_service_documents_are_not_in_sitemap_or_blocked_from_crawling():
    xml = seo.sitemap_xml(dict(seo.DEFAULTS), 'chek.ymaster.ru', 'https', '2026-11-08')
    for path in ('/oferta', '/oferty', '/oferta-proekt'):
        assert f'<loc>https://chek.ymaster.ru{path}</loc>' not in xml
        assert not any(path.startswith(item) for item in seo.DISALLOW)
    assert 'Allow: /api/v1/legal/offerta' in seo.robots_txt(dict(seo.DEFAULTS), 'chek.ymaster.ru', 'https')


def test_home_and_content_link_to_login_and_offers(client):
    assert 'href="/oferty"' in client.get('/').text
    for path in ('/dlya-buhgalterov', '/blog/proverka-chekov-dlya-buhgaltera'):
        assert 'href="/#land-login">Войти</a>' in client.get(path).text
