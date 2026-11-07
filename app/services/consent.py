# -*- coding: utf-8 -*-
"""Согласие на обработку персональных данных, полученных из MAX (152-ФЗ).

v1.70.37. Когда участник заходит в мини-приложение «Чек-Пул | Ямастер» через
MAX, сервис технически может прочитать его профиль мессенджера (имя, фамилия,
ник, фото, язык). По ст. 9 Федерального закона от 27.07.2006 № 152-ФЗ обработка
таких данных допускается только с согласия субъекта, данного свободно, своей
волей и в своём интересе, — поэтому:

  1. до согласия в карточке участника НЕ хранятся имя, фамилия, ник, фото и
     язык: сохраняется лишь идентификатор MAX, без которого начисление баллов
     невозможно (п. 5 ч. 1 ст. 6 152-ФЗ — исполнение договора/оферты сервиса);
  2. текст согласия версионируется (CONSENT_VERSION): participant соглашается с
     конкретной редакцией, и она фиксируется в карточке вместе с моментом дачи;
  3. согласие можно отозвать в любой момент — персональные поля очищаются
     (ст. 14, 21 152-ФЗ), баллы остаются в обезличенном виде, чтобы не потерять
     обязательства перед участником.

Модуль не зависит от FastAPI: его можно импортировать из тестов и скриптов.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: Версия редакции согласия. Меняется при любом изменении текста — участник
#: должен дать согласие заново, если условия обработки изменились.
CONSENT_VERSION = "1.0"

#: Оператор персональных данных. В коде — только то, что подтверждается
#: самим репозиторием (наименование, сайт, почта: они есть в README, в подвале
#: сайта и в `settings.VENDOR`). ИНН, ОГРН и юридический адрес — юридические
#: факты, которые знает владелец; выдуманные реквизиты в тексте согласия были бы
#: дефектом хуже пустого поля, поэтому они берутся ИЗ НАСТРОК (Настройки →
#: «Согласие 152-ФЗ: реквизиты оператора») и по умолчанию не заполнены.
OPERATOR_NAME = "ООО «Ямастер»"
OPERATOR_SITE = "https://ymaster.ru"

#: Куда писать по вопросам персональных данных и для отзыва согласия.
#: По умолчанию — почта из репозитория; владелец может задать отдельный ящик.
CONSENT_CONTACT_DEFAULT = "chek@ymaster.ru"

#: Ключи настроек с реквизитами оператора.
KEY_INN = "consent_operator_inn"
KEY_OGRN = "consent_operator_ogrn"
KEY_ADDRESS = "consent_operator_address"
KEY_CONTACT = "consent_contact"

#: Каналы, которыми согласие может быть получено/отозвано.
SOURCE_MAXAPP = "max-miniapp"

CONSENT_TITLE = "Согласие на обработку персональных данных"

CONSENT_LEAD = (
    "Я, действуя свободно, своей волей и в своём интересе, даю Оператору "
    "согласие на обработку моих персональных данных, полученных при входе в "
    "мини-приложение «Чек-Пул | Ямастер» через мессенджер MAX."
)

#: Разделы согласия: цель, перечень данных, действия, срок, права, отзыв.
#: Состав соответствует ч. 4 ст. 9 152-ФЗ (содержание согласия).
#: Шаблоны разделов: `{operator}` подставляется из настроек при выдаче текста.
CONSENT_SECTIONS = (
    {
        "title": "Оператор и основание",
        "items": (
            "Оператор персональных данных: {operator}.",
            "Основание обработки: ст. 9 Федерального закона от 27.07.2006 "
            "№ 152-ФЗ «О персональных данных» (согласие субъекта).",
            "Обработка осуществляется оператором самостоятельно, без передачи "
            "третьим лицам по поручению, кроме случаев, указанных в перечне "
            "действий ниже.",
        ),
    },
    {
        "title": "Цели обработки",
        "items": (
            "учёт участия в программе «Чек-Пул»: баллы, статистика сданных "
            "чеков, место в рейтинге;",
            "формирование профиля участника в личном кабинете мини-приложения "
            "и в служебных списках участников Оператора;",
            "связь с участником по вопросам участия: уведомления о "
            "начислении баллов, ответы на обращения, поддержка.",
        ),
    },
    {
        "title": "Перечень персональных данных",
        "items": (
            "идентификатор аккаунта MAX;",
            "имя и фамилия, указанные в профиле MAX;",
            "имя пользователя (ник) MAX;",
            "ссылка на фотографию профиля MAX;",
            "код языка интерфейса MAX.",
        ),
    },
    {
        "title": "Действия с данными и способы обработки",
        "items": (
            "сбор, запись, систематизация, накопление, хранение, уточнение "
            "(обновление, изменение), извлечение, использование, передача "
            "(предоставление, доступ) уполномоченным сотрудникам Оператора, "
            "обезличивание, блокирование, удаление, уничтожение;",
            "обработка осуществляется с использованием средств автоматизации "
            "на территории Российской Федерации; трансграничная передача не "
            "осуществляется.",
        ),
    },
    {
        "title": "Срок действия и отзыв согласия",
        "items": (
            "согласие действует до его отзыва;",
            "отозвать согласие можно в любой момент: кнопкой «Отозвать "
            "согласие» в кабинете мини-приложения либо письмом на адрес "
            "{contact};",
            "после отзыва обработка прекращается, а персональные данные "
            "профиля (имя, фамилия, ник, фото, язык) удаляются из карточки "
            "участника (ст. 14, 21 152-ФЗ);",
            "данные, необходимые для подтверждения начисленных баллов и "
            "исполнения обязательств перед участником, а также сведения, "
            "хранение которых требуется законом, обезличиваются и хранятся в "
            "объёме, предусмотренном п. 4 и п. 6 ч. 1 ст. 6 152-ФЗ.",
        ),
    },
    {
        "title": "Права субъекта персональных данных",
        "items": (
            "получать сведения об обработке моих персональных данных "
            "(ст. 14 152-ФЗ);",
            "требовать уточнения, блокирования или уничтожения данных "
            "(ст. 14, 21 152-ФЗ);",
            "обжаловать действия Оператора в уполномоченный орган по защите "
            "прав субъектов персональных данных (Роскомнадзор) или в суд "
            "(ст. 17 152-ФЗ).",
        ),
    },
    {
        "title": "Что остаётся вне этого согласия",
        "items": (
            "технические данные (IP-адрес, сведения об устройстве и "
            "браузере/клиенте) обрабатываются отдельно — в целях обеспечения "
            "безопасности и противодействия злоупотреблениям (п. 6 ч. 1 "
            "ст. 6 152-ФЗ) и хранятся не более 12 месяцев;",
            "данные чеков (номер, сумма, дата, ИНН продавца) не являются "
            "персональными данными участника и обрабатываются как сведения о "
            "сделке.",
        ),
    },
)

#: Поля профиля, которые заполняются ТОЛЬКО после согласия.
PROFILE_FIELDS = ("first_name", "last_name", "photo_url", "language")


def requisites(db=None) -> dict:
    """Реквизиты оператора из настроек (заполняет администратор).

    `db` может быть None — тогда возвращаются значения по умолчанию: текст
    согласия остаётся полным по составу, а вместо реквизитов указывается
    общеизвестный источник (ЕГРЮЛ) и почта для запроса.
    """
    def get(key: str) -> str:
        if db is None:
            return ""
        from . import appsettings
        return (appsettings.get_setting(db, key, "") or "").strip()

    return {
        "inn": get(KEY_INN)[:20],
        "ogrn": get(KEY_OGRN)[:20],
        "address": get(KEY_ADDRESS)[:300],
        "contact": get(KEY_CONTACT)[:120] or CONSENT_CONTACT_DEFAULT,
    }


def operator_line(db=None) -> str:
    """Строка «Оператор: …» — из настроек, без выдуманных значений."""
    r = requisites(db)
    parts = [OPERATOR_NAME]
    if r["inn"]:
        parts.append("ИНН " + r["inn"])
    if r["ogrn"]:
        parts.append("ОГРН " + r["ogrn"])
    line = ", ".join(parts)
    if r["address"]:
        return line + ", адрес: " + r["address"]
    # адреса в настройках нет: не выдумываем, а честно указываем источник
    return (f"{line} ({OPERATOR_SITE}); адрес оператора опубликован в ЕГРЮЛ "
            f"и предоставляется по запросу на {r['contact']}")


def requisites_complete(db=None) -> bool:
    """Полнота по п. 2 ч. 4 ст. 9 152-ФЗ: наименование, адрес и реквизиты."""
    r = requisites(db)
    return bool(r["inn"] and r["ogrn"] and r["address"])


def sections(db=None) -> list[dict]:
    """Разделы согласия с подставленными реквизитами и почтой."""
    r = requisites(db)
    op = operator_line(db)
    return [{"title": s["title"],
             "items": [i.format(operator=op, contact=r["contact"])
                       for i in s["items"]]}
            for s in CONSENT_SECTIONS]


def consent_payload(db=None) -> dict:
    """Структура согласия для клиента (мини-приложение, документация, тесты)."""
    r = requisites(db)
    return {
        "version": CONSENT_VERSION,
        "title": CONSENT_TITLE,
        "lead": CONSENT_LEAD,
        "operator": operator_line(db),
        "contact": r["contact"],
        "requisites_complete": requisites_complete(db),
        "sections": sections(db),
        "text": full_text(db),
    }


def full_text(db=None) -> str:
    """Плоский текст согласия — для журнала, выгрузки и сверки редакции."""
    parts = [CONSENT_TITLE, f"Редакция {CONSENT_VERSION}", "", CONSENT_LEAD]
    for s in sections(db):
        parts.append("")
        parts.append(s["title"] + ":")
        parts.extend("— " + i + ";" for i in s["items"])
    return "\n".join(parts)


def consent_state(user) -> dict:
    """Состояние согласия участника (для клиента и администратора)."""
    given = bool(getattr(user, "consent_at", None))
    return {
        "given": given,
        "version": (user.consent_version or "") if given else "",
        "source": (user.consent_source or "") if given else "",
        "at": (user.consent_at.strftime("%Y-%m-%dT%H:%M:%SZ") if given else ""),
        "current_version": CONSENT_VERSION,
        # редакция изменилась — нужно согласие заново
        "stale": given and (user.consent_version or "") != CONSENT_VERSION,
    }


def profile_dict(user) -> dict:
    """Профиль участника: пустой, пока согласие не дано (152-ФЗ)."""
    if not getattr(user, "consent_at", None):
        return {"first_name": "", "last_name": "", "photo_url": "",
                "language": ""}
    return {
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "photo_url": user.photo_url or "",
        "language": user.language or "",
    }


def fill_profile(user, raw: dict) -> dict:
    """Заполнить карточку данными из MAX. ВЫЗЫВАТЬ ТОЛЬКО ПОСЛЕ согласия.

    Возвращает список реально изменённых полей — для журнала.
    Фото принимаем только по https: ссылка хранится как есть, картинку
    показывает клиент (сервер её не скачивает и не хранит).
    """
    if not getattr(user, "consent_at", None):
        log.warning("consent: профиль не заполнен — нет согласия (%s)",
                    getattr(user, "id", "?"))
        return {}
    changed = {}
    fname = str(raw.get("first_name") or "").strip()[:64]
    lname = str(raw.get("last_name") or "").strip()[:64]
    nick = str(raw.get("username") or "").strip()[:64]
    photo = str(raw.get("photo_url") or "").strip()[:512]
    lang = str(raw.get("language_code") or "").strip()[:8]
    if not photo.startswith("https://"):
        photo = ""                       # только защищённый источник
    for field, value in (("first_name", fname), ("last_name", lname),
                         ("tg_username", nick), ("photo_url", photo),
                         ("language", lang)):
        if value and getattr(user, field, "") != value:
            setattr(user, field, value)
            changed[field] = value
    return changed


def record_consent(db, user, source: str = SOURCE_MAXAPP,
                   raw: dict | None = None) -> dict:
    """Зафиксировать согласие текущей редакции и заполнить профиль."""
    user.consent_at = datetime.now(timezone.utc).replace(tzinfo=None,
                                                         microsecond=0)
    user.consent_version = CONSENT_VERSION
    user.consent_source = (source or SOURCE_MAXAPP)[:16]
    changed = fill_profile(user, raw or {})
    if not (user.reg_source or ""):
        user.reg_source = "max"
    db.commit()
    log.info("consent: согласие %s получено (участник %s), заполнено полей: %d",
             CONSENT_VERSION, user.id, len(changed))
    return {"ok": True, "consent": consent_state(user), "filled": sorted(changed)}


def revoke_consent(db, user) -> dict:
    """Отзыв согласия: персональные поля очищаются, баллы остаются (ст. 21)."""
    cleared = [f for f in PROFILE_FIELDS if getattr(user, f, "")]
    user.first_name = ""
    user.last_name = ""
    user.photo_url = ""
    user.language = ""
    if (user.reg_source or "") == "max":
        # ник пришёл из MAX — тоже персональные данные
        if user.tg_username:
            cleared.append("tg_username")
        user.tg_username = ""
    user.consent_at = None
    user.consent_version = ""
    user.consent_source = ""
    db.commit()
    log.info("consent: согласие отозвано (участник %s), очищено полей: %d",
             user.id, len(cleared))
    return {"ok": True, "consent": consent_state(user), "cleared": sorted(cleared),
            "message": "Согласие отозвано: имя, фамилия, ник и фото удалены из "
                       "карточки. Баллы и история чеков сохранены в "
                       "обезличенном виде."}
