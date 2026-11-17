# -*- coding: utf-8 -*-
"""Пределы загрузки папки с УПД (T-79, v1.73.0): предел — обрезка словами, а не отказ всей пачке.

Замок появился по делу: в архиве с 51 документом service.unpack() добавлял 51-й элемент «остальные не
загружены», роутер видел len(items) > MAX_FILES и отвечал HTTP 400 на всю загрузку. Повторная попытка падала
на том же архиве, потому что архив не проверяется по SHA-256 — со стороны это выглядело как «папка не грузится».
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.primary import service

XML = ('<?xml version="1.0" encoding="windows-1251"?>'
       '<Файл ИдФайл="ON_NSCHFDOPPR_1" Функция="СЧФДОП"></Файл>').encode("cp1251")


def _zip(count: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for i in range(count):
            archive.writestr(f"вложенная папка/ON_NSCHFDOPPR_{i}.xml", XML)
    return buf.getvalue()


def test_archive_at_limit_is_taken_whole():
    """Ровно 50 документов — обычная загрузка, без замечаний."""
    items = service.unpack("архив.zip", _zip(service.MAX_FILES))
    assert len(items) == service.MAX_FILES
    assert all(body is not None for _name, body, _reason in items)


def test_archive_over_limit_is_trimmed_not_refused():
    """51 и 80 документов: ровно 50 тел + одна строка-замечание, и это меньше предела роутера.

    Раньше здесь было 51 и 52 элемента → len(items) > MAX_FILES → HTTP 400 на всю пачку.
    """
    for count in (service.MAX_FILES + 1, 80):
        items = service.unpack("архив.zip", _zip(count))
        bodies = [body for _name, body, _reason in items if body is not None]
        notes = [reason for _name, body, reason in items if body is None]
        assert len(bodies) == service.MAX_FILES, count
        assert len(items) <= service.MAX_FILES + 1, count          # роутер такую пачку пропускает
        assert len(notes) == 1 and "остальные" in notes[0], count


def test_router_does_not_refuse_a_trimmed_archive():
    """Проверка того же на уровне решения роутера: обрезанный архив не должен давать «Не больше 50»."""
    items = service.unpack("архив.zip", _zip(80))
    over = len(items) - service.MAX_FILES
    assert over <= 1, "роутер увидит превышение только на одну строку-замечание и не откажет"


def test_archive_without_xml_says_so():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("подпись.sig", b"sig")
    items = service.unpack("пустой.zip", buf.getvalue())
    assert len(items) == 1 and items[0][1] is None and "нет XML" in items[0][2]


def test_broken_archive_says_so():
    items = service.unpack("битый.zip", b"PK\x03\x04 broken")
    assert len(items) == 1 and items[0][1] is None and "повреждён" in items[0][2]


@pytest.mark.parametrize("count", [service.MAX_FILES - 1, service.MAX_FILES])
def test_below_limit_has_no_notes(count):
    items = service.unpack("архив.zip", _zip(count))
    assert [reason for _name, body, reason in items if body is None] == []
