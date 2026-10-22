# -*- coding: utf-8 -*-
# Ямастер Чек — тесты v1.70.44: документация платного API для внешних клиентов.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Требование выпуска: «Напиши API документацию для моего приложения. Для тех кто
# готов подключиться и пользоваться за деньги или подписку. С картинками в нашем
# стиле.»
#
# Что закрепляют эти тесты:
#  - документ описывает ТОЛЬКО то, что работает: состав методов читается из
#    app/pool/router_api.py, и каждый маршрут обязан быть описан (дрейф кода и
#    документа ломает сборку в обе стороны);
#  - ответы, которые видит клиент, сверяются с живым сервисом: состав полей
#    настоящего ответа обязан быть перечислен в документе;
#  - тексты отказов приводятся дословно — их можно показывать пользователю;
#  - тарифная сетка опубликована лимитами, а ЦЕНЫ не публикуются: внутренние
#    расчётные цифры из docs/plan.md не должны попадать в клиентский документ;
#  - три векторные схемы существуют, корректны как XML, выдержаны в стиле
#    диаграмм документации и подключены и к документу, и к лендингу;
#  - раздел лендинга «API для бизнеса» размечен и описан в стилях, без новых
#    цветов помимо фирменных токенов;
#  - ритуал выпуска: точки синхронизации версии, запись «Что нового» первая,
#    разделы CHANGELOG и журнала интерфейса.
import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

from app.pool import router_api
from tests.test_v1390 import _mkkey, _seed, _wipe

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "API.md"
README = ROOT / "README.md"
INDEX = ROOT / "app/static/index.html"
CSS = ROOT / "app/static/css/app.css"
APP_JS = ROOT / "app/static/js/app.js"
MAX_HTML = ROOT / "app/static/maxapp/index.html"
ROUTER = ROOT / "app/pool/router_api.py"
# v1.70.45: схемы вынесены из публичной статики за гейт — их отдаёт
# только закрытый маршрут кабинета подтверждённым компаниям.
IMG_DIR = ROOT / "app/b2b/assets"

SVG_FILES = ("api-access.svg", "api-request-path.svg", "api-data-boundary.svg")

# Признаки стиля диаграмм документации (все присутствуют в эталоне).
STYLE_NEEDLES = ("#0b1020", "#22d3ee", "ymaster.ru", "ООО «Ямастер»",
                 "chek@ymaster.ru")

# Тарифы, опубликованные в документе и на лендинге: это лимиты ключа, а не цены.
# «Старт» — значения по умолчанию в коде (DEFAULT_RATE / DEFAULT_QUOTA).
TARIFFS = (("Старт", 60, "5 000"), ("Бизнес", 300, "30 000"),
           ("Аналитика", 1200, "150 000"))

# Дословные тексты отказов из router_api.py — их видит клиент.
ERRORS = (
    "API для внешних клиентов выключен — обратитесь к администратору",
    "Укажите ключ в заголовке X-API-Key",
    "Ключ недействителен или отозван",
    "запросов/час",
    "Квота тарифа исчерпана",
    "Приём чеков выключен — агрегаты недоступны",
)

# Внутренние расчётные цифры коммерческого плана (docs/plan.md). В клиентском
# документе их быть не должно: API не продаёт чеки и не публикует прайс.
FORBIDDEN_PRICES = ("5 000 ₽", "50 ₽", "200 ₽", "₽/мес", "₽/чек", "₽ в месяц",
                    "9 900", "49 000")


def _read(p):
    return pathlib.Path(p).read_text(encoding="utf-8")


def doc():
    return _read(DOC)


def router_src():
    """Исходник маршрутизатора со склеенными неявными конкатенациями строк.

    Тексты отказов в коде разбиты на две строки («…выключен — » + «обратитесь
    к администратору»), а клиент видит их целиком, поэтому документ сверяется
    со склеенным текстом, а не с разметкой строк в исходнике.
    """
    return re.sub(r'"\s*\n\s*f?"', "", _read(ROUTER))


def routes_in_code():
    """Маршруты платного API straight из кода: [(метод, путь), …]."""
    src = _read(ROUTER)
    return [(m.upper(), path) for m, path
            in re.findall(r'@router\.(get|post)\("(/v1/[^"]+)"', src)]


def routes_in_doc():
    """Маршруты, описанные в документе: заголовки вида «### 6.1 `GET /v1/…`»."""
    return [(m.upper(), path) for m, path
            in re.findall(r"###\s+\d+\.\d+\s+`(GET|POST)\s+(/v1/[^`]+)`", doc())]


# ======================================================================
# 1. Ритуал выпуска
# ======================================================================
class TestRelease17044:
    VERSION = "1.70.44"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read("app/config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read("app/static/sw.js")
        assert f'\"version\": \"{self.VERSION}\"' in _read(
            "app/static/manifest.webmanifest")
        idx = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in idx
        assert f"app.js?v={self.VERSION}" in idx
        assert f"maxapp.js?v={self.VERSION}" in _read(MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        sw = _read("app/static/sw.js")
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?\'([\d.]+)\'", js)
        assert m and m.group(1) == self.VERSION, "запись выпуска не первая"

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.43': [", 1)[0]
        assert block.count("{ icon:") == 4
        for part in ("API", "тариф", "схем", "обезличен"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_whats_new_has_no_foreign_letters(self):
        """В прозе для клиента — русские буквы; латиница только в имени API."""
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.43': [", 1)[0]
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", block)
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), f"постороннее письмо {ch!r}"
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - {"API"}
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read("CHANGELOG.md")
        assert f"## [{self.VERSION}]" in text
        sec = text.split(f"## [{self.VERSION}]", 1)[1].split("## [1.70.43]", 1)[0]
        for needle in ("docs/API.md", "api-access.svg", "land-api", "X-API-Key",
                       "по запросу"):
            assert needle in sec, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        j = _read("docs/ui_journal.md")
        assert f"## v{self.VERSION} —" in j
        sec = j.split(f"## v{self.VERSION} —", 1)[1].split("## v1.70.43", 1)[0]
        for needle in ("docs/API.md", "тариф", "схем", "лендинг"):
            assert needle in sec, f"в журнале нет «{needle}»"


# ======================================================================
# 2. Документ описывает ровно то, что работает
# ======================================================================
class TestDocumentMatchesCode:
    def test_code_has_six_routes(self):
        """Опора на факт: платное API — шесть маршрутов, все под /v1."""
        assert len(routes_in_code()) == 6, routes_in_code()

    def test_every_route_is_documented(self):
        """Дрейф-замок: новый маршрут в коде обязан появиться в документе."""
        text = doc()
        for method, path in routes_in_code():
            assert f"{method} {path}" in text, \
                f"маршрут {method} {path} не описан в docs/API.md"

    def test_no_documented_route_is_missing_in_code(self):
        """Обратный дрейф: документ не обещает того, чего в коде нет."""
        code = set(routes_in_code())
        for method, path in routes_in_doc():
            assert (method, path) in code, \
                f"в документе описан несуществующий {method} {path}"
        assert len(routes_in_doc()) == len(code), "описаны не все маршруты"

    def test_defaults_match_tariff_start(self):
        """«Старт» — это значения по умолчанию в коде, а не выдуманная сетка."""
        name, rate, quota = TARIFFS[0]
        assert router_api.DEFAULT_RATE == rate
        assert f"{router_api.DEFAULT_QUOTA:,}".replace(",", " ") == quota
        assert name in doc()

    @pytest.mark.parametrize("text", ERRORS)
    def test_error_texts_are_verbatim(self, text):
        """Тексты отказов приведены дословно — их можно показывать клиенту."""
        assert text in doc(), f"в документе нет текста отказа «{text}»"
        assert text in router_src(), f"текст «{text}» не из router_api.py"

    def test_error_codes_table(self):
        for code in ("401", "403", "422", "429"):
            assert f"`{code}`" in doc(), f"в таблице ошибок нет кода {code}"

    def test_key_format_documented(self):
        assert router_api.KEY_PREFIX == "apk_"
        assert "apk_" in doc()
        assert "X-API-Key" in doc()
        # ключ показывается один раз, в базе только хэш — это обещание клиенту
        assert "sha256" in doc()

    def test_journal_retention_documented(self):
        assert router_api.CALLS_RETENTION_DAYS == 90
        assert "90 дней" in doc()

    def test_aggregate_flag_documented(self):
        assert '"aggregated": true' in doc()

    def test_admin_section_present(self):
        """Отдельный раздел для администратора сервиса — требование выпуска."""
        assert router_api.API_ENABLED_KEY == "pool_api_enabled"
        sec = doc().split("Раздел для администратора сервиса", 1)[1]
        for needle in ("pool_api_enabled", "/api-keys", "revoke",
                       "pool_api_key_created", "pool_api_key_revoked",
                       "/api/v1/pool-admin"):
            assert needle in sec, f"в разделе администратора нет «{needle}»"

    def test_roadmap_is_marked_as_plan(self):
        """Планы не должны читаться как обязательства."""
        assert "Что обсуждается дальше" in doc()
        sec = doc().split("Что обсуждается дальше", 1)[1].split("## 12", 1)[0]
        assert "не обязательства" in sec or "не реализовано" in sec.lower()

    def test_no_invented_prices(self):
        """Цены не публикуем: внутренние цифры плана не должны протечь."""
        text = doc()
        for bad in FORBIDDEN_PRICES:
            assert bad not in text, f"в клиентском документе цена «{bad}»"
        assert "по запросу" in text, "не сказано, что стоимость согласуется"

    def test_principle_stated(self):
        """Принцип продукта: чеки не продаются, продаются агрегаты."""
        assert "не прода" in doc()
        assert "152-ФЗ" in doc()

    def test_contacts_present(self):
        for needle in ("chek@ymaster.ru", "ymaster.ru", "ООО «Ямастер»"):
            assert needle in doc(), f"в документе нет «{needle}»"

    def test_no_foreign_scripts(self):
        """Документ для клиента — только кириллица и служебная латиница."""
        bad = re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                         r"\uac00-\ud7af\u0600-\u06ff\u0590-\u05ff]", doc())
        assert not bad, f"посторонние письмена: {sorted(set(bad))}"


# ======================================================================
# 3. Живой сервис отвечает тем, что описано
# ======================================================================
@pytest.fixture
def api_key(client):
    """Ключ партнёра и включённый доступ.

    _wipe() из tests/test_v1390 очищает таблицы пула и сам выставляет
    pool_enabled и pool_api_enabled — повторно включать настройку не нужно.
    """
    from app.pool import router_auth
    router_auth._HITS.clear()
    # зависимость от client поднимает приложение; заодно убеждаемся, что
    # сервис отвечает, прежде чем сверять состав его ответов с документом
    assert client.get("/health").status_code == 200
    _wipe()
    _seed(6)
    _kid, raw, prefix = _mkkey(name="ООО Документация", rate=60, quota=5000)
    yield raw, prefix
    router_auth._HITS.clear()
    _wipe()


class TestDocumentedResponsesAreReal:
    """Состав полей настоящего ответа обязан быть перечислен в документе.

    Это самая сильная проверка документации: если разработчик добавит поле в
    ответ и не опишет его, тест укажет на конкретное поле.
    """

    def _check(self, body, path):
        text = doc()
        assert body.get("aggregated") is True, f"{path}: нет признака агрегата"
        for key in body:
            assert key in text, f"{path}: поле «{key}» не описано в документе"

    def test_overview(self, client, api_key):
        raw, _ = api_key
        r = client.get("/api/v1/pool-api/v1/stats/overview",
                       headers={"X-API-Key": raw}, params={"days": 30})
        assert r.status_code == 200, r.text
        body = r.json()
        self._check(body, "overview")
        assert body["receipts_verified"] == 6
        assert body["period_days"] == 30

    def test_regions_industries_merchants(self, client, api_key):
        raw, _ = api_key
        H = {"X-API-Key": raw}
        for path, item_keys in (
            ("/v1/stats/regions", ("region_code", "region", "receipts")),
            ("/v1/stats/industries", ("industry", "name", "receipts", "avg_sum")),
            ("/v1/stats/merchants", ("merchant", "inn", "receipts", "avg_sum")),
        ):
            r = client.get(f"/api/v1/pool-api{path}", headers=H,
                           params={"days": 30})
            assert r.status_code == 200, r.text
            body = r.json()
            self._check(body, path)
            assert body["items"], f"{path}: пустой список на засеянных данных"
            for k in item_keys:
                assert k in body["items"][0], f"{path}: нет поля «{k}»"
                assert k in doc(), f"{path}: поле «{k}» не описано"

    def test_filters_count_and_aggregate(self, client, api_key):
        raw, _ = api_key
        H = {"X-API-Key": raw}
        body_in = {"date_from": "2000-01-01", "region": "78",
                   "industry": "food", "sum_min": 100, "sum_max": 1000}
        r = client.post("/api/v1/pool-api/v1/filters/count", headers=H,
                        json=body_in)
        assert r.status_code == 200, r.text
        self._check(r.json(), "filters/count")
        assert r.json()["count"] == 6

        r = client.post("/api/v1/pool-api/v1/filters/aggregate", headers=H,
                        json=body_in)
        assert r.status_code == 200, r.text
        body = r.json()
        self._check(body, "filters/aggregate")
        for k in ("sum", "avg", "min", "max", "by_industry"):
            assert k in body, f"aggregate: нет «{k}»"
        for k in ("industry", "name", "receipts"):
            assert k in body["by_industry"][0], f"by_industry: нет «{k}»"

    def test_filter_fields_are_documented(self):
        """Поля фильтра описаны по схеме запроса, а не на глаз."""
        text = doc()
        for name, field in router_api._F.model_fields.items():
            assert f"`{name}`" in text, f"поле фильтра «{name}» не описано"
            assert field.default in ("", None), \
                f"поле фильтра «{name}» должно быть необязательным"

    def test_empty_selection_is_not_an_error(self, client, api_key):
        """Пустая выборка — 200 с нулями; это обещано в документе."""
        raw, _ = api_key
        r = client.post("/api/v1/pool-api/v1/filters/aggregate",
                        headers={"X-API-Key": raw},
                        json={"region": "99", "inn": "0000000000"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 0 and body["sum"] == 0.0
        assert body["by_industry"] == []
        assert "не ошибка" in doc()

    def test_documented_refusals_are_real(self, client, api_key):
        """Тексты отказов из документа — настоящие ответы сервиса."""
        raw, _ = api_key
        r = client.get("/api/v1/pool-api/v1/stats/overview")
        assert r.status_code == 401
        assert r.json()["detail"] == "Укажите ключ в заголовке X-API-Key"
        # значение заголовка HTTP обязано быть ASCII: несуществующий ключ
        # берём латиницей, как это и сделает настоящий клиент
        r = client.get("/api/v1/pool-api/v1/stats/overview",
                       headers={"X-API-Key": "apk_not-issued-000000"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Ключ недействителен или отозван"
        # часовой лимит: ключ на два запроса
        _kid, small, _pref = _mkkey(name="Лимит", rate=2, quota=100)
        H = {"X-API-Key": small}
        codes = [client.get("/api/v1/pool-api/v1/stats/overview",
                            headers=H).status_code for _ in range(3)]
        assert codes[:2] == [200, 200] and codes[2] == 429
        r = client.get("/api/v1/pool-api/v1/stats/overview", headers=H)
        assert r.status_code == 429
        assert r.json()["detail"] == "Лимит тарифа: 2 запросов/час"
        assert "Лимит тарифа" in doc()

    def test_days_window_is_clamped_but_echo_is_raw(self, client, api_key):
        """Окно выборки зажимается в 1…365, а period_days возвращает запрошенное.

        Живой прогон: days=9999 → 200 и "period_days": 9999, хотя в расчёт идут
        чеки за 365 дней. Черновик документа обещал «приводится к границе» —
        неверно для поля ответа. Документ исправлен и предупреждает клиента не
        сверять окно по period_days.
        """
        raw, _ = api_key
        H = {"X-API-Key": raw}
        r = client.get("/api/v1/pool-api/v1/stats/overview", headers=H,
                       params={"days": 9999})
        assert r.status_code == 200, r.text
        assert r.json()["period_days"] == 9999
        assert client.get("/api/v1/pool-api/v1/stats/overview", headers=H,
                          params={"days": 0}).status_code == 200
        text = doc()
        assert "окно выборки" in text and "1…365" in text
        assert "возвращает **запрошенное** значение" in text
        assert "period_days` не стройте" in text

    def test_numeric_filters_are_strict(self, client, api_key):
        """Числа и строки проверяются схемой: 422 до обращения к данным.

        Живой прогон: {"sum_min": "не число"} → 422 с detail-строкой вида
        «поле: сообщение»; {"region": 77} → 422. Внутри _stats_filter для
        sum_min/sum_max есть try/except, но до него дело не доходит — значение
        отбраковывает схема запроса.
        """
        raw, _ = api_key
        H = {"X-API-Key": raw}
        r = client.post("/api/v1/pool-api/v1/filters/count", headers=H,
                        json={"sum_min": "не число"})
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert isinstance(detail, str) and detail.startswith("sum_min: "), detail
        r = client.post("/api/v1/pool-api/v1/filters/count", headers=H,
                        json={"region": 77})
        assert r.status_code == 422, r.text
        assert r.json()["detail"].startswith("region: ")
        text = doc()
        assert "<поле>: <сообщение проверки>" in text
        assert "Числа и строки проверяются строго" in text

    def test_dates_are_soft(self, client, api_key):
        """Даты проверяются мягко: ошибка формата — 200 и фильтр не применён."""
        raw, _ = api_key
        H = {"X-API-Key": raw}
        for bad in ("не дата", "2026-13-45", "01.09.2026"):
            r = client.post("/api/v1/pool-api/v1/filters/count", headers=H,
                            json={"date_from": bad})
            assert r.status_code == 200, f"{bad}: {r.status_code} {r.text}"
            assert r.json()["count"] == 6, f"{bad}: фильтр применился"
        assert "Даты проверяются мягко" in doc()

    def test_contradictory_sum_range_is_empty_not_error(self, client, api_key):
        """sum_min больше sum_max — 200 и пустая выборка, а не ошибка."""
        raw, _ = api_key
        r = client.post("/api/v1/pool-api/v1/filters/count",
                        headers={"X-API-Key": raw},
                        json={"sum_min": 10, "sum_max": 5})
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 0
        assert "Противоречивая вилка суммы" in doc()

    def test_limit_is_clamped_per_method(self, client, api_key):
        """Зажим limit свой у каждого метода: 95 регионы, 50 отрасли и продавцы."""
        raw, _ = api_key
        H = {"X-API-Key": raw}
        for path, ceiling in (("/v1/stats/regions", 95),
                              ("/v1/stats/industries", 50),
                              ("/v1/stats/merchants", 50)):
            r = client.get(f"/api/v1/pool-api{path}", headers=H,
                           params={"days": 30, "limit": 9999})
            assert r.status_code == 200, f"{path}: {r.text}"
            assert len(r.json()["items"]) <= ceiling, path
        assert f"max(1, min(limit, 95))" in _read(ROUTER)
        assert "до 95 для регионов" in doc()

    def test_openapi_points_are_documented(self, client):
        """Интерактивная документация существует — и упомянута в документе."""
        assert client.get("/api/openapi.json").status_code == 200
        for needle in ("/api/docs", "/api/redoc", "/api/openapi.json"):
            assert needle in doc(), f"в документе нет «{needle}»"


# ======================================================================
# 4. Схемы в нашем стиле
# ======================================================================
class TestDiagrams:
    def test_files_exist_and_are_valid_xml(self):
        for name in SVG_FILES:
            p = IMG_DIR / name
            assert p.exists(), f"нет схемы {name}"
            ET.parse(p)                     # некорректный XML — ошибка здесь
            assert p.stat().st_size > 3000, f"{name}: схема слишком мала"

    def test_house_style_matches_docs_diagrams(self):
        """Стиль — как у docs/img/architecture.svg: тот же размер холста, тёмный
        фон, акцент диаграмм и подпись разработчика. Фирменный оранжевый —
        дополнение к стилю документации: в эталоне его нет, а в продукте это
        цвет бренда, поэтому он есть во всех трёх схемах."""
        ref = _read("docs/img/architecture.svg")
        for needle in STYLE_NEEDLES:
            assert needle in ref, f"эталон не содержит «{needle}»"
        for name in SVG_FILES:
            svg = _read(IMG_DIR / name)
            for needle in STYLE_NEEDLES:
                assert needle in svg, f"{name}: нет «{needle}» из стиля эталона"
            assert "ff7a00" in svg, f"{name}: нет фирменного оранжевого"
            assert 'viewBox="0 0 1180 620"' in svg, f"{name}: другой размер"

    def test_diagrams_are_referenced_from_document(self):
        text = doc()
        for name in SVG_FILES:
            assert f"../app/b2b/assets/{name}" in text, \
                f"схема {name} не подключена к docs/API.md"

    def test_diagrams_are_behind_the_gate(self):
        """Схемы вынесены из публичной статики: отдаются только через закрытый
        маршрут кабинета компаниям с подтверждённым статусом."""
        for name in SVG_FILES:
            assert (ROOT / "app/b2b/assets" / name).exists(), name
            assert not (ROOT / "app/static/img/api" / name).exists(), \
                f"{name} осталась в публичной статике"

    def test_request_path_shows_check_order(self):
        """Вторая схема — порядок проверок и коды, как в _authenticate."""
        svg = _read(IMG_DIR / "api-request-path.svg")
        for code in ("403", "401", "429", "200"):
            assert code in svg, f"в схеме пути запроса нет кода {code}"
        for needle in ("X-API-Key", "pool_api_enabled"):
            assert needle in svg, f"в схеме нет «{needle}»"

    def test_boundary_diagram_lists_both_sides(self):
        svg = _read(IMG_DIR / "api-data-boundary.svg")
        assert "не отдаём" in svg.lower()
        for needle in ("ФН", "ФД", "ФП"):
            assert needle in svg, f"в границе данных нет «{needle}»"


# ======================================================================
# 5. Раздел закрыт от посторонних (v1.70.45)
# ======================================================================
# Директива владельца: «Пока убрать скрыть от посторонних глаз API для
# бизнеса — агрегаты Чек-Пула». Раздел переехал в кабинет компании /b2b/
# и открывается только после подтверждения статуса через корпоративную
# почту, поэтому замки ниже проверяют ОТСУТСТВИЕ раздела на публичной
# странице и перенос схем за гейт. Замки v1.70.44 на присутствие раздела
# удалены как утратившие силу — их заменили эти.
LAND_CLASSES = ("land-api", "land-api-title", "land-api-lead", "land-api-grid",
                "land-api-card", "land-api-tariffs", "land-api-note",
                "land-api-fig", "land-api-cta")


class TestLandingIsClosed:
    def html(self):
        return _read(INDEX)

    def css(self):
        return _read(CSS)

    def test_section_is_gone_from_public_landing(self):
        """Ни секции, ни её идентификатора, ни ссылок на неё."""
        h = self.html()
        assert "land-api" not in h, "класс раздела остался на публичной странице"
        assert 'id="land-api"' not in h
        assert 'href="#land-api"' not in h

    def test_faq_no_longer_advertises_the_api(self):
        """Пункт «Нужны данные по чекам для аналитики?» вёл посторонних к
        разделу — убран вместе со ссылкой."""
        h = self.html()
        assert "Нужны данные по чекам для аналитики" not in h
        assert "Есть платное API" not in h

    def test_diagrams_are_not_linked_from_landing(self):
        h = self.html()
        for name in SVG_FILES:
            assert name not in h, f"схема {name} показана посторонним"
        assert "/img/api/" not in h

    def test_section_styles_are_removed(self):
        """Мёртвых правил не остаётся: 21 правило раздела удалено из app.css."""
        css = self.css()
        for cls in LAND_CLASSES:
            assert f".{cls}" not in css, f"в app.css остался мёртвый .{cls}"

    def test_tariff_numbers_still_match_document(self):
        """Цифры лимитов в документе не изменились после переноса раздела."""
        text = doc()
        for name, rate, quota in TARIFFS:
            assert name in text, f"в документе нет тарифа «{name}»"
            assert f"{rate:,}".replace(",", " ") in text or str(rate) in text
            assert quota in text, f"в документе нет квоты {quota} тарифа «{name}»"


# ======================================================================
# 6. README ведёт к документу
# ======================================================================
class TestReadmeLink:
    def test_readme_links_the_document(self):
        r = _read(README)
        assert "[docs/API.md](docs/API.md)" in r
        assert "Платное API для партнёров" in r

    def test_readme_route_table_matches_code(self):
        """Таблица маршрутов в README — из кода, без выдуманных путей."""
        r = _read(README)
        sec = r.split("Платное API для партнёров", 1)[1]
        for method, path in routes_in_code():
            assert f"/api/v1/pool-api{path}" in sec, \
                f"в README нет {method} {path}"
        assert "X-API-Key" in sec
        assert "pool_api_enabled" in sec

    def test_readme_shows_diagram(self):
        assert "app/b2b/assets/api-access.svg" in _read(README)
