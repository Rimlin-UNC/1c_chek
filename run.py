# -*- coding: utf-8 -*-
"""
Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С.

Разработчик и владелец идеи: ООО «Ямастер»
Сайт: https://ymaster.ru  |  E-mail: info@ymaster.ru

Точка входа для разработки:  python run.py [--port 8000] [--demo]
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Ямастер Чек — сервер (dev)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true",
                        help="добавить демо-чеки перед запуском")
    args = parser.parse_args()

    if args.demo:
        from app.seed import main as seed_main
        sys.argv = ["seed", "--demo"]
        seed_main()

    import uvicorn
    print(f"Ямастер Чек — ООО «Ямастер» (ymaster.ru) | http://{args.host}:{args.port}")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
