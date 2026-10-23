r"""v1.70.36 — картинки шагов на лендинге видны на всех устройствах; вход через MAX заработал.

Две задачи выпуска (формулировки владельца):

1. «На стартовой странице … сделай картинки больше чтоб они были видны на
   разных устройствах, а текст подними, добавь картинку к 3 пункту в той же
   стилизации, не теряй нить и стиль».
2. «Подпись MAX не совпала с сохранённым токеном бота … Хотя Макс у меня
   привязан … Нужно чтоб была возможность зарегистрироваться или войти через
   свой аккаунт».

Что закрепляет набор.

**Лендинг.** Было: иллюстрации шагов УМЕНЬШАЛИСЬ именно там, где нужнее
всего — 84×62 на десктопе, 62×46 на планшете, 54×40 на телефоне; текст при
высокой картинке висел по центру карточки (`align-items: center`); у третьего
шага картинки не было вовсе (заглушка `.land-step-noimg`, из-за которой его
текст ещё и съезжал влево относительно первых двух). Стало: размер задаёт
`clamp(96px, 14vw, 156px)` с `aspect-ratio: 4 / 3` — минимум 96px на самом
узком экране (вместо 54px, +78 %) и до 156px на широком (вместо 84px); текст
поднят к верхнему краю (`align-items: flex-start`); третий шаг получил
иллюстрацию `/img/manual/points.jpg`, сгенерированную в той же стилизации, что
`scan.jpg` и `report.jpg` (светло-серый фон, индиговые контуры, оранжевые и
зелёные акценты), в той же структуре `<li><img><div><b><span>`.

**MAX.** Причина отказа подписи найдена эмпирически: initData, собранная так,
как её отдаёт платформа (`urlencode`, пробел → «+»), у прежнего валидатора
давала `bad_hash`, а незакодированная — проходила. Прежний разбор декодировал
только `%XX` (`urllib.parse.unquote`), а «+» оставлял как есть; раскрытие «+»
в пробел делает только `unquote_plus` / `parse_qsl`. JSON в поле `user` всегда
содержит пробелы (разделители `json.dumps`), поэтому строка подписи
собиралась с «+» вместо пробелов и HMAC не сходился **ни на одном**
стандартном initData: мини-приложение не поднимало сессию, а вход на сайт
через MAX (device-code, подтверждение в мини-приложении) не работал вовсе —
при верном токене и верном боте. Эталон: схема aiogram в `maxo/utils/webapp.py`
и реализация официального Go-клиента — обе читают пары через `parse_qsl`.
Попутно добавлена страховка Go-клиента (initData, закодированная ЦЕЛИКОМ ещё
раз), сохранены коды ошибок (`format` / `expired` / `bad_hash`), а в отказ и в
ошибку мини-приложения добавлен обходной путь: вход на сайт устроен как
device-code и подтверждается кодом СООБЩЕНИЕМ боту — подпись initData для него
не нужна, поэтому человек больше не остаётся без входа.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import re
import time
import urllib.parse

import pytest

from tests.test_v1690 import BOT_TOKEN, build_init_data

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "app/static/css/app.css"
INDEX = ROOT / "app/static/index.html"
MAXAPP_JS = ROOT / "app/static/maxapp/maxapp.js"
MAXAPP_ROUTER = ROOT / "app/routers/maxapp.py"
POINTS = ROOT / "app/static/img/manual/points.jpg"


def css_code() -> str:
    """CSS без комментариев: пояснения к правкам не должны попадать в проверки."""
    return re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"),
                  flags=re.S)


def html_code() -> str:
    return re.sub(r"<!--[\s\S]*?-->", "", INDEX.read_text(encoding="utf-8"))


# ==========================================================================
# 1. Лендинг: картинки крупные, текст поднят, третий шаг с иллюстрацией
# ==========================================================================
class TestLandingSteps:
    def test_one_image_rule_with_clamp(self):
        """Размер картинок шагов задаёт ОДНО правило с clamp(): прежние три
        значения (84/62/54px) жили в базе и двух media-блоках и уменьшали
        иллюстрации именно на телефонах."""
        code = css_code()
        rules = re.findall(r"\.land-steps img\s*\{[^}]*\}", code)
        assert len(rules) == 1, f"правил для картинок шагов: {len(rules)}"
        rule = rules[0]
        m = re.search(r"width:\s*clamp\(\s*(\d+)px\s*,\s*([\d.]+)vw\s*,\s*(\d+)px\s*\)",
                      rule)
        assert m, f"в правиле нет clamp(): {rule!r}"
        lo, _vw, hi = (int(m.group(1)), float(m.group(2)), int(m.group(3)))
        assert lo >= 96, f"минимум {lo}px — на телефоне иллюстрацию не разглядеть"
        assert hi >= 140, f"максимум {hi}px — на десктиве картинки мелкие"
        assert "aspect-ratio: 4 / 3" in rule
        assert "object-fit: cover" in rule

    def test_no_shrinking_media_overrides(self):
        """Уменьшающие переопределения из media-блоков удалены: именно они
        давали 62×46 и 54×40. Ищем внутри @media-блоков, а не подстроками по
        файлу: height: 40px законно живёт у кнопок панели фильтров."""
        code = css_code()
        for m in re.finditer(r"@media[^{]*\{([\s\S]*?)\n\}", code):
            assert ".land-steps img" not in m.group(1), \
                f"в media-блоке осталось правило картинок шагов: {m.group(0)[:80]!r}"

    def test_text_raised_to_top(self):
        """Текст поднят к верхнему краю карточки: при высокой картинке он
        больше не висит по центру."""
        rule = re.search(r"\.land-steps li\s*\{[^}]*\}", css_code()).group(0)
        assert "align-items: flex-start" in rule

    def test_all_three_steps_have_images(self):
        html = html_code()
        block = html[html.index('id="land-how"'):html.index('id="land-lead"')]
        steps = re.findall(r"<li>.*?</li>", block, flags=re.S)
        assert len(steps) == 3, f"шагов: {len(steps)}"
        for i, step in enumerate(steps, 1):
            assert "<img " in step, f"у шага {i} нет картинки"
            assert 'loading="lazy"' in step
            assert "<b>" in step and "<span>" in step, f"шаг {i} без текста"

    def test_third_step_image_in_same_family(self):
        """Картинка третьего шага лежит рядом с первыми двумя и подключена
        тем же способом."""
        assert POINTS.exists(), "нет /img/manual/points.jpg"
        assert POINTS.stat().st_size > 20_000, "файл подозрительно мал"
        html = html_code()
        for name in ("scan.jpg", "report.jpg", "points.jpg"):
            assert f'/img/manual/{name}"' in html, f"{name} не подключена"
        block = html[html.index('id="land-how"'):html.index('id="land-lead"')]
        assert block.count("/img/manual/") == 3

    def test_placeholder_class_is_gone(self):
        """Класс-заглушка удалён и из разметки, и из CSS: иначе проверка
        «каждый класс из CSS применяется» нашла бы мёртвое правило."""
        assert "land-step-noimg" not in html_code()
        assert "land-step-noimg" not in css_code()

    def test_thread_and_style_kept(self):
        """«Не теряй нить и стиль»: карточки шагов остались теми же
        (полупрозрачная белая подложка, граница темы, радиус 14px), порядок
        шагов и тексты не менялись."""
        rule = re.search(r"\.land-steps li\s*\{[^}]*\}", css_code()).group(0)
        assert "rgba(255, 255, 255, .6)" in rule
        assert "border: 1px solid var(--border)" in rule
        assert "border-radius: 14px" in rule
        html = html_code()
        for needle in ("1. Отсканируйте QR-код чека",
                       "2. Мы проверяем чек по официальным источникам",
                       "3. Получайте баллы"):
            assert needle in html, f"потерян текст шага: {needle}"


# ==========================================================================
# 2. MAX: подпись initData и вход через свой аккаунт
# ==========================================================================
def _signed_init_data(token: str, user_id: int = 90917036,
                      auth_date: int | None = None) -> str:
    """initData ровно в том виде, в котором её отдаёт платформа MAX:
    значения закодированы urlencode (пробел → «+»), подписана строка из
    РАСКРЫТЫХ значений."""
    data = {"auth_date": str(auth_date or int(time.time())),
            "query_id": f"q{user_id}",
            "user": json.dumps({"id": user_id, "first_name": "Владелец",
                                "last_name": "Ямастер",
                                "username": f"u{user_id}",
                                "language_code": "ru"},
                               ensure_ascii=False)}
    pairs = sorted(data.items())
    launch = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, launch.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(dict(pairs, hash=sig))


def _setup_token(client) -> None:
    from app.database import SessionLocal
    from app.services import appsettings
    s = SessionLocal()
    try:
        appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
        appsettings.set_setting(s, "pool_enabled", "1")
    finally:
        s.rollback(); s.close()


class TestMaxSignature:
    def test_platform_encoded_init_data_is_accepted(self):
        """Главный регресс выпуска: initData с «+» (как у платформы)
        проходит. До правки — bad_hash при верном токене."""
        from app.services import maxapp as ma
        user, err = ma.validate_init_data(_signed_init_data(BOT_TOKEN),
                                          BOT_TOKEN)
        assert err is None and user["id"] == 90917036, err

    def test_session_endpoint_returns_token(self, client):
        """Сквозная проверка входа: мини-приложение получает пул-JWT, то есть
        «зарегистрироваться или войти через свой аккаунт» работает."""
        _setup_token(client)
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data": _signed_init_data(BOT_TOKEN)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"] and body["me"]["max_id"] == "90917036"

    def test_foreign_bot_gets_actionable_refusal(self, client):
        """Подпись другого бота отклоняется, но в ответе есть обходной путь:
        вход на сайт device-code'ом с подтверждением сообщением боту."""
        _setup_token(client)
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data":
                              _signed_init_data("другой-бот-токен-000")})
        assert r.status_code == 401
        detail = r.json()["detail"]
        assert "Войти на сайт можно и без мини-приложения" in detail
        assert "кодом сообщением боту" in detail

    def test_unparsable_init_data_says_so(self, client):
        """Нераспознанный initData больше не маскируется под «подпись не
        совпала»: текст предлагает обновить MAX и даёт обходной путь."""
        _setup_token(client)
        r = client.post("/api/v1/maxapp/session",
                        json={"init_data": "это не запрос вовсе"})
        assert r.status_code == 401
        assert "нераспознанном виде" in r.json()["detail"]

    def test_error_codes_are_distinct(self):
        """format / expired / bad_hash не сливаются: по ним роутер выбирает
        текст ответа."""
        from app.services import maxapp as ma
        assert ma.validate_init_data("", BOT_TOKEN)[1] == "format"
        old = int(time.time()) - 25 * 3600
        assert ma.validate_init_data(_signed_init_data(BOT_TOKEN, 1, old),
                                     BOT_TOKEN)[1] == "expired"
        assert ma.validate_init_data(_signed_init_data("чужой-токен"),
                                     BOT_TOKEN)[1] == "bad_hash"

    def test_miniapp_offers_the_site_on_session_error(self):
        """В ошибке сессии мини-приложение даёт короткий путь на сайт: кнопка
        «Открыть сайт» и пояснение про вход кодом."""
        js = MAXAPP_JS.read_text(encoding="utf-8")
        assert 'id="mx-site"' in js
        assert "location.href = 'https://chek.ymaster.ru/'" in js
        assert "кодом — сообщением этому боту в чате" in js

    def test_router_bypass_text_in_source(self):
        """Обходной путь присутствует в тексте отказа роутера для обеих
        причин (format и bad_hash)."""
        src = MAXAPP_ROUTER.read_text(encoding="utf-8")
        assert src.count("Войти на сайт можно и без мини-приложения") >= 1
        assert 'if err == "format":' in src


# ==========================================================================
# 3. Релиз
# ==========================================================================
class TestRelease:
    # v1.70.41: точки синхронизации живут дальше; свою версию и
    # требование «своя запись первая» проверяет test_v17041.
    VERSION = "1.70.46"

    def test_all_sync_points(self):
        for p in (ROOT / "app/config.py", INDEX, ROOT / "app/static/sw.js",
                  ROOT / "app/static/manifest.webmanifest",
                  ROOT / "app/static/maxapp/index.html"):
            assert self.VERSION in p.read_text(encoding="utf-8"), p.name

    def test_cache_busters(self):
        html = INDEX.read_text(encoding="utf-8")
        assert f"/css/app.css?v={self.VERSION}" in html
        assert f"/js/app.js?v={self.VERSION}" in html
        mx = (ROOT / "app/static/maxapp/index.html").read_text(encoding="utf-8")
        assert f"maxapp.js?v={self.VERSION}" in mx

    def test_whats_new_entry_is_in_history(self):
        """v1.70.37: требование «своя запись первая» принадлежит текущему
        выпуску и проверяется в test_v17037; здесь остаётся наличие записи
        v1.70.36 в истории и её числа — они не должны теряться при правках."""
        js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        assert "'1.70.36':" in js, "запись v1.70.36 пропала из истории"
        entry = js[js.index("'1.70.36':"):][:9000]
        for needle in ("плюс", "clamp", "подпись"):
            assert needle in entry, f"в описании выпуска нет {needle!r}"

    def test_changelog_entry(self):
        assert f"## [{self.VERSION}]" in (ROOT / "CHANGELOG.md").read_text(
            encoding="utf-8")

    def test_journal_entry(self):
        j = (ROOT / "docs/ui_journal.md").read_text(encoding="utf-8")
        assert f"## v{self.VERSION}" in j
        for needle in ("unquote_plus", "parse_qsl", "clamp(96px",
                       "flex-start", "points.jpg"):
            assert needle in j, f"в журнале нет {needle!r}"
