"""Добровольное подтверждение выданного текста действующей оферты.

Новый канал не изменяет существующие журналы и не восстанавливает историю.
Подпись служебного билета не является электронной подписью пользователя;
сам факт выдачи текста не доказывает прочтение или полномочия лица.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import parse_qs

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..auth import decode_token, get_current_user, raw_bearer_token
from ..config import settings
from ..database import Base, get_db
from ..models import User, iso_z, utcnow
from . import offerta

router = APIRouter(prefix="/api/v1/legal/acceptance", tags=["Юридические документы"])
HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
           "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}


class OfferAcceptance(Base):
    """Идентификатор пользователя — связь с событием, а не доказательство полномочий."""
    __tablename__ = "legal_offer_acceptances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    edition: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


def _real_user(user: Annotated[User, Depends(get_current_user)],
               token: Annotated[str | None, Depends(raw_bearer_token)]) -> User:
    # Режим «посмотреть глазами» не может подтверждать документы за пользователя.
    if not token or 'act' in decode_token(token):
        raise HTTPException(403, "Подтверждение из режима просмотра недоступно")
    return user


UserDep = Annotated[User, Depends(_real_user)]
Db = Annotated[Session, Depends(get_db)]


def _key() -> bytes:
    # Отдельный ключ: билет нельзя использовать как JWT для входа.
    return hmac.new(settings.SECRET_KEY.encode(), b'legal-offer-acceptance-v1', hashlib.sha256).digest()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _ticket(user_id: str, text: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({'sub': user_id, 'jti': secrets.token_hex(32), 'sha': _digest(text),
                       'edition': offerta.EDITION, 'purpose': 'offer-acceptance',
                       'iat': now, 'exp': now + timedelta(minutes=30)}, _key(), algorithm='HS256')


def _claims(ticket: str, user_id: str) -> dict:
    try:
        claims = jwt.decode(ticket, _key(), algorithms=['HS256'],
                            options={'require': ['sub', 'jti', 'sha', 'edition', 'purpose', 'exp', 'iat']})
    except jwt.PyJWTError as exc:
        raise HTTPException(403, 'Недействительный или просроченный билет; откройте документ заново') from exc
    if (claims.get('sub') != user_id or claims.get('purpose') != 'offer-acceptance'
            or not isinstance(claims.get('jti'), str) or len(claims['jti']) != 64
            or not isinstance(claims.get('sha'), str) or len(claims['sha']) != 64
            or not isinstance(claims.get('edition'), str)):
        raise HTTPException(403, 'Недействительный билет')
    return claims


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/legal/oferta.css?v={settings.APP_VERSION}">
</head><body><main class="doc"><h1>{html.escape(title)}</h1>{body}
<p><a href="/oferty">Раздел оферт</a> · <a href="/">На главную</a></p></main></body></html>''',
                        status_code=status, headers=HEADERS)


@router.get('/form', response_class=HTMLResponse)
def acceptance_form(user: UserDep, db: Db):
    text = offerta.full_text(db)
    ticket = _ticket(user.id, text)
    body = ('<p>Добровольное подтверждение действующей оферты. Оно фиксирует действие '
            'этой учётной записи; полномочия представителя организации не проверяются. '
            'Согласие на обработку персональных данных оформляется отдельно.</p>'
            '<pre id="offer-text" style="white-space:pre-wrap;overflow-wrap:anywhere">'
            + html.escape(text) + '</pre>'
            '<form method="post" action="/api/v1/legal/acceptance">'
            + f'<input type="hidden" name="ticket" value="{html.escape(ticket, quote=True)}">'
            + '<label><input type="checkbox" name="accept" value="yes" required> '
            'Согласен с приведённым текстом действующей оферты</label>'
            '<p><button type="submit">Подтвердить</button></p></form>'
            '<p><a href="/api/v1/legal/acceptance/history">Мои подтверждения</a></p>')
    return _page('Действующая оферта — подтверждение', body)


async def _form_body(request: Request) -> dict:
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
        raise HTTPException(415, 'Нужна форма подтверждения')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise HTTPException(413, 'Форма слишком велика')
        raw.extend(chunk)
    try:
        fields = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=4, strict_parsing=True)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, 'Некорректная форма') from exc
    if set(fields) != {'ticket', 'accept'} or any(len(x) != 1 for x in fields.values()):
        raise HTTPException(422, 'Нужно подтвердить согласие')
    if fields['accept'][0] != 'yes':
        raise HTTPException(422, 'Нужно подтвердить согласие')
    return {'ticket': fields['ticket'][0]}


@router.post('')
async def accept_offer(request: Request, user: UserDep, db: Db):
    body = await _form_body(request)
    claims = _claims(body['ticket'], user.id)
    existing = db.get(OfferAcceptance, claims['jti'])
    if existing is None:
        text = offerta.full_text(db)
        if claims['edition'] != offerta.EDITION or not hmac.compare_digest(claims['sha'], _digest(text)):
            raise HTTPException(409, 'Оферта изменилась. Откройте документ и ознакомьтесь с новой редакцией')
        existing = OfferAcceptance(id=claims['jti'], user_id=user.id, edition=offerta.EDITION,
                                   text=text, sha256=claims['sha'])
        db.add(existing)
        try:
            db.commit()
        except IntegrityError:
            # Одновременный повтор той же формы: в журнале остаётся одно событие.
            db.rollback()
            existing = db.get(OfferAcceptance, claims['jti'])
            if existing is None or existing.user_id != user.id or existing.sha256 != claims['sha']:
                raise HTTPException(409, 'Не удалось сохранить подтверждение') from None
    elif existing.user_id != user.id or existing.sha256 != claims['sha']:
        raise HTTPException(403, 'Недействительный билет')
    return RedirectResponse('/api/v1/legal/acceptance/history/' + existing.id, status_code=303, headers=HEADERS)


@router.get('/history', response_class=HTMLResponse)
def history(user: UserDep, db: Db, page: Annotated[int, Query(ge=1, le=10000)] = 1):
    records = db.scalars(select(OfferAcceptance).where(OfferAcceptance.user_id == user.id)
                         .order_by(OfferAcceptance.accepted_at.desc(), OfferAcceptance.id.desc())
                         .offset((page - 1) * 100).limit(101)).all()
    links = ''.join('<li><a href="/api/v1/legal/acceptance/history/' + row.id + '">'
                    + html.escape(row.edition) + ' · ' + html.escape(iso_z(row.accepted_at) or '')
                    + '</a></li>' for row in records[:100])
    previous = f'<a href="?page={page - 1}">Новые записи</a> ' if page > 1 else ''
    following = f'<a href="?page={page + 1}">Более ранние записи</a>' if len(records) > 100 and page < 10000 else ''
    return _page('Мои подтверждения', '<p>Подтверждения этой учётной записи. '
                 'Старые события, не имеющие точного снимка, здесь не отображаются.</p><ul>' + links + '</ul>'
                 + '<p>' + previous + following + '</p>'
                 + '<p><a href="/api/v1/legal/acceptance/form">Открыть действующую оферту</a></p>')


@router.get('/history/{record_id}', response_class=HTMLResponse)
def detail(record_id: str, user: UserDep, db: Db):
    row = db.get(OfferAcceptance, record_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, 'Подтверждение не найдено')
    if not hmac.compare_digest(row.sha256, _digest(row.text)):
        raise HTTPException(409, 'Контрольная сумма сохранённого документа не совпадает')
    return _page('Сохранённое подтверждение',
                 '<p>Учётная запись: ' + html.escape(row.user_id) + '; идентификатор события: '
                 + html.escape(row.id) + '; Канал: веб-форма; действие: отметка и подтверждение.</p>'
                 '<p>Редакция: ' + html.escape(row.edition) + '; время UTC: '
                 + html.escape(iso_z(row.accepted_at) or '') + '; SHA-256: ' + html.escape(row.sha256) + '.</p>'
                 '<p>Подтверждение фиксирует действие учётной записи и выданный ей текст; '
                 'оно не доказывает прочтение или полномочия. Контрольная сумма без внешней подписи '
                 'не защищает от подмены базы администратором.</p>'
                 '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + html.escape(row.text) + '</pre>'
                 '<p><a href="/api/v1/legal/acceptance/history">Мои подтверждения</a></p>')
