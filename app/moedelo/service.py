"""«Моё дело» по API (T-71, v1.70.107, ADR-37): ключ компании, проверка и отправка актов покупок.

Этап 1 — услуги. Документ первички (УПД с функцией ДОП или СЧФДОП, введённый вручную или распознанный)
уходит в «Моё дело» актом покупки: POST /accounting/api/v1/purchases/act (PurchasesActModel). Позиции акта
не требуют справочника товаров «Моего дела», поэтому услуги отправляются сразу. Накладной на товары нужен
идентификатор товара из справочника (StockProductId в PurchasesWaybillItemSaveRequestModel) — это
следующий этап: сопоставление позиций со справочником.

Порядок отправки одного документа:
  1. проверки до запроса (blockers): ошибки проверки, счёт-фактура без передаточной части, исправленный
     документ, нет номера, даты или ИНН продавца, не рубли, ставка «НДС исчисляет налоговый агент», суммы
     строк не сходятся с итогом;
  2. поставщик — по ИНН (GET /kontragents/api/v1/kontragent?inn=…); если его нет — добавляется по ИНН
     (POST /kontragents/api/v1/kontragent/inn, реквизиты «Моё дело» заполняет само);
  3. повтор узнаётся дважды: своя таблица moedelo_links (одна строка на документ) и поиск акта с тем же
     номером, датой и поставщиком в «Моём деле» — найденный акт привязывается, второй не создаётся;
  4. акт создаётся с явными суммами каждой строки (без НДС, НДС, с НДС), поэтому способ начисления НДС не
     меняет итог; сумму из ответа программа сравнивает с документом и при расхождении предупреждает.

Ключ — только зашифрованным (services.secretbox), в ответы и журнал не попадает. Данные в тестах —
синтетические (ADR-14).
"""
from __future__ import annotations

import re
import threading
from datetime import date

from sqlalchemy.orm import Session

from ..models import Company, User, utcnow
from ..primary import service as primary_service
from ..primary.models import PrimaryDocument, PrimaryDocumentLine
from ..services import secretbox
from . import client
from .client import MoedeloError
from .models import MoedeloConnection, MoedeloLink

KONTRAGENTS_PATH = "/kontragents/api/v1/kontragent"
KONTRAGENT_BY_INN_PATH = "/kontragents/api/v1/kontragent/inn"
ACT_PATH = "/accounting/api/v1/purchases/act"
BATCH_LIMIT = 50
SELECTION_LIMIT = 500

STATUS_TITLES = {"none": "не подключено", "new": "ключ сохранён, не проверен", "active": "подключено",
                 "error": "ошибка подключения"}
LINK_TITLES = {"sent": "отправлен", "found": "уже был в «Моём деле» — привязан",
               "mismatch": "отправлен, но сумма в «Моём деле» другая"}
KIND_TITLES = {"services": "услуги — уйдёт актом покупки",
               "goods": "товары — накладные появятся следующим этапом",
               "mixed": "товары и услуги — накладные появятся следующим этапом"}

# Ставка НДС первички (fns_refs.vat_canonical) → NdsType «Моего дела»: -1 — без НДС, иначе процент.
# Расчётные ставки (20/120 и т. п.) — та же ставка: суммы строк передаются явно.
NDS_TYPES = {"22": 22, "20": 20, "18": 18, "10": 10, "7": 7, "5": 5, "0": 0, "none": -1,
             "22/122": 22, "20/120": 20, "18/118": 18, "10/110": 10, "7/107": 7, "5/105": 5}
RATE_TITLES = {"agent": "НДС исчисляет налоговый агент"}

# Признаки услуги: код ОКЕИ единицы (условная единица, час, сутки, неделя, месяц, квартал, год), название
# единицы или слова названия. Подсказка осторожная: «пена монтажная» и «ремонтный состав» — товары.
SERVICE_UNITS = frozenset({"876", "356", "359", "360", "362", "364", "366"})
_SERVICE_UNIT = re.compile(r"^\s*(?:усл|час|ч\b|сут|мес|смен|рейс|маш[\s.\-]*ч)", re.IGNORECASE)
_SERVICE_NAME = re.compile(
    r"\b(?:услуг[аиу]?|работ[аы]?|выполнени[ея]|доставк[аиу]|перевозк[аиу]|экспедировани[ея]|"
    r"аренд[аыуе]|арендн\w*|монтаж[аеу]?|демонтаж[аеу]?|ремонт[аеу]?|обслуживани[ея]|техобслуживани[ея]|"
    r"консультаци[ияй]|погрузк[аи]|разгрузк[аи]|вывоз[аеу]?|уборк[аи]|охран[аы]|интернет|хостинг|"
    r"лицензи[яи]|подписк[аи]|проектировани[ея]|экспертиз[аы]|диагностик[аи]|пусконаладк\w*|"
    r"сопровождени[ея]|обучени[ея]|размещени[ея]|страховани[ея]|абонентск\w*)\b", re.IGNORECASE)

_KEY = re.compile(r"^[\x21-\x7e]{16,200}$")      # ключ API: печатные латинские символы без пробелов
_send_lock = threading.Lock()


class MoedeloServiceError(ValueError):
    """Ошибка для пользователя. status — код ответа API; stop — пакет дальше не отправлять."""

    def __init__(self, message: str, status: int = 422, stop: bool = False):
        super().__init__(message)
        self.status = status
        self.stop = stop


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _rub(value: float | None) -> str:
    return f"{float(value or 0):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _from_md_error(exc: MoedeloError) -> MoedeloServiceError:
    stop = exc.kind in ("auth", "forbidden", "rate", "unavailable")
    return MoedeloServiceError(str(exc), 502 if exc.kind in ("rate", "unavailable") else 422, stop)


# ------------------------------------------------------------------ подключение
def connection_of(db: Session, company_id: str | None) -> MoedeloConnection | None:
    if not company_id:
        return None
    return db.query(MoedeloConnection).filter(MoedeloConnection.company_id == company_id).first()


def connection_dict(conn: MoedeloConnection | None) -> dict:
    status = conn.status if conn and conn.key_enc else "none"
    return {
        "status": status, "status_title": STATUS_TITLES.get(status, status),
        "key_set": bool(conn and conn.key_enc), "key_tail": (conn.key_tail if conn else "") or "",
        "last_check_at": _iso(conn.last_check_at) if conn else None,
        "last_ok_at": _iso(conn.last_ok_at) if conn else None,
        "last_error": (conn.last_error if conn else "") or "",
        "docs_url": client.DOCS_URL,
    }


def save_key(db: Session, company: Company, key: str, user: User) -> MoedeloConnection:
    """Сохранить ключ (только зашифрованным); проверка — отдельным шагом check()."""
    key = (key or "").strip()
    if not _KEY.match(key):
        raise MoedeloServiceError("Ключ «Моего дела» — строка из латинских букв, цифр и дефисов без пробелов, "
                                  "обычно вида xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx. Скопируйте его в «Моём деле»: "
                                  "«Настройки → Интеграция с партнёрами»")
    conn = connection_of(db, company.id)
    if conn is None:
        conn = MoedeloConnection(company_id=company.id, created_by=user.id)
        db.add(conn)
    conn.key_enc = secretbox.seal(key)
    conn.key_tail = key[-4:]
    conn.status = "new"
    conn.last_error = ""
    conn.updated_by = user.id
    conn.updated_at = utcnow()
    db.commit()
    return conn


def _key(conn: MoedeloConnection | None) -> str:
    if conn is None or not conn.key_enc:
        raise MoedeloServiceError("Подключите «Моё дело» в карточке компании: вставьте ключ API и нажмите "
                                  "«Сохранить и проверить»", 409, stop=True)
    key = secretbox.unseal(conn.key_enc)
    if not key:
        raise MoedeloServiceError("Ключ «Моего дела» не читается (на сервере сменился SECRET_KEY) — вставьте его "
                                  "заново в карточке компании", 409, stop=True)
    return key


def check(db: Session, company: Company, user: User) -> MoedeloConnection:
    """Проверка ключа запросом на чтение: список контрагентов, одна запись."""
    conn = connection_of(db, company.id)
    key = _key(conn)
    conn.last_check_at = utcnow()
    conn.updated_by = user.id
    try:
        client.request("GET", KONTRAGENTS_PATH, key, params={"pageSize": 1})
    except MoedeloError as exc:
        conn.status = "error"
        conn.last_error = str(exc)[:300]
        db.commit()
        raise _from_md_error(exc) from exc
    conn.status = "active"
    conn.last_ok_at = conn.last_check_at
    conn.last_error = ""
    db.commit()
    return conn


def remove(db: Session, company: Company) -> bool:
    """Отключить: ключ удаляется; отметки об отправленных документах остаются (защита от повторов)."""
    conn = connection_of(db, company.id)
    if conn is None:
        return False
    db.delete(conn)
    db.commit()
    return True


def _mark_failed(db: Session, company_id: str, exc: MoedeloError) -> None:
    """Ключ перестал работать во время отправки — карточка компании покажет ошибку."""
    if exc.kind not in ("auth", "forbidden"):
        return
    conn = connection_of(db, company_id)
    if conn is not None:
        conn.status = "error"
        conn.last_error = str(exc)[:300]
        conn.last_check_at = utcnow()
        db.commit()


# ------------------------------------------------------------------ разбор документа
def is_service_line(line: PrimaryDocumentLine) -> bool:
    if (line.unit_code or "").strip() in SERVICE_UNITS or _SERVICE_UNIT.match(line.unit_name or ""):
        return True
    return bool(_SERVICE_NAME.search(line.name or ""))


def classify(lines: list[PrimaryDocumentLine]) -> str:
    """services — все строки похожи на услуги; goods — ни одной; mixed — и то и другое."""
    marks = [is_service_line(ln) for ln in lines]
    if marks and all(marks):
        return "services"
    return "mixed" if any(marks) else "goods"


def _rate(line: PrimaryDocumentLine) -> str:
    from ..services.fns_refs import vat_canonical
    return vat_canonical(line.vat_rate or "none")


def act_item(line: PrimaryDocumentLine) -> dict:
    """Строка документа → позиция акта (ActItemModel) с явными суммами."""
    base, vat, total = line.sum_without_vat, line.vat_sum, line.sum_with_vat
    if total is None and base is not None:
        total = base + (vat or 0)
    if base is None and total is not None:
        base = total - (vat or 0)
    if vat is None:
        vat = (total or 0) - (base or 0)
    base, vat, total = round(base or 0, 2), round(vat or 0, 2), round(total or 0, 2)
    nds = NDS_TYPES[_rate(line)]
    if nds == -1:
        vat, total = 0.0, base
    qty, price = line.quantity, line.price
    item = {"Name": (line.name or "").strip()[:1000] or "Услуга",
            "Unit": (line.unit_name or "").strip()[:40] or "усл. ед.",
            "NdsType": nds, "SumWithoutNds": base, "NdsSum": vat, "SumWithNds": total}
    if qty and qty > 0:
        item["Count"] = qty
        item["Price"] = price if price is not None else round(base / qty, 2)
    else:                                   # услуга без количества и цены — «прочерки» (только услуги)
        item.update({"Count": 1, "Price": base, "IsUnmeasurable": True})
    return item


def plan(doc: PrimaryDocument) -> dict:
    """Что уйдёт и что мешает: blockers — причины по-русски, kind — услуги, товары или смешанный."""
    blockers: list[str] = []
    if not doc.company_id:
        blockers.append("Документ не привязан к компании — выберите компанию у документа")
    if doc.status == "errors":
        blockers.append("В документе есть ошибки проверки — исправьте их, потом отправляйте")
    if doc.function == "СЧФ":
        blockers.append("Это счёт-фактура без передаточной части — в «Моё дело» уходят акты: УПД с функцией "
                        "ДОП или СЧФДОП")
    if doc.correction_number:
        blockers.append(f"Исправленный документ (исправление № {doc.correction_number}) — поправьте акт в "
                        "«Моём деле» вручную")
    if not (doc.doc_number or "").strip():
        blockers.append("У документа нет номера")
    if not doc.doc_date:
        blockers.append("У документа нет даты")
    if len(_digits(doc.seller_inn)) not in (10, 12):
        blockers.append("У продавца нет ИНН — «Моё дело» находит и добавляет поставщика по ИНН")
    if (doc.currency or "643") != "643":
        blockers.append("Документ не в рублях — внесите его в «Моё дело» вручную")
    lines = list(doc.lines)
    if not lines:
        blockers.append("В документе нет строк")
    bad = sorted({_rate(ln) for ln in lines if _rate(ln) not in NDS_TYPES})
    if bad:
        names = ", ".join(f"«{RATE_TITLES.get(b, b)}»" for b in bad)
        blockers.append(f"Ставка НДС {names} — такой документ внесите в «Моё дело» вручную")
    elif lines:
        total = round(sum(act_item(ln)["SumWithNds"] for ln in lines), 2)
        if abs(total - round(doc.total_with_vat or 0, 2)) > max(0.05, 0.01 * len(lines)):
            blockers.append(f"Сумма строк ({_rub(total)}) не совпадает с итогом документа "
                            f"({_rub(doc.total_with_vat)}) — проверьте документ")
    kind = classify(lines) if lines else "goods"
    return {"kind": kind, "kind_title": KIND_TITLES[kind], "blockers": blockers, "can_send": not blockers,
            "lines": len(lines), "total": doc.total_with_vat, "number": doc.doc_number,
            "date": _iso(doc.doc_date), "seller": {"name": doc.seller_name, "inn": doc.seller_inn}}


def act_payload(doc: PrimaryDocument, kontragent_id: int) -> dict:
    """Акт покупки (PurchasesActModel): поставщик, номер, дата, способ НДС и строки с явными суммами."""
    items = [act_item(ln) for ln in doc.lines]
    taxed = any(i["NdsType"] != -1 for i in items)
    return {"KontragentId": int(kontragent_id), "Number": (doc.doc_number or "").strip()[:100],
            "DocDate": doc.doc_date.isoformat() if doc.doc_date else "",
            "NdsPositionType": 2 if taxed else 1, "Items": items}


# ------------------------------------------------------------------ обмен
def _resource_list(data) -> list[dict]:
    if isinstance(data, dict):
        rows = data.get("ResourceList") or data.get("resourceList") or []
    else:
        rows = data if isinstance(data, list) else []
    return [r for r in rows if isinstance(r, dict)]


def find_or_create_kontragent(key: str, doc: PrimaryDocument) -> tuple[int, bool]:
    """Поставщик по ИНН (при нескольких — с тем же КПП и не в архиве); нет — добавить по ИНН."""
    inn, kpp = _digits(doc.seller_inn), _digits(doc.seller_kpp)
    rows = [r for r in _resource_list(client.request("GET", KONTRAGENTS_PATH, key, params={"inn": inn, "pageSize": 20}))
            if _digits(r.get("Inn")) == inn and r.get("Id")]
    if rows:
        rows.sort(key=lambda r: (bool(r.get("IsArchived")), bool(kpp) and _digits(r.get("Kpp")) != kpp))
        return int(rows[0]["Id"]), False
    try:
        made = client.request("POST", KONTRAGENT_BY_INN_PATH, key, body={"Inn": inn})
    except MoedeloError as exc:
        if exc.kind != "missing":
            raise
        form = "UL" if len(inn) == 10 else "IP"        # запасной путь: карточка с названием и ИНН
        made = client.request("POST", KONTRAGENTS_PATH, key, body={
            "Name": (doc.seller_name or inn)[:500], "Inn": inn, "Kpp": kpp, "Type": "Kontragent", "Form": form})
    if not isinstance(made, dict) or not made.get("Id"):
        raise MoedeloError("«Моё дело» не вернуло номер добавленного поставщика — проверьте его в «Моём деле»",
                           "bad")
    return int(made["Id"]), True


def find_act(key: str, doc: PrimaryDocument, kontragent_id: int) -> dict | None:
    """Акт с тем же номером, датой и поставщиком уже есть в «Моём деле»?"""
    number = (doc.doc_number or "").strip()
    rows = _resource_list(client.request("GET", ACT_PATH, key, params={
        "number": number, "kontragentId": kontragent_id, "pageSize": 50}))
    day = doc.doc_date.isoformat() if doc.doc_date else ""
    for row in rows:
        if str(row.get("Number") or "").strip() == number and str(row.get("DocDate") or "")[:10] == day:
            return row
    return None


def _float(value) -> float | None:
    try:
        return None if value is None else round(float(value), 2)
    except (TypeError, ValueError):
        return None


def link_of(db: Session, doc_id: str) -> MoedeloLink | None:
    return db.query(MoedeloLink).filter(MoedeloLink.document_id == doc_id).first()


def link_title(link: MoedeloLink) -> str:
    day = link.created_at.strftime("%d.%m.%Y") if link.created_at else ""
    return f"акт покупки № {link.md_number or '—'} ({LINK_TITLES.get(link.status, link.status)}{', ' + day if day else ''})"


def link_dict(link: MoedeloLink | None) -> dict | None:
    if link is None:
        return None
    return {"md_kind": link.md_kind, "kind_title": "акт покупки", "md_id": link.md_id, "md_number": link.md_number,
            "status": link.status, "status_title": LINK_TITLES.get(link.status, link.status),
            "kontragent_new": bool(link.kontragent_new), "sum_sent": link.sum_sent, "sum_md": link.sum_md,
            "message": link.message, "created_at": _iso(link.created_at), "title": link_title(link)}


def document_state(db: Session, doc: PrimaryDocument) -> dict:
    conn = connection_of(db, doc.company_id)
    return {"connection": {"status": connection_dict(conn)["status"],
                           "status_title": connection_dict(conn)["status_title"]},
            "link": link_dict(link_of(db, doc.id)), "plan": plan(doc)}


def send_document(db: Session, user: User, doc: PrimaryDocument, as_services: bool = False) -> MoedeloLink:
    """Отправить документ актом покупки. Повтор не создаёт второй акт ни у нас, ни в «Моём деле»."""
    company = db.get(Company, doc.company_id) if doc.company_id else None
    if company is None:
        raise MoedeloServiceError("Документ не привязан к компании — выберите компанию у документа", 409)
    conn = connection_of(db, company.id)
    key = _key(conn)
    if conn.status != "active":
        raise MoedeloServiceError("Ключ «Моего дела» ещё не проверен или не работает — нажмите «Сохранить и "
                                  "проверить» в карточке компании", 409, stop=True)
    with _send_lock:
        link = link_of(db, doc.id)
        if link is not None:
            raise MoedeloServiceError(f"Документ уже в «Моём деле»: {link_title(link)}", 409)
        info = plan(doc)
        if info["blockers"]:
            raise MoedeloServiceError(info["blockers"][0], 422)
        if info["kind"] != "services" and not as_services:
            raise MoedeloServiceError("Похоже на товары: накладные с товарами появятся следующим этапом. Если это "
                                      "услуги — отметьте «Это услуги» и отправьте актом", 422)
        total = round(doc.total_with_vat or 0, 2)
        try:
            kontragent_id, created = find_or_create_kontragent(key, doc)
            found = find_act(key, doc, kontragent_id)
            if found is not None:
                answer, status = found, "found"
            else:
                answer = client.request("POST", ACT_PATH, key, body=act_payload(doc, kontragent_id))
                status = "sent"
        except MoedeloError as exc:
            _mark_failed(db, company.id, exc)
            raise _from_md_error(exc) from exc
        answer = answer if isinstance(answer, dict) else {}
        sum_md = _float(answer.get("Sum"))
        message = ""
        if sum_md is not None and abs(sum_md - total) > 0.01:
            status = "mismatch" if status == "sent" else status
            message = f"В «Моём деле» сумма {_rub(sum_md)}, в документе {_rub(total)} — проверьте акт там"
        link = MoedeloLink(company_id=company.id, document_id=doc.id, md_kind="act",
                           md_id=str(answer.get("Id") or "")[:20],
                           md_number=str(answer.get("Number") or doc.doc_number or "")[:100],
                           kontragent_id=str(kontragent_id), kontragent_new=1 if created else 0,
                           sum_sent=total, sum_md=sum_md, status=status, message=message[:300],
                           created_by=user.id)
        db.add(link)
        db.commit()
    return link


def unlink(db: Session, doc: PrimaryDocument) -> bool:
    """Забыть отправку (акт удалили в «Моём деле» и нужно отправить заново)."""
    link = link_of(db, doc.id)
    if link is None:
        return False
    db.delete(link)
    db.commit()
    return True


# ------------------------------------------------------------------ пакет за период
def selection(db: Session, user: User, *, ids: list[str] | None = None, company_id: str | None = None,
              date_from: date | None = None, date_to: date | None = None) -> list[PrimaryDocument]:
    """Документы для пакета: отмеченные или за период, в области пользователя."""
    query = primary_service.scope(db.query(PrimaryDocument), user, company_id)
    if ids:
        query = query.filter(PrimaryDocument.id.in_(ids))
    else:
        if date_from:
            query = query.filter(PrimaryDocument.doc_date >= date_from)
        if date_to:
            query = query.filter(PrimaryDocument.doc_date <= date_to)
    return (query.order_by(PrimaryDocument.doc_date.is_(None), PrimaryDocument.doc_date, PrimaryDocument.created_at)
            .limit(SELECTION_LIMIT).all())


def batch_check(db: Session, docs: list[PrimaryDocument]) -> dict:
    """Что уйдёт пакетом и почему остальное пропущено. База не меняется, в «Моё дело» не ходим."""
    ids = [d.id for d in docs]
    sent = {row.document_id for row in db.query(MoedeloLink.document_id).filter(MoedeloLink.document_id.in_(ids))} \
        if ids else set()
    ready, skipped, goods = [], [], 0
    for doc in docs:
        if doc.id in sent:
            continue
        info = plan(doc)
        title = primary_service.title(doc)
        if info["blockers"]:
            skipped.append({"id": doc.id, "title": title, "reason": info["blockers"][0]})
        elif info["kind"] != "services":
            goods += 1
            skipped.append({"id": doc.id, "title": title,
                            "reason": "товары — накладные появятся следующим этапом; если это услуги, отправьте "
                                      "из карточки документа с отметкой «Это услуги»"})
        else:
            ready.append({"id": doc.id, "title": title, "total": doc.total_with_vat})
    return {"count": min(len(ready), BATCH_LIMIT), "ready": ready[:BATCH_LIMIT],
            "more": max(0, len(ready) - BATCH_LIMIT), "already": len(sent), "goods": goods,
            "skipped": skipped[:50], "skipped_total": len(skipped), "limit": BATCH_LIMIT}


def batch_send(db: Session, user: User, docs: list[PrimaryDocument]) -> dict:
    """Отправить готовые документы по одному; ошибка ключа или связи останавливает пакет."""
    summary = batch_check(db, docs)
    by_id = {d.id: d for d in docs}
    out = {"sent": 0, "found": 0, "mismatch": 0, "failed": [], "stopped": "", "already": summary["already"],
           "skipped": summary["skipped_total"], "more": summary["more"]}
    for row in summary["ready"]:
        try:
            link = send_document(db, user, by_id[row["id"]])
        except MoedeloServiceError as exc:
            out["failed"].append({"id": row["id"], "title": row["title"], "reason": str(exc)})
            if exc.stop:
                out["stopped"] = str(exc)
                break
            continue
        out[link.status] += 1
    return out
