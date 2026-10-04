# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — человекочитаемый журнал действий (v1.19.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru
#
# Превращает технические события аудита (action + details) в понятные
# фразы: «отсканирован чек на 1 250,00 ₽» + комментарий в кавычках
# («ФД 55667, источник: фото»). Единый источник формулировок для ленты
# дашборда и журнала «последние 60 событий».
# ======================================================================
from __future__ import annotations

import json
from typing import Any

ROLE_RU = {"admin": "администратор", "accountant": "бухгалтер",
           "user": "сотрудник"}

_SOURCE_RU = {"image": "фото", "manual": "вручную", "qr": "QR-код"}


def _d(details: str | dict | None) -> dict:
    if isinstance(details, dict):
        return details
    try:
        v = json.loads(details or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _sum(v: Any) -> str:
    """1250.0 → «1 250,00 ₽»."""
    try:
        s = f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
        return s + " ₽"
    except (TypeError, ValueError):
        return ""


def _fd(d: dict) -> str:
    fd = d.get("fd")
    return f"ФД {fd}" if fd not in (None, "", "—") else ""


def _role(v: Any) -> str:
    return ROLE_RU.get(str(v or ""), str(v or "—"))


def _yes(v: Any) -> str:
    return "вкл" if v else "выкл"


def humanize_audit(action: str, details: Any = None) -> tuple[str, str]:
    """(действие, комментарий) на русском. Комментарий предназначен для
    вывода В КАВЫЧКАХ на клиенте; здесь кавычки не добавляются."""
    d = _d(details)
    fd = _fd(d)
    src = _SOURCE_RU.get(d.get("source"), "")

    if action == "login":
        return "вход в систему", (f"IP {d['ip']}" if d.get("ip") else "")
    if action == "login_failed":
        who = f"логин „{d['username']}“" if d.get("username") else "логин не указан"
        return "неудачная попытка входа — неверный логин или пароль", \
            f"{who}{', IP ' + d['ip'] if d.get('ip') else ''}"
    if action == "register":
        return "зарегистрирован новый пользователь", \
            f"роль: {_role(d.get('role'))}{', IP ' + d['ip'] if d.get('ip') else ''}"
    if action == "password_changed":
        return "изменён собственный пароль", ""

    # --- чеки -------------------------------------------------------------
    if action == "receipt_created":
        s = f" на {_sum(d['sum'])}" if d.get("sum") else ""
        return f"отсканирован чек{s}", \
            f"{fd}{', источник: ' + src if src else ''}".strip(", ")
    if action == "receipt_duplicate":
        return "отсеян дубликат чека", fd
    if action == "receipt_updated":
        fields = d.get("fields")
        f = "поля: " + ", ".join(fields) if fields else ""
        role = f" (правил: {_role(d.get('role'))})" if d.get("role") else ""
        return "исправлен чек" + role, f
    if action == "receipt_deleted":
        return "удалён чек", fd
    if action == "verify_queued":
        return f"чек{'и' if (d.get('count') or 1) != 1 else ''} отправлены на проверку в ФНС", \
            (f"количество: {d['count']}" if d.get("count") else "")
    if action == "receipts_exported":
        return "чеки выгружены в 1С", f"количество: {d.get('count', 0)}"
    if action == "receipts_exported_csv":
        return "выгружен CSV-файл", f"количество: {d.get('count', 0)}"
    if action == "receipts_assigned":
        return "назначен подотчётник на чеки", \
            f"чеков: {d.get('count', 0)}, сотрудник: „{d.get('assignee', '—')}“"
    if action == "receipts_moved":
        return "чеки перемещены в другую компанию", \
            f"чеков: {d.get('count', 0)} → „{d.get('to_company', '—')}“"
    if action == "receipts_transferred":
        return "чеки переданы другому сотруднику", \
            f"чеков: {d.get('count', 0)} (были у „{d.get('from', '—')}“)"
    if action == "external_fetch":
        n = d.get("queued", 1)
        return "запрошены данные чека из внешних сервисов", f"в очереди: {n}"

    # --- пользователи и компании -------------------------------------------
    if action == "invite_created":
        comp = f", компания „{d['company']}“" if d.get("company") else ""
        return "создано приглашение в систему", \
            f"роль: {_role(d.get('role'))}{comp}"
    if action == "invite_qr":
        return "приглашение показано QR-кодом", ""
    if action == "invite_revoked":
        return "приглашение отозвано", ""
    if action == "user_created":
        return "создан пользователь", \
            f"логин „{d.get('username', '—')}“, роль: {_role(d.get('role'))}"
    if action == "user_updated":
        return "изменены данные пользователя", ""
    if action == "user_role_changed":
        return "изменена роль пользователя", \
            f"было: {_role(d.get('from'))} → стало: {_role(d.get('to'))}"
    if action == "user_password_reset":
        return "выдан временный пароль", \
            "при входе система потребует сменить его"
    if action == "user_archived":
        return "пользователь отправлен в архив", ""
    if action == "user_unarchived":
        return "пользователь восстановлен из архива", ""
    if action == "user_moved":
        return "пользователь переведён в другую компанию", \
            f"→ „{d.get('to_company', d.get('company', '—'))}“"
    if action == "admin_transferred":
        return "права администратора переданы", \
            f"новый администратор: „{d.get('username', d.get('target', '—'))}“"
    if action == "company_created":
        return "создана компания", f"„{d.get('name', '—')}“"
    if action == "company_updated":
        return "изменена компания", f"„{d.get('name', '—')}“"
    if action == "company_deleted":
        mode = {"move": "чеки перенесены", "wipe": "чеки удалены"}.get(
            d.get("mode"), "")
        extra = (f"чеков перенесено: {d['receipts_moved']} → "
                 f"„{d.get('target', '—')}“" if d.get("receipts_moved") else mode)
        return "удалена компания", f"„{d.get('company', d.get('name', '—'))}“{'; ' + extra if extra else ''}"
    if action == "company_card_refreshed":
        return "карточка компании обновлена из ЕГРЮЛ", \
            f"ИНН {d.get('inn', '—')}, „{d.get('name_full', '—')}“"
    if action == "company_cards_bulk_refreshed":
        return "массовое обновление карточек из ЕГРЮЛ", \
            f"компаний: {d.get('count', 0)}"
    if action == "impersonate_start":
        return "включён режим просмотра", \
            f"глазами „{d.get('target', '—')}“ (роль: {_role(d.get('target_role'))})"
    if action == "impersonate_stop":
        return "выход из режима просмотра", \
            f"возвращён профиль „{d.get('target', '—')}“"

    # --- интеграции и настройки ---------------------------------------------
    if action == "fns_settings_updated":
        return "изменены настройки проверки ФНС", \
            f"провайдер: {d.get('provider', '—')}" if d.get("provider") else ""
    if action == "onec_token_updated":
        return "обновлён токен подключения 1С", ""
    if action == "onec_pull":
        return "1С забрала чеки", f"количество: {d.get('count', 0)}"
    if action == "onec_ack":
        return "1С подтвердила загрузку", ""
    if action == "mapping_updated":
        return "обновлены правила сопоставления статей", ""
    if action == "app_settings_updated":
        parts = []
        if "auto_verify" in d:
            parts.append(f"автопроверка: {_yes(d['auto_verify'])}")
        if d.get("advance_deadline_days"):
            parts.append(f"срок авансового отчёта: {d['advance_deadline_days']} дн.")
        return "изменены общие настройки", ", ".join(parts)
    if action == "external_settings_updated":
        return "изменены настройки внешних сервисов", ""
    if action == "telegram_settings_saved":
        return "сохранены настройки Telegram-уведомлений", ""
    if action == "demo_data_loaded":
        return "загружены демо-данные", f"чеков: {d.get('count', 0)}"

    # --- Checko ---------------------------------------------------------------
    if action == "checko_key_saved":
        return "сохранён API-ключ Checko", ""
    if action == "checko_key_tested":
        ok = bool(d.get("ok"))
        return "проверка ключа Checko", \
            ("результат: успех" if ok else "результат: ошибка — ключ не принят")
    if action == "checko_key_revealed":
        return "API-ключ Checko показан на экране", ""
    if action == "checko_key_reveal_blocked":
        return "попытка показать ключ Checko отклонена", \
            "разрешено только администратору"
    if action == "checko_key_reveal_failed":
        return "не удалось показать ключ Checko", ""

    # --- обслуживание ----------------------------------------------------------
    if action == "backup_created":
        return "создана резервная копия базы", f"файл „{d.get('file', '—')}“"
    if action == "backup_downloaded":
        return "скачана резервная копия базы", \
            (f"файл „{d['file']}“" if d.get("file") else "")
    if action == "update_check":
        if d.get("available"):
            return "проверка обновлений — доступна новая версия", \
                f"v{d.get('remote', '?')}"
        return "проверка обновлений — версия актуальна", ""
    if action == "update_check_failed":
        return "проверка обновлений не удалась", \
            f"„{d.get('error', '—')}“"
    if action == "update_apply":
        return "запущено обновление системы", \
            f"целевая версия v{d.get('target', '?')}, ветка „{d.get('branch', '—')}“"
    if action == "update_repo_changed":
        return "изменён источник обновлений", \
            f"репозиторий „{d.get('repo', '—')}“, ветка „{d.get('branch', '—')}“"

    # Неизвестное действие — показываем как есть, не теряя данные
    return action, ""
