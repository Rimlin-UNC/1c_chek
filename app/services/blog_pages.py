"""T-82+: блог-кластер из 10 серверных статей под 25 ключей с фирменными иллюстрациями,
микроразметкой Schema.org (HTML5 Microdata для Яндекса и Google) и интерактивной каруселью на /blog.

Страницы не дублируют варианты одного запроса: у каждой статьи своя задача, лид, шаги, спорная ситуация,
мини-инструмент, FAQ, две иллюстрации в палитре «Ямастер» (WebP + JPEG) и путь к коммерческой странице.
Весь контент и все ссылки отдаются видимым серверным HTML без JSON-LD, скрытого текста, цен, рейтингов и
несуществующих отзывов. На /blog поверх нативной CSS Scroll-Snap карусели подключается лёгкий внешний
ES-модуль прогрессивного улучшения (/js/blog.js, совместим с CSP script-src 'self'). Канонический адрес
и noindex для копий — по тем же правилам, что у главной и трёх кластерных страниц (services/seo.py).
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from . import seo

BLOG_INDEX = "/blog"
PRODUCTION_HOST = seo.PRODUCTION_HOST
IMG_WIDTH = 1376
IMG_HEIGHT = 768
DATE_PUBLISHED = "2026-11-08"
DATE_MODIFIED = "2026-11-18"

CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("qr", "QR и реквизиты", "Сканирование камерой, расшифровка полей и ручной ввод"),
    ("fns", "ФНС и ОФД", "Госреестр, маршрут фискальных данных и разбор отказов"),
    ("b2b", "Бизнес и 1С", "Подотчёт сотрудников, выписки банков, УПД и аутсорсинг"),
)


@dataclass(frozen=True)
class ArticleImage:
    src: str
    alt: str
    title: str
    caption: str

    @property
    def webp(self) -> str:
        return self.src.rsplit(".", 1)[0] + ".webp"


@dataclass(frozen=True)
class Article:
    path: str
    title: str
    description: str
    h1: str
    keywords: tuple[str, ...]
    category_slug: str
    category: str
    read_min: int
    teaser: str
    benefit: str
    lead: str
    how_title: str
    steps: tuple[str, ...]
    how_extra: str
    not_found: str
    tool_title: str
    tool_items: tuple[str, ...]
    extra_title: str
    extra_paragraphs: tuple[str, ...]
    faq: tuple[tuple[str, str], ...]
    cover: ArticleImage
    secondary: ArticleImage
    up: tuple[str, str]
    side: tuple[str, str]
    down: tuple[str, str]
    cta: str

    @property
    def images(self) -> tuple[tuple[str, str], ...]:
        """Совместимый кортеж (src, alt) для обеих иллюстраций статьи."""
        return ((self.cover.src, self.cover.alt), (self.secondary.src, self.secondary.alt))


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


def _picture(image: ArticleImage, *, eager: bool = False, itemprop: bool = False, cls: str = "") -> str:
    loading = "eager" if eager else "lazy"
    priority = ' fetchpriority="high"' if eager else ""
    prop = ' itemprop="contentUrl url"' if itemprop else ""
    class_attr = f' class="{_e(cls)}"' if cls else ""
    return (
        f"<picture>"
        f'<source srcset="{_e(image.webp)}" type="image/webp">'
        f'<img{class_attr}{prop} src="{_e(image.src)}" alt="{_e(image.alt)}" title="{_e(image.title)}" '
        f'width="{IMG_WIDTH}" height="{IMG_HEIGHT}" loading="{loading}"{priority} decoding="async">'
        f"</picture>"
    )


def _figure(image: ArticleImage, *, eager: bool = False, hero: bool = False) -> str:
    mod = " blog-figure--hero" if hero else ""
    return (
        f'<figure class="blog-figure{mod}" itemprop="image" itemscope itemtype="https://schema.org/ImageObject">'
        f"{_picture(image, eager=eager, itemprop=True)}"
        f'<meta itemprop="width" content="{IMG_WIDTH}">'
        f'<meta itemprop="height" content="{IMG_HEIGHT}">'
        f'<figcaption itemprop="caption">{_e(image.caption)}</figcaption>'
        f"</figure>"
    )


def _toc(article: Article) -> str:
    return f"""<nav class="blog-toc" aria-label="Содержание разбора">
  <span class="blog-toc__label">В статье:</span>
  <a href="#sec-how">По шагам</a>
  <a href="#sec-trust">Если не найден</a>
  <a href="#sec-tool">{_e(article.benefit)}</a>
  <a href="#sec-extra">На практике</a>
  <a href="#sec-faq">FAQ</a>
</nav>"""


def _body(article: Article) -> str:
    steps = "".join(f"<li>{step}</li>" for step in article.steps)
    tool = "".join(f"<li>{item}</li>" for item in article.tool_items)
    extra = "".join(f"<p>{paragraph}</p>" for paragraph in article.extra_paragraphs)
    faq = "".join(
        f'<div class="blog-faq__item"><h3>{_e(question)}</h3><p>{answer}</p></div>'
        for question, answer in article.faq
    )
    fig2 = _figure(article.secondary)
    return f"""<div class="blog-article__body" itemprop="articleBody">
<section class="blog-how" id="sec-how">
  <h2>{_e(article.how_title)}</h2>
  <ol class="steps">{steps}</ol>
  {article.how_extra}
</section>
<section class="blog-trust" id="sec-trust">
  <h2>Что делать, если чек не находится</h2>
  {article.not_found}
</section>
{fig2}
<section class="blog-tool" id="sec-tool">
  <h2>{_e(article.tool_title)}</h2>
  <ul class="blog-tool__list">{tool}</ul>
</section>
<section class="blog-extra" id="sec-extra">
  <h2>{_e(article.extra_title)}</h2>
  {extra}
</section>
<section class="blog-faq" id="sec-faq">
  <h2>FAQ: частые вопросы</h2>
  <div class="blog-faq__list">{faq}</div>
</section>
</div>"""


def _head(
    title: str,
    description: str,
    url: str,
    base: str,
    cover: ArticleImage,
    canonical: bool,
    *,
    og_type: str = "article",
    section: str = "",
    with_carousel_js: bool = False,
) -> str:
    if canonical:
        robots = (
            f'<link rel="canonical" href="{_e(url)}">\n'
            '<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">'
        )
    else:
        robots = '<meta name="robots" content="noindex, nofollow">'
    og_image = base + cover.src
    article_meta = ""
    if og_type == "article":
        article_meta = (
            f'\n<meta property="article:published_time" content="{DATE_PUBLISHED}">'
            f'\n<meta property="article:modified_time" content="{DATE_MODIFIED}">'
            + (f'\n<meta property="article:section" content="{_e(section)}">' if section else "")
        )
    script_tag = (
        f'\n<script type="module" src="/js/blog.js?v={_e(settings.APP_VERSION)}"></script>'
        if with_carousel_js else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
{robots}
<meta name="theme-color" content="#f5f5f5">
<meta property="og:type" content="{_e(og_type)}">
<meta property="og:site_name" content="Ямастер Чек">
<meta property="og:locale" content="ru_RU">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:url" content="{_e(url)}">
<meta property="og:image" content="{_e(og_image)}">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:width" content="{IMG_WIDTH}">
<meta property="og:image:height" content="{IMG_HEIGHT}">
<meta property="og:image:alt" content="{_e(cover.alt)}">{article_meta}
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_e(title)}">
<meta name="twitter:description" content="{_e(description)}">
<meta name="twitter:image" content="{_e(og_image)}">
<meta name="twitter:image:alt" content="{_e(cover.alt)}">
<link rel="icon" href="/img/logo.svg" type="image/svg+xml">
<link rel="icon" href="/img/icon-32.png" sizes="32x32" type="image/png">
<link rel="icon" href="/img/icon-192.png" sizes="192x192" type="image/png">
<link rel="apple-touch-icon" href="/img/apple-touch-icon.png" sizes="180x180">
<link rel="preload" href="/fonts/inter-cyrillic-wght-normal.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{_e(cover.webp)}" as="image" type="image/webp">
<link rel="stylesheet" href="/css/pages.css?v={_e(settings.APP_VERSION)}">{script_tag}
</head>"""


def _shell_top(*, reading_bar: bool = False, wide: bool = False) -> str:
    bar = '<div class="read-progress"></div>\n' if reading_bar else ""
    top_cls = "top top--wide" if wide else "top"
    nav_blog = (
        f'<a class="top__blog" href="{BLOG_INDEX}">Все разборы</a>'
        if reading_bar else '<a class="top__blog" href="/#/public?cam=1">Сканер QR</a>'
    )
    return f"""<body>
{bar}<a class="skip" href="#main">К тексту</a>
<header class="{top_cls}">
  <a class="brand" href="/" aria-label="DocVer — Ямастер Чек"><picture><source type="image/webp" srcset="/img/brand/logo-h-1x.webp 1x, /img/brand/logo-h-2x.webp 2x, /img/brand/logo-h-3x.webp 3x"><img src="/img/brand/logo-h-1x.png" srcset="/img/brand/logo-h-1x.png 1x, /img/brand/logo-h-2x.png 2x, /img/brand/logo-h-3x.png 3x" alt="DocVer — Документальная Верификация" width="141" height="44" loading="eager"></picture></a>
  <div class="top__actions">
    {nav_blog}
    <a class="btn" href="/#land-login">Войти</a>
  </div>
</header>"""


def _shell_bottom(*, wide: bool = False) -> str:
    cls = "foot foot--wide" if wide else "foot"
    return f"""<footer class="{cls}">
  <p><a href="/">Главная</a> · <a href="/blog">Блог</a> · <a href="/kak-proverit-chek">Как проверить чек</a> ·
     <a href="/cheki-sotrudnikov-v-1c">Чеки в 1С</a> · <a href="/dlya-buhgalterov">Бухгалтерам</a> ·
     <a href="/oferta">Оферта</a></p>
  <p>ООО «Ямастер» · <a href="https://ymaster.ru" rel="noopener">ymaster.ru</a> ·
     <a href="mailto:chek@ymaster.ru">chek@ymaster.ru</a></p>
</footer>
</body>
</html>"""


def _crumbs(base: str, crumb: str) -> str:
    return f"""<nav class="crumbs" aria-label="Вы здесь">
  <ol itemscope itemtype="https://schema.org/BreadcrumbList">
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
      <a itemprop="item" href="{_e(base)}/"><span itemprop="name">Ямастер Чек</span></a>
      <meta itemprop="position" content="1"></li>
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
      <a itemprop="item" href="{_e(base)}{BLOG_INDEX}"><span itemprop="name">Блог</span></a>
      <meta itemprop="position" content="2"></li>
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" aria-current="page">
      <span itemprop="name">{_e(crumb)}</span>
      <meta itemprop="position" content="3"></li>
  </ol>
</nav>"""


def _next_card(article: Article) -> str:
    neighbor = BY_PATH.get(article.side[0])
    if neighbor is None:
        return ""
    return f"""<div class="blog-next">
  <a class="blog-next__media" href="{_e(neighbor.path)}">
    {_picture(neighbor.cover, cls="blog-next__img")}
  </a>
  <div class="blog-next__body">
    <span class="blog-badge" data-cat="{_e(neighbor.category_slug)}">{_e(neighbor.category)} · {neighbor.read_min} мин</span>
    <p class="blog-next__title"><a href="{_e(neighbor.path)}">{_e(neighbor.h1)}</a></p>
    <p class="blog-next__teaser">{_e(neighbor.teaser)}</p>
  </div>
</div>"""


def _publisher_line(base: str) -> str:
    return f"""<p class="blog-byline">
  Подготовлено редакцией
  <span itemprop="author" itemscope itemtype="https://schema.org/Organization">
    <a itemprop="url" href="https://ymaster.ru" rel="noopener"><span itemprop="name">ООО «Ямастер»</span></a>
  </span>
  <span itemprop="publisher" itemscope itemtype="https://schema.org/Organization">
    <meta itemprop="name" content="Ямастер Чек">
    <link itemprop="url" href="{_e(base)}/">
    <link itemprop="logo" href="{_e(base)}/img/icon-512.png">
  </span>
  · Практика работы с кассовыми реквизитами, ФНС и 1С
</p>"""


def _cta_and_links(article: Article, base: str) -> str:
    up, side, down = article.up, article.side, article.down
    return f"""<section class="blog-cta">
  <h2>Проверьте чек сейчас</h2>
  <p class="blog-cta__text">{_e(article.cta)}</p>
  <p class="blog-cta__actions">
    <a class="btn btn-primary" href="/#/public?cam=1">📸 Проверить чек онлайн</a>
    <a class="btn" href="mailto:chek@ymaster.ru?subject=%D0%94%D0%B5%D0%BC%D0%BE%20%D0%B4%D0%BB%D1%8F%20%D0%B1%D0%B8%D0%B7%D0%BD%D0%B5%D1%81%D0%B0">Для бизнеса — демо</a>
  </p>
</section>
<nav class="blog-links" aria-label="Перелинковка">
  <h2>Читать дальше</h2>
  {_next_card(article)}
  <ul class="blog-links__list">
    <li><a href="{_e(up[0])}">{_e(up[1])}</a> — вверх к хабу</li>
    <li><a href="{_e(side[0])}">{_e(side[1])}</a> — смежная статья</li>
    <li><a href="{_e(down[0])}">{_e(down[1])}</a> — к сервису</li>
    <li><a href="{BLOG_INDEX}">Все статьи блога в карусели</a></li>
  </ul>
</nav>
{_publisher_line(base)}"""


def render(article: Article, cfg: dict | None = None, host_header: str | None = None,
           scheme: str | None = None) -> str:
    """Одна статья целиком; по умолчанию — рабочий адрес и настройки индексации auto."""
    cfg = cfg or dict(seo.DEFAULTS)
    host = host_header or PRODUCTION_HOST
    scheme = scheme or "https"
    base = seo.origin(host, scheme)
    url = base + article.path
    canonical = seo.indexing_enabled(cfg, host)
    fig1 = _figure(article.cover, eager=True, hero=True)
    return (
        _head(
            article.title,
            article.description,
            url,
            base,
            article.cover,
            canonical,
            og_type="article",
            section=article.category,
        )
        + _shell_top(reading_bar=True)
        + _crumbs(base, article.h1)
        + f"""<main id="main" class="page">
<article class="blog-article" itemscope itemtype="https://schema.org/BlogPosting">
<meta itemprop="mainEntityOfPage" content="{_e(url)}">
<meta itemprop="inLanguage" content="ru">
<meta itemprop="dateModified" content="{DATE_MODIFIED}">
<div class="blog-meta">
  <span class="blog-badge" data-cat="{_e(article.category_slug)}" itemprop="articleSection">{_e(article.category)}</span>
  <span class="blog-meta__item">{article.read_min} мин чтения</span>
  <time class="blog-meta__item" itemprop="datePublished" datetime="{DATE_PUBLISHED}">Ноябрь 2026</time>
</div>
<h1 itemprop="headline">{_e(article.h1)}</h1>
<p class="lead" itemprop="description">{_e(article.lead)}</p>
{_toc(article)}
{fig1}
{_body(article)}
{_cta_and_links(article, base)}
</article>
<aside class="more">
  <div class="more__cta">
    <p class="more__eyebrow">Быстрый старт</p>
    <p class="more__note">Первый документ сверяется с реестром сразу в браузере — без установки приложения.</p>
    <a class="btn btn-primary more__btn" href="/#/public?cam=1">Открыть сканер</a>
  </div>
  <h2>Ещё по теме</h2>
  <ul>
    <li><a href="/kak-proverit-chek">Подробная инструкция</a> — QR-код, ФНС и спорные случаи</li>
    <li><a href="/cheki-sotrudnikov-v-1c">Чеки сотрудников в 1С</a> — авансовые отчёты</li>
    <li><a href="/dlya-buhgalterov">Для бухгалтеров</a> — выписки и УПД</li>
    <li><a href="{BLOG_INDEX}">Блог о проверке чеков</a> — все 10 статей и карусель</li>
  </ul>
</aside>
</main>"""
        + _shell_bottom()
    )


def _carousel_slide(index: int, total: int, article: Article) -> str:
    num = f"{index + 1:02d}"
    tot = f"{total:02d}"
    active = ' data-active="true"' if index == 0 else ""
    eager = index == 0
    return f"""<article class="bcar__slide" id="blog-slide-{index + 1}" data-slide="{index}" data-cat="{_e(article.category_slug)}"{active} aria-label="Слайд {index + 1} из {total}: {_e(article.h1)}">
  <a class="bcar__media" href="{_e(article.path)}">
    {_picture(article.cover, eager=eager, cls="bcar__img")}
    <span class="bcar__index">{num} / {tot}</span>
  </a>
  <div class="bcar__content">
    <div class="bcar__meta">
      <span class="blog-badge" data-cat="{_e(article.category_slug)}">{_e(article.category)}</span>
      <span class="bcar__time">{article.read_min} мин чтения</span>
    </div>
    <h3 class="bcar__heading"><a class="bcar__title" href="{_e(article.path)}">{_e(article.h1)}</a></h3>
    <p class="bcar__teaser">{_e(article.teaser)}</p>
    <div class="bcar__footer">
      <span class="bcar__benefit">{_e(article.benefit)}</span>
      <a class="btn btn-primary bcar__cta" href="{_e(article.path)}">Читать статью →</a>
    </div>
  </div>
</article>"""


def _dot_button(index: int, article: Article) -> str:
    cls = "bcar__dot is-active" if index == 0 else "bcar__dot"
    cur = ' aria-current="true"' if index == 0 else ""
    return (
        f'<button type="button" class="{cls}" data-slide-to="{index}" '
        f'aria-label="Статья {index + 1}: {_e(article.h1)}"{cur}>'
        f'<span class="bcar__dot-fill"></span></button>'
    )


def _carousel_section() -> str:
    total = len(PAGES)
    slides = "\n".join(_carousel_slide(i, total, article) for i, article in enumerate(PAGES))
    dots = "".join(_dot_button(i, a) for i, a in enumerate(PAGES))
    return f"""<section class="bcar-section" aria-labelledby="carousel-heading">
  <div class="bcar-section__head">
    <div>
      <p class="blog-eyebrow">Интерактивная витрина · 10 иллюстрированных разборов</p>
      <h2 id="carousel-heading">Листайте статьи по ситуациям</h2>
    </div>
    <p class="bcar-section__hint">Листайте свайпом, стрелками или выберите карточку для перехода в разбор</p>
  </div>
  <section class="bcar" data-blog-carousel aria-roledescription="карусель" aria-label="Карусель статей блога">
    <div class="bcar__viewport">
      <div class="bcar__track" data-carousel-track role="group" tabindex="0" aria-label="Лента статей блога">
{slides}
      </div>
    </div>
    <div class="bcar__controls">
      <div class="bcar__nav">
        <button type="button" class="bcar__btn" data-carousel-prev aria-label="Предыдущая статья">←</button>
        <button type="button" class="bcar__btn" data-carousel-next aria-label="Следующая статья">→</button>
        <button type="button" class="bcar__pause" data-carousel-toggle aria-pressed="false" aria-label="Остановить автопрокрутку">Пауза</button>
      </div>
      <div class="bcar__dots" data-carousel-dots role="group" aria-label="Выбор слайда">{dots}</div>
      <p class="bcar__status" data-carousel-status aria-live="polite">01 / {total:02d}</p>
    </div>
  </section>
</section>"""


def _catalog_card(index: int, base: str, article: Article) -> str:
    url = base + article.path
    return f"""<li class="blog-card" data-card-cat="{_e(article.category_slug)}" itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
  <meta itemprop="position" content="{index + 1}">
  <article class="blog-card__inner" itemprop="item" itemscope itemtype="https://schema.org/BlogPosting">
    <meta itemprop="mainEntityOfPage" content="{_e(url)}">
    <meta itemprop="datePublished" content="{DATE_PUBLISHED}">
    <meta itemprop="dateModified" content="{DATE_MODIFIED}">
    <a class="blog-card__media" href="{_e(article.path)}" itemprop="url">
      <span class="blog-card__pic" itemprop="image" itemscope itemtype="https://schema.org/ImageObject">
        {_picture(article.cover, itemprop=True, cls="blog-card__img")}
        <meta itemprop="width" content="{IMG_WIDTH}">
        <meta itemprop="height" content="{IMG_HEIGHT}">
      </span>
    </a>
    <div class="blog-card__body">
      <div class="blog-card__meta">
        <span class="blog-badge" data-cat="{_e(article.category_slug)}" itemprop="articleSection">{_e(article.category)}</span>
        <span class="blog-card__time">{article.read_min} мин · {_e(article.benefit)}</span>
      </div>
      <h3 class="blog-card__title" itemprop="headline"><a href="{_e(article.path)}">{_e(article.h1)}</a></h3>
      <p class="blog-card__desc" itemprop="description">{_e(article.description)}</p>
      <div class="blog-card__foot">
        <span itemprop="author" itemscope itemtype="https://schema.org/Organization">
          <meta itemprop="name" content="ООО «Ямастер»">
        </span>
        <a class="blog-card__link" href="{_e(article.path)}">Перейти к статье →</a>
      </div>
    </div>
  </article>
</li>"""


def render_index(cfg: dict | None = None, host_header: str | None = None,
                 scheme: str | None = None) -> str:
    """Индекс блога: один H1, интерактивная карусель, фильтр рубрик и crawlable-карточки всех 10 статей."""
    cfg = cfg or dict(seo.DEFAULTS)
    host = host_header or PRODUCTION_HOST
    scheme = scheme or "https"
    base = seo.origin(host, scheme)
    url = base + BLOG_INDEX
    canonical = seo.indexing_enabled(cfg, host)

    counts = {slug: sum(1 for a in PAGES if a.category_slug == slug) for slug, _, _ in CATEGORIES}
    filter_buttons = [
        f'<button type="button" class="blog-filter__btn is-active" data-filter="all" aria-pressed="true">'
        f'Все статьи <span class="blog-filter__count">{len(PAGES)}</span></button>'
    ] + [
        f'<button type="button" class="blog-filter__btn" data-filter="{_e(slug)}" aria-pressed="false">'
        f'{_e(label)} <span class="blog-filter__count">{counts[slug]}</span></button>'
        for slug, label, _ in CATEGORIES
    ]
    filter_bar = "\n    ".join(filter_buttons)
    route_cards = "\n".join(
        f'<a class="blog-route" href="#blog-catalog" data-jump-filter="{_e(slug)}">'
        f'<span class="blog-route__title">{_e(label)} · {counts[slug]}</span>'
        f'<span class="blog-route__desc">{_e(desc)}</span></a>'
        for slug, label, desc in CATEGORIES
    )
    cards = "\n".join(_catalog_card(i, base, a) for i, a in enumerate(PAGES))

    return (
        _head(
            "Блог о проверке чеков: инструкции, ФНС и 1С — Ямастер Чек",
            "Десять статей о проверке кассовых чеков: QR-код, ручной код, ФНС, ОФД, налоговая, чеки сотрудников и массовая сверка для бухгалтеров.",
            url,
            base,
            PAGES[0].cover,
            canonical,
            og_type="website",
            with_carousel_js=True,
        )
        + _shell_top(wide=True)
        + f"""<nav class="crumbs crumbs--wide" aria-label="Вы здесь">
  <ol itemscope itemtype="https://schema.org/BreadcrumbList">
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
      <a itemprop="item" href="{_e(base)}/"><span itemprop="name">Ямастер Чек</span></a>
      <meta itemprop="position" content="1"></li>
    <li itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" aria-current="page">
      <span itemprop="name">Блог</span>
      <meta itemprop="position" content="2"></li>
  </ol>
</nav>
<main id="main" class="blog-hub" itemscope itemtype="https://schema.org/Blog">
<meta itemprop="url" content="{_e(url)}">
<meta itemprop="inLanguage" content="ru">
<section class="blog-hero">
  <div class="blog-hero__top">
    <p class="blog-eyebrow">Практический журнал ООО «Ямастер» · 10 разборов с иллюстрациями</p>
    <h1 itemprop="name">Блог о проверке чеков</h1>
    <p class="lead" itemprop="description">Собрали десять наглядных материалов: от сканирования QR-кода телефоном за 30 секунд до массовой сверки кассовых чеков с ФНС, работы с банковскими выписками и выгрузки в 1С. Каждая статья разбирает живую ситуацию, снабжена фирменными схемами и ведёт к рабочему инструменту.</p>
  </div>
  <nav class="blog-routes" aria-label="Быстрый выбор темы">
{route_cards}
  </nav>
</section>

{_carousel_section()}

<section class="blog-catalog" id="blog-catalog" aria-labelledby="catalog-heading" itemscope itemtype="https://schema.org/ItemList">
  <meta itemprop="numberOfItems" content="{len(PAGES)}">
  <div class="blog-catalog__head">
    <div>
      <h2 id="catalog-heading">Все статьи кластера</h2>
      <p class="blog-catalog__sub" data-filter-status aria-live="polite">Показаны все 10 статей в трёх рубриках</p>
    </div>
    <div class="blog-filter" role="group" aria-label="Фильтр статей по рубрикам">
    {filter_bar}
    </div>
  </div>
  <ol class="blog-grid" data-blog-grid>
{cards}
  </ol>
</section>

<section class="blog-about">
  <div class="blog-about__text">
    <h2>Как устроен блог и куда идти дальше</h2>
    <p>Общие запросы «проверка чека» и «проверка чеков» закрывает главная: там сканер камеры, начисление баллов и вход для организации. Уточняющие вопросы про ФНС, ОФД, ручной код и сайт налоговой подробно разобраны в инструкции и в статьях выше. Для компаний отдельно показаны контроль авансовых чеков сотрудников и пакетная работа бухгалтера с несколькими юрлицами.</p>
    <p>Мы опираемся только на реальную логику программы: считывание шести фискальных реквизитов, сопоставление с реестром налоговой, защиту от дублей по связке ФН + ФД + ФП и подготовку файлов для учётной системы.</p>
    <p class="blog-cta__actions">
      <a class="btn btn-primary" href="/#/public?cam=1">📸 Проверить чек без регистрации</a>
      <a class="btn" href="/kak-proverit-chek">Пошаговая инструкция по QR</a>
    </p>
  </div>
  <aside class="blog-hubs" aria-labelledby="hubs-heading">
    <h2 id="hubs-heading">Разделы и инструменты</h2>
    <ul class="blog-hubs__list">
      <li><a href="/">Проверить чек на главной</a> — сканер камеры без регистрации для первого документа.</li>
      <li><a href="/kak-proverit-chek">Как проверить чек</a> — подробный хаб по строке QR и реквизитам.</li>
      <li><a href="/cheki-sotrudnikov-v-1c">Чеки сотрудников в 1С</a> — авансовые отчёты, лимиты и выгрузка.</li>
      <li><a href="/dlya-buhgalterov">Для бухгалтеров</a> — выписки банков по API, УПД и несколько компаний.</li>
      <li><a href="/oferta">Оферта</a> — открытые условия использования сервиса.</li>
    </ul>
    <span itemprop="publisher" itemscope itemtype="https://schema.org/Organization">
      <meta itemprop="name" content="ООО «Ямастер»">
      <link itemprop="url" href="https://ymaster.ru">
      <link itemprop="logo" href="{_e(base)}/img/icon-512.png">
    </span>
  </aside>
</section>
</main>"""
        + _shell_bottom(wide=True)
    )


PAGES: tuple[Article, ...] = (
    Article(
        path="/blog/proverka-cheka-onlajn",
        title="Проверка чека онлайн: подлинность за 30 секунд — Ямастер Чек",
        description="Проверка чека онлайн по QR-коду: сканируйте камерой телефона, сверяйте сумму и дату с ФНС, узнавайте результат без регистрации для первого чека.",
        h1="Проверка чека онлайн: как за 30 секунд узнать подлинность",
        keywords=("проверка чека онлайн", "проверка чеков онлайн", "сайт проверки чека"),
        category_slug="qr",
        category="QR и реквизиты",
        read_min=3,
        teaser="Наведите камеру телефона на квадратный код и сразу сверьте сумму, дату и фискальный признак с базой налоговой — без установки приложения и регистрации для первого документа.",
        benefit="5 признаков подделки",
        lead="Бумажный чек выцвел, а подтвердить покупку нужно сейчас? Проверка чека онлайн занимает меньше минуты: камера читает QR-код, реквизиты сверяются с базой ФНС, а результат виден сразу. Ниже — последовательность действий и признаки, по которым отличить настоящий документ от ошибочного.",
        how_title="Как проверить чек онлайн за три шага",
        steps=(
            "Откройте сайт проверки чека и нажмите «Сканировать чек». Первый документ можно проверить без регистрации.",
            "Наведите камеру на квадратный код в верхней части чека или загрузите фотографию. Дата, сумма, ФН, ФД и ФП распознаются автоматически.",
            "Сравните ответ сервиса с напечатанной строкой. Совпадение фискального признака и суммы означает, что запись нашлась в данных налоговой.",
        ),
        how_extra="<p>Проверка чеков онлайн удобна тем, что не требует устанавливать отдельное приложение: веб-страница открывается в браузере телефона, а камера работает через стандартный доступ. Если объектив не поймал код с первого раза, улучшите освещение и расправьте бумагу — блики и перегибы чаще всего мешали именно распознаванию, а не самой передаче данных.</p>",
        not_found="<p>Иногда чек ещё не дошёл до ФНС через оператора фискальных данных. Сначала перечитайте реквизиты вручную: одна перепутанная цифра в ФН или ФД даёт совсем другую запись. Затем повторите запрос через сутки. Если результата по-прежнему нет, попросите продавца электронную копию или другой документ об оплате.</p><ol><li>Сверьте дату, сумму и контрольное число.</li><li>Подождите доставку данных через ОФД.</li><li>Сохраните переписку с продавцом для бухгалтерии.</li></ol>",
        tool_title="Мини-инструмент: 5 признаков поддельного чека",
        tool_items=(
            "Фискальный признак не совпадает с напечатанным на бумаге.",
            "Сумма в ответе налоговой отличается от итога на чеке.",
            "Дата покупки сдвинута на другой день или час.",
            "Нет позиций либо цена не умножается на количество.",
            "QR-код ведёт на стороннюю страницу вместо реквизитов.",
        ),
        extra_title="Почему скорость важна и для частного покупателя, и для компании",
        extra_paragraphs=(
            "Частному лицу результат нужен для возврата товара, гарантийного спора или простого спокойствия: чек подтверждает, что покупка действительно прошла через кассу. Чем раньше проверить документ, тем проще заметить опечатку продавца и попросить корректную копию, пока кассовая лента ещё доступна.",
            "В компании к этому добавляется подотчёт: сотрудник фотографирует чек на объекте, бухгалтер видит статус и позиции, а дубли отсекаются по связке ФН, ФД и ФП. Поэтому один и тот же сценарий обслуживает и бытовой запрос, и регулярный документооборот. Главная страница остаётся точкой конверсии, а эта статья снимает вопросы перед первым сканированием.",
            "Не стоит искать волшебную кнопку «подлинность навсегда»: данные ФНС обновляются, а корректность бумажного экземпляра всё равно проверяется человеком. Наш сервис показывает, что именно нашлось в реестре, и честно сообщает о расхождении, а не подменяет собой официальный источник.",
        ),
        faq=(
            ("Можно ли проверить первый чек без аккаунта?", "Да, гостевой сценарий открывает сканер сразу. Регистрация нужна для истории, баллов и работы с компанией."),
            ("Сколько хранится результат?", "История доступна вошедшему пользователю; компания видит свои чеки в общем реестре и выгружает их в учётную систему."),
            ("Что делать, если камера не читает код?", "Введите дату, сумму, ФН, ФД и ФП вручную — те же реквизиты напечатаны текстом под QR-кодом."),
        ),
        cover=ArticleImage(
            src="/img/blog/01-online-check.jpg",
            alt="Смартфон считывает QR-код бумажного чека и выводит зелёную отметку подлинности",
            title="Быстрое сканирование кассового чека камерой телефона в браузере",
            caption="Камера считывает квадратный код за секунду и выводит карточку совпадения суммы, времени и фискального признака.",
        ),
        secondary=ArticleImage(
            src="/img/blog/07-not-found.jpg",
            alt="Бумажный чек под лупой со знаком вопроса, контрольный планшет и часы ожидания",
            title="Пошаговая диагностика, если запись ещё не появилась в реестре",
            caption="Когда запись не вернулась с первой попытки, проверьте цифры реквизитов по списку и дайте ОФД время доставить пакет.",
        ),
        up=("/kak-proverit-chek", "проверка чека онлайн"),
        side=("/blog/proverka-cheka-po-qr-kodu", "подробная инструкция по QR-коду"),
        down=("/", "проверить чек на chek.ymaster.ru"),
        cta="Проверьте чек на chek.ymaster.ru",
    ),
    Article(
        path="/blog/proverka-cheka-po-qr-kodu",
        title="Проверка чека по QR: пошаговая инструкция — Ямастер Чек",
        description="Проверка чека по QR: что зашифровано в квадратном коде, как отсканировать его телефоном и почему повреждённый код не мешает сверить реквизиты вручную.",
        h1="Проверка чека по QR: пошаговая инструкция",
        keywords=("проверка чека по qr", "проверка чеков по qr", "проверка чека онлайн по qr"),
        category_slug="qr",
        category="QR и реквизиты",
        read_min=3,
        teaser="Разбираем анатомию квадратного кода на кассовой ленте: какие шесть полей t, s, fn, i, fp, n в нём зашифрованы, как снять блик при съёмке и что делать с потёртой бумагой.",
        benefit="Памятка: 6 полей QR",
        lead="QR-код на кассовом чеке — это не ссылка на сайт магазина, а компактная строка фискальных реквизитов. Проверка чека по qr начинается с правильного считывания этой строки; дальше сервис сам сопоставляет поля с записью ФНС. Разбираем, где искать код и как действовать, если он стёрся.",
        how_title="Как проверить чек по QR-коду без ошибок",
        steps=(
            "Найдите квадратный код рядом с датой и итоговой суммой. Не фотографируйте весь лист целиком — кадр должен захватывать код и немного свободного поля вокруг него.",
            "Держите телефон параллельно бумаге, уберите блик и дождитесь автофокуса. Проверка чеков по qr не требует специального приложения: браузер запросит доступ к камере только на время сканирования.",
            "Проверьте распознанную строку: t, s, fn, i, fp и n. Если хотя бы одно значение отличается от напечатанного, исправьте его вручную до отправки.",
        ),
        how_extra="<p>Проверка чека онлайн по qr особенно полезна на кассе: покупатель ещё держит документ в руках и может сразу заметить расхождение. Онлайн-касса формирует фискальный признак для каждого чека отдельно, поэтому две похожие покупки с одинаковой суммой всё равно имеют разные ФД и ФП. Не пытайтесь «подогнать» код под соседний чек — это частая причина ложного отказа.</p>",
        not_found="<p>Если код прочитан, а запись не находится, сначала исключите опечатку в fn или fp: эти поля длинные и легко теряют цифру при ручном вводе. Затем дайте оператору фискальных данных время передать пакет в налоговую. При устойчивом расхождении сохраните фото обеих сторон чека и обратитесь к продавцу за копией.</p><ol><li>Перечитайте QR при другом освещении.</li><li>Сверьте шесть реквизитов с печатной строкой.</li><li>Повторите запрос на следующий день.</li></ol>",
        tool_title="Мини-инструмент: памятка из шести полей QR",
        tool_items=(
            "t — дата и время расчёта, а не дата печати бумажки.",
            "s — итог со всеми позициями и скидками.",
            "fn — номер фискального накопителя кассы.",
            "i — порядковый номер фискального документа.",
            "fp — контрольное число, вычисленное накопителем.",
            "n — признак расчёта: приход, возврат или расход.",
        ),
        extra_title="Когда сканер удобнее, а когда надёжнее ручной ввод",
        extra_paragraphs=(
            "Сканер экономит время на потоке документов: сотрудник не переписывает длинные номера, а бухгалтер получает уже структурированной запись. Именно поэтому корпоративный сценарий строится вокруг камеры телефона, а не вокруг клавиатуры.",
            "Ручной ввод выручает, когда термобумага перетёрлась, код попал под штамп или чек пришёл в виде электронного изображения с низким разрешением. Форма принимает те же шесть значений; человек сверяет их глазами и видит итог до сохранения. Это не резервная магия, а тот же протокол данных в другом интерфейсе.",
            "Для массовой проверки чеков по qr важна не только скорость кадра, но и защита от повторов. Программа сравнивает связку ФН, ФД и ФП до записи в реестр, поэтому один и тот же документ, присланный двумя сотрудниками, не превратится в две покупки.",
        ),
        faq=(
            ("Что делать, если QR-код повреждён?", "Введите реквизиты вручную: они продублированы текстом на чеке."),
            ("Почему QR-код открывает стороннюю страницу?", "Корректный код кассы содержит строку реквизитов, а не рекламную ссылку; такой чек стоит перепроверить у продавца."),
            ("Можно ли проверить чек онлайн по qr с компьютера?", "Да, загрузите фото файлом или вставьте строку QR из буфера обмена."),
        ),
        cover=ArticleImage(
            src="/img/blog/02-qr-instruction.jpg",
            alt="Квадратный код кассового чека под лупой и шесть карточек расшифровки фискальных полей",
            title="Структура шести реквизитов кассового QR-кода: время, сумма, ФН, ФД, ФП и тип расчёта",
            caption="В одном квадратном коде собраны шесть точных параметров покупки: время, сумма, накопитель, номер документа, подпись и вид операции.",
        ),
        secondary=ArticleImage(
            src="/img/blog/10-qr-vs-code.jpg",
            alt="Две карточки сравнения: сканирование свежего кода смартфоном и ввод реквизитов с мятой ленты",
            title="Переход на ручную форму при выцветшей или замятой термобумаге",
            caption="Повреждённый квадрат не блокирует сверку: печатная расшифровка приводит к тому же ответу реестра.",
        ),
        up=("/kak-proverit-chek", "проверка чека по qr"),
        side=("/blog/proverka-cheka-po-kodu", "подробнее о ручном коде"),
        down=("/", "проверить чек онлайн"),
        cta="Проверьте чек на chek.ymaster.ru",
    ),
    Article(
        path="/blog/proverka-cheka-po-kodu",
        title="Проверка чека по коду: где взять реквизиты — Ямастер Чек",
        description="Проверка чека по коду: какие цифры напечатаны под QR, как перенести ФН, ФД и ФП в форму и не перепутать номер документа с фискальным признаком.",
        h1="Проверка чека по коду: где взять код и что вводить",
        keywords=("проверка чека по коду", "проверка чеков по коду"),
        category_slug="qr",
        category="QR и реквизиты",
        read_min=3,
        teaser="Термобумага побледнела, на квадрат поставили печать или прислали скан из почты? Показываем, где найти ФН, ФД и ФП текстом и как перенести их без единой опечатки.",
        benefit="Разбор 3 частых опечаток",
        lead="Под квадратным кодом всегда есть расшифровка: дата, сумма, номер накопителя, номер документа и контрольное число. Проверка чека по коду нужна, когда камера не справилась или документ пришёл в виде копии. Ниже — точный маршрут от бумажной строки до понятного результата.",
        how_title="Как проверить чек по коду вручную",
        steps=(
            "Найдите на чеке блок «ФН», «ФД», «ФП» рядом с датой и итогом. Не путайте ФД с порядковым номером покупки в кассовой ленте.",
            "Перенесите значения в форму без пробелов и разделителей; сумму вводите с копейками через точку. Проверка чеков по коду чувствительна к одной лишней цифре.",
            "Сверьте ответ с бумагой: совпали продавец, время и итог — документ принят, расхождение уходит на ручную проверку с указанием причины.",
        ),
        how_extra="<p>Ручной маршрут часто спасает архивные чеки: термобумага через месяц теряет контраст, а текстовая расшифровка ещё читается. Если сохранили PDF или скан из почты, откройте его на большом масштабе и копируйте поля по одному, не пытаясь переписать всю строку целиком.</p>",
        not_found="<p>Самая частая причина «не найдено» здесь не работа ФНС, а перенос цифр: ФП иногда принимают за ФД, а дату берут из штампа магазина вместо времени расчёта. Проверьте ещё раз все шесть значений, затем повторите запрос позже. Если чек так и не подтвердился, приложите копию к авансовому отчёту с пояснением.</p><ol><li>Сверьте ФН, ФД и ФП попарно.</li><li>Убедитесь, что сумма включает копейки.</li><li>Повторите поиск после доставки данных ОФД.</li></ol>",
        tool_title="Мини-инструмент: 3 причины ошибки при ручном вводе",
        tool_items=(
            "Перепутаны местами ФД и ФП — у полей разная длина и назначение.",
            "Дата взята из товарного чека, а не из фискальной строки t.",
            "Сумма округлена до рублей, хотя в реестре хранятся копейки.",
        ),
        extra_title="Как ручной код вписывается в учёт компании",
        extra_paragraphs=(
            "Бухгалтер работает не с одним чеком, а с пачкой: часть документов приходит со сканера, часть — фотографиями, часть — из электронной почты. Единая форма с шестью полями позволяет принять все источники без ручного переписывания в таблицу и сразу присвоить документ подотчётному лицу.",
            "После сохранения программа показывает не только статус, но и позиции с категориями. Это помогает заметить запрещённую политикой расходов покупку до утверждения авансового отчёта. Мягкая пометка не блокирует чек: решение остаётся за человеком, а не за автоматическим правилом.",
            "Для выгрузки в 1С формируется файл EnterpriseData, либо учётная система забирает проверенные записи по расписанию. Поэтому разовая проверка чека по коду превращается в аккуратный документооборот, а не в ещё одну папку на рабочем столе.",
        ),
        faq=(
            ("Где именно напечатан код?", "Обычно под QR: строка с t, s, fn, i, fp и n или подписи ФН, ФД, ФП."),
            ("Можно ли проверить чек по коду без QR?", "Да, все реквизиты продублированы текстом специально для такого случая."),
            ("Что означает статус «Не найден»?", "Запись пока не подтвердилась в данных ФНС; сначала проверьте ввод, затем повторите позже."),
        ),
        cover=ArticleImage(
            src="/img/blog/03-manual-code.jpg",
            alt="Специалист в очках переносит нижние строки реквизитов бумажного чека в форму на ноутбуке",
            title="Ручной ввод фискальных номеров ФН, ФД и ФП с бумажного экземпляра",
            caption="Нижний блок кассовой ленты хранит полную расшифровку реквизитов для аккуратного переноса с клавиатуры.",
        ),
        secondary=ArticleImage(
            src="/img/blog/10-qr-vs-code.jpg",
            alt="Сопоставление камеры смартфона и клавиатурного ввода с общим зелёным знаком подтверждения",
            title="Ручная форма и сканер отправляют в реестр одинаковый набор реквизитов",
            caption="Ручной ввод с лупой выручает на архивной термобумаге и даёт идентичный статус для авансового отчёта.",
        ),
        up=("/kak-proverit-chek", "проверка чека по коду"),
        side=("/blog/qr-kod-ili-ruchnoj-kod", "подробнее о сравнении способов"),
        down=("/cheki-sotrudnikov-v-1c", "проверить чеки сотрудников"),
        cta="Для бизнеса — демо",
    ),
    Article(
        path="/blog/proverka-cheka-na-sajte-nalogovoj",
        title="Проверка чека на сайте налоговой: порядок — Ямастер Чек",
        description="Проверка чека на сайте налоговой: какие реквизиты запрашивает официальный сервис, чем отличается государственная проверка от нашего сценария и когда нужна копия чека.",
        h1="Проверка чека на сайте налоговой: порядок и ограничения",
        keywords=("проверка чека на сайте налоговой", "проверка чеков налоговая"),
        category_slug="fns",
        category="ФНС и ОФД",
        read_min=4,
        teaser="Государственная форма подтверждает факт регистрации документа, но не собирает авансовый отчёт и не сопоставляет выписку. Разбираем, как совместить госпроверку с учётом.",
        benefit="Список полей для формы",
        lead="Официальный сервис ФНС подтверждает, что кассовый документ зарегистрирован. Проверка чека на сайте налоговой помогает сверить подлинность, но не собирает чеки в авансовый отчёт и не выгружает их в 1С. Разбираем, какие поля понадобятся и что делать дальше с результатом.",
        how_title="Как проверить чек на сайте налоговой по реквизитам",
        steps=(
            "Откройте официальный сервис проверки кассового чека и выберите ручной ввод или сканирование QR, если доступно в вашем браузере.",
            "Укажите дату, сумму, номер фискального накопителя, номер документа и фискальный признак. Проверка чеков налоговая выполняет именно по этим данным.",
            "Сохраните ответ: государственная страница показывает факт регистрации, а для учёта понадобятся ещё позиции, сотрудник и статус в вашей программе.",
        ),
        how_extra="<p>Не путайте официальный реестр и коммерческий кабинет. Первый отвечает на вопрос «есть ли такой фискальный документ», второй — «кто его сдал, к какому отчёту относится и что с ним делать дальше». Один результат не заменяет другой, поэтому в компании обычно фиксируют оба.</p>",
        not_found="<p>Если официальная форма не возвращает чек, проверьте регистр и раскладку: длинные номера легко теряют ведущий ноль. Сделайте паузу на доставку данных через ОФД и повторите попытку. Когда документ так и не нашёлся, запросите у продавца повторный экземпляр и не включайте спорную сумму в утверждённый авансовый отчёт без пояснения.</p><ol><li>Сверьте шесть реквизитов с бумагой.</li><li>Повторите запрос через сутки.</li><li>Приложите переписку к документам компании.</li></ol>",
        tool_title="Мини-инструмент: что взять с собой в официальную форму",
        tool_items=(
            "Дата и время из строки t, а не дата составления отчёта.",
            "Итоговая сумма с копейками.",
            "ФН, ФД и ФП из печатной расшифровки.",
            "Фото или скан чека на случай спора с продавцом.",
        ),
        extra_title="Где заканчивается госпроверка и начинается работа бухгалтера",
        extra_paragraphs=(
            "После подтверждения подлинности остаётся самая рутина: распределить покупки по статьям, найти платёж в выписке, отсечь дубли и подготовить проводки. Государственный сервис не знает ни вашего подотчётного лица, ни лимитов компании, ни номера договора.",
            "Наш сценарий добавляет к факту регистрации контекст: сотрудник, статус, категории позиций, напоминание о сроке сдачи и файл для учётной системы. Поэтому проверка чека на сайте налоговой — это первый шаг, а не весь процесс закрытия месяца.",
            "Для аутсорсинга с несколькими клиентами особенно важна изоляция: чеки одной организации не должны попадать в реестр другой. В программе это решается на уровне компании, а не общим списком в браузере одного сотрудника.",
        ),
        faq=(
            ("Официальный сервис бесплатный?", "Да, это государственный ресурс ФНС; мы не публикуем цен и не подменяем его."),
            ("Почему в программе чек виден, а на сайте нет?", "Возможна задержка доставки через ОФД или расхождение в ручном вводе; сравните реквизиты."),
            ("Можно ли сразу выгрузить результат в 1С?", "Официальная страница не формирует файл учётной системы; для этого нужна карточка чека в программе."),
        ),
        cover=ArticleImage(
            src="/img/blog/04-tax-site.jpg",
            alt="Окно браузера со сверкой кассового чека под лупой, стопка отмеченных документов и папки учёта",
            title="Переход от разовой сверки на портале налоговой к папке авансового отчёта",
            caption="Официальное окно фиксирует регистрацию фискального документа, после чего проверенные листы собираются в папку организации.",
        ),
        secondary=ArticleImage(
            src="/img/blog/09-accountant-mass.jpg",
            alt="Бухгалтер за широким монитором сводит карточки организаций, банковские платежи и первичку",
            title="Раздельный учёт чеков, выписок и документов по нескольким юрлицам",
            caption="В рабочем кабинете подтверждённые суммы сразу раскладываются по организациям, выпискам и архиву закрытия периода.",
        ),
        up=("/kak-proverit-chek", "проверка чека на сайте налоговой"),
        side=("/blog/proverka-cheka-fns-rossii", "подробнее о данных ФНС"),
        down=("/dlya-buhgalterov", "проверить документы бухгалтеру"),
        cta="массовая проверка чеков и выгрузка в 1С",
    ),
    Article(
        path="/blog/proverka-cheka-fns-rossii",
        title="Проверка чека ФНС России: что сверяют — Ямастер Чек",
        description="Проверка чека ФНС России: как налоговый реестр подтверждает фискальный признак, сумму и дату, почему данные приходят через ОФД и какие поля видит бухгалтер.",
        h1="Проверка чека ФНС России: что проверяет налоговая",
        keywords=("проверка чека фнс", "проверка чеков фнс", "проверка чека фнс россии"),
        category_slug="fns",
        category="ФНС и ОФД",
        read_min=3,
        teaser="Заглядываем внутрь сверки с фискальным реестром: как сопоставляются время расчёта, итог и контрольное число ФП и как из ответа налоговой рождается карточка учёта.",
        benefit="3 причины расхождений",
        lead="Касса фиксирует покупку, оператор фискальных данных передаёт её в налоговую, а реестр отвечает на запрос по шести реквизитам. Проверка чека фнс поэтому показывает не «мнение сервиса», а совпадение конкретных полей. Ниже — что именно сравнивается и какие выводы из этого делает бухгалтер.",
        how_title="Как проверить чек ФНС по шести реквизитам",
        steps=(
            "Считайте QR или введите t, s, fn, i, fp и n вручную. Проверка чека фнс россии начинается с точности этой строки.",
            "Отправьте запрос и дождитесь ответа реестра: сервис сопоставляет фискальный признак с напечатанным на бумаге.",
            "Сравните дату, время и итог с позициями чека. Проверка чеков фнс считается успешной, когда все три группы совпадают.",
        ),
        how_extra="<p>Реестр не хранит фотографию вашего экземпляра и не знает, кто именно из сотрудников принёс бумажку в офис. Он подтверждает фискальное событие. Человеческий контекст добавляет программа: автор сдачи, компания, статус и связь с банковским платежом.</p>",
        not_found="<p>Задержка между кассой и реестром — нормальная ситуация при перебоях связи у оператора. Не меняйте сумму «на всякий случай»: это создаст другой запрос. Проверьте ФП, подождите сутки и только потом просите замену документа. В программе такие чеки попадают в отдельный список «Что нужно от клиента» с именем ответственного.</p><ol><li>Убедитесь, что ФП не скопирован с соседнего чека.</li><li>Повторите запрос после обновления данных.</li><li>Зафиксируйте причину для руководителя.</li></ol>",
        tool_title="Мини-инструмент: 3 причины ошибки ФНС",
        tool_items=(
            "Опечатка в ФП при ручном переносе длинного числа.",
            "Данные ещё не дошли от ОФД до реестра.",
            "Касса пробила возврат, а в отчёт попал исходный приход.",
        ),
        extra_title="Как реестр помогает закрывать месяц",
        extra_paragraphs=(
            "Для одной покупки достаточно галочки «найден». Для учёта важнее другое: все ли чеки сотрудников собраны до срока, нет ли повторов и совпадает ли итог с корпоративной картой. Поэтому результат запроса сразу попадает в карточку документа, а не остаётся в истории браузера.",
            "Программа отсекает дубли по ФН + ФД + ФП, показывает категории позиций и готовит CSV-сводку. Бухгалтер видит не абстрактный «ответ налоговой», а список задач: кому напомнить, какой документ заменить и что уже можно выгружать в 1С.",
            "Мы сознательно не называем сервис официальным источником и не обещаем мгновенного обновления реестра. Формулировка простая: проверка чеков фнс подтверждает регистрацию по тем данным, которые передала касса; решение по документу принимает человек.",
        ),
        faq=(
            ("Что такое фискальный признак?", "Контрольное число, которое накопитель вычисляет для конкретного чека; оно есть и в QR, и в печатной строке."),
            ("Почему ФНС не находит свежий чек?", "ОФД мог ещё не передать пакет; повторите проверку позже, не меняя реквизиты."),
            ("Видит ли налоговая наши баллы?", "Нет, баллы и кэшбэк — внутренний сценарий программы, к фискальному реестру он не относится."),
        ),
        cover=ArticleImage(
            src="/img/blog/05-fns-verify.jpg",
            alt="Построчное сопоставление кассового чека и базы реестра через защитный щит с галочкой",
            title="Сверка трёх ключевых групп в реестре ФНС: время, сумма и фискальная подпись",
            caption="Реестр подтверждает документ при точном совпадении времени операции, итоговой суммы и вычисленного накопителем признака.",
        ),
        secondary=ArticleImage(
            src="/img/blog/07-not-found.jpg",
            alt="Контрольный список сверки реквизитов и таймер повторного обращения к реестру",
            title="Порядок действий при временном отсутствии свежей записи в базе",
            caption="При задержке обмена карточка остаётся на контроле и ждёт повторного запроса без создания лишних дублей.",
        ),
        up=("/kak-proverit-chek", "проверка чека фнс"),
        side=("/blog/chek-ne-prohodit-proverku-v-fns", "подробнее о причинах отказа"),
        down=("/dlya-buhgalterov", "проверить первичку"),
        cta="массовая проверка чеков и выгрузка в 1С",
    ),
    Article(
        path="/blog/ofd-i-proverka-chekov",
        title="Проверка чека ОФД: кто подтверждает документ — Ямастер Чек",
        description="Проверка чека ОФД и ФН: как оператор фискальных данных передаёт чек в налоговую, почему ФН хранит подпись документа и что видит компания в своём реестре.",
        h1="Проверка чека ОФД: кто подтверждает документ",
        keywords=("проверка чека офд", "проверка чеков офд"),
        category_slug="fns",
        category="ФНС и ОФД",
        read_min=3,
        teaser="Наглядная схема цепочки «онлайн-касса с ФН → облако ОФД → реестр ФНС»: кто подписывает покупку, откуда берутся суточные задержки и почему не нужно сканировать десять раз.",
        benefit="Схема 3 узлов цепочки",
        lead="В цепочке «касса → ОФД → ФНС» у каждого участника своя роль. Проверка чека офд объясняет, откуда берётся запись и почему она иногда появляется с задержкой. Понимание маршрута снимает большинство вопросов о статусе «Не найден» и помогает не дублировать документы.",
        how_title="Как проверить чек ОФД и понять маршрут данных",
        steps=(
            "Касса формирует фискальный документ, накопитель подписывает его и передаёт оператору фискальных данных.",
            "ОФД сохраняет пакет и отправляет сведения в налоговую; проверка чеков офд фактически начинается с корректной доставки этого пакета.",
            "Сервис запрашивает реестр по ФН, ФД и ФП и показывает пользователю результат вместе с позициями чека.",
        ),
        how_extra="<p>ОФД не оценивает покупку и не принимает решение за бухгалтера. Это транспортный и архивный узел: он обязан доставить сведения и хранить их. Поэтому при сбое связи меняется не сам чек, а только время, когда запись станет доступна для запроса.</p>",
        not_found="<p>Если чек не находится, не отправляйте его повторно десять раз подряд: повторные запросы не ускорят доставку. Сверьте реквизиты, посмотрите статус в карточке документа и оставьте задачу ответственному сотруднику. Через сутки проверка обычно даёт тот же результат, что и официальный источник.</p><ol><li>Проверьте, что касса не пробила возврат вместо прихода.</li><li>Дождитесь передачи пакета оператором.</li><li>Попросите копию только при устойчивом расхождении.</li></ol>",
        tool_title="Мини-инструмент: три узла фискальной цепочки",
        tool_items=(
            "Касса и ФН: создают документ и фискальный признак.",
            "ОФД: принимает, хранит и передаёт пакет в реестр.",
            "ФНС: отвечает на запрос по шести реквизитам.",
        ),
        extra_title="Почему бизнесу важно видеть не только «найдено»",
        extra_paragraphs=(
            "Даже когда доставка прошла идеально, компании нужен дальнейший маршрут: привязать покупку к подотчётному лицу, уточнить категорию товара, сопоставить сумму с выпиской и подготовить файл для 1С. Проверка чека офд сама по себе не даёт ни авансового отчёта, ни напоминания о сроке сдачи.",
            "В программе статус становится частью карточки: сотрудник видит только свои чеки, бухгалтер — реестр компании, руководитель — сводку просроченного. Повторно отсканированный экземпляр отсекается по связке фискальных полей, даже если его прислал другой человек.",
            "Такой подход особенно заметен на объектах и в командировках: прораб фотографирует документ сразу, а спорная запись не теряется между личными сообщениями и почтой. Официальная цепочка остаётся неизменной, меняется только аккуратность внутреннего учёта.",
        ),
        faq=(
            ("ОФД проверяет подлинность чека?", "ОФД передаёт и хранит данные; факт регистрации подтверждает реестр ФНС по реквизитам."),
            ("Сколько ждать появления записи?", "Обычно данные приходят быстро, но при перебоях связи возможна задержка до суток."),
            ("Можно ли проверить чек офд без интернета?", "Нет, запрос к реестру требует сети; офлайн доступна только сохранённая карточка."),
        ),
        cover=ArticleImage(
            src="/img/blog/06-ofd-flow.jpg",
            alt="Онлайн-касса с чипом ФН, облачный узел ОФД с защищённым пакетом и сервер реестра с галочкой",
            title="Путь кассового документа от фискального накопителя через ОФД в базу налоговой",
            caption="Три звена передачи: кассовый аппарат и чип накопителя подписывают чек, оператор доставляет зашифрованный пакет, сервер подтверждает запись.",
        ),
        secondary=ArticleImage(
            src="/img/blog/05-fns-verify.jpg",
            alt="Щит с зелёной галочкой между фискальным документом и карточкой сверки в базе",
            title="Итоговое подтверждение доставленного пакета по шести реквизитам",
            caption="Как только оператор передал смену, сверка подтверждает подпись накопителя и открывает товарные позиции для учёта.",
        ),
        up=("/kak-proverit-chek", "проверка чека офд"),
        side=("/blog/proverka-kassovyh-chekov-dlya-biznesa", "подробнее о контроле расходов"),
        down=("/cheki-sotrudnikov-v-1c", "проверить чеки сотрудников"),
        cta="Для бизнеса — демо",
    ),
    Article(
        path="/blog/chek-ne-prohodit-proverku-v-fns",
        title="Проверка чеков ФНС России: 7 причин отказа — Ямастер Чек",
        description="Проверка чеков ФНС России не находит документ? Семь причин: опечатка в ФП, задержка ОФД, возврат, неверная дата, копия, сбой кассы и спорный продавец.",
        h1="Проверка чеков ФНС России: 7 причин отказа",
        keywords=("проверка чеков фнс россии",),
        category_slug="fns",
        category="ФНС и ОФД",
        read_min=4,
        teaser="Статус «Не найден» — не повод для паники. Пройдитесь по рабочему списку из семи причин за одну минуту: от перепутанных ФД и ФП до чека возврата и нефискальной квитанции.",
        benefit="Чек-лист: 7 причин отказа",
        lead="Статус «Не найден» не всегда означает подделку. Проверка чеков фнс россии зависит от точности шести реквизитов, доставки данных и типа расчёта. Ниже — семь живых причин и порядок, который сэкономит время бухгалтеру и не создаст лишние копии в реестре.",
        how_title="Как проверить чек, который не находится",
        steps=(
            "Откройте карточку документа и сравните ФН, ФД, ФП, дату и сумму с печатной строкой; не редактируйте запись «на глаз».",
            "Определите тип расчёта: возврат прихода или расход ищется по своим реквизитам и не заменяет исходный приход.",
            "Повторите запрос после обновления данных, затем зафиксируйте результат: подтверждён, ожидает доставки или требует замены у продавца.",
        ),
        how_extra="<p>Список причин ниже составлен для рабочего стола, а не для паники. Каждый пункт проверяется за минуту и не требует обращения в поддержку. Такой порядок особенно помогает, когда в конце месяца собраны десятки авансовых документов от разных сотрудников.</p>",
        not_found="<p>Если ни один пункт не объяснил расхождение, не удаляйте карточку: она хранит фото, автора и историю попыток. Попросите у продавца повторный экземпляр и приложите переписку. Программа покажет задачу в разделе «Что нужно от клиента», а руководитель увидит просрочку без ручного обзвона.</p><ol><li>Отметьте причину в комментарии.</li><li>Назначьте ответственного сотрудника.</li><li>Не утверждайте сумму до подтверждения.</li></ol>",
        tool_title="Мини-инструмент: 7 причин быстрым списком",
        tool_items=(
            "Опечатка в длинном фискальном признаке ФП.",
            "Перепутаны ФД и ФП при ручном вводе.",
            "Данные ещё не доставлены оператором фискальных данных.",
            "В отчёт попал возврат вместо исходной покупки.",
            "Дата списана с товарного чека, а не из строки t.",
            "Копия слишком тёмная, и QR прочитан частично.",
            "Продавец выдал документ без фискальных реквизитов.",
        ),
        extra_title="Как превратить спорный чек в понятную задачу",
        extra_paragraphs=(
            "Главная ценность статуса — не в красной подсветке, а в следующем действии. Для одного человека это «введите ФП заново», для бухгалтера — «запросите копию», для руководителя — «срок сдачи уже прошёл». Поэтому в карточке рядом со статусом живут автор, компания, фото и комментарий.",
            "Проверка чеков фнс россии повторяется автоматически по расписанию, но решение о замене документа остаётся за сотрудником. Мы не блокируем весь авансовый отчёт из-за одной спорной строки и не выдаём автоматическое обвинение продавцу.",
            "Когда чек наконец подтверждается, он проходит через тот же конвейер, что и остальные: отсечение дублей, категории позиций, связь с выпиской и выгрузка. Разница лишь в том, что история попыток остаётся видимой — это удобно для аудита и спокойной переписки с клиентом.",
        ),
        faq=(
            ("Сколько раз повторять проверку?", "Достаточно сверить реквизиты и повторить запрос после предполагаемой задержки доставки, не создавая десятки копий."),
            ("Можно ли сразу удалить спорный чек?", "Нет, лучше сохранить фото и комментарий: карточка объяснит, почему сумма не попала в отчёт."),
            ("Кто видит проблему?", "Сотрудник — свои чеки, бухгалтер — реестр компании, руководитель — просроченные задачи."),
        ),
        cover=ArticleImage(
            src="/img/blog/07-not-found.jpg",
            alt="Лупа со знаком вопроса над кассовой лентой, планшет с отмеченными пунктами и часы повтора",
            title="Семь пунктов диагностики статуса «Не найден» для бухгалтера и сотрудника",
            caption="Спокойная диагностика спорного экземпляра: сверка реквизитов по планшету, контроль типа операции и отложенный повтор.",
        ),
        secondary=ArticleImage(
            src="/img/blog/03-manual-code.jpg",
            alt="Повторная внимательная вычитка печатных реквизитов чека с лупой за ноутбуком",
            title="Ручная перепроверка длинных чисел ФН, ФД и ФП в карточке документа",
            caption="Большинство ложных отказов снимается спокойной вычиткой фискального признака и копеек по оригиналу ленты.",
        ),
        up=("/kak-proverit-chek", "проверка чеков фнс россии"),
        side=("/blog/proverka-cheka-fns-rossii", "подробнее о реестре"),
        down=("/", "проверить чек повторно"),
        cta="Проверьте чек на chek.ymaster.ru",
    ),
    Article(
        path="/blog/proverka-kassovyh-chekov-dlya-biznesa",
        title="Проверка кассовых чеков для бизнеса: контроль — Ямастер Чек",
        description="Проверка кассовых чеков для компании: сотрудники сканируют авансовые и командировочные документы, бухгалтер видит дубли, лимиты и выгружает принятые чеки в 1С.",
        h1="Проверка кассовых чеков для бизнеса: контроль сотрудников",
        keywords=("проверка кассовых чеков", "проверка кассового чека"),
        category_slug="b2b",
        category="Бизнес и 1С",
        read_min=4,
        teaser="Как навести порядок в подотчёте и командировках: сотрудники снимают чеки сразу на объекте, система отсекает дубли по ФН + ФД + ФП, подсвечивает лимиты и готовит обмен с 1С.",
        benefit="Контроль подотчёта и 1С",
        lead="Один чек легко проверить вручную. Сложность начинается, когда документы приносят десятки сотрудников, часть покупок оплачена корпоративной картой, а отчёт нужно закрыть к пятому числу. Проверка кассовых чеков превращается в процесс со сроками, статусами и понятной ответственностью.",
        how_title="Как проверить кассовый чек в компании",
        steps=(
            "Сотрудник фотографирует QR сразу после покупки по ссылке-приглашению; регистрация для компании проходит только по приглашению.",
            "Сервис считывает реквизиты, подтягивает позиции и отсекает повтор по ФН + ФД + ФП. Проверка кассового чека не создаёт две покупки из одного фото.",
            "Бухгалтер назначает подотчётное лицо, смотрит пометки политики расходов и выгружает принятые строки в 1С файлом EnterpriseData или по расписанию.",
        ),
        how_extra="<p>Политика расходов работает мягко: лимит на чек и месяц, запрещённые категории и товары подсвечиваются, но документ не блокируется автоматически. Окончательное решение остаётся за бухгалтером — так проще разобрать командировочную покупку, у которой есть объяснение.</p>",
        not_found="<p>Спорный документ не должен останавливать весь отчёт. Программа собирает неподтверждённые чеки в отдельный список с именем сотрудника: понятно, у кого просить замену и кому отправить напоминание. После повторного подтверждения карточка возвращается в общий поток без ручного пересоздания.</p><ol><li>Проверьте реквизиты и тип расчёта.</li><li>Назначьте ответственного за замену.</li><li>Не включайте сумму в выгрузку до решения.</li></ol>",
        tool_title="Мини-инструмент: что увидит бухгалтер до выгрузки",
        tool_items=(
            "Статус каждого чека: действителен, не найден или недействителен.",
            "Позиции с категориями вместо кассовой строки «[М] …».",
            "Лимиты и запрещённые статьи как мягкие пометки.",
            "Покупки по карте и чеки к возмещению рядом с выпиской.",
            "CSV-сводка и файл для авансового отчёта.",
        ),
        extra_title="Почему контроль важнее разовой проверки",
        extra_paragraphs=(
            "Компания теряет время не на сканировании, а на ожидании: бумажка лежит в кармане, напоминание отправлено слишком поздно, копия запрошена в последний день. Поэтому в программе есть вечернее напоминание тем, у кого за день нет ни одного чека, и утренняя сводка бухгалтеру.",
            "Срок сдачи задаётся отдельно для каждой организации. Просроченные задачи попадают к руководителю, а раздел «Что нужно от клиента» показывает не только документы, но и контекст: кто сдавал, когда и с каким статусом. Это заменяет длинную переписку в мессенджере.",
            "Проверка кассовых чеков не подменяет договорённости внутри компании и не обещает идеальной дисциплины. Она даёт прозрачный реестр, из которого можно собрать авансовый отчёт, не перебивая цифры с выцветшей термобумаги.",
        ),
        faq=(
            ("Видит ли сотрудник чужие чеки?", "Нет, сотрудник видит только свои документы, бухгалтер — чеки своей компании."),
            ("Что происходит с дублем?", "Повтор отсекается по ФН, ФД и ФП ещё до попадания в отчёт."),
            ("Можно ли работать без файла выгрузки?", "Да, 1С может забирать проверенные чеки по расписанию через обмен."),
        ),
        cover=ArticleImage(
            src="/img/blog/08-business-control.jpg",
            alt="Чеки со смартфонов сотрудников стекаются на экран руководителя с фильтрацией дублей и выгрузкой",
            title="Корпоративный контроль подотчётных расходов, корпоративных карт и обмена с 1С",
            caption="Снимки с телефонов сотрудников собираются в единый реестр компании: видны категории трат, отсечённые повторы и готовая папка выгрузки.",
        ),
        secondary=ArticleImage(
            src="/img/blog/09-accountant-mass.jpg",
            alt="Бухгалтер сверяет пачку авансовых чеков с банковскими операциями и формирует закрывающий архив",
            title="Сопоставление подотчётных покупок с выпиской банка перед закрытием периода",
            caption="Принятые строки связываются с операциями по корпоративным картам и уходят в авансовый отчёт без ручной перепечатки.",
        ),
        up=("/kak-proverit-chek", "проверка кассовых чеков"),
        side=("/blog/proverka-chekov-dlya-buhgaltera", "подробнее о массовой сверке"),
        down=("/cheki-sotrudnikov-v-1c", "проверить авансовые чеки"),
        cta="Для бизнеса — демо",
    ),
    Article(
        path="/blog/proverka-chekov-dlya-buhgaltera",
        title="Проверка чеков для бухгалтера: массовая сверка — Ямастер Чек",
        description="Проверка чеков для бухгалтера и аутсорсинга: несколько компаний, сайт проверки чеков, пакетная сверка с налоговой, выписки банков по API, УПД из ЭДО и выгрузка в 1С.",
        h1="Проверка чеков для бухгалтера: массовая сверка с ФНС",
        keywords=("проверка чеков", "сайт проверки чеков", "проверка чеков на сайте налоговой"),
        category_slug="b2b",
        category="Бизнес и 1С",
        read_min=4,
        teaser="Одно окно для бухгалтера и аутсорсинга на десятки юрлиц: изоляция клиентов, выписки пяти банков по API, УПД 5.01–5.03 из ЭДО, список недостающих бумаг и «Пакет за месяц».",
        benefit="Карта закрытия месяца",
        lead="Аутсорсинг ведёт не одну организацию, а десятки: у каждой свои сотрудники, банки, сроки и первичка. Проверка чеков здесь перестаёт быть кнопкой и становится пакетной работой. Ниже — как связать реестр ФНС, выписки, УПД и учётную систему без ручных таблиц.",
        how_title="Как проверить чеки пакетом и не потерять компанию",
        steps=(
            "Соберите документы в один реестр: сканы сотрудников, фото из Telegram или MAX, письма поставщиков и электронные УПД.",
            "Сопоставьте запросы к налоговой с банковскими операциями; проверка чеков на сайте налоговой даёт факт регистрации, а выписка — контекст платежа.",
            "Разложите результат по организациям и выгрузите принятые строки в 1С или «Пакет за месяц» для другой учётной системы.",
        ),
        how_extra="<p>Сайт проверки чеков удобен для одного документа, но бухгалтеру важна изоляция данных: карточки клиента А не должны оказаться в отчёте клиента Б. В программе компании разделены на уровне доступа, а не только папками на диске.</p>",
        not_found="<p>Массовая сверка не делает спорные документы невидимыми. Неподтверждённые записи собираются в «Что нужно от клиента» с указанием организации и ответственного. Напоминания уходят по настроенному каналу, а просрочка поднимается руководителю — не нужно вручную обходить весь список каждый вечер.</p><ol><li>Отметьте статус и компанию.</li><li>Запросите замену у конкретного клиента.</li><li>Не переносите спорную сумму в отчёт.</li></ol>",
        tool_title="Мини-инструмент: карта закрытия месяца",
        tool_items=(
            "Реестр чеков и позиции по каждой компании.",
            "Выписки Точки, Т-Банка, Модульбанка, Сбербанка и Альфа-Банка.",
            "УПД 5.01–5.03 из ЭДО, почты и распознанных сканов.",
            "Список чеков к возмещению и платежей без документов.",
            "Архив JSON, CSV и файлов УПД 5.03 с описанием полей.",
        ),
        extra_title="Что даёт «Пакет за месяц»",
        extra_paragraphs=(
            "Не каждая компания работает именно в 1С. Для других систем программа собирает один архив: чеки, позиции, первичку, банковские операции, возмещения и описание всех полей. Это честный экспорт данных, а не попытка заставить бухгалтера сменить учётный контур.",
            "Электронные УПД проходят проверку по схеме ФНС, ИНН и КПП, арифметике и ставке НДС. PDF, скан или фото сначала становятся черновиком рядом с файлом; в реестр документ попадает только после подтверждения человеком. Так распознавание не подменяет ответственность.",
            "Проверка чеков в таком контуре — часть большого цикла: от запроса документов до напоминания и отчёта руководителю. Мы не публикуем цены и тарифы, не обещаем уникальных позиций в поиске и не выдаём себя за государственный ресурс. Задача статьи — объяснить маршрут, который программа действительно выполняет.",
        ),
        faq=(
            ("Сколько компаний можно вести?", "На одном сервере — любое число организаций; у каждой свои пользователи, чеки, банки и документы."),
            ("Какие банки поддерживаются?", "Выписки по OAuth, API и refresh_token, плюс универсальный файл 1CClientBankExchange из любого банка."),
            ("Можно ли обойтись без 1С?", "Да, «Пакет за месяц» отдаёт JSON, CSV и файлы УПД 5.03 с описанием полей."),
        ),
        cover=ArticleImage(
            src="/img/blog/09-accountant-mass.jpg",
            alt="Бухгалтер ведёт на одном экране несколько организаций, выписки банков, УПД и месячный архив",
            title="Пакетная работа бухгалтера и аутсорсинга с чеками, банками и УПД",
            caption="Мультикомпанейский контур: отдельные карточки клиентов, синхронизация банковских операций, первичка и готовый архив за месяц.",
        ),
        secondary=ArticleImage(
            src="/img/blog/04-tax-site.jpg",
            alt="Монитор со сверкой реквизитов документа и разложенные по папкам комплекты первички",
            title="Реестр налоговой как первый шаг пакетного закрытия периода",
            caption="Факт регистрации в реестре дополняется проверкой ИНН, КПП, НДС и привязкой к конкретному юрлицу.",
        ),
        up=("/kak-proverit-chek", "проверка чеков"),
        side=("/blog/proverka-kassovyh-chekov-dlya-biznesa", "подробнее о политике расходов"),
        down=("/dlya-buhgalterov", "проверить выписку и УПД"),
        cta="массовая проверка чеков и выгрузка в 1С",
    ),
    Article(
        path="/blog/qr-kod-ili-ruchnoj-kod",
        title="Проверка чека: QR-код или ручной код — Ямастер Чек",
        description="Проверка чека: QR-код или ручной ввод? Сравниваем скорость, ошибки, повреждённую термобумагу, проверку чека по QR коду и массовые сценарии для компании.",
        h1="Проверка чека: QR-код или ручной код",
        keywords=("проверка чека", "проверка чеков онлайн по qr", "проверка чека по qr коду", "проверка чеков по qr коду"),
        category_slug="qr",
        category="QR и реквизиты",
        read_min=3,
        teaser="Камера экономит секунды и убирает опечатки, а клавиатура спасает выцветший архив и размытые сканы. Таблица выбора за 10 секунд для разовой покупки и потока первички.",
        benefit="Выбор способа за 10 сек",
        lead="Оба способа отправляют в реестр одни и те же шесть реквизитов, но ошибки у них разные. Проверка чека по qr коду быстрее на потоке, а ручной ввод выручает на выцветшей бумаге. Ниже — честное сравнение, когда выбрать камеру, а когда открыть форму.",
        how_title="Как проверить чек двумя способами без потери точности",
        steps=(
            "Для свежего чека начните с камеры: проверка чеков онлайн по qr не требует переписывать длинные номера и занимает несколько секунд.",
            "Если код не читается, откройте ручную форму и перенесите t, s, fn, i, fp и n из печатной расшифровки.",
            "Сравните оба результата с бумагой. Проверка чеков по qr коду и ручной запрос должны дать одинаковый статус для одного документа.",
        ),
        how_extra="<p>Не существует «более официального» способа: реестр не знает, попали к нему цифры из камеры или с клавиатуры. Разница только в вероятности опечатки и в скорости. Поэтому обучение сотрудников начинают с QR, а форму держат как понятный запасной маршрут.</p>",
        not_found="<p>Если оба способа вернули «не найдено», сначала проверьте дату и ФП — это общая точка ошибок. Затем дайте время на доставку через ОФД. Не создавайте две карточки для одного чека: повторные попытки фиксируются в истории той же записи, иначе отчёт раздуется дублями.</p><ol><li>Сверьте расшифровку под QR.</li><li>Повторите запрос позже.</li><li>Сохраните фото и комментарий.</li></ol>",
        tool_title="Мини-инструмент: таблица выбора за 10 секунд",
        tool_items=(
            "Чек свежий, код контрастный — сканируйте камерой.",
            "Термобумага выцвела или закрыта штампом — вводите вручную.",
            "Пачка документов от сотрудников — QR плюс автопроверка дублей.",
            "Электронный скан низкого качества — ручная форма надёжнее.",
            "Расхождение после обоих способов — запросите копию у продавца.",
        ),
        extra_title="Почему для бизнеса важна не победа одного способа",
        extra_paragraphs=(
            "В реальном документообороте источники смешиваются: курьер присылает фото, прораб — снимок с объекта, поставщик — XML, бухгалтер — выписку. Программа принимает все варианты и приводит их к одной карточке с фискальными полями, автором и статусом.",
            "Проверка чека затем обрастает контекстом: категория позиции, лимит политики расходов, связь с корпоративной картой и срок сдачи. Именно это превращает разовый выбор «QR или клавиатура» в управляемый процесс, а не в спор об удобстве интерфейса.",
            "Мы не утверждаем, что один путь всегда лучше. Камера снижает количество опечаток, ручной ввод спасает архив, а связка ФН + ФД + ФП защищает от повторов независимо от того, как данные попали в систему.",
        ),
        faq=(
            ("Можно ли сначала отсканировать, а потом исправить одно поле?", "Да, распознанные значения доступны для проверки до сохранения карточки."),
            ("Дают ли разные способы разный результат?", "Нет, запрос к реестру использует те же шесть реквизитов."),
            ("Что выбрать для старого чека?", "Ручной ввод по печатной расшифровке, если QR уже не контрастный."),
        ),
        cover=ArticleImage(
            src="/img/blog/10-qr-vs-code.jpg",
            alt="Сравнение мгновенного сканирования QR-кода телефоном и ввода полей с мятого чека с общей галочкой",
            title="Камера против ручного ввода: оба маршрута ведут к единому результату сверки",
            caption="Свежую ленту быстрее снять камерой, потёртую — внести через форму: оба пути проверяют одни и те же шесть полей.",
        ),
        secondary=ArticleImage(
            src="/img/blog/02-qr-instruction.jpg",
            alt="Шесть карточек фискальных параметров, извлечённых из квадратного кода кассовой ленты",
            title="Одинаковый набор из шести полей при сканировании и при ручном вводе",
            caption="Даже после автосканирования каждое из шести полей остаётся открытым для быстрой правки до сохранения карточки.",
        ),
        up=("/kak-proverit-chek", "проверка чека"),
        side=("/blog/proverka-cheka-po-qr-kodu", "подробнее про QR"),
        down=("/", "проверить чек двумя способами"),
        cta="Проверьте чек на chek.ymaster.ru",
    ),
)

BY_PATH = {a.path: a for a in PAGES}

router = APIRouter(include_in_schema=False)


def scheme_of(request: Request) -> str:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()


def _view(article: Article | None):
    def view(request: Request) -> HTMLResponse:
        from ..database import SessionLocal
        cfg = seo.load_safely(SessionLocal)
        host = request.headers.get("host")
        enabled = seo.indexing_enabled(cfg, host)
        markup = render_index(cfg, host, scheme_of(request)) if article is None else render(
            article, cfg, host, scheme_of(request))
        return HTMLResponse(markup, headers={} if enabled else {"X-Robots-Tag": "noindex, nofollow"})
    view.__name__ = "blog_index" if article is None else "blog_" + article.path.rsplit("/", 1)[-1].replace("-", "_")
    return view


router.add_api_route(BLOG_INDEX, _view(None), methods=["GET", "HEAD"], include_in_schema=False)
for _article in PAGES:
    router.add_api_route(_article.path, _view(_article), methods=["GET", "HEAD"], include_in_schema=False)
