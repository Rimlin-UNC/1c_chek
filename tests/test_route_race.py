"""#25 (v1.70.87): поздний ответ раздела не подменяет открытый раздел.

Разделы рисуются асинхронно: сначала запросы к API, затем разметка в
#view-container. route() увеличивает номер перехода и передаёт разделу проверку
alive(); запись в контейнер после await — только пока раздел на экране.
Поведение проверяют DOM-стенд (tests/ui_harness.js — настоящий route() и
разделы бандла), Vitest (tests/js/supabase_demo.test.js) и сценарий Playwright
«дашборд: поздний ответ…». Здесь — страж шаблона во всех разделах, устройство
route() и запуск стенда.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRITE = re.compile(r"\bcontainer\.(innerHTML\s*=|replaceChildren|appendChild|append\(|insertAdjacentHTML)")
VULNERABLE = ("viewDashboard", "viewCompanies", "viewBoss", "viewExport", "viewMapping",
              "viewUsers", "viewAudit", "viewSettings")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def view_bodies(src: str):
    """Функции разделов view*(container…): имя, начало тела и само тело."""
    for m in re.finditer(r"(?:async\s+)?function\s+(view\w+)\s*\(\s*container[^)]*\)\s*\{", src):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        yield m.group(1), m.end(), src[m.end():i]


def unguarded_writes(src: str) -> list[str]:
    """Записи в контейнер раздела после await без проверки alive() между ними."""
    bad = []
    for name, start, body in view_bodies(src):
        for w in WRITE.finditer(body):
            before = body[:w.start()]
            last_await = before.rfind("await ")
            if last_await != -1 and "alive()" not in before[last_await:]:
                bad.append(f"{name}: строка {src[:start + w.start()].count(chr(10)) + 1}")
    return bad


class TestPattern:
    def test_views_check_alive_after_await(self):
        """Ни один раздел не пишет в контейнер после await без проверки alive()."""
        for rel in ("app/static/js/app.js", "app/static/js/supabase_demo.js"):
            assert unguarded_writes(read(rel)) == [], rel

    def test_detector_catches_the_race(self):
        """Страж сам видит гонку: запись после await без проверки — нарушение."""
        sample = "async function viewX(container) {\n  const a = await api.get('/x');\n  container.innerHTML = a;\n}\n"
        assert unguarded_writes(sample) == ["viewX: строка 3"]
        fixed = sample.replace("  container", "  if (!alive()) return;\n  container")
        assert unguarded_writes(fixed) == []

    def test_route_passes_alive_and_guards_fallback(self):
        src = read("app/static/js/app.js")
        guard = src[src.index("function routeGuard() {"):src.index("function route(silent = false) {")]
        assert "return () => seq === _routeSeq;" in guard
        route = src[src.index("function route(silent = false) {"):src.index("//  v1.11.0: МУЛЬТИКОМПАНИЙНОСТЬ")]
        assert "_routeSeq += 1;" in route
        assert "(renderers[view] || viewDashboard)(container, alive)" in route
        fallback = route[route.index("const _fallback"):route.index("Promise.resolve(")]
        # сбой устаревшей отрисовки не уводит пользователя на дашборд
        assert "if (!alive()) return;" in fallback

    def test_vulnerable_views_take_alive(self):
        src = read("app/static/js/app.js")
        for name in VULNERABLE:
            assert f"async function {name}(container, alive = routeGuard())" in src, name
        demo = read("app/static/js/supabase_demo.js")
        assert "export async function viewSupabaseDemo(container, alive = () => true)" in demo
        assert demo.count("|| !alive()) return;") == 2          # снимок: ответ и ошибка


class TestHarness:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node недоступен на этой машине")
    def test_dom_harness_reproduces_and_blocks_the_race(self):
        """Стенд задерживает ответ API, пока «пользователь» уходит в другой раздел."""
        from tests.test_v1121 import _build_bundle, worker_bundle_path
        path = Path(worker_bundle_path().replace(".js", "_route_race.js"))
        path.write_text(_build_bundle(), encoding="utf-8")
        r = subprocess.run(["node", "tests/ui_harness.js", str(path)], capture_output=True,
                           text=True, timeout=120, cwd=ROOT, check=False)
        out = r.stdout + r.stderr
        for name in ("дашборд ждёт ответа API", "открыт реестр чеков",
                     "поздний ответ дашборда не подменил реестр (#25)",
                     "сбой устаревшего раздела «Выгрузка» не увёл на дашборд",
                     "дашборд без гонки рисуется как прежде"):
            assert f"PASS v1.70.87: {name}" in out, out[-1500:]
        assert "HARNESS_OK" in out, out[-1500:]


class TestBrowserScenario:
    def test_playwright_scenario_holds_dashboard_stats(self):
        spec = read("tests/e2e/app.spec.js")
        block = spec.split("test('дашборд: поздний ответ не подменяет открытый раздел'", 1)[1]
        for needle in ("page.route('**/api/v1/dashboard/stats**'", "release();",
                       "toHaveText('База чеков')", "locator('#btn-focus')).toHaveCount(0)"):
            assert needle in block, needle
        # обход гонки в сценарии «число организаций» (v1.70.86) больше не нужен
        assert "dashboardReady" not in spec
