"""SEO стартовой страницы (v1.70.103, T-74, ADR-33): индексация, robots.txt, sitemap.xml, коды подтверждения.

Индексация решается по адресу сайта, а не по роботу. Режим «auto» (по умолчанию) разрешает поисковикам
только рабочий адрес chek.ymaster.ru: копии программы у компаний, стенды и превью отвечают запретом
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

PRODUCTION_HOST = "chek.ymaster.ru"
KEY_MODE = "seo_indexing"
KEY_YANDEX = "seo_yandex_verification"
KEY_GOOGLE = "seo_google_verification"
MODES = {
    "auto": "Авто — только chek.ymaster.ru",
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
# страницы карты сайта
PAGES = ("/", "/oferta", *CONTENT_PAGES, BLOG_INDEX, *BLOG_PAGES)
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
        return ("# Ямастер Чек: индексация выключена — это не рабочий адрес chek.ymaster.ru\n"
                "# или так решил администратор («Настройки» → «Поисковики»)\n"
                "User-agent: *\nDisallow: /\n")
    lines = ["# Ямастер Чек — правила для поисковых роботов", "User-agent: *", "Allow: /"]
    lines += [f"Disallow: {path}" for path in DISALLOW]
    lines += ["", "# Яндекс: служебные параметры адреса не создают копий страниц (директива межсекционная)",
              f"Clean-param: {'&'.join(CLEAN_PARAMS)} /",
              "", f"Sitemap: {origin(host_header, scheme)}/sitemap.xml"]
    return "\n".join(lines) + "\n"


def sitemap_xml(cfg: dict, host_header: str | None, scheme: str | None, lastmod: str) -> str | None:
    """Карта сайта; при выключенной индексации — None (ответ 404)."""
    if not indexing_enabled(cfg, host_header):
        return None
    base = origin(host_header, scheme)
    urls = "".join(f"  <url><loc>{html.escape(base + path)}</loc><lastmod>{lastmod}</lastmod></url>\n"
                   for path in PAGES)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")


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
