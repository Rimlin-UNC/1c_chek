"""v1.72.1: синхронизация выпуска, документация и база знаний (T-91)."""
from __future__ import annotations

import csv
from pathlib import Path

from app.config import settings
from app.services import updater

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.72.1"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_release_versions_and_cache():
    current = settings.APP_VERSION
    assert tuple(map(int, current.split("."))) >= tuple(map(int, VERSION.split(".")))
    for path in ("app/config.py", "app/static/index.html", "app/static/sw.js",
                 "app/static/manifest.webmanifest", "app/static/maxapp/index.html", "app/static/b2b/index.html"):
        assert current in read(path), path
    assert read("app/static/index.html").count(f"?v={current}") == 3
    assert read("app/static/sw.js").count(f"?v={current}") == 4
    assert read("app/static/b2b/index.html").count(f"?v={current}") == 2
    assert f"ymaster-check-v{current}" in read("app/static/sw.js")
    assert updater.read_blocks(str(ROOT / "app/services/blocks.py"))["Рабочее место аутсорсера"] == current


def test_release_texts_and_guide():
    assert f"## [{VERSION}] - 2026-11-08" in read("CHANGELOG.md")
    js = read("app/static/js/app.js")
    assert f"'{VERSION}': [" in js
    assert "### 7.7.5. Живые обновления" in read("docs/USER_GUIDE.md")
    assert "С версии 1.72.1 — живые обновления" in read("app/services/manual_content.py")
    assert "## ADR-54" in read("docs/ARCHITECTURE.md")
    assert "## v1.72.1" in read("docs/ui_journal.md")
    assert "Последний выпуск — v" + settings.APP_VERSION in read("README.md")


def test_release_kb():
    tasks = {r["ID"]: r for r in csv.DictReader(read("docs/kb/02_roadmap.csv").splitlines())}
    assert tasks["T-91"]["Статус"] == "done"
    assert VERSION in tasks["T-91"]["Задача"]
    releases = {r["Версия"]: r for r in csv.DictReader(read("docs/kb/09_releases.csv").splitlines())}
    commit = releases[VERSION]["Коммит"]
    assert commit == "после публикации" or (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit))
    assert len(releases["1.72.0"]["Коммит"]) == 40
    assert "| Последний выпуск | **v" + settings.APP_VERSION + "**" in read("docs/kb/README.md")
    journal = list(csv.reader(read("docs/kb/08_session_journal.csv").splitlines()))
    assert all(len(row) == 7 for row in journal)
    assert any("T-91" in row[2] for row in journal)
