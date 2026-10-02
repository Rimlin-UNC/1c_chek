# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — расширенные действия администратора (v1.4.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
#   • Обновление системы из приложения: проверка / применение / статус /
#     история версий (CHANGELOG) — без терминала.
#   • Резервная копия базы одним кликом (скачивание файла).
#   • Управление пользователями: смена роли, сброс пароля (временный,
#     обязательная смена), архив/восстановление, передача чеков.
#   • Системная сводка: версия, Python, размер БД, счётчики, бэкапы.
# ======================================================================
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import ROLE_ACCOUNTANT, ROLE_USER, require_admin
from ..config import settings
from ..database import get_db
from ..models import AppSetting, Receipt, User
from ..schemas import Optional
from ..services import appsettings
from ..services.audit import log_action
from ..services.updater import (APP_DIR, check_update, job as update_job,
                                start_apply)

router = APIRouter(prefix="/api/v1/admin", tags=["Администрирование"])


# --------------------------------------------------------------------------
#  Обновление системы
# --------------------------------------------------------------------------
def _repo_branch(db: Session) -> tuple[str, str]:
    repo = appsettings.get_setting(db, "repo_url", "")
    branch = appsettings.get_setting(db, "repo_branch", "") or settings.DEFAULT_BRANCH
    if not repo:
        from ..services.updater import _guess_repo
        repo = _guess_repo()
    from ..services.updater import _normalize_repo
    repo = _normalize_repo(repo) or repo      # полный URL клона → owner/repo
    return repo, branch


@router.get("/update/check", summary="Проверить наличие обновлений (админ)")
def update_check(db: Session = Depends(get_db),
                 user: User = Depends(require_admin)):
    try:
        result = check_update(db)
    except Exception as e:                                   # noqa: BLE001
        # v1.6.0: отдаём 200 с ok:false — клиент всегда получает понятную причину
        log_action(user, "update_check_failed", details={"error": str(e)[:200]})
        return {"ok": False, "error": str(e)[:600]}
    log_action(user, "update_check", details={
        "available": result["update_available"], "remote": result["remote_version"]})
    return {"ok": True, **result}


@router.put("/update/repo", summary="Источник обновлений: репозиторий и ветка (админ)")
def update_repo(body: dict,
                db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    """v1.6.0: репозиторий (owner/repo) и ветка для проверки/установки обновлений."""
    from ..services import appsettings
    repo = (body.get("repo_url") or "").strip()
    branch = (body.get("repo_branch") or "").strip()
    if repo and not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo):
        raise HTTPException(422, "Репозиторий указывается как owner/repo, например Rimlin-UNC/1c_chek")
    if branch and (len(branch) > 120 or not re.fullmatch(r"[\w./\-]+", branch)):
        raise HTTPException(422, "Некорректное имя ветки")
    if repo:
        appsettings.set_setting(db, "repo_url", repo)
    if branch:
        appsettings.set_setting(db, "repo_branch", branch)
    cur_repo, cur_branch = _repo_branch(db)
    log_action(user, "update_repo_changed", details={"repo": cur_repo, "branch": cur_branch})
    return {"ok": True, "message": "Источник обновлений сохранён",
            "repo_url": cur_repo, "repo_branch": cur_branch}


@router.post("/update/apply", summary="Применить обновление (админ)")
def update_apply(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if update_job.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Обновление уже выполняется")
    try:
        info = check_update(db)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Нет связи с GitHub: {e}")
    if not info["update_available"]:
        return {"ok": True, "updated": False,
                "message": f"У вас уже последняя версия (v{settings.APP_VERSION})"}
    repo, branch = _repo_branch(db)
    start_apply(info["remote_version"], repo, branch)
    log_action(user, "update_apply", details={
        "target": info["remote_version"], "branch": branch})
    return {"ok": True, "updated": True,
            "message": f"Обновление до v{info['remote_version']} запущено",
            "target_version": info["remote_version"]}


@router.get("/update/status", summary="Статус/журнал обновления (админ)")
def update_status(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from ..services.updater import _history
    return {"job": update_job.public(), "history": _history(db)[:20]}


@router.get("/update/changelog", summary="История версий — что менялось (админ)")
def update_changelog(db: Session = Depends(get_db),
                     user: User = Depends(require_admin)):
    """Полный CHANGELOG с GitHub + локальный, для просмотра «что в ранних версиях»."""
    repo, branch = _repo_branch(db)
    from ..services.updater import _remote_changelog
    remote = _remote_changelog(repo, branch)
    local = ""
    path = os.path.join(APP_DIR, "CHANGELOG.md")
    if os.path.exists(path):
        local = open(path, encoding="utf-8").read()
    return {"changelog": remote or local, "source": "github" if remote else "local"}


# --------------------------------------------------------------------------
#  Резервная копия базы (скачивание) + системная сводка
# --------------------------------------------------------------------------
@router.get("/backup", summary="Скачать резервную копию базы (админ)")
def backup_db(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from ..services.updater import _backup_database
    path = _backup_database()
    if not path:
        raise HTTPException(404, "Файл базы не найден")
    with open(path, "rb") as f:
        content = f.read()
    log_action(user, "backup_downloaded", details={"file": os.path.basename(path)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=content,
        media_type="application/x-sqlite3",
        headers={"Content-Disposition":
                 f'attachment; filename="ymaster-backup-{stamp}.db"'})


@router.get("/backups", summary="Список резервных копий (админ)")
def backups_list(user: User = Depends(require_admin)):
    from ..services.backups import list_backups
    return {"items": list_backups()}


@router.post("/backups", summary="Создать резервную копию сейчас (админ)")
def backups_create(user: User = Depends(require_admin)):
    from ..services.backups import create_backup, list_backups
    path = create_backup("manual")
    if not path:
        raise HTTPException(500, "Не удалось создать копию")
    log_action(user, "backup_created", details={"file": os.path.basename(path)})
    return {"ok": True, "message": "Копия создана", "items": list_backups()}


@router.get("/backups/{name}/download", summary="Скачать копию по имени (админ)")
def backups_download(name: str, user: User = Depends(require_admin)):
    from ..services.backups import backup_path
    p = backup_path(name)
    if not p:
        raise HTTPException(404, "Копия не найдена")
    log_action(user, "backup_downloaded", details={"file": name})
    with open(p, "rb") as f:
        return Response(content=f.read(), media_type="application/x-sqlite3",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


class RepoPatch(BaseModel):
    repo_url: Optional[str] = Field(default=None, max_length=300)
    repo_branch: Optional[str] = Field(default=None, max_length=120)


@router.put("/update/repo", summary="Указать репозиторий/ветку обновлений (админ)")
def update_repo(body: RepoPatch, db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    if body.repo_url is not None:
        url = body.repo_url.strip()
        if url and not url.lower().startswith("https://"):
            raise HTTPException(400, "URL репозитория должен начинаться с https://")
        appsettings.set_setting(db, "repo_url", url)
    if body.repo_branch is not None:
        appsettings.set_setting(db, "repo_branch", body.repo_branch.strip())
    log_action(user, "update_repo_changed", details={
        "repo": body.repo_url, "branch": body.repo_branch})
    return {"ok": True, "message": "Источник обновлений сохранён"}


@router.get("/system", summary="Системная сводка (админ)")
def system_info(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    import sys
    from ..services.updater import _local_commit
    db_path = os.path.join(APP_DIR, "data", "ymaster_check.db")
    from ..services.backups import list_backups
    backups = [b["name"] for b in list_backups()[:8]]
    _repo, _branch = _repo_branch(db)
    return {
        "version": settings.APP_VERSION,
        "python": sys.version.split()[0],
        "commit": _local_commit(),
        "repo_url": _repo,
        "branch": _branch,
        "db_size_mb": round(os.path.getsize(db_path) / 1_048_576, 1)
                      if os.path.exists(db_path) else 0,
        "counts": {
            "users": db.query(User).count(),
            "receipts": db.query(Receipt).count(),
        },
        "backups": backups,
        "branch": appsettings.get_setting(db, "repo_branch", "") or settings.DEFAULT_BRANCH,
    }


# --------------------------------------------------------------------------
#  Пользователи: больше действий для администратора
# --------------------------------------------------------------------------
class RoleChange(BaseModel):
    role: str  # accountant | user


@router.patch("/users/{user_id}/role", summary="Сменить роль пользователя (админ)")
def change_role(user_id: str, body: RoleChange,
                db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if body.role not in (ROLE_ACCOUNTANT, ROLE_USER):
        raise HTTPException(422, "Роль: accountant или user")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target.role == "admin":
        raise HTTPException(400, "Роль администратора передаётся через «Сделать администратором»")
    if target.role == body.role:
        return {"ok": True, "message": "Роль уже такая"}
    old = target.role
    target.role = body.role
    db.commit()
    log_action(user, "user_role_changed", "user", target.id,
               {"from": old, "to": body.role})
    return {"ok": True, "message": f"Роль изменена: {old} → {body.role}"}


@router.post("/users/{user_id}/reset-password",
             summary="Сбросить пароль: выдать временный (админ)")
def reset_password(user_id: str, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target.role == "admin":
        raise HTTPException(400, "Свой пароль меняется в Настройках; пароль админа сбрасывать нельзя")
    temp = secrets.token_urlsafe(8)
    from ..auth import hash_password
    target.password_hash = hash_password(temp)
    target.must_change_password = True
    db.commit()
    log_action(user, "user_password_reset", "user", target.id, {})
    return {"ok": True, "temp_password": temp,
            "message": "Временный пароль выдан — передайте его сотруднику; "
                       "при входе система потребует сменить"}


class TransferBody(BaseModel):
    to_user_id: str


@router.post("/users/{user_id}/transfer-receipts",
             summary="Передать чеки сотрудника другому (админ)")
def transfer_receipts(user_id: str, body: TransferBody,
                      db: Session = Depends(get_db), user: User = Depends(require_admin)):
    src = db.get(User, user_id)
    dst = db.get(User, body.to_user_id)
    if not src or not dst:
        raise HTTPException(404, "Пользователь не найден")
    n = (db.query(Receipt)
         .filter(Receipt.created_by == src.id)
         .update({Receipt.created_by: dst.id}, synchronize_session=False))
    db.commit()
    log_action(user, "receipts_transferred", "user", dst.id,
               {"from": src.username, "count": n})
    return {"ok": True, "transferred": n,
            "message": f"Чеков передано: {n} ({src.username} → {dst.username})"}


@router.post("/users/{user_id}/unarchive",
             summary="Восстановить пользователя из архива (админ)")
def unarchive_user(user_id: str, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target.is_active:
        return {"ok": True, "message": "Пользователь активен"}
    # При архиве логин переименовывается в archived_<login>_<id6> — вернём
    import re as _re
    m = _re.match(r"^archived_(.+)_[0-9a-f]{6}$", target.username)
    if m:
        base = m.group(1)
        if db.query(User).filter(User.username == base).first():
            raise HTTPException(409, f"Логин «{base}» уже занят — освободите его и повторите")
        target.username = base
    target.is_active = True
    db.commit()
    log_action(user, "user_unarchived", "user", target.id, {})
    return {"ok": True, "message": f"{target.username} восстановлен"}
