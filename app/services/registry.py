# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — свежие данные ЕГРЮЛ/ЕГРИП: скачивание выписки (v1.22.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru
#
# Механизм: кнопка в карточке компании «⬇ Скачать свежие данные».
# Источники (по порядку):
#   1. Официальный сервис ФНС egrul.nalog.ru — PDF-выписка ЕГРЮЛ/ЕГРИП
#      (публичный сценарий: POST-поиск по ИНН → токен → PDF).
#   2. Checko.ru API (уже подключён ключом) — полная карточка, из которой
#      формируется печатная HTML-выписка в фирменном стиле.
# Если источник недоступен с сервера — честная ошибка с подсказкой.
# ======================================================================
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request

FNS_EGRUL_URL = "https://egrul.nalog.ru/"
_UA = "YmasterCheck/1.22 (+https://ymaster.ru)"

# --------------------------------------------------------------------------
#  1. Официальный сервис ФНС: PDF-выписка ЕГРЮЛ/ЕГРИП
# --------------------------------------------------------------------------
def fetch_fns_pdf(inn: str, timeout: int = 20) -> bytes | None:
    """PDF-выписка с egrul.nalog.ru по ИНН. Возвращает bytes PDF или None,
    если сервис недоступен/не дал результат (никогда не бросает)."""
    try:
        data = urllib.parse.urlencode(
            {"vyp3Cookie": "", "query": inn, "mode": "default", "page": "1"}
        ).encode()
        req = urllib.request.Request(
            FNS_EGRUL_URL, data=data, method="POST",
            headers={"User-Agent": _UA,
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            token = (json.loads(r.read().decode("utf-8", "replace")) or {}).get("t")
        if not token:
            return None
        # результаты поиска: rows[0].t → PDF-ссылка
        req2 = urllib.request.Request(
            FNS_EGRUL_URL + "search-result/" + token,
            headers={"User-Agent": _UA})
        with urllib.request.urlopen(req2, timeout=timeout) as r:
            rows = (json.loads(r.read().decode("utf-8", "replace")) or {}).get("rows") or []
        if not rows or not rows[0].get("t"):
            return None
        req3 = urllib.request.Request(
            FNS_EGRUL_URL + "search-result/" + rows[0]["t"],
            headers={"User-Agent": _UA})
        with urllib.request.urlopen(req3, timeout=timeout) as r:
            blob = r.read()
        # валидация: это действительно PDF
        if blob[:5] == b"%PDF-" and len(blob) > 1000:
            return blob
        return None
    except Exception:                                        # noqa: BLE001
        return None


# --------------------------------------------------------------------------
#  2. HTML-выписка из карточки Checko (ЕГРЮЛ/ЕГРИП) — печатная, фирменная
# --------------------------------------------------------------------------
def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _row(label: str, value) -> str:
    if value in (None, "", []):
        return ""
    return (f"<tr><th>{_esc(label)}</th>"
            f"<td>{_esc(value)}</td></tr>")


def build_excerpt_html(card: dict, comp_name: str, comp_inn: str,
                       source: str = "Checko.ru API") -> str:
    """Печатная выписка по данным карточки (ru-ключи Checko уже сведены
    в стабильные английские ключи card). Открывается в браузере/печатается."""
    card = card or {}
    kind = card.get("kind", "legal")
    is_ip = kind == "individual"
    today = dt.date.today().strftime("%d.%m.%Y")
    rows = "".join([
        _row("Полное наименование", card.get("name_full")),
        _row("Сокращённое наименование", card.get("name_short")),
        _row("ИНН", card.get("inn") or comp_inn),
        _row("КПП", card.get("kpp")),
        _row("ОГРН" if not is_ip else "ОГРНИП", card.get("ogrn")),
        _row("ОКПО", card.get("okpo")),
        _row("Статус", card.get("status")),
        _row("Дата регистрации", card.get("reg_date")),
        _row("Дата записи ОГРН", card.get("ogrn_date")),
        _row("Регион", card.get("region")),
        _row("Адрес", card.get("address")),
        _row("ОКВЭД (основной)", card.get("okved")),
        _row("Руководитель", (f"{card.get('director')} "
                              f"({card.get('management_post')})"
                              if card.get("director") else "")),
        _row("Уставный капитал", card.get("capital")),
        _row("Учредителей", card.get("founders_count")),
        _row("Филиалов и представительств", card.get("branches_count")),
        _row("ИФНС", (f"{card.get('tax_office')} "
                      f"(код {card.get('tax_office_code')})"
                      if card.get("tax_office") else "")),
    ])
    warn = ""
    if card.get("address_invalid"):
        warn += ("<p class='warn'>⚠ Адрес признан недостоверным"
                 + (": " + _esc(card.get("address_invalid_note"))
                    if card.get("address_invalid_note") else "") + "</p>")
    if (card.get("mass_address_count") or 0) >= 10:
        warn += ("<p class='warn'>⚠ Признак массового адреса: "
                 f"{card['mass_address_count']} организаций по тому же адресу</p>")
    okved_extra = card.get("okved_extra") or []
    extra = ("".join(f"<span class='tag'>{_esc(x)}</span>" for x in okved_extra[:12])
             if okved_extra else "")
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Сведения ЕГР{'ИП' if is_ip else 'ЮЛ'} — {_esc(card.get('name_short') or comp_name)}</title>
<style>
  body {{ font-family: "Segoe UI", Arial, sans-serif; color: #333333;
          max-width: 820px; margin: 24px auto; padding: 0 16px; line-height: 1.5; }}
  .head {{ border-bottom: 3px solid #FF7A00; padding-bottom: 10px; margin-bottom: 6px; }}
  .brand {{ color: #4B0082; font-size: 13px; letter-spacing: .04em;
            text-transform: uppercase; font-weight: 600; }}
  h1 {{ font-size: 22px; margin: 6px 0 2px; color: #4B0082; }}
  .sub {{ color: #6b6b6b; font-size: 13px; margin-bottom: 14px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ border: 1px solid #e2e2e2; padding: 7px 10px; text-align: left;
            vertical-align: top; }}
  th {{ background: #f5f5f5; width: 34%; font-weight: 600; }}
  td {{ font-variant-numeric: tabular-nums; }}
  .warn {{ color: #d9534f; font-weight: 600; }}
  .tag {{ display: inline-block; background: #f1ecf7; color: #4B0082;
          border-radius: 6px; padding: 1px 8px; margin: 2px 4px 2px 0;
          font-size: 12.5px; }}
  .foot {{ margin-top: 18px; padding-top: 8px; border-top: 1px dashed #ccc;
           color: #6b6b6b; font-size: 12px; }}
  @media print {{ body {{ margin: 0; }} }}
</style></head><body>
<div class="head"><div class="brand">Ямастер Чек · ООО «Ямастер» · ymaster.ru</div>
<h1>Сведения из ЕГР{'ИП' if is_ip else 'ЮЛ'}</h1>
<div class="sub">по данным {source} · сформировано {today} ·
компания в системе: «{_esc(comp_name)}»</div></div>
<table>{rows}</table>
{warn}
{f'<p style="margin-top:10px"><b>Дополнительные ОКВЭД:</b><br>{extra}</p>' if extra else ''}
<div class="foot">Документ сформирован системой «Ямастер Чек» по свежим данным
реестра ({source}, ИНН {_esc(comp_inn or card.get('inn') or '—')}).
Источники: egrul.nalog.ru (ФНС России), api.checko.ru.
Выписка носит информационный характер; официальную выписку в форме PDF
можно получить на сайте ФНС.</div>
</body></html>"""


def content_disposition(filename: str) -> str:
    """Заголовок Attachment с корректной UTF-8-кодировкой имени файла.
    ASCII-fallback сохраняет расширение, чтобы браузер сразу понимал тип."""
    q = urllib.parse.quote(filename)
    ext = ("." + filename.rsplit(".", 1)[1]) if "." in filename else ""
    ascii_name = f"registry_{dt.date.today():%Y%m%d}{ext}"
    return (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{q}")
