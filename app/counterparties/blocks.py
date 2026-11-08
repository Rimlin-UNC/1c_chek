"""Три блока реквизитов (T-80, ADR-48).

Значения берутся дословно из карточки. Пустое поле не подменяется заглушкой:
машина готовит черновик, а человек проверяет его перед подписанием.
"""
from __future__ import annotations

COUNTERPARTY_BLOCKS = (
    "Реквизиты стороны",
    "Преамбула договора",
    "Для платёжного поручения",
)


def _s(value) -> str:
    return str(value or "").strip()


def _accounts(card: dict) -> list[dict]:
    return [a for a in (card.get("accounts") or []) if isinstance(a, dict)]


def checklist(card: dict) -> list[str]:
    """Что человек должен проверить перед копированием блока."""
    result = list((card.get("risk") or {}).get("reasons") or [])
    for key, label in (("address", "Юридический адрес"),
                       ("signatory_name", "ФИО подписанта"),
                       ("signatory_basis", "Основание подписанта"),
                       ("genitive", "Наименование в родительном падеже")):
        if not _s(card.get(key)) and not _accounts(card):
            result.append(f"{label} не заполнено")
    if not _accounts(card):
        result.append("Банковский счёт не добавлен")
    return result


def build_blocks(card: dict, own_company: dict | None = None) -> dict:
    own = own_company or {}
    accounts = _accounts(card)
    acc = accounts[0] if accounts else {}
    name = _s(card.get("name_full")) or _s(card.get("name_short"))
    genitive = _s(card.get("genitive")) or name
    inn, kpp = _s(card.get("inn")), _s(card.get("kpp"))
    signatory = ", ".join(x for x in (
        _s(card.get("signatory_post")), _s(card.get("signatory_name"))) if x)
    basis = _s(card.get("signatory_basis"))
    requisites = "\n".join(x for x in (
        name,
        f"ИНН/КПП: {inn}/{kpp}" if inn or kpp else "",
        f"ОГРН: {_s(card.get('ogrn'))}" if card.get("ogrn") else "",
        f"Адрес: {_s(card.get('address'))}" if card.get("address") else "",
        f"Статус: {_s(card.get('status'))}" if card.get("status") else "",
    ) if x)
    preamble = "\n".join(x for x in (
        f"{_s(own.get('name_full')) or 'Наша организация'}, именуемое "
        "в дальнейшем «Заказчик», в лице "
        f"{_s(own.get('signatory_post')) or 'уполномоченного представителя'} "
        f"{_s(own.get('signatory_name')) or ''}, действующего на основании "
        f"{_s(own.get('signatory_basis')) or 'Устава'}, с одной стороны, и",
        f"{genitive}, именуемое в дальнейшем «Исполнитель», в лице "
        f"{signatory or 'уполномоченного представителя'}, действующего "
        f"на основании {basis or 'Устава'}, с другой стороны, "
        "заключили настоящий договор.",
        "Проверьте реквизиты, падеж и подписанта перед вставкой в документ.",
    ) if x)
    payment = "\n".join(x for x in (
        f"Получатель: {name}",
        f"ИНН/КПП: {inn}/{kpp}" if inn or kpp else "",
        f"БИК: {_s(acc.get('bik'))}" if acc.get("bik") else "",
        f"Банк: {_s(acc.get('bank_name'))}" if acc.get("bank_name") else "",
        f"Счёт №: {_s(acc.get('account'))}" if acc.get("account") else "",
        f"Корсчёт: {_s(acc.get('corr_account'))}" if acc.get("corr_account") else "",
        "Назначение платежа и сумму проверьте перед вставкой в платёжное поручение.",
    ) if x)
    return dict(zip(COUNTERPARTY_BLOCKS, (requisites, preamble, payment)))
