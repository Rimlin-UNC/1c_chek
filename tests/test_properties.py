# -*- coding: utf-8 -*-
"""
Ямастер Чек — проверки свойств чистых функций (hypothesis), v1.70.38.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru

ЗАЧЕМ ЭТОТ ФАЙЛ. Обычный тест проверяет несколько примеров; проверка свойств
проверяет ПРАВИЛО на сотнях автоматически подобранных значений, включая те, до
которых человек не додумается (пустая строка, 4001 символ, одиночный суррогат,
нуль, отрицательное число). Именно так нашлись бы переполнение номера страницы
и падение на пустом файле изображения — оба нашли внешние инструменты, но
свойства закрепляют их навсегда.

Два типа свойств здесь:
  1) КРУГОВОЙ ПРОХОД — собрали корректное значение, разобрали, получили то же
     самое (QR-чек, пароль, ИНН с контрольным числом);
  2) УСТОЙЧИВОСТЬ — на любом мусоре функция либо возвращает результат, либо
     бросает СВОЙ оговорённый отказ, но никогда не падает иначе и не зависает.
     Для сервера это важнее красоты: любое исключение мимо обработчика — это
     ответ 500 пользователю.

Запуск:
    .venv/bin/pytest tests/test_properties.py -q
"""
from __future__ import annotations

import os
import string

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from app.auth import hash_password, verify_password
from app.services.companies_util import (
    _INN10_WEIGHTS,
    _INN12_W1,
    _INN12_W2,
    _inn_checksum,
    inn_is_valid,
    inn_kind,
    normalize_inn,
)
from app.services.maxapp import validate_init_data
from app.services.qr import QRParseError, parse_qr

# Свойства проверяются на десятках значений, а не на сотнях: цель — поймать
# нарушение правила, а не покрыть все ветки (это делают обычные тесты).
FAST = settings(max_examples=60, deadline=None,
                suppress_health_check=[HealthCheck.too_slow,
                                       HealthCheck.function_scoped_fixture])

# Хэш пароля — PBKDF2 с 260 000 итераций: один пример стоит ~0,3 с, поэтому
# здесь примеров меньше. Правила это не ослабляет (оно одно и то же для любого
# пароля), а прогон остаётся быстрым.
SLOW = settings(max_examples=8, deadline=None,
                suppress_health_check=[HealthCheck.too_slow])

# «мусор»: текст любой длины, включая управляющие символы, nul, суррогаты,
# emoji и обрезанный UTF-8 — то, что реально прилетает с касс и из интерфейса
JUNK = st.text(min_size=0, max_size=200)


# --------------------------------------------------------------------------
#  1. QR-код чека: круговой проход и устойчивость разбора
# --------------------------------------------------------------------------
def _valid_qr(fn: str, fd: str, fp: str, total: float, op: int) -> str:
    """Собрать строку QR в формате 54-ФЗ из корректных реквизитов."""
    return (f"t=20260115T101530&s={total:.2f}&fn={fn}&i={fd}&fp={fp}&n={op}")


@FAST
@given(fn=st.text(alphabet=string.digits, min_size=8, max_size=20),
       fd=st.text(alphabet=string.digits, min_size=1, max_size=10),
       fp=st.text(alphabet=string.digits, min_size=1, max_size=10),
       total=st.floats(min_value=0.01, max_value=9_999_999.99,
                       allow_nan=False, allow_infinity=False),
       op=st.integers(min_value=1, max_value=4))
@example(fn="9900000000016902", fd="12345", fp="3673437411", total=100.0, op=1)
def test_parse_qr_round_trip(fn, fd, fp, total, op):
    """Корректный QR разбирается, и реквизиты не искажаются."""
    raw = _valid_qr(fn, fd, fp, total, op)
    parsed = parse_qr(raw)
    assert parsed.fn == fn
    assert parsed.fd == fd
    assert parsed.fp == fp
    assert parsed.operation == op
    # сумма — копейка в копейку: разбор не должен округлять самовольно
    assert abs(parsed.total_sum - round(total, 2)) < 0.005
    # ключ дедупликации собран ровно из ФН/ФД/ФП
    assert parsed.dedup_key == f"{fn}|{fd}|{fp}"


@FAST
@given(raw=JUNK)
def test_parse_qr_never_crashes(raw):
    """На любом мусоре разбор либо даёт результат, либо свой отказ QRParseError.

    Любое другое исключение означало бы ответ 500 на ввод пользователя."""
    try:
        parsed = parse_qr(raw)
    except QRParseError:
        return
    assert parsed.fn and parsed.fd and parsed.fp


@FAST
@given(padding=st.text(min_size=0, max_size=60))
def test_parse_qr_rejects_overlong(padding):
    """Строка длиннее 4000 символов отвергается до разбора (гигиена ЦП).

    v1.70.39: хвост сделан ПОСТОЯННЫМ, а случайной осталась только короткая
    добавка. Раньше хвост генерировался случайным текстом длиной 3990–4500
    символов, и hypothesis время от времени отказывался работать:
    FailedHealthCheck «Generated inputs routinely consumed more than the
    maximum allowed entropy» — плавающее падение на ровном месте. Правило
    проверяется то же самое, энтропии на пример теперь в сотни раз меньше.
    """
    raw = ("t=20260115T101530&s=10.00&fn=9900000000016902&i=1&fp=2&n=1"
           + "0" * 4000 + padding)
    assert len(raw) > 4000
    with pytest.raises(QRParseError):
        parse_qr(raw)


@FAST
@given(fn=st.text(alphabet=string.digits, min_size=8, max_size=20),
       fd=st.text(alphabet=string.digits, min_size=1, max_size=10),
       fp=st.text(alphabet=string.digits, min_size=1, max_size=10))
def test_parse_qr_url_form_matches_key_value_form(fn, fd, fp):
    """QR в виде ссылки (его печатают некоторые ОФД) разбирается так же.

    v1.70.39: длины генерируются сразу в допустимых пределах, без assume().
    Раньше значения брались из общего набора 1–30 цифр и отсеивались через
    assume — hypothesis время от времени отвечал FailedHealthCheck
    «filtering out a lot of inputs», и тест падал плавающим образом.
    """
    kv = parse_qr(f"t=20260115T101530&s=10.00&fn={fn}&i={fd}&fp={fp}&n=1")
    url = parse_qr(f"https://ofd.ru/receipt?fn={fn}&i={fd}&fp={fp}&s=10.00")
    assert (kv.fn, kv.fd, kv.fp) == (url.fn, url.fd, url.fp)


# --------------------------------------------------------------------------
#  2. ИНН: контрольные числа, нормализация, вид
# --------------------------------------------------------------------------
def _with_check(prefix: str, weights) -> str:
    return prefix + str(_inn_checksum(prefix, weights))


@FAST
@given(prefix=st.text(alphabet=string.digits, min_size=9, max_size=9))
def test_inn10_valid_by_construction(prefix):
    """ИНН юрлица, собранный по алгоритму контрольного числа, принимается."""
    inn = _with_check(prefix, _INN10_WEIGHTS)
    assert inn_is_valid(inn)
    assert inn_kind(inn) == "legal"


@FAST
@given(prefix=st.text(alphabet=string.digits, min_size=10, max_size=10))
def test_inn12_valid_by_construction(prefix):
    """ИНН ИП: 11-я цифра считается по весам W1, 12-я — по весам W2."""
    eleven = _with_check(prefix, _INN12_W1)      # 10 цифр + контрольная
    assert len(eleven) == 11
    twelve = _with_check(eleven, _INN12_W2)      # 11 цифр + контрольная
    assert inn_is_valid(twelve)
    assert inn_kind(twelve) == "individual"


@FAST
@given(value=JUNK)
def test_inn_never_crashes_and_rejects_wrong_length(value):
    """ИНН любой длины, кроме 10 и 12 цифр, отвергается; функция не падает."""
    assert isinstance(inn_is_valid(value), bool)
    digits = normalize_inn(value)
    if len(digits) not in (0, 10, 12):
        assert not inn_is_valid(value)
        assert inn_kind(value) is None


@FAST
@given(value=JUNK)
def test_normalize_inn_is_digits_only_and_idempotent(value):
    """Нормализация оставляет только цифры и повторное применение ничего не меняет."""
    once = normalize_inn(value)
    assert once.isdigit() or once == ""
    assert normalize_inn(once) == once


@FAST
@given(prefix=st.text(alphabet=string.digits, min_size=9, max_size=9),
       noise=st.text(alphabet=" -_./()", min_size=0, max_size=6))
def test_inn_tolerates_formatting(prefix, noise):
    """Разделители и пробелы в середине ИНН не мешают проверке (так вставляют из 1С)."""
    inn = _with_check(prefix, _INN10_WEIGHTS)
    messy = inn[:4] + noise + inn[4:]
    assert inn_is_valid(messy)


# --------------------------------------------------------------------------
#  3. Пароль: круговой проход, соль, мусор в хранилище
# --------------------------------------------------------------------------
@SLOW
@given(password=st.text(min_size=1, max_size=64))
def test_password_round_trip(password):
    """Любой пароль проходит проверку по своему хэшу и только по нему."""
    stored = hash_password(password)
    assert verify_password(password, stored)
    assert not verify_password(password + "x", stored)


@SLOW
@given(password=st.text(min_size=1, max_size=32))
def test_password_salt_is_random(password):
    """Два хэша одного пароля различаются: соль случайна (радужные таблицы мимо)."""
    assert hash_password(password) != hash_password(password)


@FAST
@given(password=st.text(min_size=0, max_size=32), stored=JUNK)
def test_verify_password_never_crashes(password, stored):
    """Битая запись хэша в базе даёт отказ, а не исключение."""
    assert verify_password(password, stored) is False


# --------------------------------------------------------------------------
#  4. Вход через мини-приложение MAX: устойчивость проверки подписи
# --------------------------------------------------------------------------
@FAST
@given(init_data=JUNK)
def test_validate_init_data_never_crashes(init_data):
    """Любой initData даёт известный код отказа, а не исключение.

    Коды оговорены в docstring: not_configured | format | bad_hash | expired."""
    user, err = validate_init_data(init_data, "бот-токен")
    assert user is None
    assert err in ("format", "bad_hash", "expired", "not_configured")


@FAST
@given(init_data=JUNK)
def test_validate_init_data_without_token_is_not_configured(init_data):
    """Без токена бота проверка даже не начинается — единый код для всех."""
    user, err = validate_init_data(init_data, "")
    assert (user, err) == (None, "not_configured")


# --------------------------------------------------------------------------
#  5. Чтение числовых настроек из .env (v1.70.38: опечатка не роняет старт)
# --------------------------------------------------------------------------
# в переменную окружения нельзя записать нулевой байт — это ограничение
# операционной системы (ValueError: embedded null byte), а не приложения,
# поэтому из набора мусора он исключён
ENV_JUNK = JUNK.filter(lambda s: "\x00" not in s)


@FAST
@given(value=ENV_JUNK)
def test_int_env_never_crashes(value, monkeypatch):
    """Неразборчивое значение .env даёт рабочий дефолт, а не падение импорта."""
    import logging

    from app.config import _int_env
    # предупреждений будет по одному на пример — приглушаем, чтобы не топить
    # вывод прогона (сам текст предупреждения проверяется в test_v17038.py)
    monkeypatch.setattr(logging.getLogger("ymaster"), "level", logging.CRITICAL)
    monkeypatch.setenv("YM_TEST_INT", value)
    got = _int_env("YM_TEST_INT", 7)
    assert isinstance(got, int)
    assert got >= 1


@FAST
@given(number=st.integers(min_value=1, max_value=10_000))
def test_int_env_reads_valid_number(number):
    """Корректное число читается как есть, включая пробелы по краям."""
    from app.config import _int_env
    os.environ["YM_TEST_INT"] = f"  {number}  "
    try:
        assert _int_env("YM_TEST_INT", 7) == number
    finally:
        os.environ.pop("YM_TEST_INT", None)


@FAST
@given(number=st.integers(min_value=-10_000, max_value=0))
def test_int_env_rejects_too_small(number):
    """Ноль и отрицательные значения недопустимы — берётся дефолт."""
    from app.config import _int_env
    os.environ["YM_TEST_INT"] = str(number)
    try:
        assert _int_env("YM_TEST_INT", 7) == 7
    finally:
        os.environ.pop("YM_TEST_INT", None)
