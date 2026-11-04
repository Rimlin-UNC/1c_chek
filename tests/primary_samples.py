"""Синтетические электронные УПД и счета-фактуры (форматы 5.01 и 5.03) для тестов T-53.

Только выдуманные реквизиты (ADR-14): ИНН собираются из случайных цифр с правильной
контрольной суммой, наименования условные.
"""
from __future__ import annotations

import random
from decimal import Decimal
from xml.sax.saxutils import escape

# v1.70.96: цифры — из энтропии ОС, а не из общего генератора random: состояние random в тестах
# может совпасть, и тогда совпадали ИНН компаний (409 «Компания с таким ИНН уже есть» в CI)
_RNG = random.SystemRandom()
_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _control(digits: str, weights: tuple) -> str:
    return str(sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10)


def inn10(base9: str | None = None) -> str:
    base9 = base9 or "77" + "".join(_RNG.choice("0123456789") for _ in range(7))
    return base9 + _control(base9, _W10)


def inn12(base10: str | None = None) -> str:
    base10 = base10 or "50" + "".join(_RNG.choice("0123456789") for _ in range(8))
    first = base10 + _control(base10, _W11)
    return first + _control(first, _W12)


def broken(inn: str) -> str:
    """Тот же ИНН с испорченной последней цифрой."""
    return inn[:-1] + str((int(inn[-1]) + 1) % 10)


SELLER_INN = inn10("500100732")
CEMENT = {"name": "Цемент М500, мешок 50 кг", "unit": "796", "unit_name": "шт", "qty": "10", "price": "500.00",
          "base": "5000.00", "rate": "22%", "vat": "1100.00", "total": "6100.00", "code": "CEM-500",
          "kiz": "010460000000000021SYNTHETIC"}
DELIVERY = {"name": "Доставка до объекта", "unit": "796", "unit_name": "шт", "qty": "1", "price": "1500.00",
            "base": "1500.00", "rate": "22%", "vat": "330.00", "total": "1830.00", "code": ""}


def _attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _party(tag: str, party, version: str = "5.03") -> str:
    if party is None:
        return ""
    kind = party[0]
    if kind == "ИП":
        surname, name, patronymic = party[2]
        ident = (f'<СвИП ИННФЛ="{party[1]}"><ФИО Фамилия="{_attr(surname)}" Имя="{_attr(name)}" '
                 f'Отчество="{_attr(patronymic)}"/></СвИП>')
    elif kind == "ИН" and version == "5.03":   # v1.70.95: в 5.03 — «Наим», статус и страна обязательны
        ident = (f'<СвИнНеУч ИдСтат="ИО" КодСтр="156" НаимСтран="Китай" Наим="{_attr(party[1])}" '
                 f'Идентиф="FOREIGN-1"/>')
    elif kind == "ИН":
        ident = f'<СвИнНеУч НаимОрг="{_attr(party[1])}" Идентиф="FOREIGN-1"/>'
    else:
        ident = f'<СвЮЛУч НаимОрг="{_attr(party[0])}" ИННЮЛ="{party[1]}" КПП="{party[2]}"/>'
    return (f"<{tag}><ИдСв>{ident}</ИдСв><Адрес><АдрИнф КодСтр=\"643\" НаимСтран=\"Россия\" "
            f"АдрТекст=\"г. Тест\"/></Адрес></{tag}>")


def _line(position: int, ln: dict) -> str:
    vat = ("<БезНДС>без НДС</БезНДС>" if ln["vat"] == "без НДС" else f"<СумНал>{ln['vat']}</СумНал>")
    attrs = {"НомСтр": str(position), "НаимТов": ln["name"], "ОКЕИ_Тов": ln.get("unit", ""),
             "НаимЕдИзм": ln.get("unit_name", ""), "КолТов": ln.get("qty", ""), "ЦенаТов": ln.get("price", ""),
             "СтТовБезНДС": ln.get("base", ""), "НалСт": ln["rate"], "СтТовУчНал": ln.get("total", "")}
    head = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items() if v != "")
    kiz = (f"<НомСредИдентТов><КИЗ>{ln['kiz']}</КИЗ></НомСредИдентТов>" if ln.get("kiz") else "")
    extra = f'<ДопСведТов КодТов="{_attr(ln["code"])}">{kiz}</ДопСведТов>' if ln.get("code") or kiz else ""
    # Порядок по схеме ФНС 5.03: ДопСведТов → Акциз → СумНал (v1.70.95: образцы проходят XSD)
    return (f"<СведТов {head}>{extra}<Акциз><БезАкциз>без акциза</БезАкциз></Акциз>"
            f"<СумНал>{vat}</СумНал></СведТов>")


def _sum(lines: list[dict], key: str) -> str:
    total = sum((Decimal(ln[key]) for ln in lines if ln.get(key) not in (None, "", "без НДС")), Decimal("0"))
    return f"{total:.2f}"


def upd_xml(*, version: str = "5.03", function: str = "СЧФДОП", number: str = "УТ-15",
            day: str = "10.03.2026", seller=("ООО «СтройБаза»", SELLER_INN, "500101001"),
            buyer=("ООО «Ромашка-Строй»", None, "770401001"), lines: list[dict] | None = None,
            totals: dict | None = None, correction: tuple[str, str] | None = None,
            file_id: str | None = None, currency: str = "643", encoding: str = "windows-1251",
            signer: bool = True) -> bytes:
    """Собрать документ. buyer[1] = None → ИНН покупателя подставит тест (по умолчанию случайный)."""
    lines = [dict(CEMENT), dict(DELIVERY)] if lines is None else lines
    if buyer is not None and buyer[0] not in ("ИП", "ИН") and buyer[1] is None:
        buyer = (buyer[0], inn10(), buyer[2])
    file_id = file_id or f"ON_NSCHFDOPPR_2BM-{SELLER_INN}_2BM-buyer_20260310_{_RNG.randrange(10**9)}"
    if version == "5.03":
        info_attrs = f'НомерДок="{_attr(number)}" ДатаДок="{day}"'
        corr = (f'<ИспрДок НомИспр="{correction[0]}" ДатаИспр="{correction[1]}"/>' if correction else "")
        money = f'<ДенИзм КодОКВ="{currency}" НаимОКВ="Российский рубль"/>'
    else:
        info_attrs = f'НомерСчФ="{_attr(number)}" ДатаСчФ="{day}" КодОКВ="{currency}"'
        corr = (f'<ИспрСчФ НомИспрСчФ="{correction[0]}" ДатаИспрСчФ="{correction[1]}"/>' if correction else "")
        money = ""
    t = {"base": _sum(lines, "base"), "vat": _sum(lines, "vat"), "total": _sum(lines, "total"), **(totals or {})}
    # v1.70.95: сведения о передаче и подписант — обязательные части по схеме ФНС (signer=False — без него)
    transfer = (f'<СвПродПер><СвПер СодОпер="Товары переданы" ДатаПер="{day}"><ОснПер РеквНаимДок="Договор поставки" '
                f'РеквНомерДок="12" РеквДатаДок="01.02.2026"/></СвПер></СвПродПер>'
                if function in ("СЧФДОП", "ДОП") else "")
    signer = ('<Подписант СпосПодтПолном="1" Должн="Директор"><ФИО Фамилия="Тестов" Имя="Тест"/></Подписант>'
              if signer else "")
    total_xml = ("" if totals == {} else
                 f'<ВсегоОпл СтТовБезНДСВсего="{t["base"]}" СтТовУчНалВсего="{t["total"]}">'
                 f'<СумНалВсего><СумНал>{t["vat"]}</СумНал></СумНалВсего></ВсегоОпл>')
    body = (f'<?xml version="1.0" encoding="{encoding}"?>\n'
            f'<Файл ИдФайл="{file_id}" ВерсФорм="{version}" ВерсПрог="Тестовый генератор">'
            f'<Документ КНД="1115131" Функция="{function}" ДатаИнфПр="{day}" ВремИнфПр="10.00.00" '
            f'НаимЭконСубСост="Продавец">'
            f'<СвСчФакт {info_attrs}>{corr}{_party("СвПрод", seller, version)}{_party("СвПокуп", buyer, version)}'
            f'{money}</СвСчФакт>'
            f'<ТаблСчФакт>{"".join(_line(i, ln) for i, ln in enumerate(lines, 1))}{total_xml}</ТаблСчФакт>'
            f'{transfer}{signer}</Документ></Файл>')
    return body.encode(encoding)


def buyer_of(data: bytes) -> str:
    """ИНН покупателя из собранного документа (для сверки с компанией)."""
    from app.primary import upd
    return upd.parse(data)["buyer"]["inn"]
