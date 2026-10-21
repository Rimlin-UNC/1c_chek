# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.41: выпуск «Протестируй».
#
# Требование владельца: «Протестируй» — прогнать поточную сдачу v1.70.40
# готовыми средствами тестировщика, а найденное устранить, соблюдая
# «Главное правило не сломай программу».
#
# Живой прогон нашёл три дефекта приёма (описание и измерения — в
# CHANGELOG.md и docs/ui_journal.md, раздел v1.70.41):
#   22) слияние гостевой истории переносило 2 таблицы из 10 и удаляло гостя —
#       регистрация, вход и сдача чека из кабинета отвечали 500
#       (FOREIGN KEY constraint failed);
#   23) чек терялся, если донор регистрировался, пока чек проверялся в фоне
#       (FOREIGN KEY / ObjectDeletedError), а ответ «Чек принят» уже ушёл;
#   24) баллы терялись при параллельном приёме пачки (чтение-изменение-запись
#       в памяти сессии): 10 принятых чеков дали 9 баллов.
#
# Функциональные регрессионные замки живут в tests/test_v17040.py (раздел 4б);
# здесь — ритуал выпуска, замки на исходник (чтобы исправление не «поплыло»
# при будущей правке) и сквозной путь донора целиком.
#
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | info@ymaster.ru
# ======================================================================
import re
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from app.database import SessionLocal, engine
from app.pool import accounts, ingest
from app.pool import router_public as public
from app.pool.models import PoolReceipt, PoolUser, ensure_pool_schema

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/static/js/app.js"
ACCOUNTS = ROOT / "app/pool/accounts.py"
INGEST = ROOT / "app/pool/ingest.py"
PUBLIC = ROOT / "app/pool/router_public.py"

ensure_pool_schema(engine)      # добавляющая миграция (idempotent) до тестов

_SEQ = {"n": 91000}


def _read(path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p.read_text(encoding="utf-8")


def _triple():
    """Уникальная тройка ФН/ФД/ФП: UNIQUE-индекс пула глобальный."""
    _SEQ["n"] += 1
    k = _SEQ["n"]
    return f"999907890{k:06d}"[:20], str(k)[:10], f"780{k:06d}"[:20]


def _src(fn) -> str:
    """Исходник функции без docstring: в пояснении законно упомянуть то,
    чего в коде уже нет (например, прежнюю ошибку)."""
    import ast
    import inspect
    source = inspect.getsource(fn)
    tree = ast.parse(textwrap.dedent(source))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        first, last = body[0].lineno, body[0].end_lineno
        keep = [ln for i, ln in enumerate(source.splitlines(), 1)
                if not (first <= i <= last)]
        return "\n".join(keep)
    return source


def _reset_auth_hits():
    """Сброс счётчика попыток регистрации/входа.

    В полном прогоне воркер накапливает попытки соседних тестов, и без сброса
    своя регистрация получает честный 429 вместо проверяемого поведения
    (так же поступают фикстуры test_v1310/test_v1320).
    """
    from app.pool import router_auth
    router_auth._HITS.clear()


def _enable_pool(db):
    from app.services import appsettings
    appsettings.set_setting(db, "pool_enabled", "1")


# ======================================================================
# 1. Ритуал выпуска v1.70.41
# ======================================================================
class TestRelease17041:
    VERSION = "1.70.41"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read("app/config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read("app/static/sw.js")
        assert f'"version": "{self.VERSION}"' in _read(
            "app/static/manifest.webmanifest")
        idx = _read("app/static/index.html")
        assert f"app.css?v={self.VERSION}" in idx
        assert f"app.js?v={self.VERSION}" in idx
        assert f"maxapp.js?v={self.VERSION}" in _read(
            "app/static/maxapp/index.html")

    def test_cache_busters_in_service_worker(self):
        sw = _read("app/static/sw.js")
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?\'([\d.]+)\'", js)
        assert m and m.group(1) == self.VERSION, "запись выпуска не первая"

    def test_whats_new_entry(self):
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.40': [", 1)[0]
        assert block.count("{ icon:") == 4
        for part in ("регистрац", "балл", "потер", "фон", "чек"):
            assert part in block, f"в «Что нового» нет «{part}»"

    def test_whats_new_has_no_foreign_letters(self):
        """В прозе для донора не должно быть посторонних письмен."""
        js = _read(APP_JS)
        block = js.split(f"'{self.VERSION}': [", 1)[1].split("'1.70.40': [", 1)[0]
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", block)
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), f"постороннее письмо {ch!r}"
        allowed = {"IP", "MAX", "SQL", "XSS", "SAVEPOINT"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read("CHANGELOG.md")
        assert f"## [{self.VERSION}]" in text
        sec = text.split(f"## [{self.VERSION}]", 1)[1].split("## [1.70.40]", 1)[0]
        for needle in ("Дефект 22", "Дефект 23", "Дефект 24",
                       "FOREIGN KEY", "SAVEPOINT", "coalesce"):
            assert needle in sec, f"в CHANGELOG нет «{needle}»"

    def test_journal_entry(self):
        j = _read("docs/ui_journal.md")
        assert f"## v{self.VERSION} —" in j, "в журнале нет раздела выпуска"
        sec = j.split(f"## v{self.VERSION} —", 1)[1].split("## v1.70.40 —", 1)[0]
        for needle in ("merge_guest_into_account", "ObjectDeletedError",
                       "begin_nested", "coalesce", "vid", "Измерение",
                       "Воспроизведение", "pool-web-ingest"):
            assert needle in sec, f"в журнале нет «{needle}»"
        # решение «не чинить» обязано быть записано, иначе его примут за пропуск
        assert "не чинить отказом после обещания" in sec

    def test_previous_release_stays_in_history(self):
        """Прокат версии не стирает запись предыдущего выпуска."""
        assert "'1.70.40': [" in _read(APP_JS)
        assert "## [1.70.40]" in _read("CHANGELOG.md")
        assert "## v1.70.40 —" in _read("docs/ui_journal.md")
        assert (ROOT / "app/pool/batch.py").is_file()


# ======================================================================
# 2. Замок дефекта 22: слияние переносит всю историю и не ломает запрос
# ======================================================================
TEN_TABLES = ("PoolReceipt", "PoolPoint", "PoolConsent", "PoolToken",
              "PoolFingerprint", "PoolIpLog", "PoolSignal", "PoolReferral",
              "PoolAchievement", "PoolWithdrawal")


class TestMergeCoversWholeSchema:
    def test_every_dependent_model_is_mentioned(self):
        """Все десять таблиц, ссылающихся на pool_users.id, обязаны быть
        обработаны: иначе удаление гостя снова даст FOREIGN KEY."""
        src = _src(accounts.merge_guest_into_account)
        missing = [m for m in TEN_TABLES if m not in src]
        assert not missing, f"не переносится: {missing}"

    def test_runs_in_savepoint_with_flush_inside(self):
        src = _src(accounts.merge_guest_into_account)
        assert "db.begin_nested()" in src
        assert src.index("db.flush()") < src.index("sp.commit()"), \
            "FK-конфликт проявляется только на сбросе внутри SAVEPOINT"
        assert "sp.rollback()" in src
        tail = src.split("except Exception", 1)[1]
        assert tail.index("sp.rollback()") < tail.index("log.warning") \
            < tail.index("return 0"), \
            "после отката слияние обязано записать причину и вернуть 0"

    def test_failure_is_logged_not_raised(self):
        src = _src(accounts.merge_guest_into_account)
        assert "except Exception" in src
        assert "log.warning" in src
        assert "logging.getLogger" in _read(ACCOUNTS), \
            "журнал модуля нужен, чтобы причина откладывания слияния была видна"

    def test_unique_conflicts_are_resolved_explicitly(self):
        src = _src(accounts.merge_guest_into_account)
        assert "PoolAchievement.code.in_(own_codes)" in src, \
            "uq_pool_ach_user_code: дубликат достижения надо убрать"
        assert "PoolReferral.referred_id == account.id" in src, \
            "uq_pool_ref_referred: привязка аккаунта должна побеждать"
        assert ".delete(synchronize_session=False)" in src

    def test_antifraud_history_is_moved_not_deleted(self):
        """Сигналы, отпечатки и журнал IP переносятся: стирать их — значит
        дать возможность обнулить репутацию простой регистрацией."""
        src = _src(accounts.merge_guest_into_account)
        for model in ("PoolSignal", "PoolFingerprint", "PoolIpLog"):
            assert model in src
            block = src.split(f"{model},", 1)
            assert block, model
        assert "PoolSignal.code" not in src, \
            "сигналы не должны фильтроваться или удаляться по коду"


# ======================================================================
# 3. Замок дефекта 23: vid наследуется, фоновый приём переспрашивает владельца
# ======================================================================
class TestMergeRaceLocks:
    def test_vid_is_freed_before_it_is_inherited(self):
        """vid уникален: сначала освобождаем, отдельным сбросом, потом
        присваиваем аккаунту — иначе два UPDATE конфликтуют порядком."""
        src = _src(accounts.merge_guest_into_account)
        assert "guest.vid = None" in src
        assert "account.vid = inherited" in src
        assert src.index("guest.vid = None") < src.index("account.vid = inherited")
        assert "db.flush()" in src.split("guest.vid = None", 1)[1].split(
            "account.vid = inherited", 1)[0], "между ними обязан быть сброс"

    def test_own_account_vid_is_not_overwritten(self):
        src = _src(accounts.merge_guest_into_account)
        assert 'if not (account.vid or "").strip()' in src

    def test_background_catches_both_faces_of_the_race(self):
        src = _read(PUBLIC)
        start = src.index("    def _bg():")
        end = src.index('threading.Thread(target=_bg, name="pool-web-ingest"',
                        start)
        body = src[start:end]
        assert "except (IntegrityError, ObjectDeletedError):" in body
        assert "s.rollback()" in body
        assert "PoolUser.vid == vid" in body
        assert body.count("ingest.ingest_parsed(") == 2, \
            "повтор приёма обязан быть ровно один"

    def test_stale_id_is_captured_before_rollback(self):
        """После rollback строка гостя мертва: чтение u.id дало бы
        ObjectDeletedError вместо полезного повтора (так и было в первой
        версии исправления)."""
        src = _read(PUBLIC)
        start = src.index("    def _bg():")
        body = src[start:src.index('threading.Thread(target=_bg', start)]
        assert "stale_id = u.id" in body
        assert body.index("stale_id = u.id") < body.index("s.rollback()")
        assert "fresh.id == stale_id" in body

    def test_exception_types_are_imported(self):
        src = _read(PUBLIC)
        assert "from sqlalchemy.exc import IntegrityError" in src
        assert "from sqlalchemy.orm.exc import ObjectDeletedError" in src


# ======================================================================
# 4. Замок дефекта 24: начисление баллов атомарно
# ======================================================================
class TestAtomicPointsLocks:
    def test_increment_happens_in_the_database(self):
        src = _src(ingest.add_points)
        assert "func.coalesce(PoolUser.points, 0) + int(delta)" in src
        assert ".update(" in src
        assert 'synchronize_session="fetch"' in src

    def test_no_read_modify_write_left(self):
        """Прежняя строка теряла начисления; в памяти прибавляем только тогда,
        когда строка участника ещё не записана."""
        src = _src(ingest.add_points)
        assert src.count("user.points = (user.points or 0) + delta") == 1
        assert "if not changed:" in src
        assert src.index("if not changed:") < src.index(
            "user.points = (user.points or 0) + delta")

    def test_ledger_row_is_still_written(self):
        src = _src(ingest.add_points)
        assert "PoolPoint(user_id=user.id, delta=delta, reason=reason" in src
        assert "if delta == 0:" in src and "return" in src

    def test_answer_still_reports_user_points(self):
        """Ответ приёма обязан показывать точный счёт — для этого fetch-
        синхронизация, а не «прибавили в памяти»."""
        src = _read(INGEST)
        assert '"user_points": user.points' in src


# ======================================================================
# 5. Сквозной путь донора: сдал без регистрации → зарегистрировался → сдаёт
# ======================================================================
class TestDonorJourney:
    def test_guest_scans_then_registers_then_submits(self, client, monkeypatch):
        """Полный путь, на котором раньше падало: гость с сигналом антифрода
        регистрируется и тут же сдаёт чек из кабинета."""
        from app.pool.models import PoolSignal
        from app.services.external import ExternalItem, ExternalResult

        ticket = ExternalResult(
            ok=True, source="test", found=True, message="OK",
            date_time=datetime(2026, 9, 5, 14, 30), total_sum=1250.0,
            operation=1, merchant_name="ООО Ромашка", merchant_inn="7704001275",
            merchant_address="г. Тихвин, ул. Советская 12",
            items=[ExternalItem(name="Кофе", quantity=1.0, price=1250.0,
                                total=1250.0, vat_rate="20", vat_sum=208.33)],
            raw={"document": {"receipt": {"totalSum": 125000}}})
        monkeypatch.setattr(ingest, "_fetch_details", lambda *a, **k: ticket)

        db = SessionLocal()
        try:
            _enable_pool(db)
            _SEQ["n"] += 1
            # балл за уже сданный чек начислен: иначе ожидание суммы неверно
            guest = PoolUser(vid=f"v17041-journey-{_SEQ['n']}", points=1)
            db.add(guest)
            db.commit()
            db.refresh(guest)
            fn0, fd0, fp0 = _triple()
            db.add(PoolReceipt(pool_user_id=guest.id, fn=fn0, fd=fd0, fp=fp0,
                               status="verified", total_sum=10.0,
                               points_awarded=1))
            db.add(PoolSignal(user_id=guest.id, code="REPEAT_SUMS", severity=2,
                              points=20, status="open"))
            db.commit()
            # vid фиксируем до слияния: после него строка гостя удалена, и
            # обращение к атрибуту даст ObjectDeletedError
            gvid = guest.vid
            cookie = {"pool_vid": f"{gvid}.{public._sign(gvid)}"}
            public._IP_HITS.clear()
            _reset_auth_hits()

            _SEQ["n"] += 1
            email = f"journey-{_SEQ['n']}@test.ru"
            reg = client.post("/api/v1/pool-auth/register",
                              json={"email": email, "password": "parol-1234"},
                              cookies=cookie)
            assert reg.status_code == 200, reg.text[:300]
            assert reg.json()["merged_receipts"] == 1, "история гостя не перенесена"
            tok = {"Authorization": f"Bearer {reg.json()['token']}"}

            fn, fd, fp = _triple()
            qr = f"t=20260905T1430&s=1250.00&fn={fn}&i={fd}&fp={fp}&n=1"
            # сессионный клиент хранит cookie соседних тестов: гость из чужого
            # теста законно прилился бы к этому аккаунту и добавил бы свой чек
            client.cookies.clear()
            r = client.post("/api/v1/public/pool/check",
                            json={"qr_text": qr, "offerta": True, "hp": "",
                                  "form_ms": 5000}, headers=tok)
            assert r.status_code == 200, r.text[:300]
            assert r.json().get("accepted"), r.text[:200]

            import threading
            import time
            end = time.time() + 25
            while time.time() < end and [t for t in threading.enumerate()
                                         if t.name == "pool-web-ingest"]:
                time.sleep(0.05)

            db.expire_all()
            acc = db.query(PoolUser).filter(PoolUser.email == email).one()
            rows = (db.query(PoolReceipt)
                    .filter(PoolReceipt.pool_user_id == acc.id,
                            PoolReceipt.fn.in_([fn0, fn])).all())
            assert len(rows) == 2, \
                f"ожидали гостевой и новый чек у аккаунта, нашли {len(rows)}"
            assert acc.points == 2, f"баллы потеряны: {acc.points} вместо 2"
            assert (db.query(PoolSignal)
                    .filter(PoolSignal.user_id == acc.id).count()) == 1, \
                "антифрод-история обязана пережить слияние"
            assert db.query(PoolUser).filter(PoolUser.vid == gvid).count() == 1, \
                "vid обязан остаться ровно один — у аккаунта"
        finally:
            db.rollback()
            db.close()

    def test_no_new_settings_and_limits_untouched(self):
        """Выпуск не добавил настроек и не сдвинул пороги поточной сдачи."""
        from app.config import settings
        from app.pool import antifraud, batch
        assert ingest.DAILY_LIMIT == 100
        assert batch.SESSION_CAP == 100
        assert batch.KEYS_MAX == 200
        assert antifraud.FLOOD_PER_HOUR == ingest.DAILY_LIMIT
        assert public.WEB_IP_HOURLY_LIMIT == 20
        assert public.WEB_IP_HOURLY_LIMIT_AUTH == ingest.DAILY_LIMIT + 60
        assert public.HONEYPOT_MS == 800
        assert not hasattr(settings, "MERGE_STRICT"), \
            "новая настройка не заявлялась"


# ======================================================================
# 6. Программа не сломана: палитра, разметка и клиент не тронуты
# ======================================================================
class TestNothingElseBroken:
    def test_brand_palette_untouched(self):
        """Выпуск правит только серверный приём — палитра не менялась."""
        css = _read("app/static/css/app.css")
        head = css.split(":root", 2)[1]
        for token in ("--accent", "--info", "--bad"):
            assert token in head, token

    def test_batch_ui_still_in_place(self):
        js = _read(APP_JS)
        for needle in ('id="pub-batch-start"', "const PUB_BATCH_CAP = 100;",
                       "pubBatchSummaryText", "scanSessReset"):
            assert needle in js, needle

    def test_maxapp_batch_still_in_place(self):
        js = _read(ROOT / "app/static/maxapp/maxapp.js")
        for needle in ("batchHandle", "batchPoll", "batchAskMore", "stopBatch"):
            assert needle in js, needle

    def test_dedup_wording_untouched(self):
        """Тексты дубликата — существующий механизм, их не переписывали."""
        assert "Этот чек уже в пуле — спасибо, что проверили!" in _read(INGEST)

    @pytest.mark.parametrize("path", ("app/pool/accounts.py", "app/pool/ingest.py",
                                      "app/pool/router_public.py"))
    def test_modules_compile(self, path):
        import py_compile
        py_compile.compile(str(ROOT / path), doraise=True)
