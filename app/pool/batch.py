# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сдача чеков пачкой: статусы сессии (v1.70.40).
#
# Задача: донор сдаёт до 100 чеков подряд и видит по каждому честный
# результат. Приём асинхронный (ответ «чек принят — проверяем» уходит
# сразу, а статус verified/pending/rejected появляется в фоне), поэтому
# клиенту нужен один лёгкий запрос «что стало с моими чеками».
#
# Принципы:
#   1) чужого не показываем: выборка строго по своему участнику, поэтому
#      дубликат, сданный кем-то раньше, не превращается в «проверенный»;
#   2) один запрос на всю пачку (KEYS_MAX — защита от злоупотребления);
#   3) никакой записи в БД: только чтение, приём не ломаем.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

from sqlalchemy.orm import Session

# Рубеж сессии: после каждых 100 сданных чеков клиент спрашивает
# «продолжаем или закончим?» (задача владельца, v1.70.40). Совпадает с
# суточным антифрод-лимитом ingest.DAILY_LIMIT — оба числа видны донору.
SESSION_CAP = 100
# Защита: больше ключей за один запрос не принимаем (пачка + запас).
KEYS_MAX = 200
# Сколько раз клиент опрашивает статусы и с каким интервалом (мс):
# договорённость зафиксирована здесь, чтобы веб и мини-приложение MAX
# не расходились и не создавали лишней нагрузки.
POLL_INTERVAL_MS = 2500
POLL_MAX_TRIES = 40


def norm_key(fn: str, fd: str, fp: str) -> str:
    """Ключ чека — ФН|ФД|ФП (та же тройка, что в UNIQUE-индексе пула)."""
    return f"{(fn or '').strip()}|{(fd or '').strip()}|{(fp or '').strip()}"


def clean_keys(keys) -> list:
    """Нормализация списка ключей: только непустые тройки, без повторов.

    Терпимо к мусору (пустые строки, не-словари, лишние поля) — эндпоинт
    публичный, а падать на кривом вводе нельзя: 422 хуже честного пустого
    ответа. Длина значений ограничена как в модели PoolReceipt.
    """
    out, seen = [], set()
    for k in (keys or [])[:KEYS_MAX]:
        if not isinstance(k, dict):
            continue
        fn = str(k.get("fn") or "").strip()[:20]
        fd = str(k.get("fd") or "").strip()[:10]
        fp = str(k.get("fp") or "").strip()[:20]
        if not (fn and fd and fp):
            continue
        key = norm_key(fn, fd, fp)
        if key in seen:
            continue
        seen.add(key)
        out.append((fn, fd, fp))
    return out


def statuses(db: Session, user, keys) -> dict:
    """Статусы чеков пачки + прозрачность суточного лимита.

    Ответ: {"items": [{fn, fd, fp, status, message, sum, points, found}],
            "today", "limit", "left", "session_cap",
            "poll_interval_ms", "poll_max_tries"}

    status — как в пуле: "verified" (принят после проверки), "pending"
    (на модерации), "rejected" (не принят). Пустая строка и found=False —
    чека у этого участника нет (например, дубликат чужого чека).
    """
    from . import ingest
    from .models import PoolReceipt

    clean = clean_keys(keys)
    items = []
    if clean and user is not None:
        fns = sorted({fn for fn, _fd, _fp in clean})
        rows = (db.query(PoolReceipt)
                .filter(PoolReceipt.pool_user_id == user.id,
                        PoolReceipt.fn.in_(fns)).all())
        by_key = {norm_key(r.fn, r.fd, r.fp): r for r in rows}
        for fn, fd, fp in clean:
            r = by_key.get(norm_key(fn, fd, fp))
            if r is None:
                items.append({"fn": fn, "fd": fd, "fp": fp, "status": "",
                              "message": "", "sum": 0.0, "points": 0,
                              "found": False, "created_at": ""})
            else:
                items.append({
                    "fn": fn, "fd": fd, "fp": fp,
                    "status": r.status or "",
                    "message": (r.status_message or "")[:200],
                    "sum": float(r.total_sum or 0),
                    "points": int(r.points_awarded or 0),
                    "found": True,
                    "created_at": (r.created_at.strftime("%d.%m %H:%M")
                                   if r.created_at else ""),
                })
    return {"items": items,
            **ingest.limit_info(db, user),
            "session_cap": SESSION_CAP,
            "poll_interval_ms": POLL_INTERVAL_MS,
            "poll_max_tries": POLL_MAX_TRIES}


def count_by_status(items) -> dict:
    """Счётчики для панели сессии: принято / на модерации / не принято.

    Цвета у клиента: оранжевый (корпоративный) — verified, синий — pending,
    красный — rejected. Дубликаты клиент считает сам (они не доходят до БД
    этого участника), поэтому здесь их нет.
    """
    ok = sum(1 for x in items if x.get("status") == "verified")
    moderation = sum(1 for x in items if x.get("status") == "pending")
    bad = sum(1 for x in items if x.get("status") == "rejected")
    return {"ok": ok, "moderation": moderation, "bad": bad,
            "points": sum(int(x.get("points") or 0) for x in items)}
