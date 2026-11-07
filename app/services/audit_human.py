# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — человекочитаемый журнал действий (v1.19.0)
# Разработчик и владелец идеи: ООО «Ямастер»
# Сайт: https://ymaster.ru  |  E-mail: chek@ymaster.ru
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
    if action == "unified_claim_transfer":             # v1.70.85, только администратор
        return "чек передан компании по заявке", ""
    if action == "unified_claim_decline":
        return "заявка компании на чек отклонена", ""
    if action == "unified_claim_void":
        return "заявка компании на чек не учитывается", ""
    if action == "unified_visibility_updated":         # v1.70.86, только администратор
        modes = {"off": "выключено", "all": "всем компаниям", "selected": "выбранным компаниям"}
        roles = {"accountant": "бухгалтер", "manager": "руководитель"}
        who = ", ".join(roles.get(r, r) for r in d.get("roles") or []) or "никому"
        return "изменён показ числа организаций компаниям", \
            (f"режим: {modes.get(d.get('mode'), d.get('mode') or '—')}; роли: {who}; "
             f"компаний с разрешением: {d.get('companies', 0)}")
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
        # v1.70.99 (#50): роутер пишет ok/errors/remaining, а фраза читала count — всегда «компаний: 0»
        tail = (f", ошибок: {d['errors']}" if d.get("errors") else "") + \
            (f", ещё ждут обновления: {d['remaining']}" if d.get("remaining") else "")
        return "массовое обновление карточек из ЕГРЮЛ", \
            f"компаний: {d.get('ok', d.get('count', 0))}{tail}"
    if action == "impersonate_start":
        return "включён режим просмотра", \
            f"глазами „{d.get('target', '—')}“ (роль: {_role(d.get('target_role'))})"
    if action == "impersonate_stop":
        return "выход из режима просмотра", \
            f"возвращён профиль „{d.get('target', '—')}“"
    if action == "passkey_added":
        return "привязано устройство быстрого входа", \
            f"«{d.get('label', '—')}» (отпечаток / лицо / PIN)"
    if action == "passkey_removed":
        return "отвязано устройство быстрого входа", \
            f"«{d.get('label', '—')}»"
    if action == "login_passkey":
        return "вход по отпечатку / лицу устройства", \
            f"«{d.get('label', '—')}», IP {d.get('ip', '—')}"
    if action == "login_passkey_failed":
        return "неудачный вход по ключу устройства", \
            f"{d.get('reason', '—')}, IP {d.get('ip', '—')}"
    if action == "mail_center_saved":
        return "изменены настройки почтового центра", \
            f"SMTP: {'задан' if d.get('smtp') else 'без изменений'}, бот: " + \
            ("включён" if d.get("bot") else "выключен" if d.get("bot") is False else "без изменений")
    if action == "mail_center_test":
        return "отправлено тестовое письмо", \
            "успешно" if d.get("ok") else "не удалось"
    if action == "mail_rule_created":
        return "создано правило рассылки", f"«{d.get('name', '—')}»"
    if action == "mail_rule_updated":
        return "изменено правило рассылки", f"«{d.get('name', '—')}»"
    if action == "mail_rule_deleted":
        return "удалено правило рассылки", f"«{d.get('name', '—')}»"
    if action == "mail_rule_ran":
        return "запущена рассылка вручную", \
            f"«{d.get('name', '—')}»: отправлено {d.get('sent', 0)}"
    if action == "mail_log_cleared":
        return "очищен журнал отправок", f"записей: {d.get('count', 0)}"
    if action == "impersonate_pool_start":
        return "включён просмотр кабинета участника Чек-Пула", \
            f"глазами „{d.get('email') or d.get('participant_id', '—')}“"
    if action == "impersonate_pool_stop":
        return "выход из просмотра кабинета участника", \
            f"участник: {d.get('participant_id') or '—'}"

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
    # v1.70.96 (T-60): первичка в 1С
    if action == "primary_exported":
        skipped = f", пропущено: {d['skipped']}" if d.get("skipped") else ""
        what = {"upd": "файлы УПД", "json": "EnterpriseData JSON", "xml": "EnterpriseData XML"}.get(d.get("format"), "")
        return "первичка выгружена в 1С", f"документов: {d.get('count', 0)}{skipped}" + (f", формат: {what}" if what else "")
    # v1.70.114 (T-62, ADR-45): печатная форма УПД
    if action == "primary_print_form":
        filled = d.get("filled") or []
        return "заполнены поля печатной формы УПД", \
            (f"полей: {len(filled)}" + (f" ({', '.join(filled[:6])})" if filled else ""))
    if action == "primary_printed":
        return "напечатана печатная форма УПД", ""
    if action == "onec_primary_pull":
        return "1С забрала УПД и счета-фактуры", f"количество: {d.get('count', 0)}"
    if action == "onec_primary_ack":
        return "1С подтвердила загрузку УПД", f"количество: {d.get('count', 0)}"
    # v1.70.97 (T-58): что нужно от клиента
    if action == "docreq_settings_updated":
        state = "включены" if d.get("enabled") else "выключены"
        return "изменены срок сдачи и напоминания клиенту", \
            f"компания: „{d.get('company', '—')}“, срок: {d.get('deadline_day', '—')}-е число {d.get('x_hour', '')}, " \
            f"напоминания {state}"
    if action == "docreq_reminded":
        return "клиенту отправлено напоминание о документах", \
            f"компания: „{d.get('company', '—')}“, отправлено: {d.get('sent', 0)}, не доставлено: {d.get('failed', 0)}"
    # v1.70.98 (T-45): банковская выписка
    if action == "bank_statement_uploaded":
        return "загружена банковская выписка", \
            f"компания: „{d.get('company', '—')}“, период: {d.get('period', '—')}, операций: {d.get('operations', 0)}, " \
            f"нужны документы: {d.get('saved', 0)}, найдено документов: {d.get('matched', 0)}"
    if action == "bank_statement_deleted":
        return "удалена банковская выписка", \
            f"компания: „{d.get('company', '—')}“, удалено операций: {d.get('operations', 0)}"
    if action == "bank_operation_ignored":
        return "платёж отмечен: документы не нужны", \
            f"сумма: {d.get('amount', 0)}, дата: {d.get('date', '—')}, причина: {d.get('note', '—')}"
    if action == "bank_reimbursement_decided":
        state = "возмещено" if d.get("status") == "paid" else "не возмещать"
        return "решение по чеку сотрудника", f"компания: „{d.get('company', '—')}“, {state}" + \
            (f", причина: {d['note']}" if d.get("note") else "")
    if action == "bank_settings_updated":
        return "изменены настройки банковской выписки", \
            f"компания: „{d.get('company', '—')}“, срок хранения: {d.get('retention_days', '—')} дн., " \
            f"карт сотрудников: {d.get('cards', 0)}"
    # v1.70.99 (ADR-29): банк по API — Точка (секреты в журнал не пишутся)
    if action == "bank_connection_saved":
        return "сохранено подключение банка по API", \
            f"компания: „{d.get('company', '—')}“, банк: {d.get('bank', '—')}, способ: {d.get('method', '—')}"
    if action == "bank_connected":
        return "банк подключён по API", \
            f"компания: „{d.get('company', '—')}“, банк: {d.get('bank', '—')}, способ: {d.get('method', '—')}, " \
            f"счетов: {d.get('accounts', 0)}"
    # v1.70.101 (T-69, ADR-31): понимание чеков — категория позиции и память товаров
    if action == "goods_category_set":
        from . import goods as _goods
        before = _goods.category(d.get("before") or "").title if d.get("before") else "не разобрана"
        tail = ""
        if d.get("remembered"):
            tail = "; запомнено для компании" + (f", ещё {d.get('applied', 0)} таких же позиций"
                                                 if d.get("applied") else "")
        return "изменена категория позиции чека", \
            f"было: {before}, стало: {_goods.category(d.get('after') or '').title}{tail}"
    if action == "goods_memory_forget":
        from . import goods as _goods
        return "забыто исправление категории товара", \
            f"товар: „{d.get('name_key', '—')}“, категория: {_goods.category(d.get('category') or '').title}"
    # v1.70.100: длительность заставки загрузки задаёт администратор
    if action == "splash_settings_updated":
        def _sec(v: Any) -> str:
            return "без заставки" if str(v) == "0" else f"{v} с"
        return "изменена длительность заставки загрузки", \
            f"было: {_sec(d.get('before', '—'))}, стало: {_sec(d.get('seconds', '—'))}"
    # v1.70.100 (T-68): пакет для других учётных систем — JSON, CSV и УПД за месяц
    if action == "export_package":
        return "выгружен пакет для других учётных систем", \
            f"компания: „{d.get('company', '—')}“, месяц: {d.get('period', '—')}, чеков: {d.get('receipts', 0)}, " \
            f"документов: {d.get('primary_documents', 0)}, файлов УПД: {d.get('upd_files', 0)}, " \
            f"банковских операций: {d.get('bank_operations', 0)}"
    # v1.70.100 (T-50, ADR-30): рабочее место аутсорсера — клиентов бухгалтеру назначает администратор
    if action == "clients_access_set":
        return "назначены клиенты бухгалтера", \
            f"бухгалтер: „{d.get('username', '—')}“, клиентов: {d.get('total', 0)} " \
            f"(добавлено {d.get('added', 0)}, снято {d.get('removed', 0)})"
    if action == "bank_connection_removed":
        return "банк отключён", f"компания: „{d.get('company', '—')}“"
    # v1.70.104 (T-63, ADR-34): первичка из почтового ящика (пароль в журнал не пишется)
    if action == "primary_mailbox_saved":
        return "сохранён почтовый ящик для первички", \
            f"компания: „{d.get('company', '—')}“, сервер: {d.get('host', '—')}, папка: {d.get('folder', '—')}"
    if action == "primary_mailbox_connected":
        return "почтовый ящик для первички подключён", \
            f"компания: „{d.get('company', '—')}“, сервер: {d.get('host', '—')}, писем в папке: {d.get('messages', 0)}"
    if action == "primary_mailbox_removed":
        return "почтовый ящик для первички отключён", f"компания: „{d.get('company', '—')}“"
    # v1.70.111 (T-78): время чека возвращено по QR — след формы правки до 1.70.110
    if action == "receipt_time_restored":
        return "время чека возвращено по QR", \
            f"было: {d.get('from', '—')}, стало: {d.get('to', '—')}, сдвиг: {d.get('hours', '—')} ч"
    # v1.70.113 (T-79, ADR-43): закрытие выписки документами — ручная привязка платежа к УПД
    if action == "bank_closing_linked":
        side = "поступление" if d.get("side") == "income" else "списание"
        return "платёж привязан к документам вручную", \
            f"компания: „{d.get('company', '—')}“, {side}, документов: {d.get('documents', 0)}"
    if action == "bank_closing_unlinked":
        return "ручная привязка платежа снята", f"компания: „{d.get('company', '—')}“"
    # v1.70.112 (T-46, ADR-24): согласование авансовых отчётов — подпись и решения (подробности — в журнале отчёта)
    if action == "approvals_settings":
        limits = (f"порог руководителя: {d.get('manager_limit') or 'нет'}, порог бухгалтера: "
                  f"{d.get('accountant_limit') or 'нет'}")
        return "изменены настройки согласования авансовых отчётов", \
            f"{'включено' if d.get('enabled') else 'выключено'}, срок ответа: {d.get('timeout_hours', '—')} ч, {limits}"
    stages = {"manager": "руководитель", "accountant": "бухгалтер"}
    if action == "approval_created":
        return "авансовый отчёт сформирован на согласование", \
            f"{d.get('number', '—')}: {d.get('employee', '—')}, к учёту {d.get('total', '—')} ₽"
    if action == "approval_signed":
        return "авансовый отчёт подписан сотрудником (ПЭП)", f"{d.get('number', '—')}"
    if action in ("approval_approved", "approval_rejected"):
        verb = "согласован" if action == "approval_approved" else "отклонён"
        return f"авансовый отчёт {verb} (ПЭП)", \
            f"{d.get('number', '—')}, этап: {stages.get(d.get('stage'), d.get('stage') or '—')}"
    if action == "approval_cancelled":
        return "авансовый отчёт отменён", f"{d.get('number', '—')}"
    # v1.70.109 (T-77, ADR-39): режим второго прочтения сканов и фото
    if action == "primary_ocr_second":
        modes = {"auto": "по сомнению", "always": "всегда", "off": "выключено"}
        return "изменён режим второго прочтения сканов", f"режим: {modes.get(d.get('mode'), d.get('mode') or '—')}"
    if action == "primary_mail_checked":
        return "первичка загружена из почты", \
            f"компания: „{d.get('company', '—')}“, писем: {d.get('messages', 0)}, в реестр: {d.get('created', 0)}, " \
            f"на сверку: {d.get('drafts', 0)}, дублей: {d.get('duplicates', 0)}, не принято: {d.get('rejected', 0)}"
    if action == "bank_synced":
        return "выписка загружена из банка", \
            f"компания: „{d.get('company', '—')}“, операций: {d.get('operations', 0)}, " \
            f"нужны документы: {d.get('saved', 0)}, найдено документов: {d.get('matched', 0)}"
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
