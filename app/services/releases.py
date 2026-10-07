# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — «последние рабочие версии» (v1.46.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
#
# Реестр known-good: версия + коммит, на которых приложение УСПЕШНО
# РАБОТАЛО. Записи создаются в двух надёжных моментах:
#   1) успешный старт приложения (lifespan) — код точно запускается;
#   2) перед установкой обновления — текущая версия прямо сейчас
#      отвечает на запросы, значит рабочая.
# Хранятся ПОСЛЕДНИЕ 3 записи: к любой из них можно откатиться
# (из приложения — Настройки → «Последние рабочие версии», или из
# терминала — rollback.sh, когда приложение не стартует вовсе).
# Реестр — data/releases/known_good.json (data/ вне git).
# ======================================================================
from __future__ import annotations

import json
import os
from datetime import datetime

from ..config import settings

KEEP = 3                      # сколько рабочих версий храним
FILE = "known_good.json"


def releases_dir() -> str:
    from .updater import APP_DIR
    d = os.path.join(APP_DIR, "data", "releases")
    os.makedirs(d, exist_ok=True)
    return d


def _path() -> str:
    return os.path.join(releases_dir(), FILE)


def _read() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _write(items: list[dict]) -> None:
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())            # атомарно, без полуфайлов


def mark_healthy(version: str, commit: str, note: str = "") -> bool:
    """Запомнить рабочую версию. True — запись добавлена (новая)."""
    if not version or not commit or commit == "unknown":
        return False
    items = _read()
    for it in items:                    # уже в реестре — обновляем время
        if it.get("version") == version and it.get("commit") == commit:
            it["at"] = datetime.utcnow().isoformat() + "Z"
            if note:
                it["note"] = note[:120]
            _write(items)
            return False
    items.append({"version": version, "commit": commit,
                  "at": datetime.utcnow().isoformat() + "Z",
                  "note": note[:120]})
    items = items[-KEEP:]               # только последние KEEP рабочих версий
    _write(items)
    return True


def list_releases(limit: int = KEEP) -> list[dict]:
    """Последние рабочие версии (новые сверху) + пометка текущей."""
    items = list(reversed(_read()))[:limit]
    for it in items:
        it["current"] = (it.get("version") == settings.APP_VERSION)
        it["commit_short"] = (it.get("commit") or "")[:8]
    return items


def find_by_version(version: str) -> dict | None:
    for it in reversed(_read()):
        if it.get("version") == version:
            return it
    return None
