# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.47: публичная оферта и её страница.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерий приёмки (указание владельца): «Ты как юрист с многолетним опытом
# крупной компании ООО Ямастер напиши мне оферту для моей компании по нашей
# программе, до написания возьми уже готовые варианты крупных компаний таких
# как Яндекс 1С и других и на их базе напиши для нашей компании оферту
# учитывая наши интересы и защищая наши права, на собственную программу и
# интеллектуальную собственность, продумай оферту и размести отдельную
# страницу для нее и создай ссылки на на неё там где требуется.»
#
# Решения владельца, закреплённые этими тестами (менять только по новому
# указанию владельца):
#   * реквизиты Правообладателя берутся ИЗ НАСТРОЕК — тем же механизмом, что
#     и согласие 152-ФЗ; выдуманные юридические факты запрещены;
#   * оферта ОДНА, универсальная: и для доноров чеков, и для компаний с ИП;
#   * ответственность ограничена совокупно — 10 000 рублей для тех, кто
#     пользуется сервисом безвозмездно, и 10 % стоимости услуг за расчётный
#     период для Клиентов; упущенная выгода и косвенные убытки исключены;
#     ответственность только при наличии вины; плюс оговорка о пределах,
#     которые законом ограничить нельзя;
#   * споры: обязательная претензия и 30 календарных дней на ответ, затем
#     арбитражный суд для компаний и ИП и суд по месту нахождения
#     Правообладателя для физических лиц.
#
# Что закреплено, чтобы найденное не вернулось:
#  1. документ существует, полон и не содержит выдуманных реквизитов;
#  2. исключительное право на программу защищено: запреты, лицензия,
#     ответственность за нарушение;
#  3. редакция одна — журнал согласий, страница и галочка кабинета не могут
#     разойтись (до v1.70.47 редакция задавалась в ingest.py литералом, а
#     самого документа не существовало);
#  4. страница отдаётся без авторизации и ДО SPA-перехвата;
#  5. ссылки стоят на всех поверхностях, где условия принимаются;
#  6. стили страницы — фирменная палитра, читаемость и печать;
#  7. в пакете не осталось мёртвого кода;
#  8. ритуал выпуска версии.
# ======================================================================
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGAL = ROOT / "app" / "legal"
OFFERTA_PY = LEGAL / "offerta.py"
PAGE_PY = LEGAL / "page.py"
INIT_PY = LEGAL / "__init__.py"
OFFERTA_CSS = ROOT / "app" / "static" / "legal" / "oferta.css"
INDEX = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
SW = ROOT / "app" / "static" / "sw.js"
MANIFEST = ROOT / "app" / "static" / "manifest.webmanifest"
MAX_HTML = ROOT / "app" / "static" / "maxapp" / "index.html"
CABINET = ROOT / "app" / "static" / "b2b" / "index.html"
CABINET_CSS = ROOT / "app" / "static" / "b2b" / "b2b.css"
MAIN = ROOT / "app" / "main.py"
INGEST = ROOT / "app" / "pool" / "ingest.py"
BOT = ROOT / "app" / "services" / "telegram_bot.py"
MANUAL = ROOT / "app" / "services" / "manual_content.py"
ACCOUNTS = ROOT / "app" / "pool" / "accounts.py"
README = ROOT / "README.md"
API_DOC = ROOT / "docs" / "API.md"
B2B_DOC = ROOT / "docs" / "b2b_access.md"
GUIDE = ROOT / "docs" / "USER_GUIDE.md"
SPEC = ROOT / "docs" / "SPECIFICATION.md"
CHANGELOG = ROOT / "CHANGELOG.md"
JOURNAL = ROOT / "docs" / "ui_journal.md"

#: Латиница, законная в русском юридическом тексте: имя интерфейса, название
#: мессенджера и составные части адреса страницы и почты.
ALLOWED_LATIN = {"API", "MAX", "chek", "https", "oferta", "ru", "ymaster"}


def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _lum(color: str) -> float:
    """Относительная яркость по WCAG."""
    h = color.lstrip("#")
    vals = []
    for i in (0, 2, 4):
        v = int(h[i:i + 2], 16) / 255
        vals.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _ratio(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


def _no_comments(css: str) -> str:
    """CSS без комментариев: сканер не должен читать прозу как объявления."""
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _tokens(css: str) -> dict:
    root = _no_comments(css).split(":root {", 1)[1].split("\n}", 1)[0]
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root)}


def _corpus() -> str:
    """Всё, откуда может вызываться код пакета: app, tests, qa, точки входа."""
    parts = []
    for pattern in ("app/**/*.py", "tests/**/*.py", "qa/**/*.py"):
        for p in sorted(ROOT.glob(pattern)):
            if "__pycache__" in p.parts or "vendor" in p.parts:
                continue
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
    for name in ("run.py", "deploy.sh"):
        p = ROOT / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


# ----------------------------------------------------------------------
#  1. Документ существует и полон
# ----------------------------------------------------------------------
class TestDocumentExists:
    """Оферта — документ, а не упоминание: до v1.70.47 программа ссылалась
    на редакцию, которой негде было прочитать."""

    def test_edition_and_publication_date(self):
        from app.legal import offerta
        assert offerta.EDITION == "2026-10"
        assert re.match(r"^\d{2}\.\d{2}\.\d{4}$", offerta.EDITION_DATE)

    def test_document_has_terms_sections_and_requisites(self):
        from app.legal import offerta
        data = offerta.payload(None)
        assert len(data["definitions"]) == 13, "терминов должно быть 13"
        assert len(data["sections"]) == 17, "разделы 2—18"
        assert data["requisites"]["n"] == "19"
        clauses = sum(len(s["clauses"]) for s in data["sections"])
        assert clauses >= 85, f"пунктов стало меньше: {clauses}"

    def test_section_numbering_is_continuous(self):
        from app.legal import offerta
        numbers = [s["n"] for s in offerta.sections(None)]
        assert numbers == [str(n) for n in range(2, 19)], numbers

    def test_every_clause_carries_its_own_number(self):
        """Нумерация в тексте пункта: страница подавляет маркеры списка,
        поэтому номер обязан быть в самом тексте — иначе пункты потеряют
        нумерацию и ссылки на них («пункт 7.3») станут непроверяемыми."""
        from app.legal import offerta
        for s in offerta.sections(None):
            for c in s["clauses"]:
                assert c.startswith(s["n"] + "."), c[:40]

    def test_flat_text_repeats_the_structure(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert text.startswith(offerta.TITLE)
        assert f"Редакция {offerta.EDITION}" in text
        for s in offerta.sections(None):
            assert f"{s['n']}. {s['title']}" in text
            for c in s["clauses"]:
                assert c in text
        assert len(text.encode()) > 40_000, "документ не может быть короче"

    def test_no_unfilled_placeholders(self):
        from app.legal import offerta
        data = offerta.payload(None)
        chunks = list(data["preamble"]) + [d["text"] for d in data["definitions"]]
        chunks += [c for s in data["sections"] for c in s["clauses"]]
        chunks += data["requisites"]["lines"] + [data["requisites"]["note"]]
        for ch in chunks:
            assert "{" not in ch and "}" not in ch, ch[:80]

    def test_document_is_russian_and_cites_the_civil_code(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        for cite in ("437", "438", "1235", "1261", "1286", "1237", "1280",
                     "1260", "1334", "1252", "1301", "450.1", "395"):
            assert cite in text, f"нет ссылки на статью {cite}"
        assert "152-ФЗ" in text

    def test_no_foreign_words_in_the_document(self):
        """Класс дефекта, который уже ловили: в прозу попадали чужие слова.
        Здесь в юридическом тексте найдено и исправлено «udzielена»."""
        from app.legal import offerta
        text = offerta.full_text(None)
        words = set(re.findall(r"[A-Za-z]+", text))
        assert words <= ALLOWED_LATIN, f"чужие слова: {sorted(words - ALLOWED_LATIN)}"
        exotic = re.findall(r"[\u3000-\u9fff\uac00-\ud7af\u0600-\u06ff]", text)
        assert not exotic, f"посторонние письма: {exotic[:5]}"

    def test_no_invented_requisites_in_the_text(self):
        """Выдуманный ИНН или ОГРН в документе — юридический факт, которого
        не существует. Подряд 10 и более цифр в тексте быть не должно."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert not re.findall(r"\d{10,}", text), "в документе появились длинные числа"


# ----------------------------------------------------------------------
#  2. Решения владельца
# ----------------------------------------------------------------------
class TestOwnerDecisions:
    """Четыре ответа владельца на вопросы о документе — обязательны к исполнению."""

    def test_requisites_come_from_settings_not_from_code(self):
        """Решение «из настроек»: реквизиты берутся тем же механизмом, что и
        согласие 152-ФЗ, а не задаются в документе второй раз."""
        src = _read(OFFERTA_PY)
        assert "from ..services import consent" in src
        assert "consent.requisites" in src and "consent.operator_line" in src
        assert "consent.requisites_complete" in src
        assert "consent.CONSENT_CONTACT_DEFAULT" in src, \
            "почта задана второй копией константы"

    def test_incomplete_requisites_are_disclosed_not_faked(self):
        from app.legal import offerta
        block = offerta.requisites_block(None)
        assert block["complete"] is False
        assert block["lines"] == [], "при пустых настройках реквизитов быть не должно"
        assert "ЕГРЮЛ" in block["note"]
        assert "не освобождает Стороны от исполнения Договора" in block["note"]

    def test_one_universal_document_for_both_audiences(self):
        """Решение «одна универсальная оферта»: и доноры чеков, и компании."""
        from app.legal import offerta
        data = offerta.payload(None)
        terms = {d["term"] for d in data["definitions"]}
        assert {"Участник", "Клиент"} <= terms
        text = data["text"]
        assert "Использование Сервиса Участником безвозмездно" in text
        assert "Доступ Клиента к Агрегатам через API является возмездным" in text
        # страница одна и редакция одна
        assert data["page_path"] == "/oferta"
        assert _read(PAGE_PY).count("def offerta_page") == 1

    def test_liability_is_capped_in_aggregate(self):
        """Решение «как у Яндекса»: совокупный предел и состав ограничений."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "10 000 (десять тысяч) рублей" in text
        assert "десятью процентами стоимости услуг за расчётный период" in text
        assert "косвенные убытки и упущенную выгоду" in text
        assert "только при наличии его вины" in text

    def test_savings_clause_keeps_what_law_forbids_to_limit(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "не применяются к ответственности за умысел" in text
        assert "ограничение или исключение ответственности не допускается законом" in text
        assert "прав потребителя" in text

    def test_disputes_follow_the_owners_choice(self):
        """Решение «суд Правообладателя»: претензия 30 дней, арбитраж для
        компаний и ИП, суд по месту Правообладателя для физических лиц."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "претензионный порядок" in text
        assert "30 календарных дней" in text
        assert "арбитражным судом по месту нахождения Правообладателя" in text
        assert "судом по месту нахождения Правообладателя" in text
        # оговорка для потребителя: решение работает и без адреса в настройках
        assert "подсудность по выбору потребителя" in text

    def test_tariffs_are_not_published_in_the_document(self):
        """Цены в документе не публикуются: коммерческие цифры живут в плане
        владельца, а тариф зависит от лимитов и срока (замок FORBIDDEN_PRICES)."""
        from app.legal import offerta
        text = offerta.full_text(None)
        for price in ("5 000 ₽", "50 ₽", "200 ₽", "₽/мес", "₽/чек", "9 900",
                      "49 000", "₽ в месяц"):
            assert price not in text, f"в оферту утекла цена {price}"
        assert "В тексте Оферты тарифы не публикуются" in text


# ----------------------------------------------------------------------
#  3. Защита прав на программу
# ----------------------------------------------------------------------
class TestIpProtection:
    """Требование владельца: защитить наши права на собственную программу и
    интеллектуальную собственность."""

    def test_exclusive_right_stays_with_the_owner(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "Исключительное право" in text
        assert "сохраняется за ним в полном объёме" in text

    def test_nothing_is_construed_as_transfer_of_the_right(self):
        """Оговорка из практики РЕД СОФТ: толкование против отчуждения."""
        from app.legal import offerta
        assert "не может толковаться как отчуждение исключительного права" \
            in offerta.full_text(None)

    def test_license_is_simple_and_non_exclusive(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "простой (неисключительной) лицензии" in text
        assert "статьи 1235" in text and "1286" in text

    def test_decompilation_is_banned_with_the_statutory_caveat(self):
        """Приём «1С»: запрет с оговоркой о случаях, разрешённых законом, —
        иначе пункт был бы ничтожен и тянул за собой вопрос о добросовестности."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "декомпилировать" in text and "дизассемблировать" in text
        assert "статьёй 1280" in text

    def test_sublicence_and_transfer_need_written_consent(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "сублицензирование" in text
        assert "предварительного письменного согласия Правообладателя" in text
        assert "1237" in text

    def test_prohibitions_cover_the_real_attack_surface(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        for needle in ("обходить", "технические средства защиты", "лимиты Ключа",
                       "автоматизированным способом", "излишнюю нагрузку на серверы",
                       "мультиплексирование", "уведомления о правах",
                       "обучения моделей"):
            assert needle in text, f"нет запрета: {needle}"

    def test_key_and_account_actions_are_attributed_to_the_user(self):
        """Приём оферты Яндекса на API: сохранность ключа — за лицензиатом,
        все действия через кабинет считаются его действиями."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "считаются совершёнными самим Пользователем" in text
        assert "показывается Клиенту один раз" in text
        assert "восстановить показанный Ключ невозможно" in text

    def test_receipt_donor_grants_the_needed_license(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "безвозмездную простую (неисключительную) лицензию" in text
        for way in ("включение в состав Чек-Пула", "агрегирование",
                    "обезличивание", "доведение до всеобщего сведения"):
            assert way in text, f"нет способа использования: {way}"

    def test_license_survives_consent_revocation(self):
        """Обезличенный агрегат по множеству чеков технически неизвлекаем —
        это должно быть написано, иначе отзыв согласия выглядел бы нарушением."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "не может быть отозвана" in text
        assert "Отзыв согласия на обработку персональных данных Участника не " \
               "прекращает действие лицензии" in text

    def test_points_are_not_money(self):
        """Баллы не должны превратиться в денежное обязательство."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "не являются денежными средствами" in text
        assert "не подлежат продаже" in text

    def test_breach_entails_compensation_articles(self):
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "статьями 1252 и 1301" in text
        assert "возместить Правообладателю убытки" in text

    def test_service_is_not_an_official_source(self):
        """Сервис не ОФД и не налоговый орган: без этого пункта Агрегаты
        выглядели бы официальными данными."""
        from app.legal import offerta
        text = offerta.full_text(None)
        assert "не является оператором фискальных данных" in text
        assert "по принципу «как есть»" in text


# ----------------------------------------------------------------------
#  4. Редакция одна
# ----------------------------------------------------------------------
class TestSingleEdition:
    """До v1.70.47 редакция задавалась литералом в ingest.py, а документа не
    было: журнал согласий фиксировал согласие с текстом, который негде читать."""

    def test_ingest_takes_the_edition_from_the_document(self):
        src = _read(INGEST)
        assert re.search(r"OFFERTA_VERSION\s*=\s*\"", src) is None, \
            "редакция снова задана литералом в ingest.py"
        assert "_offerta.EDITION" in src
        # имя сохранено: его держит замок tests/test_v1310.py
        assert "OFFERTA_VERSION" in src

    def test_edition_is_the_same_everywhere(self):
        from app.legal import offerta
        from app.pool import ingest
        assert ingest.OFFERTA_VERSION == offerta.EDITION
        assert f"редакция {offerta.EDITION}" in _read(CABINET)

    def test_journal_writes_the_published_edition(self, client):
        """Сквозная проверка: редакция в журнале согласий равна редакции
        опубликованного документа."""
        from app.database import SessionLocal
        from app.legal import offerta
        from app.pool.models import PoolConsent
        db = SessionLocal()
        try:
            rows = db.query(PoolConsent).order_by(PoolConsent.id.desc()).limit(5).all()
            for r in rows:
                assert r.offerta_version == offerta.EDITION
        finally:
            db.close()

    def test_short_terms_are_part_of_the_document(self):
        from app.legal import offerta
        from app.pool import ingest
        assert ingest.OFFERTA_SHORT == offerta.short_terms()
        assert ingest.OFFERTA_SHORT.endswith(offerta.PAGE_PATH + ".")

    def test_short_terms_are_not_duplicated_in_ingest(self):
        """Вторая копия текста разъехалась бы с первой при первой же правке."""
        src = _read(INGEST)
        assert "Отправляя чек, вы подтверждаете" not in src
        assert "_offerta.short_terms()" in src


# ----------------------------------------------------------------------
#  5. Страница отдаётся
# ----------------------------------------------------------------------
class TestOfferPage:
    """Отдельная страница — требование владельца."""

    def test_page_is_public(self, client):
        r = client.get("/oferta")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Публичная оферта" in r.text

    def test_page_is_standalone(self, client):
        """Страница документа не тянет ресурсы веб-клиента: условия должны
        читаться, даже если клиентский скрипт не загрузился."""
        html = client.get("/oferta").text
        assert "/js/app.js" not in html
        assert "/css/app.css" not in html
        assert 'id="app"' not in html, "отдан SPA-индекс вместо документа"
        assert '<html lang="ru">' in html
        assert 'class="skip"' in html

    def test_page_is_registered_before_the_spa_fallback(self):
        src = _read(MAIN)
        assert "include_router(legal_page.router)" in src
        assert src.index("include_router(legal_page.router)") < src.index("async def spa(")

    def test_toc_matches_the_sections(self, client):
        html = client.get("/oferta").text
        links = re.findall(r'<li><a href="#(s[\d-]+)"', html)
        anchors = re.findall(r'<section id="(s\d+)"', html)
        assert len(links) == 19 and links == sorted(set(links), key=anchors.index)
        for a in links:
            assert f'id="{a}"' in html

    def test_plain_text_version_is_served(self, client):
        from app.legal import offerta
        r = client.get("/oferta.txt")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert offerta.EDITION in r.text
        assert r.text == offerta.full_text(_session())

    def test_json_version_is_served(self, client):
        from app.legal import offerta
        r = client.get("/api/v1/legal/offerta")
        assert r.status_code == 200
        data = r.json()
        assert data["edition"] == offerta.EDITION
        assert len(data["sections"]) == len(offerta.sections(None))
        for key in ("title", "subtitle", "preamble", "definitions", "sections",
                    "requisites", "text", "requisites_complete", "contact"):
            assert key in data

    def test_stylesheet_is_served(self, client):
        assert client.get("/legal/oferta.css").status_code == 200

    def test_unknown_legal_api_path_is_not_swallowed_by_the_page(self, client):
        assert client.get("/api/v1/legal/nope").status_code == 404

    def test_page_escapes_document_text(self, client):
        """Текст документа вставляется экранированным: пункт с угловой скобкой
        не должен превратиться в разметку."""
        from app.legal import page
        assert page._e("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
        assert "<b>x</b>" not in client.get("/oferta").text


def _session():
    from app.database import SessionLocal
    return SessionLocal()


# ----------------------------------------------------------------------
#  6. Ссылки там, где условия принимаются
# ----------------------------------------------------------------------
class TestLinksWhereRequired:
    """Требование владельца: «создай ссылки на неё там где требуется».
    Перечень — все поверхности, где пользователь принимает условия."""

    def test_public_receipt_form_keeps_the_toggle_and_gains_the_document(self):
        js = _read(APP_JS)
        # прежнее поведение сохранено: «кратко» раскрывает текст на месте,
        # без перехода — иначе отсканированный чек потерялся бы
        assert 'id="pub-offerta-link">кратко</a>' in js
        assert "e.preventDefault();" in js
        assert '<a href="/oferta" target="_blank" rel="noopener">полный текст</a>' in js

    def test_company_cabinet_checkbox_links_the_document(self):
        html = _read(CABINET)
        consent = html.split('class="b2b-field b2b-consent"', 1)[1][:600]
        assert '<a href="/oferta" target="_blank" rel="noopener">' in consent

    def test_company_cabinet_footer_links_the_document(self):
        foot = _read(CABINET).split('class="b2b-foot"', 1)[1][:600]
        assert '<a href="/oferta" target="_blank" rel="noopener">Публичная оферта</a>' in foot

    def test_cabinet_stylesheet_styles_the_new_link(self):
        css = _no_comments(_read(CABINET_CSS))
        assert ".b2b-consent a" in css
        rule = css.split(".b2b-consent a", 1)[1].split("}", 1)[0]
        assert "var(--accent-text)" in rule and "underline" in rule

    def test_max_mini_app_consent_screen_links_the_document(self):
        html = _read(MAX_HTML)
        note = html.split('id="consent-note"', 1)[1][:900]
        assert '<a href="/oferta" target="_blank" rel="noopener">публичной оферте</a>' in note
        # замок v1.70.37: пояснение о том, что хранится без согласия
        assert "идентификатор MAX" in note and "не сохраняются" in note

    def test_max_mini_app_stays_self_contained(self):
        """Ссылка на документ не должна тянуть в мини-приложение ресурсы
        веб-клиента (замок v1.70.46)."""
        html = _read(MAX_HTML)
        assert "/js/vendor/" not in html and "/js/app.js" not in html
        assert "/css/app.css" not in html

    def test_landing_footer_links_the_document(self):
        foot = _read(INDEX).split('class="app-footer"', 1)[1][:600]
        assert '<a href="/oferta" target="_blank" rel="noopener">Публичная оферта</a>' in foot

    def test_user_manual_links_the_document(self):
        assert 'href="/oferta"' in _read(MANUAL)

    def test_telegram_bot_gives_an_absolute_address(self):
        """В мессенджере относительный адрес не откроется, поэтому бот
        подставляет полный — из той же настройки, что и ссылки в письмах."""
        from app.database import SessionLocal
        from app.legal import offerta
        from app.pool import mailer
        from app.services.telegram_bot import ingest_offerta
        db = SessionLocal()
        try:
            text = ingest_offerta(db)
            url = text.rsplit("на странице ", 1)[1].rstrip(".")
            assert url == mailer.base_url(db) + offerta.PAGE_PATH
            assert url.startswith("http") and url.endswith("/oferta")
            # тавтологии из черновика не осталось
            assert text.count("Полный текст оферты") == 1
        finally:
            db.close()

    def test_bot_source_has_no_second_copy_of_the_text(self):
        src = _read(BOT)
        assert "offerta.short_terms_absolute(db)" in src
        assert "OFFERTA_SHORT +" not in src, "бот снова склеивает текст вручную"

    def test_account_removal_docstring_points_to_the_clauses(self):
        src = _read(ACCOUNTS)
        assert "пункты 7.3, 7.4 и 14.5" in src and "/oferta" in src

    def test_documentation_links_the_document(self):
        assert "`/oferta`" in _read(README)
        api = _read(API_DOC)
        assert "/oferta" in api and "/oferta.txt" in api and "/api/v1/legal/offerta" in api
        assert "/oferta" in _read(B2B_DOC)
        assert "/oferta" in _read(GUIDE)
        spec = _read(SPEC)
        assert "app/legal/" in spec and "/oferta.txt" in spec


# ----------------------------------------------------------------------
#  7. Реквизиты из настроек — сквозная проверка
# ----------------------------------------------------------------------
class TestRequisitesFromSettings:
    """Решение владельца «из настроек»: один источник реквизитов на два
    документа. Проверка идёт на отдельной базе, чтобы не спорить с тестом
    реквизитов согласия за общую настройку (работчики xdist делят базу)."""

    INN = "7707083893"
    OGRN = "1027700132195"
    ADDR = "119021, г. Москва, ул. Тимура Фрунзе, д. 11"

    def _db(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.models import AppSetting
        engine = create_engine(f"sqlite:///{tmp_path}/legal.db")
        AppSetting.__table__.create(engine)
        return sessionmaker(bind=engine)()

    def test_empty_settings_produce_the_disclosure(self, tmp_path):
        from app.legal import offerta
        db = self._db(tmp_path)
        try:
            block = offerta.requisites_block(db)
            assert block["complete"] is False and block["lines"] == []
            assert "ЕГРЮЛ" in block["note"]
        finally:
            db.close()

    def test_filled_settings_appear_in_the_document(self, tmp_path):
        from app.legal import offerta
        from app.services import appsettings
        db = self._db(tmp_path)
        try:
            appsettings.set_setting(db, "consent_operator_inn", self.INN)
            appsettings.set_setting(db, "consent_operator_ogrn", self.OGRN)
            appsettings.set_setting(db, "consent_operator_address", self.ADDR)
            block = offerta.requisites_block(db)
            assert block["complete"] is True and block["note"] == ""
            joined = "\n".join(block["lines"])
            assert self.INN in joined and self.OGRN in joined
            assert self.ADDR in joined
            data = offerta.payload(db)
            assert data["requisites_complete"] is True
            assert self.INN in data["operator"] and self.ADDR in data["operator"]
            assert self.INN in data["text"]
        finally:
            db.close()

    def test_page_shows_a_banner_until_requisites_are_filled(self, client):
        from app.services import consent
        html = client.get("/oferta").text
        # полноту проверяет тот же механизм, что и согласие 152-ФЗ
        state = consent.requisites_complete(_session())
        if state:
            assert 'class="banner"' not in html
        else:
            assert 'class="banner"' in html
            assert "ЕГРЮЛ" in html

    def test_consent_and_offer_share_one_requisites_source(self):
        """Два документа — одно юридическое лицо: ключи настроек общие."""
        offer_src = _read(OFFERTA_PY)
        from app.services import consent
        for key in (consent.KEY_INN, consent.KEY_OGRN, consent.KEY_ADDRESS):
            assert key not in offer_src, \
                "оферта завела собственные ключи настроек вместо общих"
        assert "consent.requisites(db)" in offer_src


# ----------------------------------------------------------------------
#  8. Стили страницы
# ----------------------------------------------------------------------
class TestOfferStyles:
    """Страница документа — в фирменной палитре и читается."""

    def test_no_color_literals_outside_root(self):
        css = _no_comments(_read(OFFERTA_CSS))
        body = css.split(":root {", 1)[1].split("\n}", 1)[1]
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", body), \
            "цвет литералом вне :root"

    def test_tokens_match_the_application_palette(self):
        tokens = _tokens(_read(OFFERTA_CSS))
        app_tokens = _tokens(_read(ROOT / "app" / "static" / "css" / "app.css"))
        for name in ("--bg", "--panel", "--text", "--accent", "--accent-soft",
                     "--accent-text"):
            assert tokens[name] == app_tokens[name], f"{name} разошёлся с приложением"

    def test_single_responsive_breakpoint(self):
        assert _read(OFFERTA_CSS).count("@media (max-width: 720px) {") == 1

    def test_print_version_exists(self):
        css = _no_comments(_read(OFFERTA_CSS))
        assert "@media print {" in css
        assert "break-inside: avoid-page" in css

    def test_clause_markers_are_suppressed(self):
        """Нумерация пунктов живёт в тексте документа, поэтому маркеры списка
        подавлены: иначе нумерация была бы двойной и неверной."""
        css = _no_comments(_read(OFFERTA_CSS))
        rule = css.split(".clauses {", 1)[1].split("}", 1)[0]
        assert "list-style: none" in rule
        item = css.split(".clauses li {", 1)[1].split("}", 1)[0]
        assert "text-indent" in item, "нет висячего отступа для переносов"

    def test_link_color_is_readable(self):
        tokens = _tokens(_read(OFFERTA_CSS))
        accent_text = tokens["--accent-text"]
        assert _ratio(accent_text, tokens["--panel"]) >= 4.5
        assert _ratio(accent_text, tokens["--accent-soft"]) >= 4.5
        assert _ratio(tokens["--text"], tokens["--panel"]) >= 4.5
        assert _ratio(tokens["--text-dim"], tokens["--panel"]) >= 4.5

    def test_accent_is_decorative_only(self):
        """Оранжевый даёт 2.61:1 на белом: ниже порога и для текста, и для
        значимых нетекстовых элементов, поэтому он — только линейка и край
        предупреждения, а смысл продублирован словами."""
        css = _no_comments(_read(OFFERTA_CSS))
        tokens = _tokens(_read(OFFERTA_CSS))
        assert _ratio(tokens["--accent"], tokens["--panel"]) < 3.0, \
            "порог изменился: оранжевый стал читаемым, правило можно снять"
        assert re.search(r"(?<![\w-])color:\s*var\(--accent\)", css) is None, \
            "--accent применён как цвет текста"
        assert len(re.findall(r"var\(--accent\)", css)) >= 2, \
            "токен --accent не применён"

    def test_font_sizes_are_readable(self):
        css = _no_comments(_read(OFFERTA_CSS))
        sizes = [float(v) for v in re.findall(r"font-size:\s*([\d.]+)px", css)]
        assert sizes and min(sizes) >= 13, f"кегль ниже нормы: {min(sizes)}"
        body = css.split("body {", 1)[1].split("}", 1)[0]
        assert "font-size: 16px" in body
        assert "line-height: 1.65" in body

    def test_document_column_is_limited(self):
        """Строка длиннее ~75 знаков читается с трудом: колонка уже экрана."""
        tokens = _tokens(_read(OFFERTA_CSS))
        assert tokens["--measure"] == "48rem"


# ----------------------------------------------------------------------
#  9. В пакете нет мёртвого кода
# ----------------------------------------------------------------------
class TestNoDeadCode:
    """Урок v1.70.46: новый код не должен приносить новых необращаемых
    объектов, а черновики — оставаться в файле."""

    def test_every_function_is_referenced(self):
        corpus = _corpus()
        for path in (OFFERTA_PY, PAGE_PY):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                if node.decorator_list:
                    continue    # маршруты вызывает FastAPI
                assert corpus.count(node.name) >= 2, \
                    f"{path.name}:{node.name} не вызывается нигде"

    def test_every_module_constant_is_referenced(self):
        corpus = _corpus()
        tree = ast.parse(OFFERTA_PY.read_text(encoding="utf-8"))
        names = [t.id for n in tree.body if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)
                 and t.id.isupper()]
        assert names, "в модуле не осталось констант?"
        for name in names:
            assert corpus.count(name) >= 2, f"константа {name} не используется"

    def test_draft_helper_is_gone(self):
        """Черновой `short_summary` заменён на `short_terms`: первая версия
        осталась бы необращаемой."""
        from app.legal import offerta
        assert not hasattr(offerta, "short_summary")
        assert "short_summary" not in _read(OFFERTA_PY)


# ----------------------------------------------------------------------
#  10. Ритуал выпуска
# ----------------------------------------------------------------------
class TestRelease17047:
    """v1.70.48: выпуск 1.70.47 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию — их ведёт test_v17048, а
    здесь остаётся запись выпуска в истории (как сделано для 1.70.38…1.70.46).
    Константа PREVIOUS нужна своим же проверкам записи «Что нового», раздела
    CHANGELOG и журнала: они вырезают текст между своей версией и предыдущей.

    Проверка «страница берёт версию из настроек» оставлена здесь: она не
    зависит от номера версии и держит свойство, а не число.
    """

    VERSION = "1.70.47"
    PREVIOUS = "1.70.46"

    def test_version_is_release(self):
        ver = re.search(r'APP_VERSION: str = "([^"]+)"',
                        _read(ROOT / "app/config.py")).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 47)

    def test_offer_page_reads_the_version_from_settings(self):
        """Новая точка синхронизации не появилась: страница берёт версию из
        настроек, поэтому ритуал выпуска её не касается."""
        src = _read(PAGE_PY)
        assert "settings.APP_VERSION" in src
        # зашитый номер версии означал бы вторую точку синхронизации, которую
        # ритуал выпуска обязан помнить; поиск по шаблону, а не по литералу
        assert re.search(r"\d+\.\d+\.\d+", src) is None, \
            "в модуле страницы зашит номер версии"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 6, "в записи выпуска не шесть пунктов"
        for needle in ("оферт", "страниц", "реквизит", "ссылк", "редакци",
                       "ответственност"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"API", "chek", "ymaster", "ru", "MAX", "oferta"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("оферт", "/oferta", "app/legal", "Яндекс", "1С",
                       "реквизит", "исключительн"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        text = _read(JOURNAL)
        assert f"## v{self.VERSION}" in text
        section = text.split(f"## v{self.VERSION}", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        # Заголовки журнала — с прописной, как в записях прежних выпусков:
        # иголки сверяются с принятой структурой записи, а не со случайным
        # словом в тексте.
        for needle in ("оферт", "Подготовка", "План", "Отклонённые варианты",
                       "Решение", "Проверка"):
            assert needle in section, f"в журнале вёрстки нет «{needle}»"

    def test_owner_requirement_is_quoted(self):
        for path in (CHANGELOG, JOURNAL, pathlib.Path(__file__)):
            assert "продумай оферту" in _read(path), f"{path.name}: нет цитаты владельца"
