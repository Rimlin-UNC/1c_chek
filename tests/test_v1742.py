"""Замки выпуска v1.74.2 — полноценное меню «⋯ Ещё» в разделе «Настройки»."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import settings
from app.services.blocks import BLOCKS
from app.services.manual_content import SECTIONS

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "js" / "app.js").read_text("utf-8")
LAYOUT_JS = (ROOT / "app" / "static" / "js" / "settings_layout.js").read_text("utf-8")
APP_CSS = (ROOT / "app" / "static" / "css" / "app.css").read_text("utf-8")
SW_JS = (ROOT / "app" / "static" / "sw.js").read_text("utf-8")
MANIFEST = json.loads((ROOT / "app" / "static" / "manifest.webmanifest").read_text("utf-8"))
README = (ROOT / "README.md").read_text("utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text("utf-8")
UI_JOURNAL = (ROOT / "docs" / "ui_journal.md").read_text("utf-8")


def test_version_synced_to_1742():
    assert settings.APP_VERSION == "1.74.2"
    assert MANIFEST["version"] == "1.74.2"
    assert "const CACHE = 'ymaster-check-v1.74.2';" in SW_JS
    assert SW_JS.count("?v=1.74.2") == 5
    assert "## 🆕 Последний выпуск — v1.74.2" in README
    assert "## [1.74.2]" in CHANGELOG
    assert "## v1.74.2" in UI_JOURNAL
    assert "'1.74.2':" in APP_JS
    assert BLOCKS["Настройки"] == "1.74.2"
    assert BLOCKS["Адаптивный интерфейс"] == "1.74.2"
    assert BLOCKS["Инструкция"] == "1.74.2"


def test_settings_more_outside_scrollable_tablist():
    nav_m = re.search(
        r'<nav class="settings-nav settings-tabs" id="settings-nav".*?</nav>',
        APP_JS,
        re.S,
    )
    assert nav_m, "В app.js должен присутствовать блок #settings-nav"
    nav = nav_m.group(0)
    row_end = nav.index("</div>", nav.index('class="stg-tabs-row"'))
    tools_pos = nav.index('class="stg-tabs-tools"')
    assert tools_pos > row_end, (
        ".stg-tabs-tools обязан стоять вне .stg-tabs-row[role=tablist], "
        "иначе overflow-x:auto / overflow-y:hidden обрезает выпадающее меню на телефоне"
    )
    for item_id in (
        "snav-search",
        "snav-toc",
        "snav-all",
        "snav-expand",
        "snav-collapse",
        "snav-copy-link",
    ):
        assert f'id="{item_id}"' in nav


def test_settings_more_actions_and_pure_search_helper():
    assert "export function filterSettingsCards(cards, query)" in LAYOUT_JS
    assert "document." not in LAYOUT_JS
    assert "window." not in LAYOUT_JS
    assert "function setSettingsCardCollapsed(card, collapsed)" in APP_JS
    assert "const openSettingsSearchModal = () =>" in APP_JS
    assert "max-width: calc(100vw - 24px);" in APP_CSS
    assert ".stg-find-item" in APP_CSS
    assert ".glass.card.card-flash" in APP_CSS


def test_manual_settings_guide_mentions_more_menu():
    sec = next((s for s in SECTIONS if s["id"] == "settings-guide"), None)
    assert sec is not None
    html = sec["html"]
    assert "⋯ Ещё" in html
    assert "Найти настройку" in html
    assert "Свернуть карточки" in html
