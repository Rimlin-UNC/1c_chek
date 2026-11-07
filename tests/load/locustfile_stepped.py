# -*- coding: utf-8 -*-
"""
Ямастер Чек — ступенчатая нагрузка: поиск предела (v1.70.38).

Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru

Форма нагрузки вынесена ОТДЕЛЬНО от tests/load/locustfile.py: locust
подхватывает LoadTestShape из файла сценария автоматически и после этого
игнорирует ключи -u/-r/-t, поэтому в основном сценарии её держать нельзя —
иначе обычный прогон «20 пользователей, минута» превращался бы в ступенчатый.

Методика — по чек-листу Habr «Нагрузочное тестирование в разработке
веб-приложений»: старт с 40 % базового профиля, шаг +30 %, ступень держится
фиксированное время; прогон останавливается, когда доля ошибок или p95
переходят порог — это и есть предел текущей сборки.

ЗАПУСК (стенд поднять так же, как для locustfile.py — отдельная база и
поднятые пороги частоты):
    .venv/bin/locust -f tests/load/locustfile_stepped.py \
        --host http://127.0.0.1:8001 --headless -t 5m
"""
from __future__ import annotations

# профиль пользователей — тот же, что и в основном сценарии. Импорт БЕЗ точки:
# locust загружает файл сценария как отдельный модуль, а не как пакет,
# поэтому относительный импорт здесь не работает.
import os
import sys

from locust import LoadTestShape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locustfile import YmasterUser  # noqa: E402,F401  (locust ищет класс в модуле)


class SteppedShape(LoadTestShape):
    """Ступени +30 % от 40 % базового профиля; стоп по ошибкам или p95."""

    BASE_USERS = 8              # 40 % от базового профиля в 20 пользователей
    STEP_FACTOR = 1.3           # +30 % на ступень
    STEP_SECONDS = 45
    MAX_USERS = 60              # потолок песочницы (2 ядра)
    ERROR_STOP = 0.02           # 2 % ошибок — стоп
    LIMIT_P95 = 2.0             # секунд на чтение

    def tick(self):
        run_time = self.get_run_time()
        step = int(run_time // self.STEP_SECONDS)
        users = int(self.BASE_USERS * (self.STEP_FACTOR ** step))
        if users > self.MAX_USERS:
            return None                        # профиль отработан
        stats = self.runner.stats.total        # type: ignore[union-attr]
        if stats and stats.num_requests > 50:
            fail_ratio = stats.num_failures / max(1, stats.num_requests)
            p95 = (stats.get_response_time_percentile(0.95) or 0) / 1000.0
            if fail_ratio > self.ERROR_STOP or p95 > self.LIMIT_P95:
                print(f"[SteppedShape] стоп: ошибок {fail_ratio:.2%}, "
                      f"p95 {p95:.2f} c на {users} пользователях")
                return None
        return users, max(2, users // 4)       # (пользователи, скорость роста)
