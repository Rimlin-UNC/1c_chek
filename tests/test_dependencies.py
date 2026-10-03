# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — контроль зависимостей: каждый сторонний импорт в app/
# обязан быть в requirements.txt (иначе прод-venv упадёт с 502).
# ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
import ast
import pathlib
import sys

APP = pathlib.Path("app")
LOCAL = {p.name[:-3] for p in APP.rglob("*.py")} | {"app", "app.*"}
# имена, которые не нужны в requirements (модули наших пакетов/алиасы)
LOCAL_EXTRAS = {"services", "routers"}
# транзитивные импорты: приходят вместе с перечисленными пакетами
TRANSITIVE = {
    "cv2": "opencv-python-headless",
    "pydantic": "fastapi",
    "starlette": "fastapi",
    "PIL": "pillow",
}


def _top_level_imports():
    mods = set()
    for py in APP.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_all_third_party_imports_in_requirements():
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | LOCAL_EXTRAS
    used = _top_level_imports() - stdlib - LOCAL
    # транзитивные разрешаем именем их пакета-носителя
    used = {TRANSITIVE.get(m, m) for m in used}
    req = pathlib.Path("requirements.txt").read_text(encoding="utf-8").lower()
    # имена в requirements бывают с дефисом (python-multipart), а импорт —
    # через подчёркивание: проверяем оба варианта
    missing = []
    for m in sorted(used):
        variants = {m.lower(), m.lower().replace("_", "-")}
        if not any(v in req for v in variants):
            missing.append(m)
    assert not missing, (
        f"импорты без записи в requirements.txt: {missing} — "
        "на сервере venv упадёт с 502")

    # ключевые зависимости приложения должны быть перечислены явно
    for must in ("fastapi", "uvicorn", "sqlalchemy", "httpx", "pyjwt",
                 "python-multipart", "opencv-python-headless", "numpy",
                 "qrcode", "pillow"):
        assert must in req, f"в requirements.txt нет {must}"
