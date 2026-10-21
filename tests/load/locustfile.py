# -*- coding: utf-8 -*-
"""
Ямастер Чек — нагрузочное тестирование (locust), v1.70.38.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

ЗАЧЕМ. Locust — готовый движок нагрузки (рекомендация чек-листов Habr по
тестированию API: «устойчивость» и «нагрузочное тестирование»). Сценарий ниже
имитирует реальный профиль использования системы: вход, список чеков, приём
нового чека по QR, карточка чека, аналитика, журнал действий и сдача чека
участником Чек-Пула.

ЗАПУСК
  1) поднять стенд (отдельная база, НЕ рабочая база владельца):
       DATABASE_URL="sqlite:////путь/до/data/load38.db" \
       RATE_LIMIT_API=2000000 RATE_LIMIT_LOGIN=2000000 \
       RATE_LIMIT_REGISTER=2000000 RATE_LIMIT_ONEC=2000000 \
       .venv/bin/python run.py --host 127.0.0.1 --port 8001
     пороги частоты поднимаются, иначе все пользователи упрутся в лимит
     240 запросов в минуту на один IP и мы измерим лимитер, а не приложение;
  2) прогон (без веб-интерфейса, сразу с цифрами):
       .venv/bin/locust -f tests/load/locustfile.py --headless \
           --host http://127.0.0.1:8001 -u 20 -r 5 -t 60s
     где -u сколько пользователей, -r сколько добавлять в секунду, -t длительность;
  3) тот же прогон с веб-интерфейсом и графиками (порт 8089):
       .venv/bin/locust -f tests/load/locustfile.py --host http://127.0.0.1:8001

СТУПЕНЧАТАЯ НАГРУЗКА (поиск предела, как в методике Habr: шаг +30 % от
базового профиля, шаг держим до отказа либо фиксированное время):
       .venv/bin/locust -f tests/load/locustfile.py --host http://127.0.0.1:8001 \
           --headless -t 6m \
           --processes -1 \
           -u 50 -r 5
  ступенчатый профиль (поиск предела) — ОТДЕЛЬНЫМ файлом, потому что locust
  подхватывает форму из файла сценария автоматически и игнорирует -u/-r/-t:
       .venv/bin/locust -f tests/load/locustfile_stepped.py \
           --host http://127.0.0.1:8001 --headless -t 5m

ЧТО СЧИТАЕТСЯ ПРОВАЛОМ
  • любой ответ 5xx или исключение соединения (locust пишет их в Failures);
  • p95 времени отклика выше 2 с для чтения и 5 с для приёма чека;
  • рост числа ошибок при увеличении пользователей — признак гонки
    (в v1.70.38 так нашли потерю записей журнала аудита: 38 % записей
    пропадало при 16 параллельных потоках).
"""
from __future__ import annotations

import os
import random
import string

from locust import HttpUser, between, events, task

# Логин/пароль стенда. По умолчанию — администратор, которого создаёт
# seed_if_needed при первом старте (admin/admin123).
LOAD_USER = os.environ.get("LOAD_USER", "admin")
LOAD_PASSWORD = os.environ.get("LOAD_PASSWORD", "admin123")


def _qr() -> str:
    """Случайный QR чека в формате ФНС (t=…&s=…&fn=…&i=…&fp=…&n=1).

    Каждый прогон даёт новый чек: приложение не должно ни падать на них, ни
    складывать их в одну запись (проверка на дубликаты — часть нагрузки)."""
    fn = "".join(random.choices(string.digits, k=16))
    fd = "".join(random.choices(string.digits, k=10))
    fp = "".join(random.choices(string.digits, k=10))
    total = round(random.uniform(20.0, 15000.0), 2)
    hh, mm, ss = random.randint(8, 22), random.randint(0, 59), random.randint(0, 59)
    return (f"t=20260115T{hh:02d}{mm:02d}{ss:02d}"
            f"&s={total:.2f}&fn={fn}&i={fd}&fp={fp}&n=1")


class YmasterUser(HttpUser):
    """Один пользователь системы: читает списки, сдаёт чеки, смотрит аналитику."""

    # пауза между действиями — как у живого человека (не «пулемёт»),
    # иначе измеряем не приложение, а скорость цикла
    wait_time = between(0.2, 1.2)
    token: str = ""

    def on_start(self) -> None:
        """Вход: без токена все задачи получили бы 401 и нагрузка была бы пустой."""
        with self.client.post("/api/v1/auth/login",
                              json={"username": LOAD_USER,
                                    "password": LOAD_PASSWORD},
                              name="/auth/login", catch_response=True) as r:
            if r.status_code == 200:
                self.token = r.json().get("access_token", "")
            else:
                r.failure(f"вход не удался: {r.status_code} {r.text[:120]}")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # --- чтение: самый частый сценарий --------------------------------
    @task(6)
    def receipts_list(self) -> None:
        page = random.randint(1, 3)
        self.client.get(f"/api/v1/receipts?page={page}&page_size=50",
                        headers=self._headers(), name="/receipts?page")

    @task(3)
    def dashboard(self) -> None:
        self.client.get("/api/v1/dashboard/stats?days=14",
                        headers=self._headers(), name="/dashboard/stats")
        self.client.get("/api/v1/dashboard/recent?limit=60",
                        headers=self._headers(), name="/dashboard/recent")

    @task(2)
    def participants(self) -> None:
        self.client.get("/api/v1/pool-admin/participants?limit=50",
                        headers=self._headers(), name="/pool-admin/participants")

    @task(1)
    def audit_journal(self) -> None:
        """Журнал действий — под нагрузкой именно здесь в v1.70.38 нашлась
        гонка за id (записи терялись). Задача держит это место под огнём."""
        self.client.get("/api/v1/admin/system", headers=self._headers(),
                        name="/admin/system")

    # --- запись: приём чека (главная операция системы) -----------------
    @task(4)
    def ingest_receipt(self) -> None:
        # поле тела — qr_data (модель ScanRequest); `verify` по умолчанию
        # true, поэтому каждый прогон нагружает и приём, и проверку в ФНС
        # (в песочнице провайдер mock — быстро и без сети)
        self.client.post("/api/v1/receipts/scan",
                         json={"qr_data": _qr()}, headers=self._headers(),
                         name="/receipts/scan")

    @task(1)
    def search_companies(self) -> None:
        self.client.get("/api/v1/companies/similar?name=" + random.choice(
            ("Ромашка", "ООО Вектор", "Пятёрочка", "Магнит")),
            headers=self._headers(), name="/companies/similar")


@events.test_stop.add_listener   # locust ≥2.29: add_listener
def _on_stop(environment, **_kw) -> None:
    """Итоговая строка: её достаточно для журнала тестирования."""
    stats = environment.stats.total
    p95 = stats.get_response_time_percentile(0.95) or 0
    p99 = stats.get_response_time_percentile(0.99) or 0
    print(f"\nИТОГ НАГРУЗКИ: запросов {stats.num_requests}, "
          f"ошибок {stats.num_failures} "
          f"({stats.num_failures / max(1, stats.num_requests):.2%}), "
          f"RPS {stats.total_rps:.1f}, p95 {p95:.0f} мс, p99 {p99:.0f} мс")
