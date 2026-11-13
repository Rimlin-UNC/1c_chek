"""Счётчики пунктов меню в пространстве текущего пользователя."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..services.navigation_counts import counts

router = APIRouter(prefix='/api/v1/navigation', tags=['Навигация'])


@router.get('/counts', summary='Счётчики доступных разделов')
def navigation_counts(response: Response, user: Annotated[User, Depends(get_current_user)],
                      db: Annotated[Session, Depends(get_db)],
                      company_id: Annotated[str | None, Query(max_length=36)] = None) -> dict[str, int]:
    response.headers['Cache-Control'] = 'no-store, private'
    return counts(db, user, company_id)
