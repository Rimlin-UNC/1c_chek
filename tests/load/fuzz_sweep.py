# -*- coding: utf-8 -*-
"""
Ямастер Чек — фазз-развёртка API (появилась в v1.70.38, аудит качества).

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

ЗАЧЕМ ЭТОТ ФАЙЛ. Готовый движок schemathesis проверяет контракт: сам обходит
операции OpenAPI, генерирует значения по схеме и ловит 5xx и расхождения с
описанными типами. Но он НЕ подставляет прикладные полезные нагрузки — то, чем
реально проверяют веб-приложение по чек-листам безопасности (Habr «API и
Security Testing», codeby «Фаззинг веб-приложений»): SQL-инъекции, XSS-полиглоты,
CRLF-инъекция в заголовки и журнал, выход за пределы каталога, SSRF, инъекции
команд и шаблонов, одиночные суррогаты Unicode, гигантские и глубоко вложенные
тела. Этот стенд добавляет ровно их и работает ПО СХЕМЕ ПРИЛОЖЕНИЯ: имена полей
берутся из OpenAPI, поэтому новый эндпоинт попадает под развёртку сам, без
правки стенда.

КРИТЕРИЙ ДЕФЕКТА: ответ 5xx или исключение на стороне приложения. Ответы 4xx —
ПРАВИЛЬНАЯ реакция на недопустимые данные (приложение поняло, что ему дали
мусор, и отказало без падения).

ЗАПУСК
  in-process (быстро, без сервера; база — временная):
      .venv/bin/python tests/load/fuzz_sweep.py
  по живому серверу (например, по стенду для фаззинга на 8001):
      .venv/bin/python tests/load/fuzz_sweep.py --base-url http://127.0.0.1:8001
  только нужное семейство нагрузок:
      .venv/bin/python tests/load/fuzz_sweep.py --family sqli,xss

ВЫХОД: сводка по семействам и список найденного с телом ответа — этого
достаточно, чтобы завести карточку дефекта. Код возврата 1, если найдён хотя бы
один ответ 5xx (удобно для пайплайна).

ВАЖНО: стенд пишет мусорные данные в базу, поэтому in-process-режим поднимает
СВОЮ временную базу, а для живого сервера используйте отдельный стенд
(см. tests/load/README.md), а не рабочую базу владельца.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# --------------------------------------------------------------------------
#  Полезные нагрузки (семейство → значения)
#  Приоритеты — по чек-листу codeby: SQLi, XSS, CRLF, обход пути, SSRF,
#  инъекции команд и шаблонов; отдельно — пограничные числа и Unicode.
# --------------------------------------------------------------------------
PAYLOADS: dict[str, list[str]] = {
    "sqli": [
        "' OR '1'='1",
        "' OR 1=1 --",
        "1; DROP TABLE users; --",
        "\" UNION SELECT NULL,username,password FROM users --",
        "admin'--",
        "1' AND (SELECT COUNT(*) FROM sqlite_master) > 0 --",
        "%%' AND 1=CONVERT(int,(SELECT @@version)) --",
    ],
    "xss": [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "\"><svg/onload=alert(document.domain)>",
        "javascript:alert(1)",
        # полиглот: ломает наивные фильтры сразу в нескольких контекстах
        "jaVasCript:/*-/*`/*\\`/*'/*\"/**/(/* */oNcliCk=alert() )//%0D%0A"
        "%0d%0a//&lt;/stYle/&lt;/titLe/&lt;/teXtarEa/&lt;/scRipt/--!&gt;"
        "&lt;svG/oNloAd=alert(document.cookie)//&gt;&lt;sVg/oNloAd=alert(1)//",
        "<iframe srcdoc='&#39;alert(1)&#39;'></iframe>",
    ],
    "crlf": [
        "value\r\nX-Injected: 1",
        "value\nSet-Cookie: hacked=1",
        "value%0d%0aX-Injected:%201",
        "строка\r\n\r\n<script>alert(1)</script>",
    ],
    "path": [
        "../../../../etc/passwd",
        "..\\..\\..\\windows\\win.ini",
        "..%2f..%2f..%2fetc%2fpasswd",
        "/etc/passwd",
        "%2e%2e%2f%2e%2e%2f",
        "....//....//etc/passwd",
    ],
    "ssrf": [
        "http://127.0.0.1:8001/api/v1/admin/system",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/",
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "https://ymaster.ru/../../etc/passwd",
    ],
    "cmdi": [
        "; ls -la /",
        "| whoami",
        "$(cat /etc/passwd)",
        "`id`",
        "&& curl http://127.0.0.1:9/x",
    ],
    "tpl": [
        "{{7*7}}",
        "${7*7}",
        "#{7*7}",
        "{{config.items()}}",
        "{% for i in range(99999) %}x{% endfor %}",
    ],
    "unicode": [
        "ЁёІіЇїҐґ" * 40,                       # кириллица и расширения
        "🎉🧾💰" * 40,                            # эмодзи (4 байта)
        "\u202eимя\u202c",                        # правый-налево оверрайд
        "и\u0307мя\u0308",                        # комбинируемые диакритики
        "a" * 5000,                               # длинная строка
        " \t\n\r\x00 ",                           # управляющие и нулевой байт
        "İıẞß",                                   # особые регистры (lower/upper)
    ],
    "numbers": [
        "0", "-1", "1e400", "-0.0", "NaN", "Infinity",
        "99999999999999999999999999",
        "0.30000000000000004", "1_000", "0x10", " 12 ", "+12",
    ],
}

# Значения «не тот тип»: для поля ожидаемого типа — заведомо чужое значение.
WRONG_TYPES: dict[str, list] = {
    "string": [0, -1, 1.5, True, False, None, [], {}, [1, 2], {"a": 1}],
    "integer": ["abc", "", None, 1.5, True, [], {}, "1e400", 2 ** 70, -2 ** 70],
    "number": ["abc", "", None, True, [], {}, "1e400", 1e309, -1e309],
    "boolean": ["yes", "", None, 0, 1, [], {}, "true"],
    "array": ["abc", None, {}, 0, True, [[]], [None] * 50],
    "object": ["abc", None, [], 0, True, {"a": {"b": {"c": 1}}}],
}

FAMILIES = list(PAYLOADS)


# --------------------------------------------------------------------------
#  Работа со схемой: какие поля у тела запроса и какого они типа
# --------------------------------------------------------------------------
def _resolve(spec: dict, schema: dict) -> dict:
    """Раскрыть $ref (один уровень вложенности ссылок достаточен)."""
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if not ref:
        return schema or {}
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = (node or {}).get(part, {})
    return node or {}


def body_fields(spec: dict, op: dict) -> dict[str, str]:
    """{имя поля: тип} для JSON-тела операции (пусто, если тела нет)."""
    content = ((op.get("requestBody") or {}).get("content") or {})
    js = content.get("application/json") or {}
    schema = _resolve(spec, js.get("schema") or {})
    props = schema.get("properties") or {}
    out = {}
    for name, sub in props.items():
        sub = _resolve(spec, sub or {})
        t = sub.get("type") or ("array" if "items" in sub else
                                "object" if "properties" in sub else "string")
        out[name] = t
    return out


def operations(spec: dict) -> list[tuple[str, str, dict]]:
    """[(метод, путь, операция)] по всей схеме."""
    out = []
    for path, item in (spec.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                out.append((method.upper(), path, op or {}))
    return out


# Пути, которые стенд НЕ трогает, и почему.
# 1) Управляют самим сервером и репозиторием: обновление через git,
#    восстановление базы, установка AdGuard через sudo. Фаззинг здесь мог бы
#    испортить рабочий каталог или базу владельца.
DANGEROUS = ("backup", "update/apply", "update/rollback", "update/repo",
             "adguard", "/admin/system/restart")
# 2) Ходят во внешнюю сеть: ФНС, proverkacheka, Checko, Telegram, SMTP,
#    вебхуки, GitHub. В песочнице этих адресов нет, и вместо проверки стенд
#    утонул бы в таймаутах. Хуже того — фаззинг настроек интеграций ЗАПИСЫВАЕТ
#    мусорные ключи в базу, после чего следующие запросы начинают ходить
#    наружу уже с ними (наблюдали в первом прогоне: «мастер-ключ ФНС: есть»,
#    хотя посев его не создаёт). Валидация этих полей покрыта юнит-тестами,
#    а поведение при недоступном сервисе — тестами ротации источников.
OUTBOUND = ("settings/external", "settings/fns", "settings/checko",
            "settings/telegram", "settings/max", "mail-admin", "webhook",
            "companies/lookup-checko", "companies/bulk-refresh",
            "admin/update/check", "receipts/fetch-details")


def is_dangerous(path: str) -> bool:
    """Пропускать ли путь: управляет сервером или ходит наружу."""
    low = path.lower()
    return any(d in low for d in DANGEROUS) or any(o in low for o in OUTBOUND)


# --------------------------------------------------------------------------
#  Особые случаи протокола: то, что не описывается схемой
# --------------------------------------------------------------------------
class Raw:
    """Тело запроса сырыми байтами — для заведомо битого JSON/UTF-8."""

    def __init__(self, data: bytes):
        self.data = data


def protocol_cases() -> list[tuple[str, object]]:
    """(название, тело) — злоупотребления самим протоколом/JSON."""
    deep = cur = {}
    for _ in range(120):                      # глубокая вложенность → рекурсия
        cur["a"] = {}
        cur = cur["a"]
    return [
        ("пустое тело", {}),
        ("не JSON-тело", "просто текст"),
        ("тело-массив", [1, 2, 3]),
        ("неизвестные поля", {"__нет_такого_поля__": 1, "id": 999999,
                              "role": "admin", "is_admin": True}),
        ("все поля null", None),               # подставляется в каждое поле
        ("вложенность 120 уровней", deep),
        ("строка 200 КБ", {"text": "ы" * 200_000}),
        ("массив 5000 элементов", {"items": list(range(5000))}),
        ("нулевые байты", {"text": "a\x00b\x00c"}),
        # эти два — СЫРЫЕ байты: Infinity и одиночные суррогаты не проходят
        # через json.dumps/utf-8, и в первом прогоне стенд падал на своей же
        # кодировке (354 «находки» оказались его ошибкой, а не приложения).
        # Сервер обязан отвергнуть такой JSON отказом 4xx, а не упасть.
        ("число Infinity (невалидный JSON)",
         Raw(b'{"value": Infinity}')),
        ("одиночный суррогат (невалидный UTF-8)",
         Raw(b'{"text": "\xed\xa0\x80\xed\xbf\xbf"}')),
        ("обрезанный JSON", Raw(b'{"text": "')),
        ("JSON с BOM", Raw(b'\xef\xbb\xbf{"text": "x"}')),
    ]


# --------------------------------------------------------------------------
#  Клиент: in-process (TestClient) или по сети (urllib)
# --------------------------------------------------------------------------
class Client:
    """Обёртка над двумя режимами: in-process TestClient или HTTP по сети.

    `raise_server_exceptions=False` обязателен: иначе TestClient перебрасывает
    исключение сервера наружу и вместо настоящего ответа 500 стенд видел бы
    пустое место — а нам нужен именно код ответа и тело ошибки."""

    def __init__(self, base_url: str = "", token: str = "", tc=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._tc = tc

    def _headers(self, extra: dict | None = None) -> dict:
        h = {}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, path: str, *, json_body=None, params=None,
                raw=None, ctype="application/json", headers=None):
        """Возвращает (статус, текст ответа). Исключение → (-1, текст)."""
        url = (self.base_url + path) if self.base_url else path
        hdrs = self._headers(headers)
        if self._tc is not None:
            try:
                if raw is not None:
                    r = self._tc.request(method, url, content=raw,
                                         params=params,
                                         headers={**hdrs, "Content-Type": ctype})
                elif json_body is not None:
                    r = self._tc.request(method, url, json=json_body,
                                         params=params, headers=hdrs)
                else:
                    r = self._tc.request(method, url, params=params, headers=hdrs)
                # тело НЕ обрезаем: его разбирает json.loads (вход в стенд),
                # а для печати обрежет record()
                return r.status_code, r.text
            except Exception as e:                      # noqa: BLE001
                return -1, f"{type(e).__name__}: {e}"[:600]
        import urllib.error
        import urllib.parse
        import urllib.request
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = raw
        if data is None and json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8", "replace")
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", ctype)
        for k, v in hdrs.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:                          # noqa: BLE001
            return -1, f"{type(e).__name__}: {e}"[:600]


# --------------------------------------------------------------------------
#  Развёртка
# --------------------------------------------------------------------------
def fill_path(path: str, payload: str) -> str:
    """Подставить нагрузку в каждый {параметр} пути (по одному значению).

    Значение URL-кодируется: настоящий атакующий шлёт `%0d%0a` и `%2e%2e%2f`,
    а не сырые управляющие байты — на сырых спотыкается HTTP-клиент, и запрос
    до сервера не доходит (так в первом прогоне получилась 71 пустая
    «находка» семейства crlf).
    """
    import urllib.parse
    quoted = urllib.parse.quote(payload, safe="")
    out, i = [], 0
    while i < len(path):
        if path[i] == "{":
            j = path.index("}", i)
            out.append(quoted)
            i = j + 1
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def sweep(client: Client, spec: dict, families: list[str],
          do_protocol: bool = True, limit_per_family: int = 3) -> dict:
    """Прогнать развёртку; вернуть сводку {семейство: [(путь, статус, тело)]}."""
    findings: dict[str, list] = {}
    sent = 0

    def record(family: str, path: str, method: str, status: int, body: str):
        nonlocal sent
        sent += 1
        if status >= 500 or status == -1:
            findings.setdefault(family, []).append(
                (f"{method} {path}", status, (body or "")[:300]))

    ops = [o for o in operations(spec) if not is_dangerous(o[1])]
    print(f"развёртка по {len(ops)} операциям (опасные пути пропущены)…")
    for n_op, (method, path, op) in enumerate(ops, 1):
        if n_op % 20 == 0:
            print(f"  …{n_op}/{len(ops)}: найдено {sum(len(v) for v in findings.values())}",
                  flush=True)
        params = [p.get("name") for p in (op.get("parameters") or [])]
        fields = body_fields(spec, op) if method != "GET" else {}
        for family in families:
            for payload in PAYLOADS[family][:limit_per_family]:
                # 1) нагрузка в параметры пути и запроса
                target = fill_path(path, payload)
                qs = {p: payload for p in params if p} or None
                record(family + ":params", target, method,
                       *client.request(method, target, params=qs))
                # 2) нагрузка в каждое строковое поле тела
                if fields:
                    for name, typ in fields.items():
                        val = payload if typ in ("string", "object", "array") \
                            else payload
                        record(family + ":body", path, method,
                               *client.request(method, path,
                                               json_body={name: val}))
        # 3) неверные типы по каждому полю
        for name, typ in fields.items():
            for val in WRONG_TYPES.get(typ, WRONG_TYPES["string"])[:6]:
                record("wrong-type", path, method,
                       *client.request(method, path, json_body={name: val}))
        # 4) протокольные злоупотребления
        if do_protocol:
            for label, body in protocol_cases():
                if body is None and fields:
                    body = {n: None for n in fields}
                if isinstance(body, Raw):
                    record("protocol:" + label, path, method,
                           *client.request(method, path, raw=body.data))
                else:
                    record("protocol:" + label, path, method,
                           *client.request(method, path, json_body=body))
    return {"sent": sent, "findings": findings}


def main() -> int:
    ap = argparse.ArgumentParser(description="Фазз-развёртка API Ямастер Чек")
    ap.add_argument("--base-url", default="",
                    help="адрес живого сервера (пусто — in-process)")
    ap.add_argument("--family", default="",
                    help="только эти семейства, через запятую")
    ap.add_argument("--per-family", type=int, default=3,
                    help="сколько нагрузок семейства брать (по умолчанию 3)")
    ap.add_argument("--no-protocol", action="store_true",
                    help="не гонять протокольные злоупотребления")
    ap.add_argument("--login", default="admin", help="логин (in-process)")
    ap.add_argument("--password", default="admin123", help="пароль (in-process)")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)

    if not args.base_url:
        # Всё, что влияет на импорт app.main, задаётся ДО импорта (тот же
        # порядок, что в tests/conftest.py): своя временная база — мусорные
        # данные не должны попасть к владельцу; очередь загрузки и
        # воркеры Telegram/MAX выключены — они в развёртке не нужны и
        # только добавляют недетерминизма; пороги частоты подняты, иначе
        # стенд упрётся в 429 раньше, чем дойдёт до дела.
        tmp = tempfile.mkdtemp(prefix="ymaster-fuzz-")
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/fuzz.db"
        os.environ["YM_NO_REEXEC"] = "1"
        os.environ["YM_FETCHQ_OFF"] = "1"
        for name in ("API", "LOGIN", "REGISTER", "ONEC"):
            os.environ["RATE_LIMIT_" + name] = "2000000"
        print(f"временная база стенда: {tmp}/fuzz.db")

    import logging
    # INFO приложения и httpx тонет отчёт: за 20 тысяч запросов журнал стенда
    # был бы на десятки мегабайт. Ошибки и предупреждения остаются видны.
    for name in ("ymaster", "httpx", "httpcore", "asyncio"):
        logging.getLogger(name).setLevel(logging.ERROR)

    from app.main import app
    spec = app.openapi()
    families = ([f.strip() for f in args.family.split(",") if f.strip()]
                or FAMILIES)
    print(f"операций в схеме: {len(operations(spec))}, семейств: {len(families)}")

    if args.base_url:
        client = Client(args.base_url)
        res = sweep(client, spec, families,
                    do_protocol=not args.no_protocol,
                    limit_per_family=args.per_family)
        return report(res)

    from fastapi.testclient import TestClient
    # lifespan запускается только внутри контекста — а именно там создаётся
    # база и администратор по умолчанию (app/main.py::lifespan → init_db,
    # seed_if_needed), без них вход не получить.
    with TestClient(app, raise_server_exceptions=False) as tc:
        client = Client(tc=tc)
        st, body = client.request("POST", "/api/v1/auth/login",
                                  json_body={"username": args.login,
                                             "password": args.password})
        if st == 200:
            client.token = json.loads(body)["access_token"]
        print(f"вход в стенд: {st}" + ("" if st == 200 else f" — {body[:120]}"))
        res = sweep(client, spec, families,
                    do_protocol=not args.no_protocol,
                    limit_per_family=args.per_family)
    return report(res)


def report(res: dict) -> int:
    """Печать сводки; 1 — если найден хотя бы один ответ 5xx или падение."""
    print(f"\nотправлено запросов: {res['sent']}")
    total = sum(len(v) for v in res["findings"].values())
    if not total:
        print("ответов 5xx и падений НЕ найдено — развёртка чистая")
        return 0
    print(f"НАЙДЕНО проблемных ответов: {total}\n")
    for family in sorted(res["findings"]):
        items = res["findings"][family]
        print(f"=== {family}: {len(items)} ===")
        seen = set()
        for target, status, body in items:
            key = (target.split("?")[0], status)
            if key in seen:
                continue
            seen.add(key)
            print(f"  [{status}] {target}\n       {body[:200]}")
        if len(seen) < len(items):
            print(f"  (ещё {len(items) - len(seen)} однотипных)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
