# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — конвейер подключения компаний к платному API (v1.70.45).
#
# Решение владельца (automation_default=partial_default): автоматика
# выполняет проверки, а ключ выдаёт администратор в один клик. Режим
# переключается администратором: полная автоматизация / частичная /
# отмена (автоматика выключена, всё вручную).
#
#   full    — проверки → статус подтверждён → заявка одобрена → ключ выдан
#   partial — проверки → статус подтверждён; заявка ждёт администратора
#   off     — автоматика не решает ничего; проверки запускаются вручную
#
# Ключи выдаёт существующий механизм app/pool/router_api.generate_key:
# второй реестр ключей не заводится, лимиты и счётчики работают как прежде.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..config import settings
from ..pool.router_api import DEFAULT_QUOTA, DEFAULT_RATE, generate_key
from ..services.appsettings import get_setting, set_setting
from . import letters, verify
from .models import (
    ACTOR_ADMIN,
    ACTOR_COMPANY,
    ACTOR_SYSTEM,
    AP_APPROVED,
    AP_NEW,
    AP_PARKED,
    AP_REJECTED,
    AP_REVOKED,
    ST_BLOCKED,
    ST_CHECKS,
    ST_REJECTED,
    ST_VERIFIED,
    TARIFF_ANALYTICS,
    TARIFF_BUSINESS,
    TARIFF_START,
    TARIFF_TITLES,
    TARIFFS,
    B2bApplication,
    B2bCompany,
    _utcnow,
    log_event,
)

log = logging.getLogger("ymaster.b2b.pipeline")

# Порог очереди: сколько заявок одной компании может ждать решения
# администратора одновременно. Решение принимает человек, поэтому новая заявка
# имеет смысл только после разбора прежних; лимит защищает очередь от забивания.
PENDING_MAX = 3

# --------------------------------------------------------------------------
# Режимы автоматизации
# --------------------------------------------------------------------------
MODE_FULL = "full"
MODE_PARTIAL = "partial"
MODE_OFF = "off"
MODES = (MODE_FULL, MODE_PARTIAL, MODE_OFF)

MODE_TITLES = {
    MODE_FULL: "Полная автоматизация",
    MODE_PARTIAL: "Частичная автоматизация",
    MODE_OFF: "Без автоматизации (всё вручную)",
}

MODE_NOTES = {
    MODE_FULL: "Проверки, подтверждение статуса, одобрение заявки и выдачу "
               "ключа выполняет сервис. Администратор видит журнал и может "
               "отозвать ключ.",
    MODE_PARTIAL: "Проверки и подтверждение статуса выполняет сервис; заявку "
                  "одобряет и ключ выдаёт администратор в один клик.",
    MODE_OFF: "Автоматика выключена: статус и заявки меняет только "
              "администратор, проверки запускаются кнопкой «Перепроверить».",
}

# --------------------------------------------------------------------------
# Настройки контура (ключ → значение по умолчанию)
# --------------------------------------------------------------------------
SET_ENABLED = "b2b_enabled"
SET_MODE = "b2b_auto_mode"
SET_AUTO_KEY = "b2b_auto_issue_key"
SET_REQUIRE_REGISTRY = "b2b_require_registry"
SET_ALLOW_FREE = "b2b_allow_free_domains"
SET_NOTIFY = "b2b_notify_admin"
SET_NOTIFY_EMAIL = "b2b_notify_email"

DEFAULTS = {
    SET_ENABLED: "0",          # контур по умолчанию закрыт (владелец: «скрыть»)
    SET_MODE: MODE_PARTIAL,    # решение владельца: частичная автоматизация
    SET_AUTO_KEY: "1",         # в режиме full ключ выдаётся автоматически
    SET_REQUIRE_REGISTRY: "1",
    SET_ALLOW_FREE: "0",
    SET_NOTIFY: "1",
    SET_NOTIFY_EMAIL: "",      # пусто → адрес из config.VENDOR_EMAIL
}

FLAGS = (SET_ENABLED, SET_AUTO_KEY, SET_REQUIRE_REGISTRY, SET_ALLOW_FREE,
         SET_NOTIFY)


def _flag(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "on", "yes", "да")


def get_config(db: Session) -> dict:
    """Текущие настройки контура (с значениями по умолчанию)."""
    cfg = {key: get_setting(db, key, default) for key, default in DEFAULTS.items()}
    if cfg[SET_MODE] not in MODES:
        cfg[SET_MODE] = MODE_PARTIAL
    out = {
        "enabled": _flag(cfg[SET_ENABLED]),
        "auto_mode": cfg[SET_MODE],
        "auto_mode_title": MODE_TITLES[cfg[SET_MODE]],
        "auto_mode_note": MODE_NOTES[cfg[SET_MODE]],
        "auto_issue_key": _flag(cfg[SET_AUTO_KEY]),
        "require_registry": _flag(cfg[SET_REQUIRE_REGISTRY]),
        "allow_free_domains": _flag(cfg[SET_ALLOW_FREE]),
        "notify_admin": _flag(cfg[SET_NOTIFY]),
        "notify_email": (cfg[SET_NOTIFY_EMAIL] or settings.VENDOR_EMAIL).strip(),
    }
    return out


def set_config(db: Session, payload: dict) -> dict:
    """Запись настроек администратором. Возвращает (настройки, ошибка)."""
    payload = payload or {}
    mode = (payload.get("auto_mode") or "").strip().lower()
    if mode and mode not in MODES:
        return {}, "Режим автоматизации — full, partial или off"
    if mode:
        set_setting(db, SET_MODE, mode)
    for key in FLAGS:
        short = key[4:]                      # b2b_enabled → enabled
        if short in payload:
            set_setting(db, key, "1" if payload[short] else "0")
    if "notify_email" in payload:
        email = (payload.get("notify_email") or "").strip().lower()[:254]
        set_setting(db, SET_NOTIFY_EMAIL, email)
    db.commit()
    return get_config(db), ""


# --------------------------------------------------------------------------
# Тарифы: названия — из models (единый источник), цифры — из кода сервиса
# --------------------------------------------------------------------------
TARIFF_LIMITS = {
    TARIFF_START: (DEFAULT_RATE, DEFAULT_QUOTA),      # 60 / 5 000
    TARIFF_BUSINESS: (300, 30_000),
    TARIFF_ANALYTICS: (1_200, 150_000),
}


def tariff_limits(tariff: str) -> tuple[int, int]:
    return TARIFF_LIMITS.get((tariff or TARIFF_START).strip().lower(),
                             TARIFF_LIMITS[TARIFF_START])


# --------------------------------------------------------------------------
# Шаг 1. Почта подтверждена → проверки и статус компании
# --------------------------------------------------------------------------
def after_email_confirmed(db: Session, company: B2bCompany,
                          cfg: dict | None = None) -> dict:
    """Запускает проверки после подтверждения корпоративной почты.

    full/partial: статус подтверждается автоматически при зелёных проверках.
    off: проверки не запускаются — компания ждёт решения администратора.
    """
    cfg = cfg or get_config(db)
    mode = cfg["auto_mode"]
    if mode == MODE_OFF:
        company.status = ST_CHECKS
        log_event(db, company.id, ACTOR_SYSTEM, "checks_skipped",
                  "Режим «без автоматизации»: проверки не запускались, "
                  "решение принимает администратор")
        db.commit()
        return {"mode": mode, "status": company.status, "checks": [],
                "verified": False,
                "message": "Почта подтверждена. Дальше решение принимает "
                           "администратор сервиса"}

    verify.run_checks(
        db, company,
        allow_free_domains=cfg["allow_free_domains"],
        require_registry=cfg["require_registry"])
    checks = verify.latest_checks(db, company.id)
    ok, problems = verify.verdict(checks)
    if ok:
        company.status = ST_VERIFIED
        company.verified_at = _utcnow()
        company.reject_reason = ""
        log_event(db, company.id, ACTOR_SYSTEM, "company_verified",
                  f"Проверки пройдены автоматически (режим «{MODE_TITLES[mode]}»)")
        db.commit()
        letters.send_status(db, company, approved=True)
        return {"mode": mode, "status": company.status, "checks": checks,
                "verified": True,
                "message": "Статус компании подтверждён — раздел открыт"}

    company.status = ST_REJECTED
    company.rejected_at = _utcnow()
    company.reject_reason = "; ".join(problems)[:300]
    log_event(db, company.id, ACTOR_SYSTEM, "company_rejected",
              company.reject_reason)
    db.commit()
    letters.send_status(db, company, approved=False)
    notify_admin(db, company, cfg, f"Отказ компании {company.company_name}")
    return {"mode": mode, "status": company.status, "checks": checks,
            "verified": False, "message": company.reject_reason}


def recheck(db: Session, company: B2bCompany, actor: str = ACTOR_ADMIN,
            actor_id: str = "") -> dict:
    """Ручной запуск проверок (нужен в режиме «отмена» и после исправлений)."""
    cfg = get_config(db)
    rows, _card = verify.run_checks(
        db, company,
        allow_free_domains=cfg["allow_free_domains"],
        require_registry=cfg["require_registry"])
    checks = verify.latest_checks(db, company.id)
    ok, problems = verify.verdict(checks)
    if company.status not in (ST_BLOCKED,):
        company.status = ST_VERIFIED if ok else ST_CHECKS
        if ok:
            company.verified_at = _utcnow()
            company.reject_reason = ""
        else:
            company.reject_reason = "; ".join(problems)[:300]
    log_event(db, company.id, actor, "checks_run",
              f"Проверок: {len(rows)}, итог: "
              f"{'пройдено' if ok else 'есть замечания'}", actor_id=actor_id)
    db.commit()
    return {"checks": checks, "passed": ok, "problems": problems,
            "status": company.status}


# --------------------------------------------------------------------------
# Шаг 2. Заявка на подключение
# --------------------------------------------------------------------------
def submit_application(db: Session, company: B2bCompany, tariff: str,
                       task: str, expected_volume: str = "") -> dict:
    """Компания подаёт заявку («Обсудить подключение»).

    full: одобрение и выдача ключа автоматически.
    partial/off: заявка встаёт в очередь к администратору.
    """
    cfg = get_config(db)
    mode = cfg["auto_mode"]
    tariff = (tariff or TARIFF_START).strip().lower()
    if tariff not in TARIFFS:
        return {"ok": False, "error": "Тариф — Старт, Бизнес или Аналитика"}
    if len((task or "").strip()) < 20:
        return {"ok": False,
                "error": "Опишите задачу подробнее — не короче 20 символов"}
    # Одна компания не должна забивать очередь модерации: решение по заявке
    # принимает человек, поэтому новая заявка имеет смысл только тогда, когда
    # прежние разобраны. Лимит мягкий — это не антифрод, а защита очереди.
    pending = (db.query(B2bApplication)
               .filter(B2bApplication.company_id == company.id,
                       B2bApplication.status.in_((AP_NEW, AP_PARKED)))
               .count())
    if pending >= PENDING_MAX:
        return {"ok": False,
                "error": f"У вас уже {pending} заявки в очереди — дождитесь "
                         "решения администратора или напишите на "
                         f"{letters.SUPPORT}"}

    rec = B2bApplication(company_id=company.id, tariff=tariff,
                         task=(task or "").strip()[:4000],
                         expected_volume=(expected_volume or "").strip()[:60],
                         status=AP_NEW, auto_mode=mode)
    db.add(rec)
    db.flush()
    log_event(db, company.id, ACTOR_COMPANY, "application_submitted",
              f"Тариф «{TARIFF_TITLES[tariff]}», заявка {rec.id}")

    if mode == MODE_FULL:
        company.task = rec.task
        result = _issue_key(db, rec, company, cfg, decided_by="",
                            actor=ACTOR_SYSTEM)
        db.commit()
        if rec.status == AP_APPROVED:
            letters.send_approved(db, company, rec)
            notify_admin(db, company, cfg,
                         f"Заявка одобрена автоматически: {company.company_name}")
            return {"ok": True, "application": rec.to_dict(),
                    "key": result["key"], "status": rec.status,
                    "message": "Заявка одобрена автоматически — ключ в кабинете"}
        # автовыдача ключа выключена настройкой — заявка ждёт администратора
        notify_admin(db, company, cfg, f"Новая заявка: {company.company_name}")
        return {"ok": True, "application": rec.to_dict(), "key": "",
                "status": rec.status,
                "message": "Заявка принята и ждёт решения администратора"}

    rec.status = AP_PARKED
    rec.auto_decision = ("Проверки выполнены автоматикой; одобрение и выдача "
                         "ключа — за администратором (режим «"
                         f"{MODE_TITLES[mode]}»)")
    company.task = rec.task
    db.commit()
    notify_admin(db, company, cfg, f"Новая заявка: {company.company_name}")
    return {"ok": True, "application": rec.to_dict(), "key": "",
            "status": rec.status,
            "message": "Заявка принята. Администратор сервиса проверит её и "
                       "сообщит о решении на корпоративную почту"}


def _issue_key(db: Session, rec: B2bApplication, company: B2bCompany,
               cfg: dict, decided_by: str = "", actor: str = ACTOR_ADMIN,
               rate: int | None = None, quota: int | None = None) -> dict:
    """Выдаёт ключ по тарифу заявки. Возвращает {key, prefix, rate, quota}.

    Автоматическая выдача (actor=system) возможна только при включённом
    переключателе b2b_auto_issue_key: иначе заявка остаётся в очереди, а ключ
    НЕ создаётся вовсе (записей-пустышек в реестре ключей не бывает).
    """
    default_rate, default_quota = tariff_limits(rec.tariff)
    rate = max(1, int(rate or default_rate))
    quota = max(1, int(quota or default_quota))
    if actor == ACTOR_SYSTEM and not cfg["auto_issue_key"]:
        rec.status = AP_PARKED
        rec.auto_decision = ("Проверки пройдены; выдача ключа автоматикой "
                             "выключена — ждёт администратора")
        log_event(db, company.id, ACTOR_SYSTEM, "key_not_issued",
                  "Автовыдача ключа выключена настройкой b2b_auto_issue_key")
        return {"key": "", "prefix": "", "rate": rate, "quota": quota}
    name = (f"{company.company_name or company.email}"
            f" (ИНН {company.inn or '—'})")[:200]
    key_rec, raw = generate_key(db, name, created_by=decided_by or "",
                                rate_per_hour=rate, monthly_quota=quota)
    rec.key_id = key_rec.id
    rec.key_prefix = key_rec.prefix
    rec.status = AP_APPROVED
    rec.decided_by = decided_by or ""
    rec.decided_at = _utcnow()
    rec.auto_decision = (f"Ключ {key_rec.prefix} выдан: {rate} запросов/час, "
                         f"{quota} в месяц")
    log_event(db, company.id, actor, "key_issued",
              f"Тариф «{TARIFF_TITLES.get(rec.tariff, rec.tariff)}», "
              f"{rate}/час, {quota}/мес, ключ {key_rec.prefix}",
              actor_id=decided_by or "")
    return {"key": raw, "prefix": key_rec.prefix, "rate": rate, "quota": quota}


def approve_application(db: Session, rec: B2bApplication, admin,
                        rate: int | None = None, quota: int | None = None) -> dict:
    """Администратор одобряет заявку в один клик (с выдачей ключа)."""
    company = db.get(B2bCompany, rec.company_id)
    if company is None:
        return {"ok": False, "error": "Компания не найдена"}
    cfg = get_config(db)
    result = _issue_key(db, rec, company, cfg, decided_by=admin.id,
                        actor=ACTOR_ADMIN, rate=rate, quota=quota)
    if company.status != ST_VERIFIED and company.status != ST_BLOCKED:
        company.status = ST_VERIFIED
        company.verified_at = company.verified_at or _utcnow()
    log_event(db, company.id, ACTOR_ADMIN, "application_approved",
              f"Заявка {rec.id} одобрена администратором", actor_id=admin.id)
    db.commit()
    _audit(admin, "b2b_application_approved",
           {"company": company.email, "tariff": rec.tariff,
            "prefix": result.get("prefix", "")})
    letters.send_approved(db, company, rec)
    return {"ok": True, "key": result["key"], "prefix": result.get("prefix", ""),
            "application": rec.to_dict(),
            "message": "Заявка одобрена, ключ выдан. Он показывается один раз "
                       "— скопируйте сейчас: в базе хранится только хэш, а "
                       "компания видит ключ в своём кабинете"}


def reject_application(db: Session, rec: B2bApplication, admin,
                       reason: str) -> dict:
    """Отклонение заявки с причиной (причина уходит в письме)."""
    reason = (reason or "").strip()[:300]
    if not reason:
        return {"ok": False, "error": "Укажите причину отказа"}
    company = db.get(B2bCompany, rec.company_id)
    rec.status = AP_REJECTED
    rec.reject_reason = reason
    rec.decided_by = admin.id
    rec.decided_at = _utcnow()
    log_event(db, rec.company_id, ACTOR_ADMIN, "application_rejected",
              reason, actor_id=admin.id)
    db.commit()
    _audit(admin, "b2b_application_rejected",
           {"application": rec.id, "reason": reason})
    if company is not None:
        letters.send_application_rejected(db, company, rec)
    return {"ok": True, "application": rec.to_dict(),
            "message": "Заявка отклонена, компания уведомлена"}


def revoke_key(db: Session, rec: B2bApplication, admin) -> dict:
    """Отзыв выданного ключа (доступ к API прекращается сразу)."""
    from ..pool.models import PoolApiKey
    if not rec.key_id:
        return {"ok": False, "error": "Ключ по этой заявке не выдавался"}
    key = db.get(PoolApiKey, rec.key_id)
    if key is None:
        return {"ok": False, "error": "Ключ не найден"}
    key.active = False
    rec.status = AP_REVOKED
    log_event(db, rec.company_id, ACTOR_ADMIN, "key_revoked",
              f"Ключ {key.prefix} отозван", actor_id=admin.id)
    db.commit()
    _audit(admin, "b2b_key_revoked", {"prefix": key.prefix,
                                      "application": rec.id})
    return {"ok": True, "message": f"Ключ {key.prefix} отозван"}


# --------------------------------------------------------------------------
# Решения администратора по статусу компании
# --------------------------------------------------------------------------
def verify_manually(db: Session, company: B2bCompany, admin,
                    note: str = "") -> dict:
    """Ручное подтверждение статуса компании (раздел открывается)."""
    company.status = ST_VERIFIED
    company.verified_at = _utcnow()
    company.reject_reason = ""
    if note:
        company.notes = (company.notes + "\n" if company.notes else "") \
            + note.strip()[:500]
    log_event(db, company.id, ACTOR_ADMIN, "company_verified",
              note.strip()[:300] or "Подтверждено администратором вручную",
              actor_id=admin.id)
    db.commit()
    _audit(admin, "b2b_company_verified", {"company": company.email,
                                           "inn": company.inn})
    letters.send_status(db, company, approved=True)
    return {"ok": True, "status": company.status,
            "message": "Статус компании подтверждён — раздел ей открыт"}


def reject_manually(db: Session, company: B2bCompany, admin,
                    reason: str) -> dict:
    reason = (reason or "").strip()[:300]
    if not reason:
        return {"ok": False, "error": "Укажите причину отказа"}
    company.status = ST_REJECTED
    company.rejected_at = _utcnow()
    company.reject_reason = reason
    log_event(db, company.id, ACTOR_ADMIN, "company_rejected", reason,
              actor_id=admin.id)
    db.commit()
    _audit(admin, "b2b_company_rejected", {"company": company.email,
                                           "reason": reason})
    letters.send_status(db, company, approved=False)
    return {"ok": True, "status": company.status,
            "message": "Компании отказано, письмо с причиной отправлено"}


def set_blocked(db: Session, company: B2bCompany, admin,
                blocked: bool, reason: str = "") -> dict:
    """Блокировка и разблокировка кабинета компании."""
    if blocked:
        company.status = ST_BLOCKED
        company.reject_reason = (reason or "").strip()[:300]
        # сессии гасятся сразу: токен заблокированной компании не должен
        # ожить сам, если администратор позже восстановит доступ
        company.token_epoch = int(company.token_epoch or 0) + 1
        action = "company_blocked"
    else:
        company.status = ST_VERIFIED if company.verified_at else ST_CHECKS
        action = "company_unblocked"
    log_event(db, company.id, ACTOR_ADMIN, action,
              (reason or "").strip()[:300], actor_id=admin.id)
    db.commit()
    _audit(admin, f"b2b_{action}", {"company": company.email})
    return {"ok": True, "status": company.status,
            "message": "Доступ закрыт" if blocked else "Доступ восстановлен"}


def notify_admin(db: Session, company: B2bCompany, cfg: dict,
                 subject: str) -> bool:
    """Уведомление администратора о событии в контуре (если включено)."""
    if not cfg["notify_admin"]:
        return False
    target = cfg["notify_email"] or settings.VENDOR_EMAIL
    if not target:
        return False
    return letters.send_admin_notice(db, target, company, subject)


def _audit(admin, action: str, details: dict) -> None:
    """Запись в общий журнал аудита ядра (действия администратора)."""
    try:
        from ..services.audit import log_action
        log_action(admin, action, details=details)
    except Exception as exc:  # noqa: BLE001 — аудит не должен ломать операцию
        log.warning("b2b audit failed (%s): %s", action, exc)


def queue_size(db: Session) -> int:
    """Сколько заявок ждёт решения администратора."""
    return (db.query(B2bApplication)
            .filter(B2bApplication.status.in_((AP_NEW, AP_PARKED))).count())


def counters(db: Session) -> dict:
    """Сводка по статусам компаний — для администратора."""
    from .models import COMPANY_STATUSES
    out = {status: 0 for status in COMPANY_STATUSES}
    for (status,) in db.query(B2bCompany.status).all():
        if status in out:
            out[status] += 1
    out["total"] = sum(out[s] for s in COMPANY_STATUSES)
    out["queue"] = queue_size(db)
    return out
