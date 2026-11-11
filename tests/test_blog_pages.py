"""T-82: блог-кластер из 10 серверных статей под 25 ключей (issue #72, #59).

Проверяется: ровно десять самостоятельных страниц и индекс /blog; у каждой свои title/description/H1,
видимый серверный HTML без скриптов и JSON-LD, canonical на рабочем адресе и noindex на копиях; не менее двух
локальных картинок с alt/размерами; скелет из лида, шагов, блока «чек не найден», трёх FAQ, мини-инструмента и CTA;
ровно одна ссылка вверх, одна вбок, одна вниз с разными анкорами; все 25 ключей распределены по интенту без
переспама; страницы и индекс есть в sitemap.xml и достижимы crawlable-ссылками.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.services import blog_pages, seo

ROOT = Path(__file__).resolve().parent.parent
PROD = "chek.ymaster.ru"
BLOG_INDEX = "/blog"
BLOG_PATHS = (
    "/blog/proverka-cheka-onlajn",
    "/blog/proverka-cheka-po-qr-kodu",
    "/blog/proverka-cheka-po-kodu",
    "/blog/proverka-cheka-na-sajte-nalogovoj",
    "/blog/proverka-cheka-fns-rossii",
    "/blog/ofd-i-proverka-chekov",
    "/blog/chek-ne-prohodit-proverku-v-fns",
    "/blog/proverka-kassovyh-chekov-dlya-biznesa",
    "/blog/proverka-chekov-dlya-buhgaltera",
    "/blog/qr-kod-ili-ruchnoj-kod",
)
KEYWORDS_25 = (
    "проверка чека", "проверка чеков", "проверка чека онлайн", "проверка чеков онлайн",
    "проверка чека фнс", "проверка чеков фнс", "проверка чека по qr", "проверка чеков по qr",
    "сайт проверки чека", "сайт проверки чеков", "проверка чеков налоговая", "проверка чека по коду",
    "проверка чеков по коду", "проверка кассовых чеков", "проверка кассового чека",
    "проверка чека офд", "проверка чеков офд", "проверка чека онлайн по qr",
    "проверка чеков онлайн по qr", "проверка чека фнс россии", "проверка чеков фнс россии",
    "проверка чека по qr коду", "проверка чеков по qr коду", "проверка чека на сайте налоговой",
    "проверка чеков на сайте налоговой",
)
HTML_VALIDATE = os.environ.get("HTML_VALIDATE_BIN", "/tmp/htmlval/node_modules/.bin/html-validate")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Audit(HTMLParser):
    """Видимый текст, заголовки, ссылки с анкорами и картинки без head/script/style."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.hidden = 0
        self.words: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.headings: list[tuple[str, str]] = []
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        data = {k: (v or "") for k, v in attrs}
        classes = data.get("class", "").split()
        hidden = (
            self.hidden > 0 or tag in {"head", "script", "style", "template"}
            or "hidden" in classes or "visually-hidden" in classes
            or "hidden" in data or data.get("aria-hidden") == "true"
        )
        if tag not in VOID:
            self.stack.append((tag, hidden))
            if hidden:
                self.hidden += 1
        if hidden:
            return
        if tag == "a":
            self._anchor = (data.get("href", ""), [])
        elif tag == "img":
            self.images.append(data)
        elif tag in {"h1", "h2", "h3"}:
            self._heading = tag
            self._heading_text = []

    def handle_endtag(self, tag: str):
        if self._heading == tag:
            self.headings.append((tag, re.sub(r"\s+", " ", "".join(self._heading_text)).strip()))
            self._heading = None
            self._heading_text = []
        if self._anchor is not None and tag == "a":
            href, parts = self._anchor
            self.links.append((href, re.sub(r"\s+", " ", "".join(parts)).strip()))
            self._anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                was_hidden = self.stack[index][1]
                del self.stack[index:]
                if was_hidden:
                    self.hidden -= 1
                break

    def handle_data(self, data: str):
        if self.hidden:
            return
        self.words += re.findall(r"[а-яёa-z0-9]+", data.lower())
        if self._heading is not None:
            self._heading_text.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)


def audit(markup: str) -> _Audit:
    parser = _Audit()
    parser.feed(markup)
    return parser


def get(client, path: str, host: str = PROD, scheme: str = "https"):
    return client.get(path, headers={"host": host, "x-forwarded-proto": scheme})


def test_registry_and_sitemap():
    assert blog_pages.BLOG_INDEX == BLOG_INDEX
    paths = tuple(a.path for a in blog_pages.PAGES)
    assert paths == BLOG_PATHS == seo.BLOG_PAGES
    assert seo.CONTENT_PAGES == ("/kak-proverit-chek", "/cheki-sotrudnikov-v-1c", "/dlya-buhgalterov")
    assert seo.PAGES == ("/", *seo.CONTENT_PAGES, BLOG_INDEX, *BLOG_PATHS)
    xml = seo.sitemap_xml(dict(seo.DEFAULTS), PROD, "https", "2026-11-08")
    assert xml.count("<url>") == 15
    for path in (BLOG_INDEX, *BLOG_PATHS):
        assert f"<loc>https://chek.ymaster.ru{path}</loc>" in xml
        assert not any(path.startswith(d) for d in seo.DISALLOW)
    assert seo.sitemap_xml(dict(seo.DEFAULTS), "localhost", "http", "2026-11-08") is None


def test_blog_index_is_server_html_and_lists_all_articles(client):
    res = get(client, BLOG_INDEX)
    assert res.status_code == 200
    assert f'<link rel="canonical" href="https://chek.ymaster.ru{BLOG_INDEX}">' in res.text
    assert "x-robots-tag" not in res.headers
    assert "<script" not in res.text and "ld+json" not in res.text and "style=" not in res.text
    parsed = audit(res.text)
    assert [tag for tag, _ in parsed.headings].count("h1") == 1
    links = {href for href, _ in parsed.links}
    assert set(BLOG_PATHS) <= links
    assert {"/", "/kak-proverit-chek", "/oferta"} <= links
    assert len(parsed.words) >= 200
    copy = get(client, BLOG_INDEX, host="localhost:8000", scheme="http")
    assert copy.headers.get("x-robots-tag") == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in copy.text


def test_metadata_is_unique_and_all_25_keys_are_distributed():
    articles = blog_pages.PAGES
    assert len(articles) == 10
    for attr in ("title", "description", "h1"):
        assert len({getattr(a, attr) for a in articles}) == 10, attr
    all_keys: set[str] = set()
    for article in articles:
        assert 30 <= len(article.title) <= 70, article.title
        assert 100 <= len(article.description) <= 170, article.description
        assert article.title.endswith(" — Ямастер Чек")
        assert article.title.lower().count(article.keywords[0]) == 1
        assert article.h1.lower().count(article.keywords[0]) == 1
        assert all(key in KEYWORDS_25 for key in article.keywords)
        all_keys.update(article.keywords)
    assert all_keys == set(KEYWORDS_25), sorted(set(KEYWORDS_25) - all_keys)


@pytest.mark.parametrize("article", blog_pages.PAGES, ids=lambda a: a.path)
def test_article_structure_images_cta_and_links(client, article):
    res = get(client, article.path)
    assert res.status_code == 200
    markup = res.text
    assert f'<link rel="canonical" href="https://chek.ymaster.ru{article.path}">' in markup
    assert "x-robots-tag" not in res.headers
    assert markup.count("<h1") == 1 and markup.count("<title>") == 1
    assert "<script" not in markup and "ld+json" not in markup and "style=" not in markup
    assert '<meta name="keywords"' not in markup.lower()
    for hidden in ('class="hidden"', "display:none", "display: none", "visibility:hidden", 'aria-hidden="true"'):
        assert hidden not in markup, hidden
    assert '<ol itemscope itemtype="https://schema.org/BreadcrumbList">' in markup
    assert markup.count('itemtype="https://schema.org/ListItem"') == 3
    assert re.search(r'<link rel="stylesheet" href="/css/pages\.css\?v=[\d.]+">', markup)
    parsed = audit(markup)
    headings = [text for _, text in parsed.headings]
    assert any("Как проверить" in h for h in headings), headings
    assert any("Что делать, если чек не находится" in h for h in headings), headings
    assert any("FAQ" in h or "Частые вопросы" in h for h in headings), headings
    assert sum(1 for tag, _ in parsed.headings if tag == "h3") >= 3
    assert len(parsed.words) >= 400, (article.path, len(parsed.words))
    content_images = [image for image in parsed.images if image.get("src", "").startswith("/img/blog/")]
    assert len(content_images) >= 2
    for image in content_images:
        assert image.get("alt"), image
        assert image.get("width") and image.get("height"), image
        assert image.get("loading") in {"eager", "lazy"}, image
    visible = " ".join(parsed.words)
    for key in article.keywords:
        assert key in visible, (article.path, key)
        density = visible.count(key) / max(len(visible) / 1000, 1)
        assert density <= 2.5, (article.path, key, density)
    raw = markup.lower()
    for banned in ("№1", "лучший сервис", "гарантируем", "бесплатно навсегда", "₽", "руб/мес", "рейтинг", "отзывы"):
        assert banned not in raw, banned
    assert any(cta in markup for cta in (
        "Проверьте чек на chek.ymaster.ru", "Для бизнеса — демо", "массовая проверка чеков и выгрузка в 1С"))
    links = dict(parsed.links)
    up, side, down = article.up, article.side, article.down
    assert up[0] in ("/", "/kak-proverit-chek")
    assert side[0] in BLOG_PATHS and side[0] != article.path
    assert down[0] in seo.CONTENT_PAGES or down[0] == "/"
    for href in (up[0], side[0], down[0]):
        assert href in links, (article.path, href)
    anchors = (up[1], side[1], down[1])
    assert len(set(anchors)) == 3
    assert any(any(key in anchor.lower() for key in article.keywords) for anchor in anchors)
    assert any("инструкция" in anchor.lower() or "подробнее" in anchor.lower() for anchor in anchors)
    assert any("сервис" in anchor.lower() or "проверить" in anchor.lower() for anchor in anchors)
    assert get(client, article.path + "/").status_code == 404
    assert client.head(article.path, headers={"host": PROD}).status_code == 200


def test_articles_are_distinct_and_not_doorways(client):
    shared = {"ямастер", "чек", "чеки", "чека", "чеков", "проверка", "проверить", "qr", "фнс", "данных", "главная"}
    texts = {}
    for path in BLOG_PATHS:
        words = set(audit(get(client, path).text).words)
        texts[path] = {w for w in words if len(w) > 3} - shared
    paths = list(texts)
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            jaccard = len(texts[a] & texts[b]) / len(texts[a] | texts[b])
            assert jaccard < 0.45, (a, b, round(jaccard, 2))


def test_host_indexing_rules_survive_on_every_article(client):
    for path in BLOG_PATHS:
        prod = get(client, path)
        assert prod.status_code == 200 and "x-robots-tag" not in prod.headers
        assert f'<link rel="canonical" href="https://chek.ymaster.ru{path}">' in prod.text
        copy = get(client, path, host="localhost:8000", scheme="http")
        assert copy.status_code == 200 and copy.headers.get("x-robots-tag") == "noindex, nofollow"
        assert '<meta name="robots" content="noindex, nofollow">' in copy.text and 'rel="canonical"' not in copy.text


def test_links_from_start_and_cluster_pages(client):
    index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert f'href="{BLOG_INDEX}"' in index
    for path in BLOG_PATHS[:3]:
        assert f'href="{path}"' in index, path
    for path in seo.CONTENT_PAGES:
        links = {href for href, _ in audit(get(client, path).text).links}
        assert BLOG_INDEX in links, path


@pytest.mark.skipif(not Path(HTML_VALIDATE).exists(), reason="html-validate не установлен")
def test_markup_passes_html_validate(tmp_path):
    def article_render(article):
        return lambda cfg, host, scheme: blog_pages.render(article, cfg, host, scheme)

    pages = [(BLOG_INDEX, blog_pages.render_index)]
    pages += [(a.path, article_render(a)) for a in blog_pages.PAGES]
    files = []
    for name, render in pages:
        for host, scheme in ((PROD, "https"), ("localhost:8000", "http")):
            f = tmp_path / f"{name.strip('/').replace('/', '-')}-{scheme}.html"
            f.write_text(render(dict(seo.DEFAULTS), host, scheme), encoding="utf-8")
            files.append(str(f))
    report = tmp_path / "html-validate.json"
    with report.open("w", encoding="utf-8") as stream:
        subprocess.run([HTML_VALIDATE, "--formatter=json", *files], cwd=ROOT, stdout=stream,
                       stderr=subprocess.PIPE, text=True, timeout=300, check=False)
    data = json.loads(report.read_text(encoding="utf-8") or "[]")
    errors = [(m["ruleId"], m["line"], Path(f["filePath"]).name)
              for f in data for m in f.get("messages", []) if m["severity"] == 2]
    assert not errors, errors[:8]
