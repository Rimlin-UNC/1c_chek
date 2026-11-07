# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — учёт пакетной проверки чеков в ФНС (v1.70.72)
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
#
# Зачем: при отправке нескольких чеков человек должен видеть не только
# «отправлено 12 чеков», но и итог — сколько проверено, сколько
# подтверждено, сколько не найдено или ждёт сброса лимита ФНС.
# Задание живёт в памяти процесса (это счётчик хода работы, а не данные
# предприятия): перезапуск сервера обнуляет историю, сами чеки в базе.
# ======================================================================
from __future__ import annotations

import threading
import time
import uuid

FINAL_STATES = ("done",)
_MAX_JOBS = 200                     # храним последние задания, старые — прочь


class _Job:
    __slots__ = ("id", "total", "done", "counters", "started_at",
                 "finished_at", "state", "user_id")

    def __init__(self, total: int, user_id: str = "") -> None:
        self.id = uuid.uuid4().hex[:12]
        self.total = total
        self.done = 0
        self.counters = {"valid": 0, "invalid": 0, "not_found": 0,
                         "unknown": 0, "quota": 0, "error": 0}
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.state = "running"
        self.user_id = user_id

    def snapshot(self) -> dict:
        left = self.total - self.done
        return {
            "job_id": self.id,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "left": max(0, left),
            "percent": int(round(self.done * 100 / self.total)) if self.total else 100,
            "counters": dict(self.counters),
            "elapsed_sec": int((self.finished_at or time.time())
                               - self.started_at),
            "finished": self.state in FINAL_STATES,
        }


_LOCK = threading.Lock()
_JOBS: dict[str, _Job] = {}
_ORDER: list[str] = []


def create(total: int, user_id: str = "") -> dict:
    """Создать задание проверки и вернуть его начальное состояние."""
    job = _Job(total, user_id)
    with _LOCK:
        _JOBS[job.id] = job
        _ORDER.append(job.id)
        while len(_ORDER) > _MAX_JOBS:
            _JOBS.pop(_ORDER.pop(0), None)
    return job.snapshot()


def note(job_id: str | None, outcome: str) -> None:
    """Отметить итог по одному чеку (valid/invalid/not_found/quota/…)."""
    if not job_id:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.state in FINAL_STATES:
            return
        job.done += 1
        key = outcome if outcome in job.counters else "error"
        job.counters[key] += 1
        if job.done >= job.total:
            job.state = "done"
            job.finished_at = time.time()


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return job.snapshot() if job else None


def latest(user_id: str = "") -> dict | None:
    """Последнее задание пользователя (для восстановления окна после F5)."""
    with _LOCK:
        for job_id in reversed(_ORDER):
            job = _JOBS.get(job_id)
            if job and (not user_id or job.user_id == user_id):
                return job.snapshot()
    return None


def summary_text(snap: dict) -> str:
    """Человеческий итог пакетной проверки — для уведомления."""
    c = snap["counters"]
    parts = []
    if c["valid"]:
        parts.append(f"подтверждено {c['valid']}")
    if c["invalid"]:
        parts.append(f"отклонено ФНС {c['invalid']}")
    if c["not_found"]:
        parts.append(f"не найдено {c['not_found']}")
    if c["quota"]:
        parts.append(f"ждут сброса лимита {c['quota']}")
    if c["unknown"]:
        parts.append(f"не проверено {c['unknown']}")
    if c["error"]:
        parts.append(f"ошибок {c['error']}")
    tail = "; ".join(parts) if parts else "результатов нет"
    return f"Проверено чеков: {snap['done']} из {snap['total']} — {tail}"
