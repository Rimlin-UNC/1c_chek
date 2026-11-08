"""Страницы под кластеры запросов (T-75, v1.70.106, ADR-36): без дорвеев, текст виден без JavaScript.

Проверяется: страницы отвечают на рабочем адресе с canonical, на копиях программы — noindex в теге и заголовке;
один H1, свои title и description, хлебные крошки микроданными (без JSON-LD и без скриптов); нет скрытого текста;
каждая страница — со своим содержанием (не почти копия соседней) и достаточно полная; ключ — не больше одного раза в
title и в H1; страницы в карте сайта, ссылки на них — на главной и друг на друга; неизвестные адреса — 404.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.services import seo, seo_pages

ROOT = Path(__file__).resolve().parent.parent
PROD = "chek.ymaster.ru"
PATHS = ("/kak-proverit-chek", "/cheki-sotrudnikov-v-1c", "/dlya-buhgalterov")
HTML_VALIDATE = os.environ.get("HTML_VALIDATE_BIN", "/tmp/htmlval/node_modules/.bin/html-validate")


class _Text(HTMLParser):
    """Видимый текст страницы и ссылки (без содержимого head)."""

    def __init__(self):
        super().__init__()
        self.words: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("head", "style", "script"):
            self._skip += 1
        if tag == "a":
            self.links.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag):
        if tag in ("head", "style", "script"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.words += re.findall(r"[а-яёa-z0-9]+", data.lower())


def parse(html: str) -> _Text:
    p = _Text()
    p.feed(html)
    return p


def get(client, path: str, host: str = PROD, scheme: str = "https"):
    return client.get(path, headers={"host": host, "x-forwarded-proto": scheme})


def test_registry_matches_sitemap_and_robots():
    assert tuple(p.path for p in seo_pages.PAGES) == PATHS == seo.CONTENT_PAGES
    assert seo.BLOG_INDEX == "/blog" and len(seo.BLOG_PAGES) == 10
    assert seo.PAGES == ("/", "/oferta", *PATHS, seo.BLOG_INDEX, *seo.BLOG_PAGES)
    xml = seo.sitemap_xml(dict(seo.DEFAULTS), PROD, "https", "2026-11-08")
    assert xml.count("<url>") == 16
    for path in (*PATHS, seo.BLOG_INDEX, *seo.BLOG_PAGES):
        assert f"<loc>https://chek.ymaster.ru{path}</loc>" in xml
        assert not any(path.startswith(d) for d in seo.DISALLOW)
    assert seo.sitemap_xml(dict(seo.DEFAULTS), "localhost", "http", "2026-11-08") is None


@pytest.mark.parametrize("path", PATHS)
def test_page_on_production_host(client, path):
    res = get(client, path)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert "x-robots-tag" not in res.headers
    html = res.text
    assert f'<link rel="canonical" href="https://chek.ymaster.ru{path}">' in html
    assert 'name="robots"' not in html
    assert html.count("<h1") == 1 and html.count("<title>") == 1
    assert "<script" not in html and "ld+json" not in html and "style=" not in html     # CSP и без JSON-LD
    for hidden in ('class="hidden"', "display:none", "display: none", "visibility:hidden", 'aria-hidden="true"'):
        assert hidden not in html, hidden
    assert '<ol itemscope itemtype="https://schema.org/BreadcrumbList">' in html
    assert html.count('itemtype="https://schema.org/ListItem"') == 2
    assert '<meta itemprop="position" content="1">' in html and '<meta itemprop="position" content="2">' in html
    assert '<a itemprop="item" href="https://chek.ymaster.ru/">' in html
    assert re.search(r'<link rel="stylesheet" href="/css/pages\.css\?v=[\d.]+">', html)
    assert get(client, path + "/").status_code == 404                     # без дублей адреса со слешем
    head = client.head(path, headers={"host": PROD})
    assert head.status_code == 200


@pytest.mark.parametrize("path", PATHS)
def test_page_on_copy_is_noindex(client, path):
    res = get(client, path, host="localhost:8000", scheme="http")
    assert res.status_code == 200 and res.headers.get("x-robots-tag") == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in res.text and 'rel="canonical"' not in res.text


def test_titles_descriptions_and_keys_without_spam():
    titles = [p.title for p in seo_pages.PAGES] + ["Проверка чека онлайн по QR-коду — Ямастер Чек"]
    assert len(set(titles)) == len(titles)
    assert len({p.description for p in seo_pages.PAGES}) == 3 and len({p.h1 for p in seo_pages.PAGES}) == 3
    for page in seo_pages.PAGES:
        assert 30 <= len(page.title) <= 70, page.title                      # длиннее — обрезается в выдаче
        assert 100 <= len(page.description) <= 170, page.description
        assert page.title.endswith(" — Ямастер Чек")
    keys = {"/kak-proverit-chek": "проверить", "/cheki-sotrudnikov-v-1c": "чеки сотрудников",
            "/dlya-buhgalterov": "бухгалтер"}
    for page in seo_pages.PAGES:
        key = keys[page.path]
        assert page.title.lower().count(key) == 1 and page.h1.lower().count(key) <= 1, page.path


def test_pages_are_distinct_and_substantial(client):
    texts = {path: parse(get(client, path).text) for path in PATHS}
    shared = {"ямастер", "чек", "главная"}
    for path, t in texts.items():
        assert len(t.words) >= 450, (path, len(t.words))                  # не «тонкая» страница
    paths = list(texts)
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            wa, wb = set(texts[a].words) - shared, set(texts[b].words) - shared
            jaccard = len(wa & wb) / len(wa | wb)
            assert jaccard < 0.45, (a, b, round(jaccard, 2))              # не почти копии друг друга


def test_links_between_pages_and_from_start_page(client):
    for path in PATHS:
        links = set(parse(get(client, path).text).links)
        assert "/" in links and "/oferta" in links and "/blog" in links, path
        assert set(PATHS) - {path} <= links, path
    index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    for path in PATHS:
        assert f'href="{path}"' in index, path
    assert 'href="/blog"' in index
    land = index.split('<div class="land-faq">', 1)[1].split('<div class="land-right">', 1)[0]
    assert '<p class="land-more"><span>Полезное:</span>' in land          # ссылки видны, а не только в ответах


def test_only_real_facts_and_no_prices():
    html = "".join(p.lead + p.body for p in seo_pages.PAGES).lower()
    for absent in ("₽", "руб/мес", "тариф", "бесплатно навсегда", "№1", "лучш", "отзыв", "рейтинг"):
        assert absent not in html, absent
    how = seo_pages.HOW_TO.body
    assert "t=20250901T1430&amp;s=1520.50&amp;fn=9999078902001234&amp;i=54321&amp;fp=812345678&amp;n=1" in how
    assert "«Принят в пул»" in how and "«Не найден»" in how
    emp = seo_pages.EMPLOYEES.body
    for fact in ("EnterpriseData", "«Авансовый отчёт»", "ФН + ФД + ФП", "алкоголь", "18:00", "5-е число"):
        assert fact in emp, fact
    acc = seo_pages.ACCOUNTANTS.body
    for fact in ("Точка — по OAuth 2.0", "Т-Банк и Модульбанк", "Сбербанк", "Альфа-Банк", "1CClientBankExchange",
                 "5.01–5.03", "только на чтение", "Telegram или MAX", "УПД 5.03"):
        assert fact in acc, fact


def test_unknown_page_and_static_still_work(client):
    assert get(client, "/kak-proverit").status_code == 404
    assert get(client, "/css/pages.css").status_code == 200
    css = (ROOT / "app/static/css/pages.css").read_text(encoding="utf-8")
    assert css.count("@font-face") == 3 and "url(/fonts/inter-cyrillic-wght-normal.woff2)" in css
    assert "@media (max-width: 560px)" in css


@pytest.mark.skipif(not Path(HTML_VALIDATE).exists(), reason="html-validate не установлен (как в tests/test_v17033.py)")
def test_markup_passes_html_validate(tmp_path):
    files = []
    for page in seo_pages.PAGES:
        for host, scheme in ((PROD, "https"), ("localhost:8000", "http")):
            f = tmp_path / f"{page.path.strip('/')}-{scheme}.html"
            f.write_text(seo_pages.render(page, dict(seo.DEFAULTS), host, scheme), encoding="utf-8")
            files.append(str(f))
    proc = subprocess.run([HTML_VALIDATE, "--formatter=json", *files], cwd=ROOT, capture_output=True, text=True,
                          timeout=300, check=False)                      # итог читается из JSON, а не из кода выхода
    errors = [(m["ruleId"], m["line"], Path(f["filePath"]).name)
              for f in json.loads(proc.stdout or "[]") for m in f.get("messages", []) if m["severity"] == 2]
    assert not errors, errors[:5]
