# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — проверки статуса компании для доступа к платному API
# (v1.70.45, контур B2B).
#
# Решение владельца (verify_method=email_plus_checko): статус компании
# подтверждается КОРПОРАТИВНОЙ ПОЧТОЙ (переход по ссылке из письма) И
# карточкой ЕГРЮЛ/ЕГРИП по ИНН через Checko. В карточке реестра адреса
# почты нет — поэтому реестр подтверждает, что организация существует,
# действующая и называется так, как указал заявитель, а ящик на своём
# домене подтверждает, что заявитель её представляет.
#
# Если ключ Checko не настроен, проверка реестра становится МЯГКОЙ:
# формальные признаки ИНН (контрольная сумма, 10 цифр = ООО, 12 = ИП)
# и явная пометка в журнале — администратор видит, что реестр не
# запрашивался, и решает вручную.
#
# Каждая проверка возвращает словарь {kind, ok, hard, weight, detail} и
# пишется в b2b_checks (история); вердикт считается по последним
# результатам каждого вида.
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from ..services import checko
from ..services.appsettings import get_setting
from ..services.companies_util import (
    canonical_name, inn_is_valid, inn_kind, normalize_inn, risk_assessment,
    similar_ratio,
)
from .models import (
    B2bCheck, B2bCompany, CK_DUPLICATE, CK_EMAIL_DOMAIN, CK_EMAIL_FORMAT,
    CK_INN_FORMAT, CK_REGISTRY, CK_RISK, KIND_IP, KIND_UL, _utcnow,
)

log = logging.getLogger("ymaster.b2b.verify")

# Бесплатные почтовые службы: такой домен не доказывает принадлежность к
# компании, поэтому по умолчанию не принимается (администратор может
# разрешить исключения переключателем b2b_allow_free_domains).
FREE_MAIL_DOMAINS = frozenset({
    "mail.ru", "list.ru", "bk.ru", "inbox.ru", "internet.ru",
    "yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "ya.ru",
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "rambler.ru", "lenta.ru", "myrambler.ru", "ro.ru",
    "mail.ua", "ukr.net", "i.ua", "seznam.cz",
    "proton.me", "protonmail.com", "pm.me",
    "aol.com", "gmx.com", "gmx.de", "zoho.com", "fastmail.com",
    "mail.com", "email.com", "yahoo.com", "qq.com", "163.com",
})

# Соответствие наименований: полное наименование в ЕГРЮЛ длиннее того, что
# пишет заявитель («ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЯМАСТЕР"» против
# «ООО «Ямастер»»), поэтому сравниваем ЯДРА названий без юридических форм.
NAME_MATCH_MIN = 0.60

# Юридические формы и шум, которые не участвуют в сравнении названий
_LEGAL_NOISE = (
    "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ",
    "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО",
    "НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО",
    "ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО",
    "ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО",
    "АКЦИОНЕРНОЕ ОБЩЕСТВО",
    "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ",
    "ОБЩЕСТВО С ДОПОЛНИТЕЛЬНОЙ ОТВЕТСТВЕННОСТЬЮ",
    "УНИТАРНОЕ ПРЕДПРИЯТИЕ",
    "ОБЩЕСТВО", "ТОВАРИЩЕСТВО", "ПРЕДПРИЯТИЕ", "КООПЕРАТИВ", "ФОНД",
    "ООО", "ОАО", "ЗАО", "ПАО", "АО", "ИП", "ТОО", "ГУП", "МУП", "ФГУП",
)
_PUNCT = re.compile(r"[^0-9A-ZА-ЯЁ]+")


def email_domain(email: str) -> str:
    """Домен почтового адреса (нижний регистр, без пробелов)."""
    value = (email or "").strip().lower()
    return value.rsplit("@", 1)[1] if "@" in value else ""


def is_free_domain(email: str) -> bool:
    return email_domain(email) in FREE_MAIL_DOMAINS


def name_core(name: str) -> str:
    """Ядро названия без юридической формы и знаков препинания."""
    canon = canonical_name(name)
    for noise in _LEGAL_NOISE:
        canon = canon.replace(noise, " ")
    return _PUNCT.sub(" ", canon).strip()


def names_match(registry_name: str, given_name: str,
                kind: str = "") -> tuple[bool, float, str]:
    """Совпадает ли указанное наименование с наименованием в реестре.

    Возвращает (совпало, похожесть, пояснение). Для ИП достаточно совпадения
    фамилии: в реестре хранится «Иванов Иван Иванович», а заявитель пишет
    «ИП Иванов И. И.» — полное сравнение дало бы ложный отказ.
    """
    core_r, core_g = name_core(registry_name), name_core(given_name)
    if not core_r or not core_g:
        return False, 0.0, "Наименование в реестре или в заявке пустое"
    ratio = similar_ratio(core_r, core_g)
    if ratio >= NAME_MATCH_MIN:
        return True, ratio, f"Наименования совпадают (похожесть {ratio:.2f})"
    tokens_g = core_g.split()
    tokens_r = core_r.split()
    if tokens_g and tokens_g[0] in tokens_r:
        return True, ratio, (f"Совпало ключевое слово названия «{tokens_g[0]}» "
                             f"(похожесть {ratio:.2f})")
    if kind in (KIND_IP, "individual") and tokens_g:
        # фамилия ИП: первое слово заявки ищется в ФИО из реестра
        if any(t.startswith(tokens_g[0][:4]) for t in tokens_r if len(t) >= 4):
            return True, ratio, "Фамилия предпринимателя совпадает с реестром"
    return False, ratio, (f"Наименование «{given_name}» не совпадает с реестром "
                          f"«{registry_name}» (похожесть {ratio:.2f})")


def _row(kind: str, ok: bool, detail: str, hard: bool = True,
         weight: int = 0) -> dict:
    return {"kind": kind, "ok": bool(ok), "hard": bool(hard),
            "weight": int(weight), "detail": detail[:500]}


# --------------------------------------------------------------------------
# Отдельные проверки
# --------------------------------------------------------------------------
def check_email_format(email: str) -> dict:
    value = (email or "").strip()
    if not value or len(value) > 254 or "@" not in value:
        return _row(CK_EMAIL_FORMAT, False, "Укажите корпоративный e-mail")
    local, _, domain = value.rpartition("@")
    if not local or "." not in domain or len(domain) < 4:
        return _row(CK_EMAIL_FORMAT, False, "E-mail неполный: нужен адрес вида "
                                            "имя@доменкомпании.ru")
    return _row(CK_EMAIL_FORMAT, True, f"Адрес {value} корректен")


def check_email_domain(email: str, allow_free: bool = False) -> dict:
    domain = email_domain(email)
    if not domain:
        return _row(CK_EMAIL_DOMAIN, False, "Не определён почтовый домен")
    if domain in FREE_MAIL_DOMAINS:
        if allow_free:
            return _row(CK_EMAIL_DOMAIN, True,
                        f"Домен {domain} — бесплатная почта; разрешено "
                        "настройкой администратора", hard=False)
        return _row(CK_EMAIL_DOMAIN, False,
                    f"Нужна почта компании на своём домене, а не {domain}. "
                    "Бесплатный ящик не подтверждает, что вы представляете "
                    "организацию")
    return _row(CK_EMAIL_DOMAIN, True, f"Корпоративный домен {domain}")


def check_inn(inn: str) -> dict:
    digits = normalize_inn(inn)
    if not digits:
        return _row(CK_INN_FORMAT, False, "ИНН не указан")
    if not inn_is_valid(digits):
        return _row(CK_INN_FORMAT, False,
                    f"ИНН {digits} не проходит контрольную сумму")
    kind = inn_kind(digits)
    if kind == "legal":
        return _row(CK_INN_FORMAT, True, f"ИНН {digits}: юридическое лицо (10 цифр)")
    if kind == "individual":
        return _row(CK_INN_FORMAT, True,
                    f"ИНН {digits}: индивидуальный предприниматель (12 цифр)")
    return _row(CK_INN_FORMAT, False, f"ИНН {digits}: неизвестный формат")


def kind_title(kind_code: str) -> str:
    return KIND_UL if kind_code == "legal" else (
        KIND_IP if kind_code == "individual" else "")


def status_is_active(status: str) -> bool:
    """Статус в реестре: действующая организация (не ликвидация/банкротство)."""
    text = (status or "").lower()
    if not text.strip():
        return False
    if any(bad in text for bad in ("ликвид", "прекрат", "банкрот", "исключен",
                                   "недейств")):
        return False
    return "действ" in text or "работа" in text or "актив" in text


def check_registry(db: Session, company: B2bCompany,
                   require_registry: bool = True) -> tuple[dict, dict | None]:
    """Карточка ЕГРЮЛ/ЕГРИП по ИНН. Возвращает (проверка, карточка).

    Ключ Checko не настроен → мягкая проверка по формальным признакам ИНН с
    явной пометкой: администратор видит, что реестр не запрашивался.
    """
    inn = normalize_inn(company.inn)
    key = (get_setting(db, checko.SETTING_KEY, "") or "").strip()
    if not key:
        title = KIND_UL if inn_kind(inn) == "legal" else KIND_IP
        detail = ("Ключ Checko не настроен — реестр не запрашивался. "
                  "Проверка по формальным признакам ИНН: контрольная сумма "
                  f"и тип ({title or 'не определён'})")
        # Мягкая проверка: не блокирует, но администратор видит пометку
        # «реестр не запрашивался» и решает сам (режим «частичная»).
        return _row(CK_REGISTRY, True, detail, hard=False, weight=0), None
    try:
        result = checko.fetch_card(key, inn)
    except checko.CheckoError as exc:
        return _row(CK_REGISTRY, False, f"Реестр не подтвердил ИНН {inn}: {exc}"), None
    except Exception as exc:  # noqa: BLE001 — сбой сети не должен ломать приём заявки
        log.warning("b2b registry check failed for %s: %s", inn, exc)
        return _row(CK_REGISTRY, False,
                    "Не удалось запросить реестр (сеть или ключ). "
                    "Повторите проверку позже"), None

    card = (result or {}).get("card") or {}
    if not card.get("name_full"):
        return _row(CK_REGISTRY, False,
                    f"В реестре нет организации с ИНН {inn}"), None
    if not status_is_active(card.get("status", "")):
        return _row(CK_REGISTRY, False,
                    f"Организация не действующая: статус «{card.get('status') or 'не указан'}»"), card
    ok, ratio, note = names_match(card.get("name_full", ""),
                                  company.company_name, card.get("kind", ""))
    if not ok:
        return _row(CK_REGISTRY, False, note, weight=10), card
    return _row(CK_REGISTRY, True,
                f"ЕГРЮЛ/ЕГРИП: {card.get('name_full')} — статус "
                f"«{card.get('status')}», ОГРН {card.get('ogrn') or '—'}. {note}"), card


def check_duplicates(db: Session, company: B2bCompany) -> dict:
    """Дубликаты в контуре: тот же ИНН или та же почта уже зарегистрированы."""
    inn = normalize_inn(company.inn)
    if inn:
        same = (db.query(B2bCompany)
                .filter(B2bCompany.inn == inn, B2bCompany.id != company.id)
                .first())
        if same is not None:
            return _row(CK_DUPLICATE, False,
                        f"ИНН {inn} уже зарегистрирован на {same.email} — "
                        "войдите в тот кабинет или напишите нам")
    same_mail = (db.query(B2bCompany)
                 .filter(B2bCompany.email == company.email,
                         B2bCompany.id != company.id).first())
    if same_mail is not None:
        return _row(CK_DUPLICATE, False,
                    f"Почта {company.email} уже используется другим кабинетом")
    return _row(CK_DUPLICATE, True, "Дубликатов по ИНН и почте нет")


def check_risk(card: dict | None, inn: str) -> dict:
    """Светофор риска. Красный — не автоматический отказ, а повод для админа."""
    verdict = risk_assessment(card, inn)
    level = verdict.get("level", "none")
    reasons = "; ".join(verdict.get("reasons") or []) or "данных для оценки нет"
    weight = {"red": 40, "yellow": 15, "none": 5}.get(level, 0)
    if level == "red":
        return _row(CK_RISK, False, f"Высокий риск: {reasons}",
                    hard=False, weight=weight)
    return _row(CK_RISK, True, f"Риск: {level} — {reasons}",
                hard=False, weight=weight)


# --------------------------------------------------------------------------
# Прогон проверок и вердикт
# --------------------------------------------------------------------------
def run_checks(db: Session, company: B2bCompany, *,
               allow_free_domains: bool = False,
               require_registry: bool = True) -> tuple[list[dict], dict | None]:
    """Выполняет все проверки компании и пишет их в журнал (история).

    Возвращает (список результатов, карточка реестра или None).
    Commit не делает — транзакцию ведёт вызывающий (конвейер).
    """
    rows: list[dict] = [
        check_email_format(company.email),
        check_email_domain(company.email, allow_free=allow_free_domains),
        check_inn(company.inn),
    ]
    reg, card = check_registry(db, company, require_registry=require_registry)
    rows.append(reg)
    rows.append(check_duplicates(db, company))
    rows.append(check_risk(card, normalize_inn(company.inn)))

    for row in rows:
        db.add(B2bCheck(company_id=company.id, kind=row["kind"], ok=row["ok"],
                        hard=row["hard"], weight=row["weight"],
                        detail=row["detail"], created_at=_utcnow()))
    # тип организации и риск сохраняем на карточке компании
    code = inn_kind(normalize_inn(company.inn)) or ""
    company.kind = kind_title(code)
    company.risk_score = sum(r["weight"] for r in rows)
    if card:
        company.card_json = json.dumps(card, ensure_ascii=False)[:60000]
    return rows, card


def latest_checks(db: Session, company_id: str) -> list[dict]:
    """Последние результаты каждого вида проверки (для кабинета и админа)."""
    rows = (db.query(B2bCheck).filter(B2bCheck.company_id == company_id)
            .order_by(B2bCheck.created_at.asc(), B2bCheck.id.asc()).all())
    by_kind: dict[str, B2bCheck] = {}
    for row in rows:
        by_kind[row.kind] = row
    order = (CK_EMAIL_FORMAT, CK_EMAIL_DOMAIN, CK_INN_FORMAT, CK_REGISTRY,
             CK_DUPLICATE, CK_RISK)
    return [by_kind[k].to_dict() for k in order if k in by_kind]


def latest_checks_many(db: Session, company_ids: list[str]) -> dict[str, list]:
    """Последние проверки сразу для нескольких компаний (без N+1 запросов)."""
    ids = [c for c in company_ids if c]
    if not ids:
        return {}
    rows = (db.query(B2bCheck).filter(B2bCheck.company_id.in_(ids))
            .order_by(B2bCheck.created_at.asc(), B2bCheck.id.asc()).all())
    order = (CK_EMAIL_FORMAT, CK_EMAIL_DOMAIN, CK_INN_FORMAT, CK_REGISTRY,
             CK_DUPLICATE, CK_RISK)
    by_company: dict[str, dict[str, B2bCheck]] = {}
    for row in rows:
        by_company.setdefault(row.company_id, {})[row.kind] = row
    return {cid: [by_company[cid][k].to_dict() for k in order
                  if k in by_company[cid]] for cid in by_company}


def verdict(checks: list[dict]) -> tuple[bool, list[str]]:
    """Итог по результатам проверок: (пройдено, причины отказа).

    Обязательные (hard) проверки должны быть зелёными; мягкие влияют на
    риск и уходят администратору, но сами не блокируют.
    """
    problems = [f"{c.get('title') or c.get('kind')}: {c.get('detail')}"
                for c in checks if not c.get("ok") and c.get("hard")]
    return (not problems), problems
