"""SEO стартовой страницы (v1.70.103, T-74, ADR-33): индексация, robots.txt, sitemap.xml, коды подтверждения.

Индексация решается по адресу сайта, а не по роботу. Режим «auto» (по умолчанию) разрешает поисковикам
только рабочий адрес docver.ru: копии программы у компаний, стенды и превью отвечают запретом
(robots.txt «Disallow: /», тег и заголовок noindex) — иначе они попадали бы в выдачу дублями главной.
Администратор может разрешить или запретить индексацию явно («Настройки» → «Поисковики»). Роботы и люди
получают одну и ту же страницу — это не клоакинг (правила Яндекса и Google, docs/kb/12_seo.md).
С 1.70.106 те же правила действуют и для трёх страниц под кластеры запросов (services/seo_pages.py, ADR-36), а с T-82 — для индекса блога и десяти статей (services/blog_pages.py, ADR-47).

Коды подтверждения прав (Яндекс Вебмастер, Google Search Console) — не секреты: их и так видно в коде
главной. Хранятся в настройках и подставляются на главную тегами meta. Администратор может вставить тег
<meta …> из кабинета поисковика целиком — значение content извлекается.
"""
from __future__ import annotations

import html
import re

from sqlalchemy.orm import Session

from . import appsettings

PRODUCTION_HOST = "docver.ru"
KEY_MODE = "seo_indexing"
KEY_YANDEX = "seo_yandex_verification"
KEY_GOOGLE = "seo_google_verification"
MODES = {
    "auto": "Авто — только docver.ru",
    "on": "Разрешить на этом адресе",
    "off": "Запретить",
}
DEFAULT_MODE = "auto"
DEFAULTS = {"mode": DEFAULT_MODE, "yandex": "", "google": ""}
# место в <head> index.html, куда подставляются canonical, robots и коды подтверждения
MARKER = "<!-- seo:head -->"
# закрыто от роботов: API, обмен с 1С, сокеты, кабинет компании, мини-приложение MAX
DISALLOW = ("/api/", "/onec/", "/ws/", "/b2b/", "/maxapp/")
# служебные параметры адреса — одноразовый билет входа и метки рекламы: Яндекс не считает их новыми страницами
CLEAN_PARAMS = ("maxlogin", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
# v1.70.106 (T-75, ADR-36): страницы под кластеры запросов — по одной на кластер, серверный HTML (services/seo_pages.py)
CONTENT_PAGES = ("/kak-proverit-chek", "/cheki-sotrudnikov-v-1c", "/dlya-buhgalterov")
# T-82 (issue #72, ADR-47): индекс блога и десять статей-спутников — тоже серверный HTML (services/blog_pages.py)
BLOG_INDEX = "/blog"
BLOG_PAGES = (
    "/blog/proverka-cheka-onlajn", "/blog/proverka-cheka-po-qr-kodu", "/blog/proverka-cheka-po-kodu",
    "/blog/proverka-cheka-na-sajte-nalogovoj", "/blog/proverka-cheka-fns-rossii", "/blog/ofd-i-proverka-chekov",
    "/blog/chek-ne-prohodit-proverku-v-fns", "/blog/proverka-kassovyh-chekov-dlya-biznesa",
    "/blog/proverka-chekov-dlya-buhgaltera", "/blog/qr-kod-ili-ruchnoj-kod",
)
# Иллюстрации блога для Image Sitemap 1.1 (Google Картинки, Google Discover и Яндекс Вебмастер)
BLOG_IMAGES: dict[str, tuple[str, str]] = {
    "/blog/proverka-cheka-onlajn": (
        "/img/blog/01-online-check.jpg",
        "Проверка кассового чека онлайн камерой смартфона по QR-коду",
    ),
    "/blog/proverka-cheka-po-qr-kodu": (
        "/img/blog/02-qr-instruction.jpg",
        "Шесть фискальных реквизитов в QR-коде кассового чека",
    ),
    "/blog/proverka-cheka-po-kodu": (
        "/img/blog/03-manual-code.jpg",
        "Ручной ввод реквизитов ФН, ФД и ФП с бумажного чека на ноутбуке",
    ),
    "/blog/proverka-cheka-na-sajte-nalogovoj": (
        "/img/blog/04-tax-site.jpg",
        "Сверка кассового чека с реестром налоговой и передача в учёт компании",
    ),
    "/blog/proverka-cheka-fns-rossii": (
        "/img/blog/05-fns-verify.jpg",
        "Сопоставление даты, суммы и фискального признака чека с реестром ФНС",
    ),
    "/blog/ofd-i-proverka-chekov": (
        "/img/blog/06-ofd-flow.jpg",
        "Маршрут фискальных данных от онлайн-кассы и ФН через ОФД в ФНС",
    ),
    "/blog/chek-ne-prohodit-proverku-v-fns": (
        "/img/blog/07-not-found.jpg",
        "Диагностика семи причин, почему кассовый чек не найден в реестре ФНС",
    ),
    "/blog/proverka-kassovyh-chekov-dlya-biznesa": (
        "/img/blog/08-business-control.jpg",
        "Контроль авансовых чеков сотрудников, отсечение дублей и выгрузка в 1С",
    ),
    "/blog/proverka-chekov-dlya-buhgaltera": (
        "/img/blog/09-accountant-mass.jpg",
        "Пакетная сверка чеков, банковских выписок и УПД для бухгалтера и аутсорсинга",
    ),
    "/blog/qr-kod-ili-ruchnoj-kod": (
        "/img/blog/10-qr-vs-code.jpg",
        "Сравнение сканирования QR-кода камерой и ручного ввода реквизитов чека",
    ),
}
# Иллюстрации главной и кластерных страниц для Image Sitemap 1.1 (по 1–2 на страницу)
CONTENT_IMAGES: dict[str, tuple[tuple[str, str], ...]] = {
    "/": (
        ("/img/manual/hero.jpg", "Проверка кассового чека онлайн по QR-коду, баллы и выгрузка в 1С — DocVer"),
    ),
    "/kak-proverit-chek": (
        ("/img/manual/scan.jpg", "Сканирование QR-кода кассового чека камерой смартфона"),
        ("/img/pages/01-qr-verify-steps.jpg", "Шесть реквизитов QR-кода и проверка признаков подлинного чека в ФНС"),
    ),
    "/cheki-sotrudnikov-v-1c": (
        ("/img/manual/ao.jpg", "Бухгалтер разбирает авансовые чеки сотрудников для выгрузки в 1С"),
        ("/img/pages/02-employees-to-1c.jpg", "Путь чека сотрудника через фильтр дублей и политики расходов в 1С"),
    ),
    "/dlya-buhgalterov": (
        ("/img/manual/clients.jpg", "Экран «Клиенты» для бухгалтера на аутсорсинге с карточками компаний"),
        ("/img/pages/03-accountants-api-upd.jpg", "Автозагрузка выписок банков по API, сверка УПД 5.03 и пакет за месяц"),
    ),
}
# страницы карты сайта
PAGES = ("/", *CONTENT_PAGES, BLOG_INDEX, *BLOG_PAGES)
_HOST = re.compile(r"^(?:[a-z0-9-]{1,63}\.)*[a-z0-9-]{1,63}(?::\d{1,5})?$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{6,100}$")
_CONTENT = re.compile(r"""content\s*=\s*["']([^"']*)["']""", re.I)


def clean_host(host_header: str | None) -> str:
    """Адрес из заголовка Host: только имя узла и порт, в нижнем регистре; иное (подделка, IPv6) — пусто."""
    host = (host_header or "").strip().lower().rstrip(".")
    return host if _HOST.match(host) else ""


def hostname(host_header: str | None) -> str:
    return clean_host(host_header).split(":")[0]


def mode(db: Session) -> str:
    raw = appsettings.get_setting(db, KEY_MODE, DEFAULT_MODE)
    return raw if raw in MODES else DEFAULT_MODE


def load(db: Session) -> dict[str, str]:
    """Настройки поисковиков: режим индексации и коды подтверждения."""
    return {"mode": mode(db), "yandex": appsettings.get_setting(db, KEY_YANDEX, ""),
            "google": appsettings.get_setting(db, KEY_GOOGLE, "")}


def load_safely(session_factory) -> dict[str, str]:
    """Настройки для главной и robots.txt: база недоступна (обновление, блокировка) — значения по умолчанию.

    Стартовая страница не должна падать из-за настроек поисковиков."""
    try:
        db = session_factory()
    except Exception:  # noqa: BLE001 — главная важнее настроек
        return dict(DEFAULTS)
    try:
        return load(db)
    except Exception:  # noqa: BLE001
        return dict(DEFAULTS)
    finally:
        db.close()


def normalize_code(raw: str | None) -> str:
    """Код подтверждения: сам код или тег <meta … content="код">; пусто — убрать код. Неверный — ValueError."""
    text = (raw or "").strip()
    if not text:
        return ""
    found = _CONTENT.search(text)
    if found:
        text = found.group(1).strip()
    if not _CODE.match(text):
        raise ValueError("Код подтверждения — латинские буквы, цифры, «-» и «_», от 6 до 100 символов: "
                         "вставьте значение content из тега meta или тег целиком")
    return text


def save(db: Session, *, mode_value: str | None = None, yandex: str | None = None,
         google: str | None = None) -> None:
    """Сохранить то, что передано (None — не менять). Ошибка в любом поле — ничего не сохраняется."""
    if mode_value is not None and mode_value not in MODES:
        raise ValueError("Режим индексации — auto, on или off")
    y = normalize_code(yandex) if yandex is not None else None
    g = normalize_code(google) if google is not None else None
    if mode_value is not None:
        appsettings.set_setting(db, KEY_MODE, mode_value)
    if y is not None:
        appsettings.set_setting(db, KEY_YANDEX, y)
    if g is not None:
        appsettings.set_setting(db, KEY_GOOGLE, g)


def indexing_enabled(cfg: dict, host_header: str | None) -> bool:
    current = cfg.get("mode", DEFAULT_MODE)
    if current == "on":
        return bool(clean_host(host_header))
    if current == "off":
        return False
    return hostname(host_header) == PRODUCTION_HOST


def origin(host_header: str | None, scheme: str | None = "https") -> str:
    """Адрес сайта для canonical и карты: рабочий — всегда https; неизвестный адрес — рабочий."""
    host = clean_host(host_header) or PRODUCTION_HOST
    if host.split(":")[0] == PRODUCTION_HOST:
        return f"https://{host}"
    return f"{scheme if scheme in ('http', 'https') else 'https'}://{host}"


def head_tags(cfg: dict, host_header: str | None, scheme: str | None) -> str:
    """Теги для <head> главной: canonical при разрешённой индексации, иначе noindex; коды подтверждения."""
    tags = []
    if indexing_enabled(cfg, host_header):
        tags.append(f'<link rel="canonical" href="{html.escape(origin(host_header, scheme))}/">')
    else:
        tags.append('<meta name="robots" content="noindex, nofollow">')
    for name, key in (("yandex-verification", "yandex"), ("google-site-verification", "google")):
        if cfg.get(key):
            tags.append(f'<meta name="{name}" content="{html.escape(cfg[key])}">')
    return "\n".join(tags)


def render_index(template: str, cfg: dict, host_header: str | None, scheme: str | None) -> str:
    return template.replace(MARKER, head_tags(cfg, host_header, scheme), 1)


def robots_txt(cfg: dict, host_header: str | None, scheme: str | None) -> str:
    if not indexing_enabled(cfg, host_header):
        return ("# DocVer: индексация выключена — это не рабочий адрес docver.ru\n"
                "# или так решил администратор («Настройки» → «Поисковики»)\n"
                "User-agent: *\nDisallow: /\n")
    lines = ["# DocVer — правила для поисковых роботов", "User-agent: *", "Allow: /"]
    lines += [f"Disallow: {path}" for path in DISALLOW]
    # Документ API должен быть доступен роботу, чтобы тот прочитал noindex.
    lines.append("Allow: /api/v1/legal/offerta")
    lines += ["", "# Яндекс: служебные параметры адреса не создают копий страниц (директива межсекционная)",
              f"Clean-param: {'&'.join(CLEAN_PARAMS)} /",
              "", f"Sitemap: {origin(host_header, scheme)}/sitemap.xml"]
    return "\n".join(lines) + "\n"


def _sitemap_image(base: str, path: str) -> str:
    items: list[tuple[str, str]] = list(CONTENT_IMAGES.get(path, ()))
    blog_meta = BLOG_IMAGES.get(path)
    if blog_meta:
        items.append(blog_meta)
    return "".join(
        f"<image:image><image:loc>{html.escape(base + src)}</image:loc>"
        f"<image:title>{html.escape(title)}</image:title></image:image>"
        for src, title in items
    )


def sitemap_xml(cfg: dict, host_header: str | None, scheme: str | None, lastmod: str) -> str | None:
    """Карта сайта с иллюстрациями статей (Image Sitemap 1.1); при выключенной индексации — None (ответ 404)."""
    if not indexing_enabled(cfg, host_header):
        return None
    base = origin(host_header, scheme)
    urls = "".join(
        f"  <url><loc>{html.escape(base + path)}</loc><lastmod>{lastmod}</lastmod>"
        f"{_sitemap_image(base, path)}</url>\n"
        for path in PAGES
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
            + urls + "</urlset>\n")


def status(cfg: dict, host_header: str | None) -> dict:
    """Состояние для карточки «Поисковики» в настройках."""
    return {
        "mode": cfg.get("mode", DEFAULT_MODE),
        "modes": [{"id": k, "label": v} for k, v in MODES.items()],
        "host": hostname(host_header),
        "production_host": PRODUCTION_HOST,
        "enabled": indexing_enabled(cfg, host_header),
        "yandex": cfg.get("yandex", ""),
        "google": cfg.get("google", ""),
    }
