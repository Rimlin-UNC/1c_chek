# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — сквозная проверка обмена с API ФНС по HTTP.
#
# Поднимает поддельный сервер ФНС (qa/fake_fns_api.py) и прогоняет через
# настоящий httpx-клиент все пути: токен → GetTicket → чек, «чек не
# найден», отказ по IP, исчерпанный лимит. Сеть наружу не нужна.
#
# Запуск:  python3 qa/e2e_fns_check.py
# ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
# ======================================================================
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Проверка не должна касаться рабочей базы: отдельный файл в /tmp
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/ymaster_fns_e2e.db")
os.environ.setdefault("YM_FETCHQ_OFF", "1")
os.environ.setdefault("YM_OSMON_OFF", "1")

PORT = 8099
OWNER_FN = "7381440700130934"      # чек есть в «базе» поддельного сервера
OTHER_FN = "9999999999999999"      # чека нет


def _start_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fake_fns_api", ROOT / "qa" / "fake_fns_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = mod.serve(PORT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return mod, server


def main() -> int:
    from app import models  # noqa: F401  (регистрирует таблицы в metadata)
    from app.database import Base, engine
    from app.services import fns

    Base.metadata.create_all(bind=engine)      # отдельная база проверки

    mod, server = _start_server()
    base = f"http://127.0.0.1:{PORT}"
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'✅' if ok else '❌'} {name}{(' — ' + detail) if detail else ''}")

    # 1. Связь и токен
    good = fns.FnsClient(api_base=base, master_token=mod.MASTER_TOKEN,
                         client_app_id="YmasterAPI", timeout=10)
    ok, msg = good.probe()
    check("мастер-токен принят, временный токен получен", ok, msg)

    # 2. Чек есть в базе → валиден и пришёл полный документ
    res = good.get_ticket(OWNER_FN, "11100", "2060442762", 890.0,
                          datetime(2026, 10, 1, 9, 8))
    ticket = (res.raw or {}).get("ticket") or {}
    check("чей-то чек найден, Code 200", res.status == "valid", res.message)
    check("полный документ чека получен (Ticket)",
          bool(ticket.get("receipt", {}).get("items")),
          f"позиций: {len(ticket.get('receipt', {}).get('items', []))}")

    # 3. Чека нет → «не найден», а не ошибка связи
    res2 = good.get_ticket(OTHER_FN, "22200", "1111111111", 100.0,
                           datetime(2026, 10, 1, 9, 8))
    check("отсутствующий чек помечен как «не найден»",
          res2.ok and res2.status == "not_found", res2.message)

    # 4. Неверный мастер-токен → понятная причина
    bad = fns.FnsClient(api_base=base, master_token="WRONG", timeout=10)
    ok_bad, msg_bad = bad.probe()
    check("неверный мастер-токен объяснён", not ok_bad and "токен" in msg_bad.lower(),
          msg_bad)

    # 5. Отказ по IP (сервис ФНС пускает только с согласованных адресов)
    mod.DENY_IP = True
    ip_client = fns.FnsClient(api_base=base, master_token=mod.MASTER_TOKEN,
                              timeout=10)
    ok_ip, msg_ip = ip_client.probe()
    check("запрет по IP объяснён", not ok_ip and "IP" in msg_ip, msg_ip)
    mod.DENY_IP = False

    # 6. Исчерпанный суточный лимит → статус quota (чек в очередь)
    mod.DAILY_LIMIT = mod.STATE.requests
    quota_client = fns.FnsClient(api_base=base, master_token=mod.MASTER_TOKEN,
                                 timeout=10)
    res3 = quota_client.get_ticket(OWNER_FN, "11100", "2060442762", 890.0,
                                   datetime(2026, 10, 1, 9, 8))
    check("суточный лимит ФНС распознан как quota",
          res3.status == "quota", res3.message)

    server.shutdown()
    failed = [c for c in checks if not c[1]]
    print()
    print(f"итог: {len(checks) - len(failed)} из {len(checks)} проверок пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
