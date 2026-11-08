"""Главная как коммерческий SEO-хаб (T-83, issue #72): люди, а не ключи, и без поломки входа.

Проверяется серверный HTML до JavaScript: уникальные title/description под интент «проверка чека онлайн»,
один H1, ориентиры страницы, видимые ссылки на кластеры, честные факты, альтернативы картинок,
микроданные без JSON-LD и прежние правила индексации на рабочем адресе и копиях программы.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.services import seo, seo_pages

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "app" / "static" / "index.html"
PROD = "chek.ymaster.ru"
TITLE = "Проверка чека онлайн по QR-коду — Ямастер Чек"
DESCRIPTION = (
    "Сайт проверки чеков: отсканируйте QR-код, сверим кассовый чек с данными ФНС и покажем результат. "
    "Баллы за чеки, авансовые отчёты и выгрузка в 1С."
)
HTML_VALIDATE = os.environ.get("HTML_VALIDATE_BIN", "/tmp/htmlval/node_modules/.bin/html-validate")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def whole_page() -> str:
    return INDEX.read_text(encoding="utf-8")


def home_source() -> str:
    """Публичная главная без скрытых экранов приложения."""
    return whole_page().split('id="login-screen"', 1)[1].split('id="register-screen"', 1)[0]


class _Visible(HTMLParser):
    """Текст, ссылки, картинки и ориентиры без скрытого и технического содержимого."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.hidden = 0
        self.words: list[str] = []
        self.links: list[str] = []
        self.images: list[dict[str, str]] = []
        self.landmarks: list[tuple[str, dict[str, str]]] = []
        self.headings: list[tuple[str, str]] = []
        self._heading: str | None = None
        self._heading_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        data = {k: (v or "") for k, v in attrs}
        classes = data.get("class", "").split()
        hidden = (
            self.hidden > 0
            or tag in {"script", "style", "template", "head"}
            or "hidden" in classes
            or "visually-hidden" in classes
            or "hidden" in data
            or data.get("aria-hidden") == "true"
        )
        if tag not in VOID:
            self.stack.append((tag, hidden))
            if hidden:
                self.hidden += 1
        if hidden:
            return
        if tag == "a":
            self.links.append(data.get("href", ""))
        elif tag == "img":
            self.images.append(data)
        elif tag in {"header", "nav", "main", "footer", "section", "article", "aside"}:
            self.landmarks.append((tag, data))
        elif tag in {"h1", "h2", "h3"}:
            self._heading = tag
            self._heading_text = []

    def handle_endtag(self, tag: str):
        if self._heading == tag:
            text = re.sub(r"\s+", " ", "".join(self._heading_text)).strip()
            self.headings.append((tag, text))
            self._heading = None
            self._heading_text = []
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


def audit() -> _Visible:
    parser = _Visible()
    parser.feed(home_source())
    return parser


def test_metadata_matches_intent_without_keyword_stuffing():
    page = whole_page()
    title = re.search(r"<title>(.*?)</title>", page, re.S).group(1)
    description = re.search(r'<meta name="description" content="([^"]*)">', page).group(1)
    assert title == TITLE and len(title) <= 70
    assert description == DESCRIPTION and 100 <= len(description) <= 170
    assert title.lower().count("проверка чека онлайн") == 1
    assert description.lower().count("сайт проверки чеков") == 1
    assert '<meta name="keywords"' not in page.lower()
    assert "<!-- seo:head -->" in page
    for tag in ("og:type", "og:title", "og:description", "og:url", "og:image", "twitter:card"):
        assert f'property="{tag}"' in page or f'name="{tag}"' in page, tag
    titles = [TITLE, *(page.title for page in seo_pages.PAGES)]
    assert len(set(titles)) == len(titles), "главная и кластеры не должны повторять title"


def test_one_meaningful_h1_and_accessible_landmarks():
    page = home_source()
    assert page.count("<h1") == 1
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S).group(1)
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", h1))
    plain = re.sub(r"\s+", " ", plain).strip()
    assert "Проверка чека онлайн" in plain
    assert "QR-кода" in plain and "1С" in plain
    assert not re.search(r"<h[23][^>]*>", page[: page.index("<h1")], re.S)
    parsed = audit()
    assert parsed.headings and parsed.headings[0][0] == "h1"
    tags = [tag for tag, _ in parsed.landmarks]
    for tag in ("header", "nav", "main", "footer"):
        assert tags.count(tag) == 1, f"должен быть ровно один видимый <{tag}>"
    assert tags.count("section") >= 4
    assert any(tag == "nav" and data.get("aria-label") for tag, data in parsed.landmarks)
    assert '<a class="skip" href="#main">' in whole_page()
    assert re.search(r'<main[^>]*id="main"', page)


def test_commercial_hub_links_are_visible_and_crawlable():
    links = audit().links
    for path in seo.CONTENT_PAGES:
        assert links.count(path) == 2, (path, links.count(path))
    assert "/oferta" in links and "/blog" in links
    for path in seo.BLOG_PAGES[:3]:
        assert path in links, path
    assert "#/public?cam=1" in links
    for anchor in ("#land-how", "#land-business", "#land-faq", "#land-login"):
        assert anchor in links, anchor
    assert not any(link.startswith("javascript:") for link in links)
    for link in links:
        if link.startswith("https://"):
            assert link.startswith("https://ymaster.ru") or link.startswith("mailto:")
    assert '<p class="land-more"><span>Полезное:</span>' in home_source()


def test_visible_content_is_substantial_true_and_not_spammy():
    parsed = audit()
    text = " ".join(parsed.words)
    assert len(parsed.words) >= 400, len(parsed.words)
    for phrase in ("проверка чека онлайн", "сайт проверки чеков", "фнс", "офд", "qr", "1с", "оферта"):
        assert phrase in text, phrase
    assert text.count("проверка чека онлайн") <= 3
    assert text.count("сайт проверки чеков") <= 2
    raw = home_source().lower()
    for banned in ("№1", "лучший сервис", "гарантируем", "бесплатно навсегда", "₽", "руб/мес", "рейтинг", "отзывы"):
        assert banned not in raw, banned
    assert 'style="display: none"' not in raw and 'style="display:none"' not in raw
    assert 'style="visibility: hidden"' not in raw and 'style="visibility:hidden"' not in raw


def test_images_have_dimensions_alt_and_lazy_loading():
    parsed = audit()
    assert len(parsed.images) >= 5
    for image in parsed.images:
        assert image.get("alt") is not None, image
        assert image.get("width") and image.get("height"), image
        assert image.get("loading") in {"eager", "lazy"}, image
        assert image.get("src", "").startswith("/") and not image.get("src", "").startswith("//"), image
    steps = [image for image in parsed.images if "/img/manual/" in image.get("src", "")]
    assert len(steps) == 3
    for image in steps:
        assert image.get("loading") == "lazy" and image.get("decoding") == "async", image


def test_microdata_describes_visible_page_only():
    page = home_source()
    assert '<div class="land-left" itemscope itemtype="https://schema.org/WebSite">' in page
    assert 'itemprop="publisher" itemscope itemtype="https://schema.org/Organization"' in page
    assert '<p class="land-lead" itemprop="description">Сканируйте чеки камерой телефона' in page
    assert '<span itemprop="name">ООО «Ямастер»</span>' in page
    assert '<span itemprop="email">chek@ymaster.ru</span>' in page
    for absent in ("ld+json", "FAQPage", "aggregateRating", "ratingValue", "price", "SoftwareApplication"):
        assert absent not in page, absent


def test_host_indexing_rules_survive(client):
    prod = client.get("/", headers={"host": PROD, "x-forwarded-proto": "https"})
    assert prod.status_code == 200 and "x-robots-tag" not in prod.headers
    assert '<link rel="canonical" href="https://chek.ymaster.ru/">' in prod.text
    assert seo.MARKER not in prod.text
    copy = client.get("/", headers={"host": "localhost:8000"})
    assert copy.headers["x-robots-tag"] == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in copy.text
    cfg = {"mode": "on", "yandex": "abc123def", "google": "go-0987654321"}
    tags = seo.head_tags(cfg, PROD, "https")
    assert "abc123def" in tags and "go-0987654321" in tags


@pytest.mark.skipif(not Path(HTML_VALIDATE).exists(), reason="html-validate не установлен")
def test_homepage_markup_passes_html_validate(tmp_path):
    template = whole_page()
    files = []
    for host, scheme in ((PROD, "https"), ("localhost:8000", "http")):
        name = tmp_path / f"home-{scheme}.html"
        name.write_text(seo.render_index(template, dict(seo.DEFAULTS), host, scheme), encoding="utf-8")
        files.append(str(name))
    report = tmp_path / "html-validate.json"
    with report.open("w", encoding="utf-8") as stream:
        subprocess.run([HTML_VALIDATE, "--formatter=json", *files], cwd=ROOT, stdout=stream,
                       stderr=subprocess.PIPE, text=True, timeout=300, check=False)
    data = json.loads(report.read_text(encoding="utf-8") or "[]")
    errors = [(m["ruleId"], m["line"], Path(f["filePath"]).name)
              for f in data for m in f.get("messages", []) if m["severity"] == 2]
    assert not errors, errors[:8]
