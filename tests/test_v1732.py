# -*- coding: utf-8 -*-
"""Замок 1.73.2: опечатка в требованиях не должна ронять установку на Linux и сборку демо-контура.

17.11.2026 вышел opencv-python-headless 5.1.0.95 — и только колёсами для Windows (win32/win_amd64/
win_arm64, ни manylinux, ни архива исходников). Требование `opencv-python-headless>=4.9` без потолка
тянуло этот выпуск и роняло установку там, где колёс нет:

    error: Distribution `opencv-python-headless==5.1.0.95` can't be installed
    because it doesn't have a source distribution or wheel for the current platform

Так покраснела сборка Vercel (коммит 5af78f6, деплой dpl_D8MikWjxjWK4KbXgvU6AG3NWPk9A): в журнале
`uv sync` отдал ту же ошибку за 4 секунды. Рабочий сервер ставит зависимости через pip — там тот же
предел: пока 5.1 без колёс для Linux, верхняя граница должна стоять и в требованиях.

Здесь — не «мозги» установщика, а замок: граница на месте, объяснена в файле и не мешает pip на этой
машине. Живая проверка разрешения — в тексте коммита и CHANGELOG (uv и pip выбирают 5.0.0.93).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _requirement(name: str) -> str:
    lines = [line.strip() for line in read("requirements.txt").splitlines()]
    found = [line for line in lines if line and not line.startswith("#") and line.split("=")[0].split(">")[0]
             .split("<")[0].strip().lower() == name.lower()]
    assert len(found) == 1, f"в requirements.txt ровно одна строка {name}: {found}"
    return found[0]


class TestOpencvCeiling:
    def test_opencv_has_upper_bound(self):
        req = _requirement("opencv-python-headless")
        assert "<5.1" in req.replace(" ", ""), (
            "у opencv-python-headless нужен потолок <5.1: выпуск 5.1.0.95 без колёс для Linux "
            "роняет установку и сборку Vercel")
        assert ">=4.9" in req.replace(" ", ""), "нижняя граница остаётся прежней"

    def test_ceiling_is_explained_next_to_it(self):
        """Причину пишут рядом с требованием: иначе потолок снимут «как устаревший»."""
        text = read("requirements.txt")
        before = text.split("opencv-python-headless", 1)[0][-700:]
        assert "5.1.0.95" in before, "рядом нет объяснения, какой выпуск и чем помешал"
        assert "Windows" in before and "Linux" in before
        assert "pip download" in before, "нет подсказки, как проверить снятие потолка"

    def test_other_packages_untouched(self):
        """Правка касается только opencv: остальные строки требований как были."""
        text = read("requirements.txt").lower()
        for must in ("numpy>=1.26", "qrcode>=7.4", "pillow>=10.0", "pypdf>=4.0", "pyjwt>=2.8"):
            assert must in text, f"пропала строка {must}"
        assert "openstacksdk" not in text

    def test_install_scripts_use_requirements_file(self):
        """Скрипты установки берут именно requirements.txt — потолок действует и там."""
        for rel in ("install.sh", "deploy.sh", "Dockerfile"):
            text = read(rel)
            assert re.search(r"requirements\.txt", text), f"{rel} не ставит зависимости из requirements.txt"

    def test_documented_in_changelog(self):
        """Раздел выпуска 1.73.2 объясняет потолок: без этого следующий агент снимет его как «лишний»."""
        ver = "1.73.2"
        text = read("CHANGELOG.md")
        assert f"## [{ver}]" in text, ver
        entry = text.split(f"## [{ver}]", 1)[1].split("\n## [", 1)[0]
        assert "opencv" in entry and "5.1.0.95" in entry
        assert "pip download" in entry, "нет подсказки, как проверить снятие потолка"
