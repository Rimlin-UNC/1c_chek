# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — партнёрская программа «Чек-Пула» (v1.38.0, Этап 9).
# Кэшбэк-канал из плана: партнёр (магазин у кассы с нашим QR) платит
# участнику процент от чека баллами. Партнёр опознаётся по ИНН чека;
# справочник — офлайн (geo/data/partners.json), без внешних запросов.
#
# QR на кассах: QR кодирует ссылку на страницу сдачи чека (base_url +
# /#/pub) и рендерится в SVG (вендоренный генератор qrcode, BSD).
# Кэшбэк не платится в карантине антифрода и только за верифицированные
# чеки, кап на чек. ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "geo" / "data"
VENDOR = Path(__file__).resolve().parents[1] / "vendor"

CASHBACK_CAP = 100          # максимум баллов кэшбэка за один чек
QR_MAX_LEN = 128            # защита: QR только для коротких ссылок пула


@lru_cache(maxsize=1)
def _load() -> tuple[dict, ...]:
    data = json.loads((DATA / "partners.json").read_text(encoding="utf-8"))
    return tuple(dict(p, inn=str(p["inn"]).strip()) for p in data)


def list_all() -> list[dict]:
    """Список партнёров (публичные поля)."""
    return [{"code": p["code"], "name": p["name"], "pct": int(p["pct"]),
             "city": p.get("city", "")} for p in _load()]


def find_by_inn(inn: str) -> dict | None:
    inn = str(inn or "").strip()
    if not inn:
        return None
    for p in _load():
        if p["inn"] == inn:
            return p
    return None


def find_by_code(code: str) -> dict | None:
    code = (code or "").strip().upper()
    for p in _load():
        if p["code"].upper() == code:
            return p
    return None


# --- кэшбэк -----------------------------------------------------------------
def on_verified_receipt(db, user, receipt) -> int:
    """Кэшбэк за верифицированный чек партнёра. Возвращает баллы (0/бонус).
    Дедуп по чеку (reason+ref_id), карантин не платит, кап на чек."""
    from .models import PoolPoint
    p = find_by_inn(getattr(receipt, "merchant_inn", ""))
    if p is None:
        return 0
    if getattr(user, "quarantined_at", None):
        return 0
    exists = (db.query(PoolPoint)
              .filter_by(user_id=user.id, reason="partner_cashback",
                         ref_id=receipt.id).first())
    if exists is not None:
        return 0
    bonus = int(round(float(receipt.total_sum or 0) * int(p["pct"]) / 100))
    if bonus <= 0:
        return 0
    bonus = min(bonus, CASHBACK_CAP)
    from .ingest import add_points
    add_points(db, user, bonus, "partner_cashback", receipt.id)
    db.flush()   # ядро с autoflush=False: дедуп-запрос должен увидеть запись
    return bonus


# --- QR (SVG) ----------------------------------------------------------------
def qr_svg(text: str) -> str:
    """Ссылка → QR-код в виде SVG-строки (вендоренный qrcode, BSD).
    Только короткие ссылки пула — сторонний текст не кодируем."""
    text = (text or "").strip()
    if not text or len(text) > QR_MAX_LEN:
        raise ValueError("Слишком длинный текст для QR")
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    import qrcode                                  # noqa: E402 (вендор)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       border=2)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    pts: list[str] = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                pts.append(f"M{x} {y}h1v1h-1z")
    path_el = f'<path d="{"".join(pts)}" fill="#1c1c1c"/>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
            f'shape-rendering="crispEdges" width="{n * 8}" height="{n * 8}" '
            f'role="img" aria-label="QR-код">{path_el}</svg>')
