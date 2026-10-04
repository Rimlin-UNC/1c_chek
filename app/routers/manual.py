# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — встроенная инструкция по приложению (v1.21.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru
#
# Инструкция хранится на сервере и отдаётся как всплывающее окно
# («классическая инструкция»): разделы по ролям, картинки, версия =
# версия приложения. Обновляется вместе с программой.
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_USER, get_current_user
from ..database import get_db
from ..models import User
from ..services.manual_content import build_manual

router = APIRouter(prefix="/api/v1/manual", tags=["Инструкция"])


@router.get("", summary="Инструкция по приложению (по роли пользователя)")
def get_manual(db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Разделы инструкции: общие + по роли. Администратору — весь комплект
    (в интерфейсе он может переключить взгляд на бухгалтера/сотрудника)."""
    data = build_manual()
    if user.role == ROLE_ADMIN:
        return {**data, "your_role": ROLE_ADMIN, "show_tabs": True}
    audience = ROLE_ACCOUNTANT if user.role == ROLE_ACCOUNTANT else ROLE_USER
    sections = [s for s in data["sections"]
                if s["audience"] in ("all", audience)]
    return {**data, "your_role": audience, "show_tabs": False,
            "sections": sections}
