# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — компании-клиенты (мультикомпанийность, v1.11.0).
#
# Платформа аутсорсинга бухгалтерии: администратор ведёт неограниченное
# число компаний (ООО, ИП); каждая — изолированное пространство со своими
# сотрудниками и чеками. Модель изоляции: общая БД, разграничение по
# company_id на уровне КАЖДОГО запроса (см. services/scoping.py) —
# стандартный подход SaaS для этой связки технологий.
#
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_USER, require_admin
from ..database import get_db
from ..models import Company, Invite, Receipt, User
from ..schemas import (CheckoLookup, CompanyCreate, CompanyDeleteBody,
                       CompanyPatch)
from ..services import checko, appsettings
from ..services import registry
from ..services.audit import log_action
from ..services.companies_util import (SIMILAR_THRESHOLD, canonical_name,
                                       inn_is_valid, inn_kind,
                                       normalize_inn, risk_assessment,
                                       similar_ratio)

router = APIRouter(prefix="/api/v1/companies", tags=["Компании"])


# --- v1.13.0: защита от дублей компаний (канон + ИНН) + похожесть -----------

def _canonical_taken(db: Session, name: str, exclude_id: str | None = None) -> Company | None:
    """Компания с тем же каноническим названием («ООО "Ямастер"» == «ООО «ЯМАСТЕР»»)."""
    key = canonical_name(name)
    for c in db.query(Company).all():
        if c.id != exclude_id and canonical_name(c.name) == key:
            return c
    return None


def _inn_taken(db: Session, inn: str, exclude_id: str | None = None) -> Company | None:
    inn = normalize_inn(inn)
    if not inn:
        return None
    for c in db.query(Company).all():
        if c.id != exclude_id and normalize_inn(c.inn) == inn:
            return c
    return None


def _similar_companies(db: Session, name: str, exclude_id: str | None = None,
                       limit: int = 5) -> list[dict]:
    out = []
    for c in db.query(Company).all():
        if c.id == exclude_id:
            continue
        ratio = similar_ratio(name, c.name)
        if ratio >= SIMILAR_THRESHOLD:
            out.append({"id": c.id, "name": c.name, "inn": c.inn,
                        "ratio": round(ratio, 2)})
    return sorted(out, key=lambda x: -x["ratio"])[:limit]


def _stats(db: Session, company_ids: list[str]) -> dict[str, dict]:
    """Чеки/сумма/последняя активность по списку компаний (одним запросом)."""
    if not company_ids:
        return {}
    rows = (db.query(Receipt.company_id,
                     func.count(Receipt.id),
                     func.coalesce(func.sum(Receipt.total_sum), 0.0),
                     func.max(Receipt.created_at))
            .filter(Receipt.company_id.in_(company_ids))
            .group_by(Receipt.company_id).all())
    return {r[0]: {"receipts": int(r[1]), "sum": round(float(r[2]), 2),
                   "last_activity": r[3].isoformat() if r[3] else None}
            for r in rows}


def _team(db: Session, company_ids: list[str]) -> dict[str, dict]:
    """Сотрудники по компаниям: бухгалтеры / пользователи / активные."""
    if not company_ids:
        return {}
    rows = (db.query(User.company_id, User.role, func.count(User.id))
            .filter(User.company_id.in_(company_ids), User.is_active == True)  # noqa: E712
            .group_by(User.company_id, User.role).all())
    out: dict[str, dict] = {cid: {"accountants": 0, "users": 0} for cid in company_ids}
    for cid, role, cnt in rows:
        if cid not in out:
            continue
        if role == ROLE_ACCOUNTANT:
            out[cid]["accountants"] = int(cnt)
        elif role == ROLE_USER:
            out[cid]["users"] = int(cnt)
    return out


@router.get("", summary="Список компаний со статистикой (администратор)")
def list_companies(admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    comps = db.query(Company).order_by(Company.created_at).all()
    ids = [c.id for c in comps]
    receipts = _stats(db, ids)
    teams = _team(db, ids)
    return [{
        **c.to_dict(),
        "receipts": receipts.get(c.id, {}).get("receipts", 0),
        "receipts_sum": receipts.get(c.id, {}).get("sum", 0.0),
        "last_activity": receipts.get(c.id, {}).get("last_activity"),
        "accountants": teams.get(c.id, {}).get("accountants", 0),
        "users": teams.get(c.id, {}).get("users", 0),
    } for c in comps]


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Создать компанию-клиента (администратор)")
def create_company(body: CompanyCreate, admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Название компании слишком короткое")
    # v1.13.0: защита от дублей — каноническое имя и ИНН
    dup = _canonical_taken(db, name)
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Похожая компания уже есть: «{dup.name}» — это дубли")
    inn = normalize_inn(body.inn)
    if not inn_is_valid(inn):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "ИНН некорректен: 10 цифр для ООО или 12 для ИП")
    dup_inn = _inn_taken(db, inn)
    if dup_inn:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Компания с таким ИНН уже есть: «{dup_inn.name}»")
    comp = Company(name=name, inn=inn, note=body.note.strip())
    db.add(comp)
    db.commit()
    db.refresh(comp)
    log_action(admin, "company_created", "company", comp.id, {"name": comp.name})
    return {**comp.to_dict(), "receipts": 0, "receipts_sum": 0.0,
            "last_activity": None, "accountants": 0, "users": 0}


@router.patch("/{company_id}", summary="Изменить компанию (администратор)")
def patch_company(company_id: str, body: CompanyPatch,
                  admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    if body.name is not None:
        name = body.name.strip()
        if len(name) < 2:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Название компании слишком короткое")
        name_cf = name.casefold()
        dup = any(c.name.casefold() == name_cf and c.id != comp.id
                  for c in db.query(Company).all())
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Компания с таким названием уже есть")
        comp.name = name
    if body.inn is not None:
        inn = normalize_inn(body.inn)
        if not inn_is_valid(inn):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Некорректный ИНН (10 цифр для ООО, 12 для ИП)")
        dup_inn = _inn_taken(db, inn, exclude_id=comp.id)
        if dup_inn:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Компания с таким ИНН уже есть: «{dup_inn.name}»")
        comp.inn = inn
    if body.note is not None:
        comp.note = body.note.strip()
    if body.is_active is not None:
        if body.is_active is False:
            # Архивировать можно; последняя активная — нет (система остаётся
            # хотя бы с одной рабочей компанией)
            active = db.query(func.count(Company.id)).filter(
                Company.is_active == True, Company.id != comp.id).scalar()  # noqa: E712
            if not active:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "Нельзя архивировать последнюю активную компанию")
            comp.is_active = False
        else:
            comp.is_active = True
    db.commit()
    log_action(admin, "company_updated", "company", comp.id,
               {"name": comp.name, "is_active": comp.is_active})
    return comp.to_dict()


@router.get("/{company_id}/users", summary="Сотрудники компании (администратор)")
def company_users(company_id: str, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    rows = (db.query(User).filter(User.company_id == company_id)
            .order_by(User.role, User.username).all())
    return [u.to_dict() for u in rows]


# --------------------------------------------------------------------------
#  v1.13.0: карточка компании, Checko.ru, умное удаление
# --------------------------------------------------------------------------
@router.get("/similar", summary="Похожие компании по названию (администратор)")
def similar_companies(name: str = Query(min_length=2, max_length=200),
                      exclude_id: str | None = Query(None, max_length=36),
                      admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Подсказка в диалоге создания: «похожая компания уже есть»."""
    return _similar_companies(db, name, exclude_id)


@router.post("/lookup-checko", summary="Предпросмотр карточки по ИНН из Checko (без сохранения)")
def lookup_checko(body: CheckoLookup, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    inn = normalize_inn(body.inn)
    if not inn_is_valid(inn):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Некорректный ИНН (10 цифр для ООО, 12 для ИП)")
    key = appsettings.get_setting(db, checko.SETTING_KEY, "")
    try:
        result = checko.fetch_card(key, inn)
    except checko.CheckoError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    return result["card"]


def _require_inn(comp: Company) -> str:
    inn = normalize_inn(comp.inn)
    if not inn:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "У компании не указан ИНН — заполните его и сохраните")
    return inn


@router.get("/{company_id}/card", summary="Карточка компании: статистика, сотрудники, ЕГРЮЛ (администратор)")
def company_card(company_id: str, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    stats = _stats(db, [comp.id]).get(comp.id, {})
    team = (db.query(User).filter(User.company_id == comp.id)
            .order_by(User.role, User.username).all())
    import json as _json
    try:
        card = _json.loads(comp.card_json or "{}")
    except ValueError:
        card = {}
    return {
        "company": comp.to_dict(),
        "receipts": stats.get("receipts", 0),
        "receipts_sum": stats.get("sum", 0.0),
        "last_activity": stats.get("last_activity"),
        "team": [u.to_dict() for u in team],
        "card": card,
        "card_updated_at": (comp.card_updated_at.isoformat()
                            if comp.card_updated_at else None),
        "has_checko_key": bool(appsettings.get_setting(db, checko.SETTING_KEY, "")),
        "risk": risk_assessment(card if isinstance(card, dict) else None,
                                comp.inn or ""),
        "other_companies": [{"id": c.id, "name": c.name, "is_active": c.is_active}
                            for c in db.query(Company).all() if c.id != comp.id],
    }


@router.post("/{company_id}/refresh-card", summary="Обновить карточку из ЕГРЮЛ (Checko.ru, администратор)")
def refresh_card(company_id: str, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    inn = _require_inn(comp)
    key = appsettings.get_setting(db, checko.SETTING_KEY, "")
    try:
        result = checko.fetch_card(key, inn)
    except checko.CheckoError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    import json as _json
    comp.card_json = _json.dumps(result["card"], ensure_ascii=False)
    from ..models import utcnow as _utcnow
    comp.card_updated_at = _utcnow()
    # v1.22.0: сокращённое наименование из карточки — для списков и селекторов
    if result["card"].get("name_short"):
        comp.short_name = result["card"]["name_short"][:200]
    # v1.15.0: автозаполнение реквизитов из ЕГРЮЛ — ИНН и полное название
    if not (comp.inn or "").strip() and result["card"].get("inn"):
        comp.inn = result["card"]["inn"]
    # если название в системе — сокращённое, а ЕГРЮЛ вернул полное, предложим его
    db.commit()
    log_action(admin, "company_card_refreshed", "company", comp.id,
               {"inn": inn, "name_full": result["card"]["name_full"]})
    return {"card": result["card"],
            "card_updated_at": comp.card_updated_at.isoformat(),
            "name_full": result["card"]["name_full"], "company": comp.to_dict()}


@router.post("/{company_id}/registry/download",
             summary="Скачать свежие данные ЕГРЮЛ/ЕГРИП (администратор)")
def registry_download(company_id: str,
                      admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Кнопка в карточке компании: свежие данные реестра одним файлом.
    Источники: 1) официальный сервис ФНС egrul.nalog.ru (PDF-выписка);
    2) Checko.ru API (обновляет карточку и формирует печатную выписку)."""
    from ..services import registry as reg
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    inn = (comp.inn or "").strip()
    if not inn:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "У компании не указан ИНН — добавьте его и повторите")

    # 1) официальный сервис ФНС — PDF-выписка
    pdf = reg.fetch_fns_pdf(inn)
    if pdf:
        log_action(admin, "registry_downloaded", "company", comp.id,
                   {"inn": inn, "source": "fns_egrul", "format": "pdf"})
        fname = f"ЕГРЮЛ_{inn}_{__import__('datetime').date.today():%Y%m%d}.pdf"
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 reg.content_disposition(fname)})

    # 2) Checko: свежая карточка → печатная HTML-выписка (и обновление в системе)
    key = appsettings.get_setting(db, checko.SETTING_KEY, "")
    card_saved: dict = {}
    try:
        result = checko.fetch_card(key, inn)
        card_saved = result["card"]
        import json as _json
        comp.card_json = _json.dumps(card_saved, ensure_ascii=False)
        from ..models import utcnow as _utcnow
        comp.card_updated_at = _utcnow()
        if card_saved.get("name_short"):
            comp.short_name = card_saved["name_short"][:200]   # v1.22.0
        db.commit()
    except checko.CheckoError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Сервисы реестра недоступны с сервера. {e} "
            "Проверьте интернет на сервере и ключ Checko в Настройках.")
    log_action(admin, "registry_downloaded", "company", comp.id,
               {"inn": inn, "source": "checko", "format": "html"})
    html = reg.build_excerpt_html(card_saved, comp.name, inn)
    fname = f"ЕГРЮЛ_{inn}_{__import__('datetime').date.today():%Y%m%d}.html"
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition":
                             reg.content_disposition(fname)})


@router.post("/{company_id}/delete", summary="Удалить компанию с обработкой её данных (администратор)")
def delete_company(company_id: str, body: CompanyDeleteBody,
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Удаление компании. Чеки НЕ теряются «молча»: mode="move" — переезд в
    другую компанию (сотрудники по выбору), mode="wipe" — безвозвратное
    удаление чеков компании; в обоих случаях приглашения компании удаляются,
    сотрудники либо переезжают, либо открепляются (аккаунты сохраняются)."""
    comp = db.get(Company, company_id)
    if not comp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компания не найдена")
    others = [c for c in db.query(Company).all() if c.id != comp.id]
    if not others:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Это единственная компания — сначала создайте другую")
    rc = db.query(func.count(Receipt.id)).filter(Receipt.company_id == comp.id).scalar() or 0
    uc = db.query(func.count(User.id)).filter(User.company_id == comp.id).scalar() or 0

    if body.mode == "move":
        target = db.get(Company, body.target_company_id or "")
        if not target or target.id == comp.id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Укажите компанию, в которую переносятся данные")
        if not target.is_active:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Компания назначения заархивирована")
        if rc:
            (db.query(Receipt).filter(Receipt.company_id == comp.id)
             .update({Receipt.company_id: target.id, Receipt.exported: False,
                      Receipt.exported_at: None}, synchronize_session=False))
        if body.move_users and uc:
            (db.query(User).filter(User.company_id == comp.id)
             .update({User.company_id: target.id}, synchronize_session=False))
        (db.query(Invite).filter(Invite.company_id == comp.id)
         .update({Invite.company_id: target.id}, synchronize_session=False))
        name = comp.name
        db.delete(comp)
        db.commit()
        log_action(admin, "company_deleted", details={
            "company": name, "mode": "move", "receipts_moved": rc,
            "users_moved": (uc if body.move_users else 0),
            "target": target.name})
        broadcast_company_change()
        return {"ok": True, "mode": "move", "receipts_moved": rc,
                "users_moved": (uc if body.move_users else 0),
                "target": {"id": target.id, "name": target.name},
                "message": f"Компания «{name}» удалена; чеков перенесено: {rc}"}
    # mode == "wipe": безвозвратно удалить чеки компании
    (db.query(Receipt).filter(Receipt.company_id == comp.id)
     .delete(synchronize_session=False))
    (db.query(Invite).filter(Invite.company_id == comp.id)
     .delete(synchronize_session=False))
    if body.move_users and uc:
        (db.query(User).filter(User.company_id == comp.id)
         .update({User.company_id: None}, synchronize_session=False))
    name = comp.name
    db.delete(comp)
    db.commit()
    log_action(admin, "company_deleted", details={
        "company": name, "mode": "wipe", "receipts_deleted": rc})
    broadcast_company_change()
    return {"ok": True, "mode": "wipe", "receipts_deleted": rc,
            "message": f"Компания «{name}» и её чеки ({rc}) удалены безвозвратно"}


def broadcast_company_change() -> None:
    from ..services.events import broadcast
    broadcast("companies_changed", {})


@router.post("/bulk-refresh", summary="Обновить карточки ЕГРЮЛ у компаний с ИНН (админ)")
def bulk_refresh_cards(body: dict, admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Партнёрский сценарий (v1.16.0): раз в месяц актуализируем реквизиты всех
    клиентов сразу. Лимит за раз — 40 (бесплатный тариф Checko: 100 запросов/день)."""
    import time as _time
    import json as _json
    from ..models import utcnow as _utcnow
    from ..services import checko as _checko
    limit = min(int((body or {}).get("limit") or 40), 40)
    key = appsettings.get_setting(db, checko.SETTING_KEY, "")
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Ключ Checko не задан — укажите его в настройках")
    comps = (db.query(Company)
             .filter(Company.is_active.is_(True), Company.inn.isnot(None),
                     Company.inn != "").limit(limit).all())
    ok_cnt, errors = 0, []
    for c in comps:
        try:
            result = _checko.fetch_card(key, c.inn.strip())
            c.card_json = _json.dumps(result["card"], ensure_ascii=False)
            if result["card"].get("name_short"):
                c.short_name = result["card"]["name_short"][:200]   # v1.22.0
            c.card_updated_at = _utcnow()
            ok_cnt += 1
        except _checko.CheckoError as e:
            errors.append(f"{c.name} (ИНН {c.inn}): {e}")
        _time.sleep(0.15)                                # бережём лимит и 32 r/s
    db.commit()
    log_action(admin, "company_cards_bulk_refreshed",
               details={"ok": ok_cnt, "errors": len(errors)})
    return {"ok": True, "updated": ok_cnt, "total_with_inn": len(comps),
            "errors": errors[:10],
            "message": f"Обновлено карточек: {ok_cnt} из {len(comps)}"
                       + (f"; ошибок: {len(errors)}" if errors else "")}
