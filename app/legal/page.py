# -*- coding: utf-8 -*-
"""Страница публичной оферты: `/oferta` и `/api/v1/legal/offerta`.

Документ должен быть прочитан ДО того, как пользователь поставит галочку,
поэтому страница отдаётся без авторизации и без обращения к клиентскому
скрипту: текст приходит целиком с сервера. Это же делает её пригодной для
печати и для архива — лицензиат вправе хранить экземпляр соглашения как
доказательство объёма предоставленных прав (приём лицензионных соглашений
фирмы «1С»).

Разметка собирается на сервере по тем же данным, что и плоский текст
(`offerta.payload`), поэтому страница, текст для выгрузки и ответ
программного интерфейса не могут разъехаться: источник один.

Версия в адресе стилей берётся из `settings.APP_VERSION`, поэтому ритуал
выпуска версии не требует правки этого модуля — новой точки синхронизации
не появляется.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from . import offerta

router = APIRouter(tags=["Юридические документы"])


def _e(text: str) -> str:
    """Экранирование для вставки в текст разметки."""
    return html.escape(text or "", quote=True)


def _anchor(n: str) -> str:
    """Идентификатор якоря раздела: «s2», «s19»."""
    return "s" + n.replace(".", "-")


def _toc(data: dict) -> str:
    """Оглавление: переход к разделу без прокрутки всего документа."""
    items = ['<li><a href="#s1">1. Термины и определения</a></li>']
    items += [
        f'<li><a href="#{_anchor(s["n"])}">{s["n"]}. {_e(s["title"])}</a></li>'
        for s in data["sections"]]
    r = data["requisites"]
    items.append(f'<li><a href="#{_anchor(r["n"])}">{r["n"]}. '
                 f'{_e(r["title"])}</a></li>')
    return '<ol class="toc">' + "".join(items) + "</ol>"


def _definitions(data: dict) -> str:
    """Раздел 1: термин и его значение."""
    rows = "".join(
        f'<dt id="d{i}">{_e(d["term"])}</dt><dd>{_e(d["text"])}</dd>'
        for i, d in enumerate(data["definitions"], 1))
    return ('<section id="s1" class="sec">'
            '<h2>1. Термины и определения</h2>'
            '<dl class="defs">' + rows + "</dl></section>")


def _sections(data: dict) -> str:
    """Разделы 2—18: пункты сохраняют нумерацию текста документа."""
    out = []
    for s in data["sections"]:
        items = "".join(f"<li>{_e(c)}</li>" for c in s["clauses"])
        # Нумерация пунктов уже содержится в самом тексте документа («2.1.»),
        # поэтому маркеры списка подавлены стилями и атрибут start не нужен:
        # он обещал бы нумерацию, которой на странице нет.
        out.append(
            f'<section id="{_anchor(s["n"])}" class="sec">'
            f'<h2>{s["n"]}. {_e(s["title"])}</h2>'
            f'<ol class="clauses">{items}</ol></section>')
    return "".join(out)


def _requisites(data: dict) -> str:
    """Раздел 19: реквизиты Правообладателя либо пояснение об их отсутствии.

    Пока владелец не заполнил реквизиты в настройках, документ честно
    сообщает об этом и указывает, где сведения доступны, вместо подстановки
    выдуманных юридических фактов.
    """
    r = data["requisites"]
    body = "".join(f"<p>{_e(x)}</p>" for x in r["lines"])
    note = (f'<p class="note">{_e(r["note"])}</p>' if r["note"] else "")
    return (f'<section id="{_anchor(r["n"])}" class="sec">'
            f'<h2>{r["n"]}. {_e(r["title"])}</h2>'
            f'<div class="requisites">{body}{note}</div></section>')


def render_html(db: Session | None = None) -> str:
    """Полная страница документа."""
    d = offerta.payload(db)
    pre = "".join(f"<p>{_e(p)}</p>" for p in d["preamble"])
    req = d["requisites"]
    banner = ("" if req["complete"] else
              '<p class="banner">Реквизиты Правообладателя ещё не внесены в '
              'настройки сервиса: раздел 19 указывает источник сведений. '
              'На условия Договора это не влияет.</p>')
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{_e(d['title'])}: {_e(d['subtitle'])}. Редакция {_e(d['edition'])} от {_e(d['edition_date'])}. Правообладатель — {_e(d['operator'])}.">
<title>{_e(d['title'])} · {_e(offerta.PRODUCT)}</title>
<link rel="stylesheet" href="/legal/oferta.css?v={settings.APP_VERSION}">
</head>
<body>
<a class="skip" href="#doc">К тексту документа</a>
<header class="head">
  <p class="product">{_e(d['product'])}</p>
  <h1>{_e(d['title'])}</h1>
  <p class="subtitle">{_e(d['subtitle'])}</p>
  <p class="edition">Редакция <b>{_e(d['edition'])}</b> от {_e(d['edition_date'])}
     · Правообладатель: {_e(d['operator'])}</p>
  <p class="edition">Обращения: <a href="mailto:{_e(d['contact'])}">{_e(d['contact'])}</a>
     · <a href="{offerta.PAGE_PATH}.txt">текст для печати</a>
     · <a href="/">на сайт</a></p>
</header>
<main id="doc" class="doc">
{banner}
<section class="sec" id="lead">
  <h2>Преамбула</h2>
  {pre}
</section>
<section class="sec" id="toc">
  <h2>Содержание</h2>
  {_toc(d)}
</section>
{_definitions(d)}
{_sections(d)}
{_requisites(d)}
<footer class="foot">
  <p>Редакция {_e(d['edition'])} от {_e(d['edition_date'])}. Принятая
     Пользователем редакция фиксируется в журнале согласий сервиса вместе с
     моментом принятия. Согласие на обработку персональных данных
     (редакция {_e(d['consent_version'])}) показывается Пользователю отдельно
     и является неотъемлемой частью Договора.</p>
  <p><a href="/">На главную</a> · <a href="/b2b/">Кабинет компании</a> ·
     <a href="{offerta.PAGE_PATH}.txt">Скачать текст</a></p>
</footer>
</main>
</body>
</html>
"""


@router.get("/oferta", include_in_schema=False)
def offerta_page(db: Session = Depends(get_db)):
    """Страница публичной оферты — без авторизации, целиком с сервера."""
    return HTMLResponse(render_html(db))


@router.get("/oferta.txt", include_in_schema=False)
def offerta_plain(db: Session = Depends(get_db)):
    """Плоский текст оферты: печать, архив, сверка редакций."""
    return PlainTextResponse(offerta.full_text(db), media_type="text/plain; charset=utf-8")


@router.get("/api/v1/legal/offerta",
            summary="Публичная оферта: структура документа")
def offerta_json(db: Session = Depends(get_db)):
    """Документ для мини-приложения, кабинета компании и документации.

    Отдаётся без авторизации: оферту должен иметь возможность прочитать
    любой пользователь до того, как примет её условия.
    """
    return offerta.payload(db)
