"""T-78 (v1.70.111): время чеков, сдвинутое формой правки до 1.70.110.

До 1.70.110 форма правки слала время в UTC (``toISOString``), а сервер сохранял его как
местное: каждое сохранение сдвигало чек на часовой пояс браузера (Челябинск — на 5 часов,
Москва — на 3). Сдвиг уходил и в единую запись, и в Чек-Пул (``sync_registry``), а время
входит в запрос проверки ФНС и в выгрузку 1С. Исходное время кассы хранится в строке QR
(``qr_data``, параметр ``t=``).

- :func:`find_shifted` — отчёт, только чтение: чеки, у которых время расходится с QR на целое
  число часов (до 72) с точностью до потерянных секунд.
- :func:`restore` — вернуть время из QR выбранным чекам: копия чека, затем единая запись и
  Чек-Пул тем же путём, что правка бухгалтера (``sync_registry``); строка журнала на каждый
  чек со старым и новым временем. Резервную копию перед исправлением делает маршрут.

Вернуть можно только чек, который правили формой (``details_source == "manual_edit"``) и
который пришёл не ручным вводом: у ручного ввода строка QR собрана из введённого времени, и
расхождение может быть законным исправлением. Такие чеки — в отчёте с причиной.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Company, Receipt, User
from ..services.qr import QRParseError, parse_qr

MAX_HOURS = 72                         # больше трёх суток — уже не сдвиг поясом
TOLERANCE = timedelta(seconds=60)      # форма теряла секунды: 15:56:37 → 10:56:00
REASON_MANUAL = "ручной ввод: время в QR — введённое, расхождение может быть исправлением"
REASON_NOT_EDITED = "формой правки не сохранялся — причина расхождения другая"


@dataclass
class Shifted:
    """Чек, у которого время расходится с QR на целое число часов."""
    receipt_id: str
    company_id: str | None
    company: str
    fn: str
    fd: str
    fp: str
    receipt_date: datetime
    qr_time: datetime
    hours: int
    fixable: bool
    reason: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["receipt_date"] = self.receipt_date.isoformat()
        data["qr_time"] = self.qr_time.isoformat()
        return data


def qr_time(qr_data: str | None) -> datetime | None:
    """Время кассы из строки QR или None, если строки нет или она не читается."""
    if not qr_data:
        return None
    try:
        return parse_qr(qr_data).date_time
    except QRParseError:
        return None


def shift_hours(receipt_date: datetime, original: datetime) -> int:
    """Сдвиг в целых часах (0 — сдвига нет или он не кратен часу)."""
    delta = receipt_date - original
    hours = round(delta.total_seconds() / 3600)
    if hours == 0 or abs(hours) > MAX_HOURS:
        return 0
    return hours if abs(delta - timedelta(hours=hours)) < TOLERANCE else 0


def find_shifted(db: Session) -> list[Shifted]:
    """Отчёт: чеки со сдвигом времени относительно QR (база не меняется)."""
    names = dict(db.query(Company.id, Company.name).all())
    query = (db.query(Receipt.id, Receipt.company_id, Receipt.fn, Receipt.fd, Receipt.fp, Receipt.qr_data,
                      Receipt.receipt_date, Receipt.source, Receipt.details_source)
             .filter(Receipt.receipt_date.isnot(None), Receipt.qr_data.isnot(None), Receipt.qr_data != "")
             .order_by(Receipt.receipt_date.desc(), Receipt.id))
    found = []
    for row in query.yield_per(500):
        original = qr_time(row.qr_data)
        if original is None:
            continue
        hours = shift_hours(row.receipt_date, original)
        if not hours:
            continue
        reason = ""
        if row.source == "manual":
            reason = REASON_MANUAL
        elif row.details_source != "manual_edit":
            reason = REASON_NOT_EDITED
        found.append(Shifted(row.id, row.company_id, names.get(row.company_id, ""), row.fn, row.fd, row.fp,
                             row.receipt_date, original, hours, not reason, reason))
    return found


def report(db: Session, limit: int = 500) -> dict:
    """Ответ для экрана: числа и первые ``limit`` чеков (сначала те, что можно вернуть)."""
    found = find_shifted(db)
    checked = (db.query(Receipt.id)
               .filter(Receipt.receipt_date.isnot(None), Receipt.qr_data.isnot(None), Receipt.qr_data != "")
               .count())
    ordered = sorted(found, key=lambda s: (not s.fixable, s.company, s.receipt_date))
    return {"checked": checked, "shifted": len(found), "fixable": sum(s.fixable for s in found),
            "items": [s.to_dict() for s in ordered[:limit]], "limit": limit}


def restore(db: Session, user: User, ids: list[str] | None = None) -> dict:
    """Вернуть время из QR: копия чека, затем единая запись и Чек-Пул; журнал на каждый чек.

    ``ids`` — выбранные чеки (None — все, которые можно вернуть). Чек, который нельзя вернуть
    или которого нет в отчёте, пропускается и считается в ``skipped``.
    """
    from ..services.audit import log_action
    from . import service as unified

    fixable = {s.receipt_id: s for s in find_shifted(db) if s.fixable}
    wanted = list(fixable) if ids is None else list(dict.fromkeys(ids))
    restored = []
    for receipt_id in wanted:
        item = fixable.get(receipt_id)
        receipt = db.get(Receipt, receipt_id) if item else None
        if receipt is None:
            continue
        receipt.receipt_date = item.qr_time
        unified.sync_registry(db, receipt, user, details={"time_restored_hours": item.hours})
        restored.append(item)
    db.commit()
    for item in restored:
        log_action(user, "receipt_time_restored", "receipt", item.receipt_id,
                   {"from": item.receipt_date.isoformat(), "to": item.qr_time.isoformat(),
                    "hours": item.hours, "company": item.company})
    return {"restored": len(restored), "skipped": len(wanted) - len(restored)}
