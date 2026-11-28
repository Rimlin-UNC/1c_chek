# -*- coding: utf-8 -*-
"""Замок выпуска 1.73.5: печать УПД с телефона и бланк без электронной подписи.

Что закреплено:
 1. вкладка печати открывается без cookie сессии — по одноразовому билету, а не «Требуется
    авторизация» (так было на Android); билет не годится как токен доступа и живёт минуты;
 2. там, где браузер не вызывает печать из скрипта (мобильный Chrome), страница объясняет
    словами, где печать, а не молчит;
 3. отказ печати — это экран с объяснением, а не сырой JSON в новой вкладке;
 4. блок «ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ» в бланк не печатается (указание владельца),
    а поля этих сведений остаются в окне заполнения;
 5. точки синхронизации выпуска переведены на 1.73.5.

Проверки версии — структурные (>= 1.73.5), чтобы следующие выпуски не ломали тест.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.primary import printupd
from app.services import updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.73.5"
PREV = "1.73.4"
PRIMARY_BLOCK = "Первичка (электронные УПД)"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ver(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class TestPrintOnPhone:
    def test_print_ticket_route_is_declared(self):
        paths = app.openapi()["paths"]
        assert "get" in paths["/api/v1/primary/{doc_id}/print-ticket"]
        assert "get" in paths["/api/v1/primary/{doc_id}/print"]

    def test_print_page_does_not_require_a_cookie(self):
        """Ключевое: без cookie и без заголовка адрес с билетом отдаёт бланк, а не 401."""
        router = read("app/primary/router.py")
        assert "decode_print_ticket" in router and "current_user_optional" in router
        assert 'Query(max_length=600)' in router                  # билет приходит в адресе
        page = read("app/static/js/primary_print_page.js")
        assert "window.print()" in page

    def test_screen_never_opens_bare_print_url(self):
        """Окно печати берёт билет: иначе на телефоне снова «Требуется авторизация»."""
        js = read("app/static/js/primary.js")
        assert "/print-ticket" in js and "openWithTicket" in js
        assert not re.search(r"win\.location = `/api/v1/primary/", js), \
            "вкладка печати не должна открывать адрес без билета"

    def test_mobile_prints_are_explained_not_silent(self):
        """Android: Chrome не печатает из скрипта — вместо тишины подсказка словами."""
        page = read("app/static/js/primary_print_page.js")
        assert "Android" in page and "«Поделиться»" in page
        app_js = read("app/static/js/app.js")
        assert "printWithHint" in app_js and "printBlockedByBrowser" in app_js
        assert "«Поделиться»" in read("app/static/js/clients.js")

    def test_print_ticket_is_not_an_access_token(self):
        """Билет попадает в адрес и историю — токеном доступа он быть не должен."""
        auth = read("app/auth.py")
        body = auth.split("def decode_token", 1)[1].split("\ndef ", 1)[0]
        assert 'payload.get("scope")' in body
        assert "create_print_ticket" in auth and '"scope": "print"' in auth


class TestDeniedPrintIsHuman:
    """Никакого сырого JSON в новой вкладке: отказ печати — это экран с объяснением."""

    def test_denied_page_is_html_with_a_hint(self):
        from app.primary import router as primary_router

        for status, title in ((401, "Нужно войти в программу"),
                              (403, "Недостаточно прав для печати"),
                              (401, "Ссылка печати больше не действует")):
            page = primary_router.PRINT_DENIED_PAGE.format(title=title, hint="подсказка")
            assert page.lstrip().startswith("<!doctype html")
            assert "viewport" in page and title in page and "Печать / PDF" in page
            assert status in (401, 403)

    def test_route_returns_html_not_json_on_denial(self):
        src = read("app/primary/router.py")
        body = src.split("def print_form(", 1)[1].split("\n@router.", 1)[0]
        assert "except PrintDenied" in body and "HTMLResponse(PRINT_DENIED_PAGE" in body
        assert 'raise HTTPException(401, "Требуется авторизация")' not in src


class TestBlankWithoutEdo:
    def test_edo_block_is_not_printed(self):
        from app.primary import upd
        from app.primary.models import PrimaryDocument

        doc = PrimaryDocument()
        doc.id = "d1"
        doc.kind = "УПД"
        doc.function = "СЧФДОП"
        doc.doc_number = "118050913"
        doc.seller_name = "Интернет Решения, ООО"
        doc.buyer_name = 'ООО "ЯМАСТЕР"'
        doc.print_json = printupd.dump({"edo_operator": 'ООО "Компания "Тензор""',
                                        "edo_sent_cert": "50D9410CDF", "edo_id": "855f6271"})
        page = printupd.html(doc)
        for absent in ("ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ", "ОТПРАВЛЕНО",
                       "УТВЕРЖДЕНО", "Сертификат", "Оператор ЭДО"):
            assert absent not in page, absent
        assert "М.П." in page and "Универсальный передаточный документ" in page
        assert upd is not None   # разбор XML не тронут

    def test_edo_fields_still_available_in_the_window(self):
        """Поля не удалены: их видно и можно заполнить — они просто не печатаются."""
        assert {"edo_operator", "edo_id", "edo_sent_cert"} <= set(printupd.FIELDS)
        js = read("app/static/js/primary.js")
        assert "edo_sent_cert" in js and "group: 'ЭДО'" in js


class TestRelease1735:
    def test_version_and_updater_visibility(self):
        cur = settings.APP_VERSION
        assert _ver(cur) >= _ver(VERSION), cur
        for older in (PREV, "1.73.3", "1.73.2", "1.72.38"):
            assert updater._is_newer(cur, older) is True, older
            assert updater._is_newer(older, cur) is False, older

    def test_all_static_cache_busters_synced(self):
        cur = settings.APP_VERSION
        sw = read("app/static/sw.js")
        assert f'APP_VERSION: str = "{cur}"' in read("app/config.py")
        assert f"docver-v{cur}" in sw and sw.count(f"?v={cur}") == 5
        assert read("app/static/index.html").count(f"?v={cur}") == 3
        assert read("app/static/b2b/index.html").count(f"?v={cur}") == 2
        assert f"/maxapp/maxapp.js?v={cur}" in read("app/static/maxapp/index.html")
        assert f'"version": "{cur}"' in read("app/static/manifest.webmanifest")
        assert f"./server_monitor.js?v={cur}" in read("app/static/js/osm.js")
        about = TestClient(app).get("/api/v1/about")
        assert about.status_code == 200 and about.json()["version"] == cur

    def test_primary_block_bumped(self):
        reg = updater.read_blocks(str(ROOT / "app/services/blocks.py"))
        assert _ver(reg[PRIMARY_BLOCK]) >= _ver(VERSION), reg[PRIMARY_BLOCK]
        changed = {i["name"]: i for i in updater.diff_blocks(dict(reg, **{PRIMARY_BLOCK: PREV}), reg)}
        assert changed[PRIMARY_BLOCK]["from"] == PREV

    def test_changelog_entry(self):
        ch = read("CHANGELOG.md")
        assert ch.count(f"## [{VERSION}]") == 1
        assert ch.index(f"## [{VERSION}]") < ch.index(f"## [{PREV}]")
        entry = ch.split(f"## [{VERSION}]", 1)[1].split("\n## [", 1)[0]
        assert len(entry) <= 4000, len(entry)
        for needle in ("Требуется авторизация", "print-ticket", "Android",
                       "электронной подписи", "blocks.py"):
            assert needle in entry, needle

    def test_whats_new_and_docs(self):
        js = read("app/static/js/app.js")
        assert f"'{VERSION}': [" in js
        assert js.index(f"'{VERSION}': [") < js.index(f"'{PREV}': [")
        block = js.split(f"'{VERSION}': [", 1)[1].split("\n  ],", 1)[0]
        assert block.count("{ icon:") == 2
        for needle in ("Требуется авторизация", "Поделиться", "электронной подписи"):
            assert needle in block, needle

        match = re.search(r"## 🆕 Последний выпуск — v([\d.]+)", read("README.md"))
        assert match and _ver(match.group(1)) >= _ver(VERSION)
        assert f"| Последний выпуск | **v{settings.APP_VERSION}**" in read("docs/kb/README.md")

        journal = read("docs/ui_journal.md")
        assert journal.count(f"## v{VERSION} —") == 1
        assert journal.index(f"## v{VERSION} —") < journal.index(f"## v{PREV} —")

        rows = list(csv.DictReader(read("docs/kb/09_releases.csv").splitlines()))
        row = next(r for r in rows if r["Версия"] == VERSION)
        assert row["Дата"] == "2026-11-19"
        assert not [k for k, v in row.items() if "," in v]
