# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — письма контура B2B (v1.70.45).
#
# Отправка через app/pool/mailer.py: те же SMTP-настройки, что задаёт
# администратор, тот же адрес отправителя и подпись chek@ymaster.ru.
# Сбой почты не поднимает исключений — регистрация и заявки должны
# проходить даже без настроенного SMTP (письмо можно выслать повторно).
#
# ВАЖНО: полный ключ API в письмах НЕ отправляется. Письмо содержит
# ссылку на кабинет, где ключ показывается один раз после одобрения.
# Письмо может быть переслано или попасть в чужой ящик — ключ так
# передавать нельзя.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..pool import mailer
from .models import B2bApplication, B2bCompany, TARIFF_TITLES

log = logging.getLogger("ymaster.b2b.letters")

FROM_NAME = "Ямастер Чек — API для бизнеса"
SUPPORT = "chek@ymaster.ru"


def _wrap(title: str, body_html: str) -> str:
    """Единое оформление писем контура (фирменные цвета, читаемый текст)."""
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;'
        'padding:24px"><div style="max-width:640px;margin:0 auto;background:#fff;'
        'border:1px solid rgba(51,51,51,.12);border-radius:16px;overflow:hidden">'
        '<div style="background:#ff7a00;padding:18px 24px">'
        f'<div style="color:#fff;font-size:19px;font-weight:700">{title}</div>'
        '<div style="color:#fff;font-size:13px;opacity:.92">API для бизнеса · '
        'ООО «Ямастер»</div></div>'
        f'<div style="padding:24px;color:#333;font-size:15px;line-height:1.6">{body_html}'
        '</div><div style="padding:16px 24px;border-top:1px solid rgba(51,51,51,.12);'
        'color:#5c5c5c;font-size:12.5px">Вопросы по подключению: '
        f'<a href="mailto:{SUPPORT}" style="color:#9a4a00">{SUPPORT}</a><br>'
        'ООО «Ямастер» · ymaster.ru</div></div></div>'
    )


def _send(db: Session, to_email: str, subject: str, body: str,
          html: str) -> bool:
    """Отправка с логом результата. Возвращает признак успеха."""
    ok, err = mailer.send_mail(db, to_email, subject, body, html=html,
                               from_name=FROM_NAME)
    if not ok:
        log.info("b2b letter to %s not sent: %s", to_email, err)
    return bool(ok)


def cabinet_url(db: Session) -> str:
    """Адрес кабинета компании (для ссылок в письмах)."""
    return mailer.base_url(db).rstrip("/") + "/b2b/"


def verify_link(db: Session, token: str) -> str:
    return cabinet_url(db) + "#/confirm?token=" + token


# --------------------------------------------------------------------------
# Письма компании
# --------------------------------------------------------------------------
def send_verify(db: Session, company: B2bCompany, token: str) -> bool:
    """Подтверждение корпоративной почты: одноразовая ссылка на 24 часа."""
    link = verify_link(db, token)
    name = company.company_name or "коллеги"
    subject = "Ямастер Чек — подтвердите корпоративную почту для доступа к API"
    text = (
        f"Здравствуйте, {name}!\n\n"
        "Вы запросили доступ к разделу «API для бизнеса» — обезличенным\n"
        "агрегатам по кассовым чекам. Подтвердите корпоративную почту:\n\n"
        f"{link}\n\n"
        "Ссылка действует 24 часа и только один раз.\n\n"
        "Что будет дальше:\n"
        "1. Мы проверим почту и карточку ЕГРЮЛ/ЕГРИП по вашему ИНН.\n"
        "2. Статус компании подтвердится — откроются документация API,\n"
        "   тарифы и состав.\n"
        "3. Вы отправите заявку с описанием задачи («Обсудить подключение»).\n"
        "4. После одобрения в кабинете появится ключ API. Стоимость\n"
        "   сообщим по запросу — она зависит от лимитов и срока подписки.\n\n"
        f"Если письмо пришло по ошибке — просто не подтверждайте его или\n"
        f"напишите нам: {SUPPORT}\n"
    )
    html = _wrap("Подтвердите корпоративную почту", (
        f'<p>Здравствуйте, {name}!</p>'
        '<p>Вы запросили доступ к разделу <b>«API для бизнеса»</b> — '
        'обезличенным агрегатам по кассовым чекам.</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#ff7a00;'
        'color:#fff;text-decoration:none;padding:13px 22px;border-radius:10px;'
        'font-weight:700">Подтвердить почту</a></p>'
        '<p style="color:#5c5c5c;font-size:13px">Ссылка действует 24 часа и '
        'только один раз. Если кнопка не работает, скопируйте адрес:<br>'
        f'<span style="word-break:break-all">{link}</span></p>'
        '<p><b>Что будет дальше:</b></p><ol>'
        '<li>Проверим почту и карточку ЕГРЮЛ/ЕГРИП по вашему ИНН.</li>'
        '<li>Статус компании подтвердится — откроются документация API, '
        'тарифы и состав.</li>'
        '<li>Вы отправите заявку с описанием задачи («Обсудить подключение»).</li>'
        '<li>После одобрения в кабинете появится ключ API.</li></ol>'
        '<p>Стоимость не публикуется: она зависит от лимитов и срока подписки '
        f'— сообщим по запросу или на <a href="mailto:{SUPPORT}">{SUPPORT}</a>.</p>'
    ))
    return _send(db, company.email, subject, text, html)


def send_status(db: Session, company: B2bCompany, approved: bool) -> bool:
    """Итог проверки статуса компании: подтверждён или отклонён с причиной."""
    link = cabinet_url(db)
    if approved:
        subject = "Ямастер Чек — статус компании подтверждён, раздел API открыт"
        text = (
            f"{company.company_name or 'Здравствуйте'}!\n\n"
            "Статус вашей компании подтверждён: корпоративная почта и\n"
            "карточка ЕГРЮЛ/ЕГРИП проверены.\n\n"
            f"Раздел «API для бизнеса» открыт в кабинете: {link}\n"
            "В нём — документация API, тарифы и состав, а также кнопка\n"
            "«Обсудить подключение»: отправьте заявку с описанием задачи,\n"
            "и мы сообщим стоимость и условия.\n"
        )
        html = _wrap("Статус компании подтверждён", (
            f'<p><b>{company.company_name}</b> — статус подтверждён.</p>'
            '<p>Корпоративная почта и карточка ЕГРЮЛ/ЕГРИП проверены. '
            'Раздел «API для бизнеса» открыт в вашем кабинете.</p>'
            f'<p><a href="{link}" style="display:inline-block;background:#ff7a00;'
            'color:#fff;text-decoration:none;padding:13px 22px;border-radius:10px;'
            'font-weight:700">Открыть кабинет</a></p>'
            '<p>В разделе: документация API, тарифы и состав, кнопка '
            '«Обсудить подключение». Отправьте заявку с описанием задачи — '
            'сообщим стоимость и условия.</p>'
        ))
        return _send(db, company.email, subject, text, html)

    reason = company.reject_reason or "проверки не пройдены"
    subject = "Ямастер Чек — доступ к разделу API пока не подтверждён"
    text = (
        f"{company.company_name or 'Здравствуйте'}!\n\n"
        f"Мы не смогли подтвердить статус компании: {reason}\n\n"
        "Что можно сделать:\n"
        "— зарегистрируйтесь с почтой на домене компании (не mail.ru,\n"
        "  yandex.ru, gmail.com и подобных);\n"
        "— укажите ИНН действующей организации и наименование так, как оно\n"
        "  записано в ЕГРЮЛ/ЕГРИП;\n"
        f"— напишите нам, если данные верны: {SUPPORT}\n"
    )
    html = _wrap("Доступ пока не подтверждён", (
        f'<p>Мы не смогли подтвердить статус компании.</p>'
        f'<p style="background:#fff1e3;border-left:4px solid #ff7a00;'
        f'padding:12px 14px;border-radius:6px"><b>Причина:</b> {reason}</p>'
        '<p><b>Что можно сделать:</b></p><ul>'
        '<li>зарегистрируйтесь с почтой на домене компании (не mail.ru, '
        'yandex.ru, gmail.com и подобных);</li>'
        '<li>укажите ИНН действующей организации и наименование так, как оно '
        'записано в ЕГРЮЛ/ЕГРИП;</li>'
        f'<li>напишите нам, если данные верны: <a href="mailto:{SUPPORT}">'
        f'{SUPPORT}</a>.</li></ul>'
    ))
    return _send(db, company.email, subject, text, html)


def send_approved(db: Session, company: B2bCompany,
                  rec: B2bApplication) -> bool:
    """Заявка одобрена: ключ ждёт в кабинете (в письме ключа нет)."""
    link = cabinet_url(db) + "#/cabinet"
    tariff = TARIFF_TITLES.get(rec.tariff, rec.tariff)
    subject = "Ямастер Чек — заявка одобрена, ключ API в кабинете"
    text = (
        f"{company.company_name or 'Здравствуйте'}!\n\n"
        f"Заявка на подключение одобрена, тариф «{tariff}».\n\n"
        f"Ключ API показывается ОДИН раз в кабинете: {link}\n"
        "Скопируйте его сразу — повторно увидеть его нельзя (в базе хранится\n"
        "только хэш). Запросы идут с заголовком X-API-Key.\n\n"
        "Условия и счёт — по запросу: ответьте на это письмо или напишите на\n"
        f"{SUPPORT}\n"
    )
    html = _wrap("Заявка одобрена", (
        f'<p><b>{company.company_name}</b> — заявка на подключение одобрена.</p>'
        f'<p>Тариф: <b>{tariff}</b>.</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#ff7a00;'
        'color:#fff;text-decoration:none;padding:13px 22px;border-radius:10px;'
        'font-weight:700">Получить ключ в кабинете</a></p>'
        '<p style="background:#fff1e3;border-left:4px solid #ff7a00;'
        'padding:12px 14px;border-radius:6px">Ключ показывается <b>один раз</b>. '
        'Скопируйте его сразу: повторно увидеть нельзя — в базе хранится только '
        'хэш. Запросы идут с заголовком <code>X-API-Key</code>.</p>'
        '<p>Условия и счёт — по запросу: ответьте на это письмо.</p>'
    ))
    return _send(db, company.email, subject, text, html)


def send_application_rejected(db: Session, company: B2bCompany,
                              rec: B2bApplication) -> bool:
    """Отказ по заявке с причиной (статус компании при этом сохраняется)."""
    reason = rec.reject_reason or "не указано"
    link = cabinet_url(db) + "#/contact"
    subject = "Ямастер Чек — по заявке на подключение принято решение"
    text = (
        f"{company.company_name or 'Здравствуйте'}!\n\n"
        f"Заявка на подключение к API отклонена.\nПричина: {reason}\n\n"
        f"Раздел остаётся доступным в кабинете: {link}\n"
        f"Уточнить условия или отправить заявку заново: {SUPPORT}\n"
    )
    html = _wrap("Решение по заявке", (
        f'<p>Заявка на подключение к API <b>отклонена</b>.</p>'
        f'<p style="background:#fff1e3;border-left:4px solid #ff7a00;'
        f'padding:12px 14px;border-radius:6px"><b>Причина:</b> {reason}</p>'
        f'<p>Раздел «API для бизнеса» остаётся доступным в кабинете. '
        f'Уточнить условия: <a href="mailto:{SUPPORT}">{SUPPORT}</a>.</p>'
    ))
    return _send(db, company.email, subject, text, html)


def send_reset(db: Session, company: B2bCompany, token: str) -> bool:
    """Восстановление доступа: одноразовая ссылка на 1 час."""
    link = cabinet_url(db) + "#/newpass?token=" + token
    subject = "Ямастер Чек — ссылка для входа в кабинет API"
    text = (
        f"{company.company_name or 'Здравствуйте'}!\n\n"
        f"Ссылка для входа в кабинет (действует 1 час):\n{link}\n\n"
        f"Если вы не запрашивали вход — проигнорируйте письмо: {SUPPORT}\n"
    )
    html = _wrap("Ссылка для входа", (
        f'<p><a href="{link}" style="display:inline-block;background:#ff7a00;'
        'color:#fff;text-decoration:none;padding:13px 22px;border-radius:10px;'
        'font-weight:700">Войти в кабинет</a></p>'
        '<p style="color:#5c5c5c;font-size:13px">Ссылка действует 1 час и '
        'только один раз. Если вы не запрашивали вход — проигнорируйте письмо.</p>'
    ))
    return _send(db, company.email, subject, text, html)


# --------------------------------------------------------------------------
# Письмо администратору
# --------------------------------------------------------------------------
def send_admin_notice(db: Session, target: str, company: B2bCompany,
                      subject: str, extra: str = "") -> bool:
    """Уведомление администратора о событии в контуре.

    В письме нет ссылок на действия: решение принимается в разделе
    администратора (#/b2badmin), где видны проверки и журнал.
    """
    target = (target or "").strip()
    if not target:
        return False
    title = f"{subject} — B2B API"
    text = (
        f"{title}\n\n"
        f"Компания: {company.company_name or '—'}\n"
        f"ИНН: {company.inn or '—'} ({company.kind or 'тип не определён'})\n"
        f"Почта: {company.email}\n"
        f"Статус: {company.status}\n"
        f"Риск: {company.risk_score}\n"
        + (f"\n{extra}\n" if extra else "")
        + "\nРешение — в разделе администратора «API для бизнеса (модерация)».\n"
    )
    html = _wrap(title, (
        f'<p><b>Компания:</b> {company.company_name or "—"}</p>'
        f'<p><b>ИНН:</b> {company.inn or "—"} ({company.kind or "тип не определён"})</p>'
        f'<p><b>Почта:</b> {company.email}</p>'
        f'<p><b>Статус:</b> {company.status} · <b>риск:</b> {company.risk_score}</p>'
        + (f'<p>{extra}</p>' if extra else "")
        + '<p>Решение — в разделе администратора «API для бизнеса (модерация)».</p>'
    ))
    return _send(db, target, title, text, html)
