"""Приёмная папка первички: сервер сам забирает УПД, выгруженные из СБИС (v1.73.0, T-109, ADR-60).

Зачем это, а не прежняя «📁 Папка с УПД» (ADR-43). Прежний путь работает, пока человек сидит в браузере:
папку выбирают заново на каждой загрузке, браузер обходит её сам и гоняет найденные файлы по сети.
Владелец (issue #109) описал беду прямо: «это очень плохо работает». Здесь каталог задаётся один раз,
и дальше сервер забирает из него всё новое сам — без браузера, без повторного выбора и без копирования
файлов через интернет на машину бухгалтера.

Почему опрос, а не события файловой системы. Приёмная папка почти всегда лежит на сетевом диске
(SMB/CIFS, NFS) или в синхронизируемой папке. На таких файловых системах inotify не получает событий от
удалённой стороны: файл появляется, а программа «не видит» его до перезапуска — ровно та грабля, из-за
которой в paperless-ngx пришлось вводить PAPERLESS_CONSUMER_POLLING
(https://stackoverflow.com/questions/8124617/getting-file-create-notifications-for-cifs-mount-in-linux).
Поэтому обход — обычный опрос по таймеру, а «файл ещё пишется» проверяется двумя замерами размера и
времени изменения подряд: недописанный XML не должен попасть в разбор.

Что происходит с файлом. Разбор — тот же, что у «Папки с УПД» (service.accept_routed): компания по ИНН,
покупатель — расход, продавец — продажа, документ не своих компаний отклоняется. Принятый и уже
загруженный файл переносится в «processed/ГГГГ-ММ», отказ — в «quarantine» с рядом файлом причины;
исходный файл не удаляется никогда. Пути проверяются по белому списку корней
(YM_PRIMARY_INBOX_ROOTS / настройка администратора): это защита от чтения чужих каталогов сервера.

Границы. Фоновый обход включается явно администратором. Пока он выключен, работает только кнопка
«Забрать сейчас». Переменная YM_PRIMARY_INBOX_OFF=1 гасит тик целиком (тесты и стенды).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import ROLE_ACCOUNTANT
from ..config import BASE_DIR
from ..models import Company, User
from ..services import appsettings
from . import service
from .models import PrimaryDocument, PrimaryInboxFile, PrimaryInboxSetting

# ------------------------------------------------------------------ пределы и умолчания
MAX_BATCH = 200                      # файлов за один обход: сервер на 2 ГБ не должен «зависнуть» на папке
MAX_RUN_BYTES = 50 * 1024 * 1024     # прочитанных байт за обход (как MAX_UNPACKED у архива)
MAX_DEPTH = 8                        # подпапки глубже не обходим
MAX_PATH = 400
MIN_INTERVAL = 1                     # минут между обходами
MAX_INTERVAL = 720
DEFAULT_INTERVAL = 10
DEFAULT_SUBDIR = "data/inbox"
KEEP_LOG_DAYS = 365                  # журнал приёмной папки хранится год, как журнал почты

SETTING_PATH = "primary_inbox_path"
SETTING_ENABLED = "primary_inbox_enabled"
SETTING_INTERVAL = "primary_inbox_interval"

# Каталоги, которые обход не заходит: наши же архивы и служебные файлы Windows/macOS.
SKIP_DIRS = frozenset({"processed", "quarantine", "$RECYCLE.BIN", "__MACOSX", ".git", ".Trash", ".Trashes",
                       "System Volume Information", "node_modules", ".venv"})
SEND_EXT = frozenset({".xml", ".zip"})
# Имена файлов, которые пишутся «на полпути»: не читаем, даже если они похожи на XML.
TEMP_EXT = frozenset({".part", ".tmp", ".crdownload", ".filepart", ".partial", ".download", ".!qb"})

STATUSES = {"ok": "принят", "duplicate": "уже был", "rejected": "не принят"}
SOURCES = {"inbox": "приёмная папка", "console": "кнопка на сервере"}

_lock = threading.Lock()             # один обход за раз: тик и кнопка не должны обрабатывать один файл дважды
_purged = {"day": ""}


class InboxError(ValueError):
    """Понятное сообщение администратору; status — код ответа API."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ путь: белый список корней
def allowed_roots() -> list[Path]:
    """Корни, внутри которых вообще может лежать приёмная папка.

    Переменная YM_PRIMARY_INBOX_ROOTS (через «;») — решение администратора сервера; без неё доступен
    только каталог данных программы. Путь вне корней не принимается: иначе поле настроек превращается
    в чтение любого каталога сервера от имени службы.
    """
    raw = (os.environ.get("YM_PRIMARY_INBOX_ROOTS") or "").strip()
    items = [p for p in raw.replace("\n", ";").split(";") if p.strip()]
    roots: list[Path] = []
    for item in items:
        path = _absolute(item.strip())
        if path is not None and path.is_dir():
            roots.append(path)
    default = _absolute(DEFAULT_SUBDIR)
    if default is not None:
        roots.append(default)
    return roots


def _absolute(value: str | None) -> Path | None:
    """Строка → абсолютный путь без «..» и без NUL; иначе None."""
    text = (value or "").strip()
    if not text or len(text) > MAX_PATH or "\x00" in text:
        return None
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = Path(BASE_DIR) / path
    # resolve(strict=False) убирает «..» и раскрывает симлинки; результат не должен уехать наружу.
    return Path(os.path.abspath(path))


def normalize_path(value: str | None) -> str:
    """Путь для хранения в настройке: абсолютный, без «..». Проверку корней делает check_path."""
    path = _absolute(value)
    if path is None:
        raise InboxError("Путь пустой или содержит недопустимые символы")
    return str(path)


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def check_path(_db: Session | None, value: str | None) -> Path:
    """Проверить путь приёмной папки: существует, каталог, внутри разрешённого корня. → Path.

    Аргумент db оставлен ради прежних вызовов: настройки путей в базе не участвуют.
    """
    text = normalize_path(value)
    roots = allowed_roots()
    if not roots:
        raise InboxError("Не задан ни один разрешённый корень: укажите YM_PRIMARY_INBOX_ROOTS", 409)
    target = Path(text)
    if not any(_inside(root, target) for root in roots):
        raise InboxError("Путь вне разрешённых каталогов: разрешено " + ", ".join(str(r) for r in roots), 403)
    if not target.is_dir():
        raise InboxError(f"Каталог не найден: {text}", 404)
    return target


def writable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


# ------------------------------------------------------------------ настройки (одна строка)
def row(db: Session) -> PrimaryInboxSetting:
    found = db.execute(select(PrimaryInboxSetting).limit(1)).scalars().first()
    if found is None:
        found = PrimaryInboxSetting(path="", enabled=False, interval_minutes=DEFAULT_INTERVAL)
        db.add(found)
        db.commit()
    return found


def _int_setting(db: Session, key: str, default: int) -> int:
    try:
        return int(appsettings.get_setting(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def state(db: Session) -> dict:
    """Состояние приёмной папки для экрана «Первичка» и настроек."""
    item = row(db)
    enabled = appsettings.get_setting(db, SETTING_ENABLED, "0") == "1"
    interval = min(MAX_INTERVAL, max(MIN_INTERVAL, _int_setting(db, SETTING_INTERVAL, DEFAULT_INTERVAL)))
    roots = [str(r) for r in allowed_roots()]
    path = item.path or os.environ.get("YM_PRIMARY_INBOX_PATH", "") or str(_absolute(DEFAULT_SUBDIR) or "")
    problems: list[str] = []
    if not path:
        problems.append("Путь не задан")
    else:
        try:
            check_path(db, path)
        except InboxError as exc:
            problems.append(str(exc))
    if path and Path(path).is_dir() and not writable(Path(path)):
        problems.append("Нет прав на запись: принятые файлы нельзя перенести в processed")
    if os.environ.get("YM_PRIMARY_INBOX_OFF") == "1":
        problems.append("Фоновый обход выключен на сервере (YM_PRIMARY_INBOX_OFF=1)")
    return {
        "path": path, "enabled": enabled, "interval_minutes": interval, "roots": roots,
        "status": item.status, "last_error": item.last_error,
        "last_run_at": _iso(item.last_run_at), "last_ok_at": _iso(item.last_ok_at),
        "last_result": _loads(item.last_result_json), "problems": problems,
        "off": os.environ.get("YM_PRIMARY_INBOX_OFF") == "1",
        "counters": counters(db),
    }


def save(db: Session, *, path: str | None, enabled: bool, interval: int, user: User | None = None) -> dict:
    """Сохранить настройку после проверки пути. Ошибка — ничего не меняем."""
    target = check_path(db, path)
    if not writable(target):
        raise InboxError("Нет прав на запись в каталог: программа должна переносить принятые файлы")
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise InboxError(f"Интервал — от {MIN_INTERVAL} до {MAX_INTERVAL} минут")
    item = row(db)
    item.path = str(target)
    item.status = "active" if enabled else "new"
    item.last_error = ""
    item.updated_by = user.id if user else ""
    item.updated_at=_now()
    db.commit()
    appsettings.set_setting(db, SETTING_PATH, str(target))
    appsettings.set_setting(db, SETTING_ENABLED, "1" if enabled else "0")
    appsettings.set_setting(db, SETTING_INTERVAL, str(interval))
    return state(db)


def counters(db: Session) -> dict:
    """Итоги журнала: сколько принято, уже было, отклонено и сколько ждёт разбора в папке."""
    out = {"total": 0, "ok": 0, "duplicate": 0, "rejected": 0}
    for status, count in db.execute(select(PrimaryInboxFile.status, func.count()).group_by(PrimaryInboxFile.status)):
        out[status] = count
        out["total"] += count
    return out


# ------------------------------------------------------------------ даты и сериализация
def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    """Момент в UTC с «Z»: браузер покажет его в местной зоне (models.iso_z)."""
    return value.isoformat() + "Z" if value else None


def _loads(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------ обход папки
@dataclass
class Candidate:
    """Файл, готовый к разбору: путь, размер, время изменения (для журнала)."""
    path: Path
    rel: str
    size: int
    mtime: float
    data: bytes


def _skip_name(name: str) -> bool:
    lower = name.lower()
    if name.startswith(".") or name.startswith("~$") or name.startswith("._"):
        return True
    return any(lower.endswith(ext) for ext in TEMP_EXT)


def walk(root: Path) -> tuple[list[Path], list[str]]:
    """Файлы-кандидаты (.xml, .zip) и замечания обхода. Симлинки и служебные каталоги не берём."""
    found: list[Path] = []
    notes: list[str] = []
    root = Path(root)
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        if depth >= MAX_DEPTH:
            dirs[:] = []
        kept = []
        for name in sorted(dirs):
            if name in SKIP_DIRS or name.startswith("."):
                continue
            if (here / name).is_symlink():          # переход по ссылке увёл бы обход наружу
                notes.append(f"ссылка пропущена: {name}")
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = here / name
            if _skip_name(name) or path.is_symlink():
                continue
            if path.suffix.lower() not in SEND_EXT:
                continue
            found.append(path)
    return found, notes


def _read_ready(path: Path, pause: float) -> tuple[bytes | None, str]:
    """Прочитать файл, только если он точно дописан: два замера подряд с паузой. → (байты, причина)."""
    try:
        first = path.stat()
    except OSError as exc:
        return None, f"файл недоступен: {exc.strerror or exc}"
    if first.st_size == 0:
        return None, "файл пустой"
    if first.st_size > service.MAX_ZIP_BYTES:
        return None, "файл больше 20 МБ"
    if pause > 0:
        day = threading.Event()
        day.wait(pause)
    try:
        second = path.stat()
        data = path.read_bytes()
    except OSError as exc:
        return None, f"файл недоступен: {exc.strerror or exc}"
    if (second.st_size, second.st_mtime_ns) != (first.st_size, first.st_mtime_ns):
        return None, "файл ещё пишется — вернёмся к нему в следующий раз"
    if len(data) != second.st_size:
        return None, "файл изменился во время чтения — вернёмся к нему в следующий раз"
    return data, ""


def _move(root: Path, path: Path, folder: str, stamp: datetime) -> str:
    """Перенести файл в processed/ГГГГ-ММ или quarantine; вернуть относительный путь назначения."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    target_dir = root / folder
    if folder == "processed":
        target_dir = target_dir / stamp.strftime("%Y-%m")
    target = target_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():                          # тот же файл уже лежит (повторная раскладка) — не затираем
        target = target.with_name(f"{target.stem}.{stamp.strftime('%Y%m%d%H%M%S')}{target.suffix}")
    shutil.move(str(path), str(target))
    _tidy_empty(root, path.parent)          # пустые папки выгрузки за собой не оставляем
    return str(target.relative_to(root))


def _tidy_empty(root: Path, folder: Path) -> None:
    """Удалить опустевшие каталоги до корня приёмной папки (не сам корень, не ошибку — молча)."""
    current = Path(folder)
    root = Path(root)
    while current != root and root in current.parents:
        try:
            current.rmdir()                 # только пустой: чужие файлы не тронем
        except OSError:
            return
        current = current.parent


def _reject_note(path: Path, reason: str) -> None:
    """Рядом с файлом в карантине — причина словами: человек должен понять, что делать."""
    note = path.with_name(path.name + ".reason.txt")
    try:
        note.write_text(f"{_now().isoformat()}Z\n{reason}\n", encoding="utf-8")
    except OSError:
        pass


def _robot(item: PrimaryInboxSetting) -> User:
    """Разбор идёт от имени включившего приём, но с правами бухгалтера: админских полномочий у фона нет."""
    return SimpleNamespace(id=item.updated_by or item.created_by or "", role=ROLE_ACCOUNTANT, company_id=None,
                           username="приёмная папка")


def _companies(db: Session, user: User | None) -> list[str]:
    """Компании, по которым разрешено раскладывать документы из папки.

    Фон (user=None) и администратор раскладывают по всем действующим компаниям — это его право и его папка.
    Бухгалтеру (в том числе нажавшему «Забрать сейчас») — только своя и назначенные клиенты, как в загрузке
    на экране (ADR-30). Пустой список — документ не примут вовсе: лучше отказ с причиной, чем запись наугад.
    """
    from ..clients import service as clients
    allowed = clients.allowed_company_ids(db, user) if user is not None else None
    if allowed is None:
        return list(db.execute(select(Company.id).where(Company.is_active.is_(True))).scalars())
    return list(allowed or [])


def _seen(db: Session, sha: str) -> bool:
    return db.execute(select(PrimaryInboxFile.id).where(PrimaryInboxFile.sha256 == sha).limit(1)).scalars().first() is not None


def _outcomes(results: list[dict]) -> tuple[str, str, str, str]:
    """Итог по одному документу: (статус, причина, id документа, метка «чужой»).

    status: ok — в реестре, duplicate — уже был, rejected — не принят (причина словами для карантина).
    """
    if not results:
        return "rejected", "документ не разобран", "", ""
    doc_id = next(((res.get("document") or {}).get("id", "") for res in results
                   if res.get("result") == "created"), "")
    statuses = {res.get("result") for res in results}
    if statuses == {"created"}:
        between_own = len(results) > 1
        return "ok", ("документ между своими компаниями: записан в обе" if between_own else ""), doc_id, ""
    if statuses == {"duplicate"}:
        return "duplicate", "этот документ уже загружен", doc_id, ""
    reason = next((str(res.get("message") or "") for res in results if res.get("result") == "rejected"), "")
    foreign = "foreign" if any(res.get("reason") == "foreign" for res in results) else ""
    return "rejected", reason or "не принят", doc_id, foreign


def run_once(db: Session, *, user: User | None = None, pause: float = 1.0, limit: int = MAX_BATCH) -> dict:
    """Один обход приёмной папки. Возвращает итог для экрана и журнала.

    Идемпотентен: повторный вызов на той же папке ничего не задваивает — принятые файлы уже перенесены,
    а остальные узнаются по хешу файла в журнале. В журнал попадает каждая строка отдельно: в архиве ЭДО
    рядом с нашими УПД бывают титулы покупателя и чужие документы, и один «итог на архив» их бы спрятал.
    """
    item = row(db)
    path_text = item.path or os.environ.get("YM_PRIMARY_INBOX_PATH", "") or str(_absolute(DEFAULT_SUBDIR) or "")
    if not path_text:
        raise InboxError("Приёмная папка не настроена", 409)
    root = check_path(db, path_text)
    if not _lock.acquire(blocking=False):
        raise InboxError("Обход уже идёт — повторите через минуту", 429)
    now = _now()
    counters_: dict[str, int] = {"files": 0, "ok": 0, "duplicate": 0, "rejected": 0, "waiting": 0, "foreign": 0,
                                 "left": 0}
    problems: list[str] = []
    try:
        files, notes = walk(root)
        problems.extend(notes)
        robot_user = user or _robot(item)
        allowed_companies = _companies(db, user)      # фон — все действующие; бухгалтер — свои компании
        overhead = 0
        for n, path in enumerate(files):
            if n >= limit or overhead >= MAX_RUN_BYTES:
                counters_["left"] = len(files) - n
                break
            counters_["files"] += 1
            rel = str(path.relative_to(root))[:MAX_PATH]
            data, reason = _read_ready(path, pause)
            if data is None:
                if "пишется" in reason:
                    counters_["waiting"] += 1
                problems.append(f"{rel}: {reason}")
                continue
            overhead += len(data)
            sha = hashlib.sha256(data).hexdigest()
            if _seen(db, sha):
                counters_["duplicate"] += 1
                _log(db, rel, sha, len(data), "duplicate", "этот файл уже обработан", "", "inbox", robot_user)
                _move(root, path, "processed", now)
                db.commit()
                continue
            statuses: list[str] = []
            for label, body, why in service.unpack(path.name, data):
                if body is None:
                    statuses.append("rejected")
                    counters_["rejected"] += 1
                    _log(db, f"{rel}/{label}"[:MAX_PATH], "", 0, "rejected", why, "", "inbox", robot_user)
                    continue
                results = service.accept_routed(db, robot_user, body, label, channel="inbox",
                                                allowed=allowed_companies)
                status, message, doc_id, foreign = _outcomes(results)
                statuses.append(status)
                counters_[status] += 1
                counters_["foreign"] += 1 if foreign else 0
                _log(db, f"{rel}/{label}"[:MAX_PATH], sha, len(body), status, message, doc_id, "inbox", robot_user)
            db.commit()
            # Архив с нашим УПД уходит в processed, а причины отказов по его строкам остаются в журнале:
            # возвращать весь архив в карантин из-за одного титула покупателя — значит грузить его снова и снова.
            if statuses and all(item_status == "rejected" for item_status in statuses):
                target = _move(root, path, "quarantine", now)
                why = _last_reason(db, rel)
                _reject_note(root / target, why)
            else:
                _move(root, path, "processed", now)
            db.commit()
    finally:
        _lock.release()
    result = {"ok": True, "at": _iso(now), "path": str(root), **counters_, "problems": problems[:50],
              "statuses": STATUSES}
    item.last_run_at = now
    item.last_result_json = json.dumps(result, ensure_ascii=False)
    if counters_["ok"] or counters_["duplicate"]:
        item.last_ok_at = now
    if appsettings.get_setting(db, SETTING_ENABLED, "0") == "1":
        item.status = "active"
    item.last_error = "; ".join(problems[:3])[:300]
    db.commit()
    _purge(db)
    return result


def _last_reason(db: Session, rel: str) -> str:
    """Причина отказа для файла причины в карантине — из журнала этого обхода."""
    entry = db.execute(select(PrimaryInboxFile).where(PrimaryInboxFile.path.like(f"{rel}%"))
                       .order_by(PrimaryInboxFile.created_at.desc()).limit(1)).scalars().first()
    if entry is None:
        return "документ не принят"
    return f"{entry.file_name}: {entry.message}" if entry.file_name else entry.message


def _log(db: Session, rel: str, sha: str, size: int, status: str, message: str, doc_id: str, channel: str,
         user: User | None) -> PrimaryInboxFile:
    entry = PrimaryInboxFile(path=rel[:MAX_PATH], file_name=Path(rel).name[:255], sha256=sha, size=size,
                             status=status, message=(message or "")[:300], document_id=doc_id,
                             channel=channel, created_by=(user.id if user else ""))
    db.add(entry)
    return entry


def _purge(db: Session) -> None:
    """Журнал старше года удаляется — как журнал почты: документы остаются, записи о файлах нет."""
    day = _now().date().isoformat()
    if _purged.get("day") == day:
        return
    _purged["day"] = day
    cutoff = _now() - timedelta(days=KEEP_LOG_DAYS)
    db.query(PrimaryInboxFile).filter(PrimaryInboxFile.created_at < cutoff).delete(synchronize_session=False)
    db.commit()


# ------------------------------------------------------------------ журнал и тик
def log(db: Session, user: User, limit: int = 50, status: str = "") -> dict:
    """Последние записи журнала в пределах компаний, доступных пользователю."""
    from ..clients import service as clients
    query = select(PrimaryInboxFile)
    if status in STATUSES:
        query = query.where(PrimaryInboxFile.status == status)
    allowed = clients.allowed_company_ids(db, user)
    if allowed is not None:                      # документ чужой компании в журнале не показываем
        ids = set(db.execute(select(PrimaryDocument.id).where(PrimaryDocument.company_id.in_(allowed or [""]))).scalars())
        query = query.where((PrimaryInboxFile.document_id == "") | PrimaryInboxFile.document_id.in_(ids or [""]))
    items = db.execute(query.order_by(PrimaryInboxFile.created_at.desc()).limit(max(1, min(limit, 200)))).scalars().all()
    return {"items": [{"id": i.id, "file": i.path, "status": i.status, "status_title": STATUSES.get(i.status, i.status),
                       "message": i.message, "document_id": i.document_id, "size": i.size,
                       "created_at": _iso(i.created_at)} for i in items],
            "counters": counters(db)}


def due(db: Session, now: datetime | None = None) -> bool:
    """Пора ли обходить папку по расписанию."""
    if os.environ.get("YM_PRIMARY_INBOX_OFF") == "1":
        return False
    if appsettings.get_setting(db, SETTING_ENABLED, "0") != "1":
        return False
    item = row(db)
    if not item.path:
        return False
    now = now or _now()
    interval = min(MAX_INTERVAL, max(MIN_INTERVAL, _int_setting(db, SETTING_INTERVAL, DEFAULT_INTERVAL)))
    if item.last_run_at is None:
        return True
    return now - item.last_run_at >= timedelta(minutes=interval)


def tick(db: Session) -> dict | None:
    """Тик фонового обхода: обходит папку, если пора. Ошибки не бросает — пишет в состояние."""
    if not due(db):
        return None
    try:
        return run_once(db, pause=0.5)
    except InboxError as exc:
        item = row(db)
        item.status, item.last_error = "error", str(exc)[:300]
        db.commit()
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                     # noqa: BLE001 — тик не должен ронять процесс
        db.rollback()
        item = row(db)
        item.status, item.last_error = "error", f"обход прерван: {exc}"[:300]
        db.commit()
        return {"ok": False, "error": str(exc)}
