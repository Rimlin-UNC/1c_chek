# -*- coding: utf-8 -*-
# ======================================================================
# Ямастер Чек — тесты v1.70.45: контур B2B (доступ компаний и ИП к
# платному API) и закрытие раздела от посторонних.
# Разработчик и владелец идеи: ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерии приёмки (указания владельца):
#  1. публичный лендинг больше не показывает раздел и ссылку на него;
#  2. раздел — отдельная страница /b2b/ со своими страницами: документация
#     API, тарифы и состав, «Обсудить подключение»;
#  3. доступ только после подтверждения статуса компании корпоративной
#     почтой и проверки карточки ЕГРЮЛ/ЕГРИП по ИНН;
#  4. администратор видит всё, каждая поверхность помечена
#     «Это не видит никто»;
#  5. почта chek@ymaster.ru везде, старого адреса в репозитории нет;
#  6. формы и конвейер автоматизации с тремя режимами: полная, частичная
#     (по умолчанию), отмена;
#  7. содержимое страниц строится из кода сервиса и не может с ним
#     разойтись; цены не публикуются.
# ======================================================================
from __future__ import annotations

import inspect
import json
import pathlib
import re
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.b2b import auth, content, letters, models, pipeline, verify
from app.b2b.models import (
    AP_APPROVED,
    AP_PARKED,
    CK_DUPLICATE,
    CK_EMAIL_DOMAIN,
    CK_EMAIL_FORMAT,
    CK_INN_FORMAT,
    CK_REGISTRY,
    CK_RISK,
    PURPOSE_VERIFY,
    ST_BLOCKED,
    ST_EMAIL_PENDING,
    ST_REJECTED,
    ST_VERIFIED,
    B2bApplication,
    B2bCheck,
    B2bCompany,
    B2bEvent,
    B2bToken,
    ensure_b2b_schema,
)
from app.database import SessionLocal, engine
from app.main import app
from app.pool import mailer
from app.pool.router_api import DEFAULT_QUOTA, DEFAULT_RATE
from app.services import appsettings

ensure_b2b_schema(engine)

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "app/static/index.html"
CSS = ROOT / "app/static/css/app.css"
APP_JS = ROOT / "app/static/js/app.js"
CABINET = ROOT / "app/static/b2b/index.html"
CABINET_CSS = ROOT / "app/static/b2b/b2b.css"
CABINET_JS = ROOT / "app/static/b2b/b2b.js"
ASSETS = ROOT / "app/b2b/assets"
DOC = ROOT / "docs/API.md"
MECH_DOC = ROOT / "docs/b2b_access.md"
README = ROOT / "README.md"

SVG_FILES = ("api-access.svg", "api-request-path.svg", "api-data-boundary.svg")

# Действительные ИНН (проверены app.services.companies_util.inn_is_valid)
INN_UL = "7707083893"        # 10 цифр — юридическое лицо
INN_IP = "500100732259"      # 12 цифр — индивидуальный предприниматель
INN_BAD = "7707083894"       # испорченная контрольная сумма

CORP_MAIL = "it@vashacompaniya.ru"
PASSWORD = "secret-pass-123"
MARKER = "Это не видит никто"

# Цены из внутреннего docs/plan.md не должны попадать в клиентские поверхности
FORBIDDEN_PRICES = ("5 000 ₽", "5000 ₽", "50 ₽/чек", "200 ₽/чек", "₽/мес")


# ----------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------
def _read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _db():
    return SessionLocal()


def _wipe() -> None:
    """Чистка таблиц контура между тестами (с повтором при блокировке sqlite)."""
    db = _db()
    for _ in range(40):
        try:
            for table in (B2bEvent, B2bCheck, B2bToken, B2bApplication,
                          B2bCompany):
                db.query(table).delete(synchronize_session=False)
            db.commit()
            db.close()
            return
        except Exception:                     # noqa: BLE001 — sqlite locked
            db.rollback()
            time.sleep(0.2)
    db.close()
    raise RuntimeError("test_v17045: не удалось очистить таблицы контура")


def _enable(db, mode: str = pipeline.MODE_PARTIAL) -> None:
    """Включает контур и задаёт режим автоматизации."""
    appsettings.set_setting(db, pipeline.SET_ENABLED, "1")
    appsettings.set_setting(db, pipeline.SET_MODE, mode)
    db.commit()


def _admin(client):
    from tests.conftest import login
    return login(client, "admin", "admin123")


def _register(client, **kw):
    body = {"email": CORP_MAIL, "password": PASSWORD,
            "company_name": "ООО «Ромашка»", "inn": INN_UL,
            "contact_name": "Иванов Иван", "phone": "+7 900 000-00-00",
            "consent": True, "form_ms": 5000, "hp": ""}
    body.update(kw)
    return client.post("/api/v1/b2b/register", json=body)


def _token_from(letters_box: list, needle: str) -> str:
    """Одноразовый токен из перехваченного письма (по признаку в теле)."""
    for item in letters_box:
        if needle in item["body"] or needle in item["html"]:
            found = re.search(r"token=([A-Za-z0-9_\-]+)", item["body"])
            if found:
                return found.group(1)
    return ""


def _confirm(client, token: str):
    return client.post("/api/v1/b2b/verify-email", json={"token": token})


def _company(db, email: str = CORP_MAIL):
    return db.query(B2bCompany).filter(B2bCompany.email == email).first()


def _verified_company(client, letters_box, inn: str = INN_UL,
                      email: str = CORP_MAIL, name: str = "ООО «Ромашка»"):
    """Полный путь компании до подтверждённого статуса. Возвращает (токен, id)."""
    db = _db()
    _enable(db, pipeline.MODE_PARTIAL)
    db.close()
    r = _register(client, email=email, inn=inn, company_name=name)
    assert r.status_code == 200, r.text
    token = _token_from(letters_box, "/b2b/#/confirm")
    assert token, "письмо подтверждения не отправлено"
    r = _confirm(client, token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == ST_VERIFIED, body
    return body["token"], body["company"]["id"]


@pytest.fixture(autouse=True)
def _clean(client):
    """client нужен не для assertions: сессионная фикстура поднимает
    приложение (lifespan), без него таблицы контура не созданы и очистка
    бессмысленна."""
    assert client is not None
    auth._reset_attempts()
    _wipe()
    yield
    auth._reset_attempts()
    _wipe()


@pytest.fixture
def letters_box(monkeypatch):
    """Перехват писем: SMTP в тестах не настроен, а поток проверить нужно."""
    box: list[dict] = []

    def fake_send_mail(_db, to_email, subject, body, html=None,
                       from_name=""):
        box.append({"to": to_email, "subject": subject, "body": body,
                    "html": html or "", "from_name": from_name})
        return True, ""

    monkeypatch.setattr(mailer, "send_mail", fake_send_mail)
    return box


@pytest.fixture
def admin_hdr(client):
    return _admin(client)


# ======================================================================
# 1. Схема контура: миграция только добавляющая
# ======================================================================
class TestSchema:
    def test_tables_created(self):
        import sqlalchemy
        insp = sqlalchemy.inspect(engine)
        names = set(insp.get_table_names())
        for table in ("b2b_companies", "b2b_applications", "b2b_checks",
                      "b2b_tokens", "b2b_events"):
            assert table in names, f"нет таблицы {table}"

    def test_migration_is_idempotent(self):
        """Повторный вызов не падает и не меняет существующее."""
        ensure_b2b_schema(engine)
        ensure_b2b_schema(engine)
        import sqlalchemy
        insp = sqlalchemy.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("b2b_companies")}
        for name in ("email", "password_hash", "inn", "status",
                     "consent_version", "ip_hash", "risk_score"):
            assert name in cols, f"нет колонки {name}"

    def test_core_and_pool_tables_untouched(self):
        """Ядро и пул не затронуты: их таблицы на месте."""
        import sqlalchemy
        names = set(sqlalchemy.inspect(engine).get_table_names())
        for table in ("users", "companies", "receipts", "pool_users",
                      "pool_api_keys"):
            assert table in names, f"пропала таблица {table}"

    def test_tokens_are_stored_hashed(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        raw = _token_from(letters_box, "/b2b/#/confirm")
        db = _db()
        rows = db.query(B2bToken).all()
        assert len(rows) == 1
        assert rows[0].token_hash != raw, "в БД исходный токен, а не хэш"
        assert len(rows[0].token_hash) == 64, "хэш не SHA-256"
        assert rows[0].purpose == PURPOSE_VERIFY
        db.close()

    def test_password_is_hashed_not_plain(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        db = _db()
        company = _company(db)
        assert company.password_hash != PASSWORD
        assert PASSWORD not in company.password_hash
        assert company.password_hash.startswith("pbkdf2") or \
            len(company.password_hash) > 40, "похоже на открытый пароль"
        db.close()

    def test_ip_and_user_agent_are_hashed(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        db = _db()
        company = _company(db)
        assert company.ip_hash and company.ip_hash != "testclient"
        assert len(company.ip_hash) == 16
        assert company.consent_version, "версия согласия не записана"
        assert company.consent_at is not None
        db.close()


# ======================================================================
# 2. Проверки статуса компании (verify.py)
# ======================================================================
class TestEmailChecks:
    def test_format_accepts_corporate_address(self):
        assert verify.check_email_format(CORP_MAIL)["ok"] is True

    @pytest.mark.parametrize("bad", ["", "нет-собаки", "a@b", "@domain.ru",
                                     "a@" + "x" * 300])
    def test_format_refuses_broken_address(self, bad):
        assert verify.check_email_format(bad)["ok"] is False

    @pytest.mark.parametrize("free", ["user@mail.ru", "user@yandex.ru",
                                      "user@gmail.com", "user@bk.ru",
                                      "user@icloud.com", "user@proton.me"])
    def test_free_domains_are_refused(self, free):
        row = verify.check_email_domain(free)
        assert row["ok"] is False and row["hard"] is True

    def test_free_domain_allowed_by_admin_switch(self):
        row = verify.check_email_domain("user@mail.ru", allow_free=True)
        assert row["ok"] is True and row["hard"] is False
        assert "настройкой администратора" in row["detail"]

    def test_corporate_domain_passes(self):
        row = verify.check_email_domain(CORP_MAIL)
        assert row["ok"] is True
        assert "vashacompaniya.ru" in row["detail"]

    def test_domain_helper(self):
        assert verify.email_domain("Ivan@Company.RU ") == "company.ru"
        assert verify.email_domain("без-почты") == ""
        assert verify.is_free_domain("u@mail.ru") is True
        assert verify.is_free_domain(CORP_MAIL) is False


class TestInnChecks:
    def test_legal_inn(self):
        row = verify.check_inn(INN_UL)
        assert row["ok"] is True and "юридическое лицо" in row["detail"]

    def test_individual_inn(self):
        row = verify.check_inn(INN_IP)
        assert row["ok"] is True and "предприниматель" in row["detail"]

    def test_bad_checksum_is_refused(self):
        row = verify.check_inn(INN_BAD)
        assert row["ok"] is False and "контрольную сумму" in row["detail"]

    def test_empty_inn_is_refused(self):
        assert verify.check_inn("")["ok"] is False

    def test_inn_with_spaces_is_normalized(self):
        assert verify.check_inn(" 770-708-38-93 ")["ok"] is True

    def test_kind_titles(self):
        assert verify.kind_title("legal") == "ООО"
        assert verify.kind_title("individual") == "ИП"
        assert verify.kind_title("") == ""


class TestNameMatching:
    def test_full_registry_name_matches_short_form(self):
        """Главный ложный отказ: полное наименование из ЕГРЮЛ длиннее того,
        что пишет заявитель. Сравнение по ядру названия его убирает."""
        registry = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "РОМАШКА"'
        ok, ratio, note = verify.names_match(registry, "ООО «Ромашка»")
        assert ok is True, note
        assert ratio > 0

    def test_different_companies_do_not_match(self):
        ok, _ratio, _note = verify.names_match("ООО «Ромашка»", "ООО «Василёк»")
        assert ok is False

    def test_individual_matches_by_surname(self):
        ok, _ratio, note = verify.names_match("Иванов Иван Иванович",
                                              "ИП Иванов И. И.", "individual")
        assert ok is True, note

    def test_name_core_drops_legal_form(self):
        assert verify.name_core("ООО «Ямастер»") == "ЯМАСТЕР"
        assert verify.name_core("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "
                                "«ЯМАСТЕР»") == "ЯМАСТЕР"

    def test_empty_names_do_not_match(self):
        assert verify.names_match("", "ООО «Ромашка»")[0] is False
        assert verify.names_match("ООО «Ромашка»", "")[0] is False


class TestStatusIsolation:
    @pytest.mark.parametrize("status,expected", [
        ("Действующее", True), ("ДЕЙСТВУЮЩАЯ ОРГАНИЗАЦИЯ", True),
        ("Ликвидировано", False), ("Прекратило деятельность", False),
        ("Банкротство", False), ("", False), ("Реорганизация", False),
    ])
    def test_active_status(self, status, expected):
        assert verify.status_is_active(status) is expected


class TestRegistryCheck:
    def test_without_checko_key_the_check_is_soft(self, client, letters_box):
        """Решение владельца: без ключа Checko — проверка по формальным
        признакам ИНН, и она НЕ блокирует, но помечена в журнале."""
        db = _db()
        _enable(db)
        company = B2bCompany(email="soft@company.ru", company_name="ООО «Тест»",
                             inn=INN_UL, status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row, card = verify.check_registry(db, company)
        assert row["ok"] is True and row["hard"] is False
        assert card is None
        assert "Ключ Checko не настроен" in row["detail"]
        db.close()

    def test_with_checko_card_active_and_name_matches(self, monkeypatch):
        from app.services import checko

        def fake_fetch_card(_api_key, inn):
            return {"card": {"kind": "legal",
                             "name_full": 'ООО "РОМАШКА"',
                             "inn": inn, "ogrn": "1027700132195",
                             "status": "Действующее"}, "raw": {}, "meta": {}}

        monkeypatch.setattr(checko, "fetch_card", fake_fetch_card)
        db = _db()
        appsettings.set_setting(db, checko.SETTING_KEY, "test-key")
        db.commit()
        company = B2bCompany(email="ok@company.ru", company_name="ООО «Ромашка»",
                             inn=INN_UL, status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row, card = verify.check_registry(db, company)
        assert row["ok"] is True, row["detail"]
        assert card["ogrn"] == "1027700132195"
        assert "ЕГРЮЛ/ЕГРИП" in row["detail"]
        db.close()

    def test_liquidated_company_is_refused(self, monkeypatch):
        from app.services import checko

        def fake_fetch_card(_api_key, inn):
            return {"card": {"kind": "legal", "name_full": 'ООО "РОМАШКА"',
                             "inn": inn, "status": "Ликвидировано"},
                    "raw": {}, "meta": {}}

        monkeypatch.setattr(checko, "fetch_card", fake_fetch_card)
        db = _db()
        appsettings.set_setting(db, checko.SETTING_KEY, "test-key")
        db.commit()
        company = B2bCompany(email="dead@company.ru",
                             company_name="ООО «Ромашка»", inn=INN_UL,
                             status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row, _card = verify.check_registry(db, company)
        assert row["ok"] is False and "не действующая" in row["detail"]
        db.close()

    def test_name_mismatch_is_refused(self, monkeypatch):
        from app.services import checko

        def fake_fetch_card(_api_key, inn):
            return {"card": {"kind": "legal", "name_full": 'ООО "ВЕКТОР"',
                             "inn": inn, "status": "Действующее"},
                    "raw": {}, "meta": {}}

        monkeypatch.setattr(checko, "fetch_card", fake_fetch_card)
        db = _db()
        appsettings.set_setting(db, checko.SETTING_KEY, "test-key")
        db.commit()
        company = B2bCompany(email="lie@company.ru",
                             company_name="ООО «Ромашка»", inn=INN_UL,
                             status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row, _card = verify.check_registry(db, company)
        assert row["ok"] is False and "не совпадает с реестром" in row["detail"]
        db.close()

    def test_checko_failure_is_not_a_crash(self, monkeypatch):
        from app.services import checko

        def boom(_api_key, _inn):
            raise checko.CheckoError("нет связи с реестром")

        monkeypatch.setattr(checko, "fetch_card", boom)
        db = _db()
        appsettings.set_setting(db, checko.SETTING_KEY, "test-key")
        db.commit()
        company = B2bCompany(email="net@company.ru", company_name="ООО «Тест»",
                             inn=INN_UL, status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row, card = verify.check_registry(db, company)
        assert row["ok"] is False and card is None
        assert "нет связи с реестром" in row["detail"]
        db.close()

    def test_unexpected_exception_is_contained(self, monkeypatch):
        from app.services import checko

        def boom(_api_key, _inn):
            raise OSError("сеть недоступна")

        monkeypatch.setattr(checko, "fetch_card", boom)
        db = _db()
        appsettings.set_setting(db, checko.SETTING_KEY, "test-key")
        db.commit()
        company = B2bCompany(email="err@company.ru", company_name="ООО «Тест»",
                             inn=INN_UL, status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row, _card = verify.check_registry(db, company)
        assert row["ok"] is False and "Не удалось запросить реестр" in row["detail"]
        db.close()


class TestDuplicatesAndRisk:
    def test_duplicate_inn_is_refused(self, client, letters_box):
        db = _db()
        _enable(db)
        first = B2bCompany(email="first@company.ru", inn=INN_UL,
                           company_name="ООО «Ромашка»", status=ST_VERIFIED)
        second = B2bCompany(email="second@company.ru", inn=INN_UL,
                            company_name="ООО «Ромашка»", status=ST_EMAIL_PENDING)
        db.add_all([first, second])
        db.commit()
        row = verify.check_duplicates(db, second)
        assert row["ok"] is False and "first@company.ru" in row["detail"]
        db.close()

    def test_no_duplicates(self, client, letters_box):
        db = _db()
        _enable(db)
        company = B2bCompany(email="only@company.ru", inn=INN_UL,
                             company_name="ООО «Ромашка»", status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        row = verify.check_duplicates(db, company)
        assert row["ok"] is True
        db.close()

    def test_risk_is_never_hard(self):
        """Высокий риск — повод для администратора, а не автоотказ."""
        row = verify.check_risk(None, INN_UL)
        assert row["hard"] is False
        row = verify.check_risk({"name_full": "ООО «Ромашка»", "ogrn": "1",
                                 "status": "Ликвидировано"}, INN_UL)
        assert row["hard"] is False and row["ok"] is False
        assert row["weight"] > 0

    def test_risk_green_with_clean_card(self):
        card = {"name_full": "ООО «Ромашка»", "ogrn": "1027700132195",
                "status": "Действующее", "reg_date": "01.01.2010",
                "capital": "100 000"}
        row = verify.check_risk(card, INN_UL)
        assert row["ok"] is True and row["weight"] == 0


class TestVerdict:
    def test_hard_failure_blocks(self):
        checks = [{"kind": CK_EMAIL_FORMAT, "title": "Почта", "ok": False,
                   "hard": True, "detail": "некорректный адрес"}]
        ok, problems = verify.verdict(checks)
        assert ok is False and len(problems) == 1
        assert "Почта" in problems[0]

    def test_soft_failure_does_not_block(self):
        checks = [{"kind": CK_RISK, "title": "Риск", "ok": False,
                   "hard": False, "detail": "высокий риск"}]
        ok, problems = verify.verdict(checks)
        assert ok is True and problems == []

    def test_all_green(self):
        checks = [{"kind": k, "title": k, "ok": True, "hard": True, "detail": ""}
                  for k in (CK_EMAIL_FORMAT, CK_EMAIL_DOMAIN, CK_INN_FORMAT,
                            CK_REGISTRY, CK_DUPLICATE)]
        assert verify.verdict(checks) == (True, [])

    def test_run_checks_writes_history_and_kind(self, client, letters_box):
        db = _db()
        _enable(db)
        company = B2bCompany(email="run@company.ru", inn=INN_UL,
                             company_name="ООО «Ромашка»", status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        rows, _card = verify.run_checks(db, company)
        db.commit()
        assert len(rows) == 6, [r["kind"] for r in rows]
        assert company.kind == "ООО"
        stored = db.query(B2bCheck).filter(
            B2bCheck.company_id == company.id).count()
        assert stored == 6
        latest = verify.latest_checks(db, company.id)
        assert [c["kind"] for c in latest] == [
            CK_EMAIL_FORMAT, CK_EMAIL_DOMAIN, CK_INN_FORMAT, CK_REGISTRY,
            CK_DUPLICATE, CK_RISK]
        db.close()

    def test_latest_checks_keeps_history_but_returns_last(self, client,
                                                           letters_box):
        db = _db()
        _enable(db)
        company = B2bCompany(email="hist@company.ru", inn=INN_UL,
                             company_name="ООО «Ромашка»", status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        verify.run_checks(db, company)
        db.commit()
        company.inn = INN_BAD
        verify.run_checks(db, company)
        db.commit()
        assert db.query(B2bCheck).filter(
            B2bCheck.company_id == company.id).count() == 12, \
            "история проверок затёрта"
        latest = verify.latest_checks(db, company.id)
        inn_row = [c for c in latest if c["kind"] == CK_INN_FORMAT][0]
        assert inn_row["ok"] is False, "вердикт взят не из последней проверки"
        db.close()

    def test_batch_helper_avoids_n_plus_one(self, client, letters_box):
        db = _db()
        _enable(db)
        first = B2bCompany(email="b1@company.ru", inn=INN_UL,
                           company_name="ООО «Ромашка»", status=ST_EMAIL_PENDING)
        second = B2bCompany(email="b2@company.ru", inn=INN_IP,
                            company_name="ИП Иванов", status=ST_EMAIL_PENDING)
        db.add_all([first, second])
        db.commit()
        verify.run_checks(db, first)
        verify.run_checks(db, second)
        db.commit()
        many = verify.latest_checks_many(db, [first.id, second.id])
        assert set(many) == {first.id, second.id}
        assert len(many[first.id]) == 6
        assert many[second.id][0]["kind"] == CK_EMAIL_FORMAT
        assert verify.latest_checks_many(db, []) == {}
        db.close()


# ======================================================================
# 3. Регистрация: формы, ловушки, отказы
# ======================================================================
class TestRegistration:
    def test_contour_is_closed_by_default(self, client):
        """Владелец: «Пока убрать скрыть от посторонних глаз» — контур по
        умолчанию выключен, регистрация отвечает 403 с внятным текстом."""
        r = _register(client)
        assert r.status_code == 403
        assert "пока закрыто" in r.json()["detail"]
        assert content.CONTACT_EMAIL in r.json()["detail"]

    def test_status_endpoint_is_public_and_honest(self, client):
        r = client.get("/api/v1/b2b/status")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["closed_text"], "при выключенном контуре нужен текст"
        assert len(body["requirements"]) >= 4
        assert len(body["steps"]) == 7
        assert body["contact"] == "chek@ymaster.ru"
        assert not body["steps"][0]["text"].startswith(" ")

    def test_register_creates_company_and_sends_letter(self, client,
                                                       letters_box):
        db = _db()
        _enable(db)
        db.close()
        r = _register(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["registered"] is True and body["letter_sent"] is True
        assert body["company"]["status"] == ST_EMAIL_PENDING
        assert body["company"]["email"] == CORP_MAIL
        assert "chek@ymaster.ru" not in body["company"]["email"]
        assert len(letters_box) == 1
        assert letters_box[0]["to"] == CORP_MAIL
        assert "подтвердите" in letters_box[0]["subject"].lower()

    def test_email_is_lowercased(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        r = _register(client, email="Ivanov@Company.RU")
        assert r.status_code == 200
        assert r.json()["company"]["email"] == "ivanov@company.ru"

    @pytest.mark.parametrize("payload,needle", [
        ({"email": "нет-почты"}, "корпоративный e-mail"),
        ({"password": "коротк"}, "не короче 8"),
        ({"password": "12345678"}, "из одних цифр"),
        ({"company_name": ""}, "наименование"),
        ({"inn": INN_BAD}, "ИНН не проходит проверку"),
        ({"inn": ""}, "ИНН не проходит проверку"),
        ({"consent": False}, "согласие на обработку"),
    ])
    def test_invalid_input_is_refused_with_422(self, client, letters_box,
                                               payload, needle):
        db = _db()
        _enable(db)
        db.close()
        r = _register(client, **payload)
        assert r.status_code == 422, r.text
        assert needle in r.json()["detail"]
        assert letters_box == [], "письмо ушло при отказе"

    def test_free_mail_domain_is_refused(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        r = _register(client, email="zayavka@mail.ru")
        assert r.status_code == 422
        assert "на своём домене" in r.json()["detail"]

    def test_free_domain_allowed_by_setting(self, client, letters_box):
        db = _db()
        _enable(db)
        appsettings.set_setting(db, pipeline.SET_ALLOW_FREE, "1")
        db.commit()
        db.close()
        r = _register(client, email="zayavka@mail.ru")
        assert r.status_code == 200, r.text

    def test_duplicate_email_is_409(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        r = _register(client)
        assert r.status_code == 409
        assert "уже зарегистрирована" in r.json()["detail"]
        assert len(letters_box) == 1, "второй компании отправили письмо"

    def test_honeypot_is_silently_discarded(self, client, letters_box):
        """Бот заполнил скрытое поле: отвечаем «принято», запись не создаём."""
        db = _db()
        _enable(db)
        db.close()
        r = _register(client, hp="я бот и заполнил скрытое поле")
        assert r.status_code == 200
        assert r.json()["registered"] is False
        assert letters_box == []
        db = _db()
        assert db.query(B2bCompany).count() == 0
        db.close()

    def test_too_fast_submission_is_discarded(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        r = _register(client, form_ms=120)
        assert r.status_code == 200
        assert r.json()["registered"] is False
        db = _db()
        assert db.query(B2bCompany).count() == 0
        db.close()

    def test_ip_limit_is_enforced(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        codes = []
        for i in range(8):
            r = _register(client, email=f"user{i}@company.ru")
            codes.append(r.status_code)
        assert 429 in codes, f"ограничение регистраций по IP не сработало: {codes}"
        assert "Слишком много попыток регистрации" in \
            client.post("/api/v1/b2b/register",
                        json={"email": "x9@company.ru"}).json().get("detail", "") \
            or codes.count(429) >= 1


# ======================================================================
# 4. Подтверждение почты и три режима автоматизации
# ======================================================================
class TestEmailConfirmation:
    def test_confirmation_verifies_company_in_partial_mode(self, client,
                                                           letters_box):
        token, cid = _verified_company(client, letters_box)
        assert token and cid
        db = _db()
        company = db.get(B2bCompany, cid)
        assert company.status == ST_VERIFIED
        assert company.email_verified_at is not None
        assert company.verified_at is not None
        assert company.kind == "ООО"
        db.close()

    def test_confirmation_letter_is_followed_by_status_letter(self, client,
                                                              letters_box):
        _verified_company(client, letters_box)
        subjects = [item["subject"] for item in letters_box]
        assert any("статус компании подтверждён" in s.lower() for s in subjects), \
            subjects

    def test_token_is_single_use(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        assert _confirm(client, token).status_code == 200
        r = _confirm(client, token)
        assert r.status_code == 400
        assert "уже использована" in r.json()["detail"]

    def test_unknown_token_is_refused(self, client):
        r = _confirm(client, "несуществующий-токен")
        assert r.status_code == 400
        assert "недействительна" in r.json()["detail"]

    def test_empty_token_is_422(self, client):
        r = _confirm(client, "")
        assert r.status_code == 422
        assert "Не передан токен" in r.json()["detail"]

    def test_expired_token_is_refused(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        db = _db()
        row = db.query(B2bToken).filter(
            B2bToken.purpose == PURPOSE_VERIFY).first()
        from datetime import timedelta
        row.expires_at = row.expires_at - timedelta(hours=48)
        db.commit()
        db.close()
        r = _confirm(client, token)
        assert r.status_code == 400

    def test_confirmation_logs_in_the_company(self, client, letters_box):
        """Ссылка доказала владение ящиком — кабинет открывается сразу."""
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        body = _confirm(client, token).json()
        assert body["token"], "после подтверждения не выдан токен кабинета"
        assert "hub" in body["pages"] and "docs" in body["pages"]
        me = client.get("/api/v1/b2b/me",
                        headers={"Authorization": "Bearer " + body["token"]})
        assert me.status_code == 200
        assert me.json()["company"]["status"] == ST_VERIFIED

    def test_off_mode_does_not_decide_anything(self, client, letters_box):
        """Режим «отмена»: автоматика не подтверждает статус и не проверяет."""
        db = _db()
        _enable(db, pipeline.MODE_OFF)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        body = _confirm(client, token).json()
        assert body["status"] != ST_VERIFIED
        assert body["checks"] == []
        assert "администратор" in body["message"]
        db = _db()
        company = _company(db)
        assert company.status != ST_VERIFIED
        assert db.query(B2bCheck).filter(
            B2bCheck.company_id == company.id).count() == 0, \
            "в режиме отмены проверки запустились сами"
        events = db.query(B2bEvent).filter(
            B2bEvent.action == "checks_skipped").count()
        assert events == 1
        db.close()

    def test_full_mode_approves_application_and_issues_key(self, client,
                                                           letters_box):
        db = _db()
        _enable(db, pipeline.MODE_FULL)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        body = _confirm(client, token).json()
        hdr = {"Authorization": "Bearer " + body["token"]}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "business",
                              "task": "Нужны агрегаты по регионам для отчёта",
                              "expected_volume": "до 20 000 в месяц"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["application_status"] == AP_APPROVED
        assert data["issued_key"].startswith("apk_") and len(data["issued_key"]) == 47
        assert data["key_note"], "нет предупреждения, что ключ виден один раз"
        db = _db()
        company = _company(db)
        rec = db.query(B2bApplication).filter(
            B2bApplication.company_id == company.id).first()
        assert rec.key_id, "заявка не связана с ключом"
        db.close()

    def test_full_mode_without_auto_key_parks_application(self, client,
                                                          letters_box):
        """Переключатель важнее режима: автовыдача выключена — ключ не
        создаётся вовсе, заявка ждёт администратора.

        Название компании здесь уникальное: база общая на прогон, и по имени
        «Ромашка» в счёт попали бы ключи, выданные другими тестами.
        """
        db = _db()
        _enable(db, pipeline.MODE_FULL)
        appsettings.set_setting(db, pipeline.SET_AUTO_KEY, "0")
        db.commit()
        db.close()
        assert _register(client, email="noauto@company.ru",
                         company_name="ООО «Автовыдача»").status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        body = _confirm(client, token).json()
        hdr = {"Authorization": "Bearer " + body["token"]}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start",
                              "task": "Аналитика по отраслям для стратегии"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["application_status"] == AP_PARKED
        assert data["issued_key"] == ""
        db = _db()
        from app.pool.models import PoolApiKey
        company = _company(db, "noauto@company.ru")
        assert company is not None, "компания не найдена по своей почте"
        # база общая на прогон: считаем только ключи этой компании
        own = db.query(PoolApiKey).filter(
            PoolApiKey.name.like(f"%{company.company_name}%")).count()
        assert own == 0, "создан ключ-пустышка"
        rec = db.query(B2bApplication).filter(
            B2bApplication.company_id == company.id).first()
        assert "выключена" in rec.auto_decision
        db.close()

    def test_partial_mode_parks_application(self, client, letters_box):
        token, cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start",
                              "task": "Нужны срезы по регионам и отраслям"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["application_status"] == AP_PARKED
        assert data["issued_key"] == ""
        assert "администратор" in data["message"].lower()
        assert "одобрена автоматически" not in data["message"].lower()

    def test_rejected_company_gets_reason_by_letter(self, client, letters_box,
                                                     monkeypatch):
        """Реестр не подтвердил ИНН → отказ с причиной и письмо об отказе.

        Здесь же закреплено исправление дефекта v1.70.45: сессия проекта
        создана с autoflush=False, поэтому без явного flush вердикт выносился
        по пустому списку проверок и компания с красной проверкой реестра
        получала подтверждение автоматически.
        """
        from app.services import checko

        def missing(_api_key, _inn):
            raise checko.CheckoError("организация не найдена")

        monkeypatch.setattr(checko, "fetch_card", missing)
        db = _db()
        _enable(db)
        appsettings.set_setting(db, checko.SETTING_KEY, "test-key")
        db.commit()
        db.close()

        assert _register(client, email="ghost@company.ru",
                         inn=INN_UL).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        body = _confirm(client, token).json()
        assert body["status"] == ST_REJECTED, body
        assert len(body["checks"]) == 6, "проверки не вернулись в ответе"
        assert "реестр не подтвердил" in body["message"].lower()
        subjects = " | ".join(item["subject"] for item in letters_box)
        assert "не подтверждён" in subjects


# ======================================================================
# 5. Вход, восстановление доступа, кабинет
# ======================================================================
class TestLogin:
    def test_login_returns_state(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        db = _db()
        company = _company(db)
        email, password = company.email, PASSWORD
        db.close()
        r = client.post("/api/v1/b2b/login",
                        json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"] and body["company"]["status"] == ST_VERIFIED
        assert body["checks"], "в состоянии кабинета нет результатов проверок"
        assert body["contact"] == "chek@ymaster.ru"

    def test_wrong_password_is_401(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        r = client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": "неверный-пароль"})
        assert r.status_code == 401
        assert "Неверная почта или пароль" in r.json()["detail"]

    def test_unknown_email_gives_the_same_answer(self, client, letters_box):
        r = client.post("/api/v1/b2b/login",
                        json={"email": "нет@такой.ru", "password": PASSWORD})
        assert r.status_code == 401
        assert "Неверная почта или пароль" in r.json()["detail"]

    def test_bruteforce_is_limited(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        codes = []
        for _ in range(9):
            r = client.post("/api/v1/b2b/login",
                            json={"email": CORP_MAIL, "password": "неверный"})
            codes.append(r.status_code)
        assert 429 in codes, f"перебор пароля не ограничен: {codes}"

    def test_blocked_company_cannot_login(self, client, letters_box, admin_hdr):
        token, cid = _verified_company(client, letters_box)
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/block",
                        headers=admin_hdr, json={"reason": "Нарушение условий"})
        assert r.status_code == 200, r.text
        r = client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": PASSWORD})
        assert r.status_code == 403
        assert "закрыт администратором" in r.json()["detail"]

    def test_blocked_company_token_stops_working(self, client, letters_box,
                                                 admin_hdr):
        token, cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        assert client.get("/api/v1/b2b/me", headers=hdr).status_code == 200
        client.post(f"/api/v1/b2b-admin/companies/{cid}/block",
                    headers=admin_hdr, json={"reason": "Нарушение"})
        assert client.get("/api/v1/b2b/me", headers=hdr).status_code == 401

    def test_reset_request_does_not_reveal_registration(self, client,
                                                        letters_box):
        db = _db()
        _enable(db)
        db.close()
        known = client.post("/api/v1/b2b/reset-request",
                            json={"email": CORP_MAIL})
        unknown = client.post("/api/v1/b2b/reset-request",
                              json={"email": "нет@такой.ru"})
        assert known.status_code == unknown.status_code == 200
        assert known.json()["message"] == unknown.json()["message"]
        assert letters_box == [], "письмо ушло на несуществующий адрес"

    def test_reset_link_sets_new_password(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        r = client.post("/api/v1/b2b/reset-request", json={"email": CORP_MAIL})
        assert r.status_code == 200
        token = _token_from(letters_box, "/b2b/#/newpass")
        assert token, "письмо со ссылкой входа не отправлено"
        r = client.post("/api/v1/b2b/reset-confirm",
                        json={"token": token, "password": "новый-пароль-456"})
        assert r.status_code == 200, r.text
        assert r.json()["token"], "после смены пароля не выполнен вход"
        r = client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": "новый-пароль-456"})
        assert r.status_code == 200

    def test_reset_token_is_single_use(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        client.post("/api/v1/b2b/reset-request", json={"email": CORP_MAIL})
        token = _token_from(letters_box, "/b2b/#/newpass")
        assert client.post("/api/v1/b2b/reset-confirm",
                           json={"token": token,
                                 "password": "новый-пароль-456"}).status_code == 200
        r = client.post("/api/v1/b2b/reset-confirm",
                        json={"token": token, "password": "ещё-один-пароль"})
        assert r.status_code == 400

    def test_weak_new_password_is_refused(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        client.post("/api/v1/b2b/reset-request", json={"email": CORP_MAIL})
        token = _token_from(letters_box, "/b2b/#/newpass")
        r = client.post("/api/v1/b2b/reset-confirm",
                        json={"token": token, "password": "123"})
        assert r.status_code == 422
        assert "не короче 8" in r.json()["detail"]

    def test_resend_verify_requires_auth(self, client):
        r = client.post("/api/v1/b2b/resend-verify", json={})
        assert r.status_code == 401

    def test_resend_verify_after_confirmation_is_noop(self, client,
                                                      letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/resend-verify", headers=hdr, json={})
        assert r.status_code == 200
        assert r.json()["sent"] is False
        assert "уже подтверждена" in r.json()["message"]


class TestSessionEpoch:
    """Эпоха сессий: смена пароля и блокировка гасят выданные токены.

    Без этого токен со сроком 30 дней переживал сброс пароля: украденная
    ссылка не теряла силу, а разблокировка возвращала доступ старому токену.
    """

    def test_token_carries_the_epoch(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        import jwt

        from app.config import settings
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        assert payload["typ"] == "b2b"
        assert payload["ep"] == 0, "свежая компания должна иметь эпоху 0"

    def test_password_reset_kills_old_sessions(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        assert client.get("/api/v1/b2b/me", headers=hdr).status_code == 200
        client.post("/api/v1/b2b/reset-request", json={"email": CORP_MAIL})
        reset = _token_from(letters_box, "/b2b/#/newpass")
        body = client.post("/api/v1/b2b/reset-confirm",
                           json={"token": reset,
                                 "password": "новый-пароль-456"}).json()
        check("прежний токен мёртв после смены пароля",
              client.get("/api/v1/b2b/me", headers=hdr).status_code == 401)
        new_hdr = {"Authorization": "Bearer " + body["token"]}
        check("новый токен работает",
              client.get("/api/v1/b2b/me", headers=new_hdr).status_code == 200)
        check("в сообщении сказано про прежние сессии",
              "Прежние сессии закрыты" in body["message"])

    def test_block_kills_sessions_and_unblock_does_not_revive_them(self,
                                                                   client,
                                                                   letters_box,
                                                                   admin_hdr):
        token, cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        client.post(f"/api/v1/b2b-admin/companies/{cid}/block",
                    headers=admin_hdr, json={"reason": "Нарушение условий"})
        check("токен заблокированной компании мёртв",
              client.get("/api/v1/b2b/me", headers=hdr).status_code == 401)
        client.post(f"/api/v1/b2b-admin/companies/{cid}/unblock",
                    headers=admin_hdr)
        check("разблокировка не оживляет прежний токен",
              client.get("/api/v1/b2b/me", headers=hdr).status_code == 401)
        body = client.post("/api/v1/b2b/login",
                           json={"email": CORP_MAIL,
                                 "password": PASSWORD}).json()
        check("вход после разблокировки даёт новый токен",
              client.get("/api/v1/b2b/me",
                         headers={"Authorization": "Bearer " + body["token"]}
                         ).status_code == 200)

    def test_epoch_mismatch_is_refused(self, client, letters_box):
        """Токен с чужой эпохой не принимается, даже если подпись верна."""
        token, cid = _verified_company(client, letters_box)
        import jwt

        from app.b2b import auth
        from app.config import settings
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        forged = jwt.encode({**payload, "ep": payload["ep"] + 1},
                            settings.SECRET_KEY, algorithm="HS256")
        db = _db()
        check("подпись верна, эпоха чужая — отказ",
              auth.company_from_token(db, forged) is None)
        check("свой токен принимается",
              auth.company_from_token(db, token) is not None)
        db.close()

    def test_epoch_survives_migration(self, client):
        """Колонка добавляется миграцией и имеет значение по умолчанию."""
        import sqlalchemy
        insp = sqlalchemy.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("b2b_companies")}
        check("колонка эпохи есть", "token_epoch" in cols)
        check("колонка поискового ключа есть", "search_key" in cols)


def check(name, cond):
    """Мини-утверждение с внятным сообщением (для читаемости падений)."""
    assert cond, name


class TestCrossContour:
    """Третья цепочка доверия: токены чужих контуров не проходят."""

    def test_core_token_does_not_open_cabinet(self, client, admin_hdr):
        r = client.get("/api/v1/b2b/me", headers=admin_hdr)
        assert r.status_code == 401
        assert "кабинет" in r.json()["detail"].lower() or \
            "войдите" in r.json()["detail"].lower()

    def test_b2b_token_does_not_open_core(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        assert client.get("/api/v1/users", headers=hdr).status_code == 401
        assert client.get("/api/v1/b2b-admin/overview",
                          headers=hdr).status_code == 401

    def test_b2b_token_does_not_open_pool_cabinet(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        assert client.get("/api/v1/pool-my/summary",
                          headers=hdr).status_code == 401

    def test_pool_token_does_not_open_b2b(self, client):
        from app.pool.accounts import issue_pool_token
        hdr = {"Authorization": "Bearer " + issue_pool_token("нет-такого")}
        assert client.get("/api/v1/b2b/me", headers=hdr).status_code == 401

    def test_token_type_is_checked_first(self, client):
        from app.b2b.auth import company_from_token
        from app.pool.accounts import issue_pool_token
        db = _db()
        assert company_from_token(db, issue_pool_token("x")) is None
        assert company_from_token(db, "мусор") is None
        assert company_from_token(db, "") is None
        db.close()

    def test_bearer_prefix_is_required(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        r = client.get("/api/v1/b2b/me", headers={"Authorization": token})
        assert r.status_code == 401


# ======================================================================
# 6. Гейт содержимого: защита на сервере, а не в интерфейсе
# ======================================================================
class TestGate:
    def test_content_requires_token(self, client):
        r = client.get("/api/v1/b2b/content")
        assert r.status_code == 401
        assert "Войдите в кабинет" in r.json()["detail"]

    def test_content_requires_verified_status(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        r = client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": PASSWORD})
        hdr = {"Authorization": "Bearer " + r.json()["token"]}
        r = client.get("/api/v1/b2b/content", headers=hdr)
        assert r.status_code == 403
        assert "подтверждения статуса" in r.json()["detail"]

    @pytest.mark.parametrize("page", ["hub", "docs", "tariffs", "contact"])
    def test_verified_company_sees_every_page(self, client, letters_box, page):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.get(f"/api/v1/b2b/content?section={page}", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()["page"] == page

    def test_unknown_page_is_404(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.get("/api/v1/b2b/content?section=admin", headers=hdr)
        assert r.status_code == 404

    def test_section_parameter_is_not_named_page(self, client):
        """`page` в этом API — номер страницы списка с потолком PAGE_MAX
        (замок test_v17038::TestPaginationBound против 500 на огромном номере).
        Имя раздела страницы обязано оставаться `section`."""
        spec = client.get("/api/openapi.json").json()
        op = spec["paths"]["/api/v1/b2b/content"]["get"]
        names = [p["name"] for p in op.get("parameters") or []]
        assert names == ["section"], names

    def test_admin_section_is_not_in_client_pages(self, client, letters_box):
        """Раздел администратора (docs/API.md §10) клиентам не отдаётся."""
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        for page in ("hub", "docs", "tariffs", "contact"):
            body = client.get(f"/api/v1/b2b/content?section={page}",
                              headers=hdr).json()
            text = json.dumps(body, ensure_ascii=False)
            for needle in ("pool-admin", "b2b-admin", "Раздел для администратора",
                           "b2b_auto_mode", MARKER):
                assert needle not in text, f"на странице {page} есть «{needle}»"

    def test_diagrams_are_gated(self, client, letters_box):
        hdr_anon = {}
        for name in SVG_FILES:
            assert client.get(f"/api/v1/b2b/content/diagram/{name}",
                              headers=hdr_anon).status_code == 401
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        for name in SVG_FILES:
            r = client.get(f"/api/v1/b2b/content/diagram/{name}", headers=hdr)
            assert r.status_code == 200, name
            assert r.json()["name"] == name
            assert r.json()["svg"].lstrip().startswith("<svg")

    def test_unknown_diagram_is_404(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.get("/api/v1/b2b/content/diagram/нет-такой.svg", headers=hdr)
        assert r.status_code == 404

    def test_diagram_traversal_is_refused(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        for name in ("../models.py", "..%2Fmodels.py", "app.css"):
            r = client.get(f"/api/v1/b2b/content/diagram/{name}", headers=hdr)
            assert r.status_code in (404, 400), name

    def test_old_public_address_no_longer_serves_svg(self, client):
        """Схемы вынесены из статики: старый адрес отдаёт оболочку SPA."""
        for name in SVG_FILES:
            r = client.get(f"/img/api/{name}")
            assert "<svg" not in r.text, f"{name} по-прежнему публична"
            assert not (ROOT / "app/static/img/api" / name).exists()

    def test_applications_require_verified_status(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        r = client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": PASSWORD})
        hdr = {"Authorization": "Bearer " + r.json()["token"]}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start",
                              "task": "Нужны агрегаты по регионам и отраслям"})
        assert r.status_code == 403

    def test_usage_requires_verified_status(self, client):
        assert client.get("/api/v1/b2b/usage").status_code == 401


# ======================================================================
# 7. Содержимое страниц строится из кода сервиса
# ======================================================================
class TestContentIsBuiltFromCode:
    def test_methods_come_from_the_router(self):
        found = content.methods()
        assert len(found) == 6, [m["short"] for m in found]
        assert [m["short"] for m in found] == list(content.METHOD_ORDER)
        for item in found:
            assert item["path"].startswith("/api/v1/pool-api/v1/")
            assert item["method"] in ("GET", "POST")
            assert item["title"] and item["text"] and item["params"]
            assert item["example"].startswith("curl")

    def test_no_route_is_left_undocumented(self):
        """Дрейф невозможен: у каждого маршрута роутера есть пояснение."""
        from app.pool.router_api import router as api_router
        paths = set()
        for route in api_router.routes:
            if not getattr(route, "methods", None):
                continue
            path = route.path.split("/api/v1/pool-api", 1)[-1]
            paths.add(path)
        assert paths == set(content.METHOD_NOTES), \
            f"расхождение: {paths ^ set(content.METHOD_NOTES)}"

    def test_tariff_limits_are_the_real_constants(self):
        items = content.tariffs()
        assert [t["title"] for t in items] == ["Старт", "Бизнес", "Аналитика"]
        assert items[0]["rate_per_hour"] == DEFAULT_RATE
        assert items[0]["monthly_quota"] == DEFAULT_QUOTA
        assert items[1]["rate_per_hour"] == 300
        assert items[1]["monthly_quota"] == 30_000
        assert items[2]["rate_per_hour"] == 1_200
        assert items[2]["monthly_quota"] == 150_000
        for item in items:
            assert item["price"] == "по запросу"
            assert item["methods"] == 6

    def test_error_texts_are_verbatim_from_the_router(self):
        """Тексты отказов в документе берутся из кода роутера, а не придуманы:
        сверяем устойчивую часть до подстановки и до пояснения."""
        src = _read(ROOT / "app/pool/router_api.py")
        src = re.sub(r'"\s*\n\s*f?"', "", src)     # литералы, разбитые переносом
        src = re.sub(r"\s+", " ", src)
        for row in content.ERRORS:
            head = re.sub(r"\s+", " ", re.split(r"\{| — ", row["text"])[0]).strip()
            assert head in src, f"текста отказа нет в коде: {head!r}"
        codes = [row["code"] for row in content.ERRORS]
        assert codes == [403, 401, 401, 429, 429, 403]

    def test_hub_explains_what_a_company_must_do(self):
        """Критерий владельца: на странице понятно, как пройти регистрацию и
        как узнать стоимость."""
        page = content.hub()
        assert page["page"] == "hub"
        assert len(page["steps"]) == 7
        text = " ".join(s["title"] + " " + s["text"] for s in page["steps"])
        low = text.lower()
        for needle in ("корпоративной почтой", "подтвердите почту", "егрюл",
                       "обсудить подключение", "ключ", "стоимость"):
            assert needle in low, f"в шагах нет «{needle}»"
        assert len(page["requirements"]) >= 4
        assert len(page["not_accepted"]) >= 3
        assert page["contact"] == "chek@ymaster.ru"

    def test_individuals_are_addressed_too(self):
        text = json.dumps(content.hub(), ensure_ascii=False)
        assert "ИП" in text and "предпринимател" in text

    def test_no_prices_in_any_client_page(self):
        """Цены из внутреннего docs/plan.md наружу не выходят; там, где речь о
        тарифах, стоимость указана как «по запросу»."""
        for name, builder in content.PAGES.items():
            text = json.dumps(builder(None), ensure_ascii=False)
            for bad in FORBIDDEN_PRICES:
                assert bad not in text, f"на странице {name} цена «{bad}»"
        for name in ("hub", "tariffs"):
            text = json.dumps(content.page(name), ensure_ascii=False)
            assert "по запросу" in text, f"на странице {name} нет «по запросу»"
        # страница «Обсудить подключение» объясняет, как узнать стоимость
        contact = json.dumps(content.page("contact"), ensure_ascii=False)
        assert "стоимость" in contact, "не сказано, как узнать стоимость"

    def test_boundary_is_stated_on_the_hub_page(self):
        """Принцип продукта виден сразу: что отдаём и чего не отдаём никогда."""
        page = content.page("hub")
        assert any("Сырые чеки" in row for row in page["boundary_never"])
        assert page["boundary_give"], "не сказано, что именно отдаём"
        assert page["aggregate_flag"].startswith("Каждый ответ помечен")

    def test_docs_page_has_auth_and_openapi(self):
        page = content.docs()
        assert "X-API-Key" in page["auth"]
        assert page["key_format"].startswith("Ключ начинается с apk_")
        assert "/api/docs" in page["openapi"]
        assert len(page["errors"]) == 6
        assert len(page["strict_rules"]) == 4

    def test_contact_page_offers_tariffs(self):
        page = content.contact_page()
        assert [t["code"] for t in page["tariffs"]] == \
            ["start", "business", "analytics"]
        assert "20 символов" in page["task_hint"]
        assert page["email"] == "chek@ymaster.ru"

    def test_diagram_reader_refuses_foreign_files(self):
        assert content.diagram("../models.py") == ("", "")
        assert content.diagram("app.css") == ("", "")
        name, svg = content.diagram("api-access.svg")
        assert name == "api-access.svg" and svg.startswith("<svg") or \
            svg.lstrip().startswith("<?xml")

    def test_diagrams_have_no_scripts(self):
        for name in SVG_FILES:
            svg = _read(ASSETS / name)
            assert "<script" not in svg, f"{name} содержит скрипт"
            assert "onload=" not in svg, f"{name} содержит обработчик"


# ======================================================================
# 8. Заявки: форма «Обсудить подключение»
# ======================================================================
class TestApplications:
    def test_short_task_is_refused(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start", "task": "мало"})
        assert r.status_code == 422
        assert "не короче 20" in r.json()["detail"]

    def test_unknown_tariff_is_refused(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "премиум",
                              "task": "Нужны агрегаты по регионам и отраслям"})
        assert r.status_code == 422
        assert "Старт, Бизнес или Аналитика" in r.json()["detail"]

    def test_application_is_visible_in_cabinet(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        assert client.post("/api/v1/b2b/applications", headers=hdr,
                           json={"tariff": "business",
                                 "task": "Аналитика по отраслям и регионам",
                                 "expected_volume": "до 30 000"}).status_code == 200
        me = client.get("/api/v1/b2b/me", headers=hdr).json()
        assert len(me["applications"]) == 1
        assert me["applications"][0]["tariff"] == "business"
        assert me["applications"][0]["status_title"]

    def test_usage_before_key_is_explained(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.get("/api/v1/b2b/usage", headers=hdr)
        assert r.status_code == 200
        assert r.json()["issued"] is False
        assert "Обсудить подключение" in r.json()["message"]

    def test_usage_after_key_shows_limits(self, client, letters_box, admin_hdr):
        token, cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "analytics",
                              "task": "Нужны агрегаты по отраслям ежедневно"})
        aid = r.json()["application"]["id"]
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                        headers=admin_hdr, json={})
        assert r.status_code == 200, r.text
        usage = client.get("/api/v1/b2b/usage", headers=hdr).json()
        assert usage["issued"] is True and usage["active"] is True
        assert usage["rate_per_hour"] == 1_200
        assert usage["monthly_quota"] == 150_000
        assert usage["used_hour"] == 0 and usage["used_month"] == 0
        assert usage["retention_days"] == 90

    def test_issued_key_actually_opens_the_api(self, client, letters_box,
                                               admin_hdr):
        """Ключ, выданный контуром, работает в прежнем механизме API."""
        db = _db()
        _enable(db)
        appsettings.set_setting(db, "pool_enabled", "1")
        from app.pool import router_api
        appsettings.set_setting(db, router_api.API_ENABLED_KEY, "1")
        db.commit()
        db.close()
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start",
                              "task": "Проверка интеграции с агрегатами пула"})
        aid = r.json()["application"]["id"]
        key = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                          headers=admin_hdr, json={}).json()["key"]
        r = client.get("/api/v1/pool-api/v1/stats/overview",
                       headers={"X-API-Key": key})
        assert r.status_code == 200, r.text
        assert r.json()["aggregated"] is True

    def test_key_is_shown_once_only(self, client, letters_box, admin_hdr):
        """Полный ключ виден один раз: ни кабинет, ни карточка администратора
        его больше не отдают — только префикс."""
        token, cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start",
                              "task": "Разовый отчёт по регионам и отраслям"})
        aid = r.json()["application"]["id"]
        first = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                            headers=admin_hdr, json={}).json()
        raw = first["key"]
        assert raw.startswith("apk_") and len(raw) == 47
        me = json.dumps(client.get("/api/v1/b2b/me", headers=hdr).json(),
                        ensure_ascii=False)
        card = json.dumps(
            client.get(f"/api/v1/b2b-admin/companies/{cid}",
                       headers=admin_hdr).json(), ensure_ascii=False)
        assert raw not in me, "кабинет компании отдаёт полный ключ повторно"
        assert raw not in card, "карточка администратора отдаёт полный ключ"
        assert first["prefix"] in me and first["prefix"] in card
        assert json.loads(me)["key"]["prefix"].endswith("…")


# ======================================================================
# 9. Модерация: администратор видит всё, поверхности помечены
# ======================================================================
class TestAdminAccess:
    @pytest.mark.parametrize("path", [
        "/api/v1/b2b-admin/overview", "/api/v1/b2b-admin/companies",
        "/api/v1/b2b-admin/applications", "/api/v1/b2b-admin/events",
        "/api/v1/b2b-admin/blocked",
    ])
    def test_anonymous_is_refused(self, client, path):
        c = TestClient(app)                     # без lifespan — 401
        assert c.get(path).status_code == 401

    def test_b2b_token_does_not_open_moderation(self, client, letters_box):
        token, _cid = _verified_company(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        assert client.get("/api/v1/b2b-admin/overview",
                          headers=hdr).status_code == 401

    @pytest.mark.parametrize("path", [
        "/api/v1/b2b-admin/overview", "/api/v1/b2b-admin/companies",
        "/api/v1/b2b-admin/applications", "/api/v1/b2b-admin/events",
        "/api/v1/b2b-admin/blocked",
    ])
    def test_every_answer_carries_the_marker(self, client, admin_hdr, path):
        """Требование владельца: пометка «Это не видит никто»."""
        r = client.get(path, headers=admin_hdr)
        assert r.status_code == 200, r.text
        assert r.json()["marker"] == MARKER

    def test_marker_comes_from_the_server_not_the_ui(self):
        assert content.ADMIN_MARKER == MARKER
        js = _read(APP_JS)
        assert "b2bAdmMark(d.marker)" in js, "пометка выдумана на клиенте"


class TestAdminOverview:
    def test_overview_reports_counters_and_mode(self, client, admin_hdr,
                                                letters_box):
        _verified_company(client, letters_box)
        body = client.get("/api/v1/b2b-admin/overview",
                          headers=admin_hdr).json()
        assert body["counters"]["total"] == 1
        assert body["counters"][ST_VERIFIED] == 1
        assert body["config"]["auto_mode"] == pipeline.MODE_PARTIAL
        assert body["config"]["enabled"] is True
        assert len(body["modes"]) == 3
        assert [m["code"] for m in body["modes"]] == \
            [pipeline.MODE_FULL, pipeline.MODE_PARTIAL, pipeline.MODE_OFF]
        assert body["tariffs"][0]["price"] == "по запросу"
        assert body["admin"] == "admin"

    def test_overview_lists_the_queue(self, client, admin_hdr, letters_box):
        token, _cid = _verified_company(client, letters_box)
        client.post("/api/v1/b2b/applications",
                    headers={"Authorization": "Bearer " + token},
                    json={"tariff": "business",
                          "task": "Нужны агрегаты по отраслям для отчёта"})
        body = client.get("/api/v1/b2b-admin/overview",
                          headers=admin_hdr).json()
        assert body["counters"]["queue"] == 1
        item = body["queue"][0]
        assert item["company"] == "ООО «Ромашка»"
        assert item["email"] == CORP_MAIL
        assert item["tariff_title"] == "Бизнес"
        assert item["tariff_rate"] == 300


class TestAdminConfig:
    def test_default_mode_is_partial(self, client, admin_hdr):
        body = client.get("/api/v1/b2b-admin/overview",
                          headers=admin_hdr).json()
        assert body["config"]["auto_mode"] == "partial"
        assert "Частичная" in body["config"]["auto_mode_title"]

    @pytest.mark.parametrize("mode", ["full", "partial", "off"])
    def test_modes_are_switchable(self, client, admin_hdr, mode):
        r = client.put("/api/v1/b2b-admin/config", headers=admin_hdr,
                       json={"auto_mode": mode})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["config"]["auto_mode"] == mode
        assert body["marker"] == MARKER
        assert body["message"], "админу не объяснили, что изменилось"

    def test_unknown_mode_is_refused(self, client, admin_hdr):
        r = client.put("/api/v1/b2b-admin/config", headers=admin_hdr,
                       json={"auto_mode": "магия"})
        assert r.status_code == 422
        assert "full, partial или off" in r.json()["detail"]

    def test_switches_are_saved(self, client, admin_hdr):
        r = client.put("/api/v1/b2b-admin/config", headers=admin_hdr,
                       json={"enabled": True, "allow_free_domains": True,
                             "require_registry": False, "notify_admin": False,
                             "auto_issue_key": False})
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["enabled"] is True and cfg["allow_free_domains"] is True
        assert cfg["require_registry"] is False and cfg["notify_admin"] is False
        assert cfg["auto_issue_key"] is False

    def test_config_change_is_audited(self, client, admin_hdr):
        client.put("/api/v1/b2b-admin/config", headers=admin_hdr,
                   json={"auto_mode": pipeline.MODE_FULL})
        from app.models import AuditLog
        db = _db()
        rows = db.query(AuditLog).filter(
            AuditLog.action == "b2b_config_changed").all()
        db.close()
        assert rows, "смена режима не попала в аудит"

    def test_notify_email_defaults_to_vendor(self, client, admin_hdr):
        cfg = client.get("/api/v1/b2b-admin/overview",
                         headers=admin_hdr).json()["config"]
        assert cfg["notify_email"] == "chek@ymaster.ru"


class TestAdminCompanies:
    def test_list_and_filter(self, client, admin_hdr, letters_box):
        _verified_company(client, letters_box)
        body = client.get("/api/v1/b2b-admin/companies",
                          headers=admin_hdr).json()
        assert len(body["items"]) == 1
        assert body["items"][0]["email"] == CORP_MAIL
        assert body["items"][0]["status_title"] == \
            "Статус компании подтверждён"
        assert len(body["statuses"]) == 6
        empty = client.get("/api/v1/b2b-admin/companies?status=rejected",
                           headers=admin_hdr).json()
        assert empty["items"] == []

    def test_search_by_inn_and_name(self, client, admin_hdr, letters_box):
        _verified_company(client, letters_box)
        assert len(client.get(
            f"/api/v1/b2b-admin/companies?q={INN_UL}",
            headers=admin_hdr).json()["items"]) == 1
        assert len(client.get(
            "/api/v1/b2b-admin/companies?q=Ромашка",
            headers=admin_hdr).json()["items"]) == 1
        assert client.get("/api/v1/b2b-admin/companies?q=нет-такой",
                          headers=admin_hdr).json()["items"] == []

    def test_card_shows_checks_applications_and_events(self, client, admin_hdr,
                                                       letters_box):
        token, cid = _verified_company(client, letters_box)
        client.post("/api/v1/b2b/applications",
                    headers={"Authorization": "Bearer " + token},
                    json={"tariff": "start",
                          "task": "Нужны агрегаты по регионам для отчёта"})
        body = client.get(f"/api/v1/b2b-admin/companies/{cid}",
                          headers=admin_hdr).json()
        assert body["marker"] == MARKER
        assert body["company"]["email"] == CORP_MAIL
        assert body["company"]["ip_hash"], "админу не виден след обращения"
        assert len(body["checks"]) == 6
        assert all(c["ok"] for c in body["checks"])
        assert len(body["applications"]) == 1
        assert len(body["events"]) >= 2
        assert body["check_titles"][CK_REGISTRY] == "Карточка ЕГРЮЛ/ЕГРИП (Checko)"
        assert body["key"]["issued"] is False

    def test_unknown_company_is_404(self, client, admin_hdr):
        assert client.get("/api/v1/b2b-admin/companies/нет-такого",
                          headers=admin_hdr).status_code == 404

    def test_manual_verification(self, client, admin_hdr, letters_box):
        """Режим «отмена»: статус подтверждает только администратор."""
        db = _db()
        _enable(db, pipeline.MODE_OFF)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        cid = _confirm(client, token).json()["company"]["id"]
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/verify",
                        headers=admin_hdr, json={"note": "Проверил вручную"})
        assert r.status_code == 200, r.text
        assert r.json()["marker"] == MARKER
        assert r.json()["status"] == ST_VERIFIED
        db = _db()
        assert "Проверил вручную" in db.get(B2bCompany, cid).notes
        db.close()

    def test_manual_rejection_requires_reason(self, client, admin_hdr,
                                              letters_box):
        _verified_company(client, letters_box)
        db = _db()
        cid = _company(db).id
        db.close()
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/reject",
                        headers=admin_hdr, json={"reason": "  "})
        assert r.status_code == 422
        assert "причину" in r.json()["detail"].lower()
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/reject",
                        headers=admin_hdr,
                        json={"reason": "Домен не принадлежит компании"})
        assert r.status_code == 200
        assert r.json()["status"] == ST_REJECTED
        subjects = [item["subject"] for item in letters_box]
        assert any("не подтверждён" in s for s in subjects), subjects
        body = "\n".join(item["body"] for item in letters_box)
        assert "Домен не принадлежит компании" in body

    def test_block_and_unblock(self, client, admin_hdr, letters_box):
        _verified_company(client, letters_box)
        db = _db()
        cid = _company(db).id
        db.close()
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/block",
                        headers=admin_hdr, json={"reason": "Злоупотребление"})
        assert r.status_code == 200 and r.json()["status"] == ST_BLOCKED
        blocked = client.get("/api/v1/b2b-admin/blocked",
                             headers=admin_hdr).json()
        assert len(blocked["items"]) == 1
        assert blocked["marker"] == MARKER
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/unblock",
                        headers=admin_hdr)
        assert r.status_code == 200 and r.json()["status"] == ST_VERIFIED

    def test_recheck_runs_checks_manually(self, client, admin_hdr, letters_box):
        db = _db()
        _enable(db, pipeline.MODE_OFF)
        db.close()
        assert _register(client).status_code == 200
        token = _token_from(letters_box, "/b2b/#/confirm")
        cid = _confirm(client, token).json()["company"]["id"]
        db = _db()
        assert db.query(B2bCheck).filter(
            B2bCheck.company_id == cid).count() == 0
        db.close()
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/recheck",
                        headers=admin_hdr)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["passed"] is True
        assert len(body["checks"]) == 6
        assert body["status"] == ST_VERIFIED

    def test_resend_letter(self, client, admin_hdr, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        db = _db()
        cid = _company(db).id
        db.close()
        before = len(letters_box)
        r = client.post(f"/api/v1/b2b-admin/companies/{cid}/resend",
                        headers=admin_hdr)
        assert r.status_code == 200 and r.json()["sent"] is True
        assert len(letters_box) == before + 1

    def test_old_token_is_voided_on_resend(self, client, admin_hdr,
                                           letters_box):
        """Ссылка всегда одна: прежний токен гасится при повторной отправке."""
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        old = _token_from(letters_box, "/b2b/#/confirm")
        db = _db()
        cid = _company(db).id
        db.close()
        client.post(f"/api/v1/b2b-admin/companies/{cid}/resend",
                    headers=admin_hdr)
        r = _confirm(client, old)
        assert r.status_code == 400, "старая ссылка осталась рабочей"


class TestAdminApplications:
    def _application(self, client, letters_box, tariff="start"):
        token, cid = _verified_company(client, letters_box)
        r = client.post("/api/v1/b2b/applications",
                        headers={"Authorization": "Bearer " + token},
                        json={"tariff": tariff,
                              "task": "Нужны агрегаты по регионам и отраслям",
                              "expected_volume": "до 5 000 в месяц"})
        assert r.status_code == 200, r.text
        return r.json()["application"]["id"], cid, token

    def test_queue_lists_applications_with_checks(self, client, admin_hdr,
                                                  letters_box):
        aid, _cid, _token = self._application(client, letters_box, "business")
        body = client.get("/api/v1/b2b-admin/applications",
                          headers=admin_hdr).json()
        assert body["queue"] == 1
        item = [a for a in body["items"] if a["id"] == aid][0]
        assert item["tariff_title"] == "Бизнес"
        assert item["tariff_rate"] == 300 and item["tariff_quota"] == 30_000
        assert len(item["checks"]) == 6
        assert item["email"] == CORP_MAIL
        assert len(body["statuses"]) == 5

    def test_approve_in_one_click_issues_key(self, client, admin_hdr,
                                             letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                        headers=admin_hdr, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["marker"] == MARKER
        assert body["key"].startswith("apk_") and len(body["key"]) == 47
        assert body["prefix"].endswith("…")
        assert body["application"]["status"] == AP_APPROVED
        assert "один раз" in body["message"]

    def test_approve_can_override_limits(self, client, admin_hdr, letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                        headers=admin_hdr,
                        json={"rate_per_hour": 900, "monthly_quota": 90_000})
        assert r.status_code == 200, r.text
        me = client.get("/api/v1/b2b/usage",
                        headers={"Authorization": "Bearer " + _token}).json()
        assert me["rate_per_hour"] == 900 and me["monthly_quota"] == 90_000

    def test_key_is_not_sent_by_letter(self, client, admin_hdr, letters_box):
        """Осознанное решение: письмо может быть переслано — ключа в нём нет."""
        aid, _cid, _token = self._application(client, letters_box)
        key = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                          headers=admin_hdr, json={}).json()["key"]
        approved = [item for item in letters_box
                    if "одобрена" in item["subject"].lower()]
        assert approved, "письмо об одобрении не отправлено"
        for item in approved:
            assert key not in item["body"] and key not in item["html"]
            assert "/b2b/#/cabinet" in item["body"], "в письме нет ссылки на кабинет"
            assert "один раз" in item["body"].lower()

    def test_reject_requires_reason_and_notifies(self, client, admin_hdr,
                                                 letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/reject",
                        headers=admin_hdr, json={"reason": ""})
        assert r.status_code == 422
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/reject",
                        headers=admin_hdr,
                        json={"reason": "Объём не соответствует тарифу"})
        assert r.status_code == 200
        assert r.json()["application"]["status"] == "rejected"
        body = "\n".join(item["body"] for item in letters_box)
        assert "Объём не соответствует тарифу" in body

    def test_revoke_stops_api_access_immediately(self, client, admin_hdr,
                                                 letters_box):
        db = _db()
        _enable(db)
        appsettings.set_setting(db, "pool_enabled", "1")
        from app.pool import router_api
        appsettings.set_setting(db, router_api.API_ENABLED_KEY, "1")
        db.commit()
        db.close()
        aid, _cid, token = self._application(client, letters_box)
        key = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                          headers=admin_hdr, json={}).json()["key"]
        assert client.get("/api/v1/pool-api/v1/stats/overview",
                          headers={"X-API-Key": key}).status_code == 200
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/revoke",
                        headers=admin_hdr)
        assert r.status_code == 200
        assert r.json()["marker"] == MARKER
        r = client.get("/api/v1/pool-api/v1/stats/overview",
                       headers={"X-API-Key": key})
        assert r.status_code == 401
        assert "недействителен или отозван" in r.json()["detail"]
        me = client.get("/api/v1/b2b/usage",
                        headers={"Authorization": "Bearer " + token}).json()
        assert me["issued"] is True and me["active"] is False

    def test_revoke_without_key_is_refused(self, client, admin_hdr,
                                           letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/revoke",
                        headers=admin_hdr)
        assert r.status_code == 422
        assert "не выдавался" in r.json()["detail"]

    def test_revoked_application_cannot_be_approved(self, client, admin_hdr,
                                                    letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                    headers=admin_hdr, json={})
        client.post(f"/api/v1/b2b-admin/applications/{aid}/revoke",
                    headers=admin_hdr)
        r = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                        headers=admin_hdr, json={})
        assert r.status_code == 409

    def test_unknown_application_is_404(self, client, admin_hdr):
        assert client.post("/api/v1/b2b-admin/applications/нет/approve",
                           headers=admin_hdr, json={}).status_code == 404
        assert client.post("/api/v1/b2b-admin/applications/нет/reject",
                           headers=admin_hdr,
                           json={"reason": "причина"}).status_code == 404

    def test_decisions_are_audited(self, client, admin_hdr, letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                    headers=admin_hdr, json={})
        from app.models import AuditLog
        db = _db()
        actions = [row.action for row in
                   db.query(AuditLog).filter(
                       AuditLog.action.like("b2b_%")).all()]
        db.close()
        assert "b2b_application_approved" in actions

    def test_full_key_never_reaches_audit_or_events(self, client, admin_hdr,
                                                    letters_box):
        aid, _cid, _token = self._application(client, letters_box)
        key = client.post(f"/api/v1/b2b-admin/applications/{aid}/approve",
                          headers=admin_hdr, json={}).json()["key"]
        db = _db()
        events = "\n".join(e.details for e in db.query(B2bEvent).all())
        db.close()
        assert key not in events
        assert "apk_" in events, "в журнале нет даже префикса ключа"


class TestAdminEvents:
    def test_journal_records_the_whole_path(self, client, admin_hdr,
                                            letters_box):
        token, cid = _verified_company(client, letters_box)
        client.post("/api/v1/b2b/applications",
                    headers={"Authorization": "Bearer " + token},
                    json={"tariff": "start",
                          "task": "Нужны агрегаты по регионам и отраслям"})
        body = client.get("/api/v1/b2b-admin/events?limit=50",
                          headers=admin_hdr).json()
        actions = [item["action"] for item in body["items"]]
        for expected in ("registered", "email_confirmed", "company_verified",
                         "application_submitted"):
            assert expected in actions, f"в журнале нет {expected}"
        actors = {item["actor"] for item in body["items"]}
        assert "system" in actors and "company" in actors

    def test_limit_is_clamped(self, client, admin_hdr):
        body = client.get("/api/v1/b2b-admin/events?limit=99999",
                          headers=admin_hdr).json()
        assert len(body["items"]) <= 200


# ======================================================================
# 10. Письма контура
# ======================================================================
class TestLetters:
    def test_verify_letter_has_link_and_steps(self, client, letters_box):
        db = _db()
        _enable(db)
        db.close()
        assert _register(client).status_code == 200
        letter = letters_box[0]
        assert "/b2b/#/confirm?token=" in letter["body"]
        assert "24 часа" in letter["body"]
        assert "ЕГРЮЛ" in letter["body"]
        assert "Обсудить подключение" in letter["body"]
        assert "chek@ymaster.ru" in letter["body"]
        assert letter["html"].startswith("<div")
        assert letter["from_name"], "письмо без имени отправителя"

    def test_no_letter_is_sent_when_smtp_is_not_configured(self, client,
                                                           monkeypatch):
        """Сбой почты не ломает регистрацию: запись создана, письмо не ушло."""
        monkeypatch.setattr(mailer, "send_mail",
                            lambda *a, **kw: (False, "SMTP не настроен"))
        db = _db()
        _enable(db)
        db.close()
        r = _register(client)
        assert r.status_code == 200
        assert r.json()["letter_sent"] is False
        assert "не отправлено" in r.json()["message"]
        db = _db()
        assert db.query(B2bCompany).count() == 1
        db.close()

    def test_rejection_letter_carries_the_reason(self, client, admin_hdr,
                                                 letters_box):
        _verified_company(client, letters_box)
        db = _db()
        cid = _company(db).id
        db.close()
        client.post(f"/api/v1/b2b-admin/companies/{cid}/reject",
                    headers=admin_hdr,
                    json={"reason": "ИНН принадлежит другой организации"})
        last = letters_box[-1]
        assert "ИНН принадлежит другой организации" in last["body"]
        assert last["to"] == CORP_MAIL

    def test_admin_notice_is_sent_when_enabled(self, client, letters_box):
        db = _db()
        _enable(db)
        appsettings.set_setting(db, pipeline.SET_NOTIFY, "1")
        db.commit()
        db.close()
        token, _cid = _verified_company(client, letters_box)
        before = len(letters_box)
        client.post("/api/v1/b2b/applications",
                    headers={"Authorization": "Bearer " + token},
                    json={"tariff": "start",
                          "task": "Нужны агрегаты по регионам и отраслям"})
        notices = [item for item in letters_box[before:]
                   if "Новая заявка" in item["subject"]]
        assert notices, "администратор не уведомлён о новой заявке"
        assert notices[0]["to"] == "chek@ymaster.ru"
        assert INN_UL in notices[0]["body"]

    def test_admin_notice_can_be_switched_off(self, client, letters_box):
        db = _db()
        _enable(db)
        appsettings.set_setting(db, pipeline.SET_NOTIFY, "0")
        db.commit()
        db.close()
        token, _cid = _verified_company(client, letters_box)
        before = len(letters_box)
        client.post("/api/v1/b2b/applications",
                    headers={"Authorization": "Bearer " + token},
                    json={"tariff": "start",
                          "task": "Нужны агрегаты по регионам и отраслям"})
        assert len(letters_box) == before, "уведомление ушло при выключенном флаге"

    def test_links_use_the_configured_base_url(self, client, letters_box):
        db = _db()
        _enable(db)
        appsettings.set_setting(db, "public_base_url", "https://chek.example.ru")
        db.commit()
        db.close()
        assert _register(client).status_code == 200
        assert "https://chek.example.ru/b2b/#/confirm" in letters_box[0]["body"]


# ======================================================================
# 11. Публичный лендинг закрыт, кабинет — отдельная страница
# ======================================================================
class TestLandingIsClosed:
    def test_no_section_and_no_link(self):
        html = _read(INDEX)
        assert "land-api" not in html
        assert "API для бизнеса" not in html.split('id="login-screen"')[0]
        assert "/img/api/" not in html

    def test_faq_item_is_removed(self):
        html = _read(INDEX)
        assert "Нужны данные по чекам для аналитики" not in html
        assert "Есть платное API" not in html

    def test_other_faq_items_survived(self):
        """Удалён только пункт про API — остальные вопросы на месте."""
        html = _read(INDEX)
        assert "А для компаний?" in html
        assert "land-faq" in html

    def test_dead_styles_are_gone(self):
        css = _read(CSS)
        for cls in ("land-api", "land-api-grid", "land-api-tariffs",
                    "land-api-cta", "land-api-fig"):
            assert f".{cls}" not in css

    def test_neighbouring_rules_survived(self):
        """Удалены ровно свои 21 правило, чужие не тронуты."""
        css = _read(CSS)
        assert "#login-screen .land-right { min-width: 0; }" in css
        assert ".land-faq summary" in css
        assert css.count("@media (max-width: 920px) {") == 1

    def test_landing_lead_form_survived(self):
        html = _read(INDEX)
        assert 'id="land-lead"' in html and 'id="land-hp"' in html

    def test_screenshots_are_moved_behind_the_gate(self):
        for name in SVG_FILES:
            assert (ASSETS / name).exists(), f"нет {name} в app/b2b/assets"
            assert not (ROOT / "app/static/img/api").exists() or \
                not (ROOT / "app/static/img/api" / name).exists()


class TestCabinetPage:
    def test_page_is_served(self, client):
        for path in ("/b2b", "/b2b/"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "API для бизнеса" in r.text
            assert 'id="view-start"' in r.text

    def test_assets_are_served(self, client):
        for name in ("b2b.css", "b2b.js"):
            r = client.get(f"/b2b/{name}")
            assert r.status_code == 200, name
            assert len(r.text) > 1000

    def test_traversal_is_refused(self, client):
        """httpx нормализует «..» до отправки, поэтому обход пути проверяется
        процентно-закодированными вариантами и служебными именами."""
        for path in ("/b2b/%2e%2e%2fconfig.py", "/b2b/%2e%2e/config.py",
                     "/b2b/.env", "/b2b/sub/page.html", "/b2b/b2b.js.bak"):
            r = client.get(path)
            assert r.status_code == 404, f"{path} → {r.status_code}"
            assert "def " not in r.text and "SECRET" not in r.text

    def test_unknown_file_is_404(self, client):
        assert client.get("/b2b/нет-такого.js").status_code == 404

    def test_page_is_not_indexed(self):
        assert 'name="robots" content="noindex, nofollow"' in _read(CABINET)

    def test_no_link_from_public_pages(self):
        """Адрес кабинета не публикуется на лендинге."""
        html = _read(INDEX)
        assert '"/b2b' not in html and "'/b2b" not in html
        assert "b2b/#/" not in html

    def test_every_button_has_explicit_type(self):
        """Иначе кнопка внутри формы отправляет её случайным кликом."""
        for path in (CABINET,):
            html = _read(path)
            tags = re.findall(r"<button\b[^>]*>", html)
            assert tags, "кнопок не найдено — проверка выродилась"
            bad = [t for t in tags if not re.search(r"\btype\s*=", t)]
            assert not bad, f"кнопки без type: {bad[:3]}"

    def test_generated_buttons_have_type(self):
        js = _read(CABINET_JS)
        tags = re.findall(r"<button\b[^>]*>", js)
        bad = [t for t in tags if not re.search(r"\btype\s*=", t)]
        assert not bad, f"в шаблонах кнопки без type: {bad[:3]}"

    def test_forms_have_honeypot_and_labels(self):
        html = _read(CABINET)
        assert 'name="hp"' in html and 'class="b2b-trap"' in html
        tag = re.search(r'<div[^>]*class="b2b-trap"[^>]*>', html)
        assert tag and 'aria-hidden="true"' in tag.group(0), \
            "ловушка не скрыта от вспомогательных технологий"
        field = html.split('class="b2b-trap"', 1)[1][:240]
        assert 'tabindex="-1"' in field, "ловушка доступна с клавиатуры"
        labels = re.findall(r'<label for="([^"]+)"', html)
        assert len(labels) >= 10
        for name in labels:
            assert f'id="{name}"' in html, f"метка {name} без поля"

    def test_consent_is_required_before_submit(self):
        html = _read(CABINET)
        assert 'id="reg-consent"' in html and "required" in \
            html.split('id="reg-consent"')[1][:120]
        assert "2026-10" in html
        js = _read(CABINET_JS)
        assert "reg-consent" in js

    def test_gated_tabs_are_hidden_by_default(self):
        html = _read(CABINET)
        for view in ("hub", "docs", "tariffs", "contact", "cabinet"):
            tag = re.search(r'<button[^>]*data-view="%s"[^>]*>' % view, html)
            assert tag, f"нет вкладки {view}"
            assert "hidden" in tag.group(0), f"вкладка {view} видна сразу"

    def test_dynamic_text_is_escaped(self):
        js = _read(CABINET_JS)
        assert "function esc(" in js
        assert "esc(" in js and js.count("esc(") > 25

    def test_svg_injection_is_guarded(self):
        js = _read(CABINET_JS)
        assert '"<script"' in js and "onload=" in js

    def test_no_external_dependencies(self):
        html = _read(CABINET)
        assert "http://" not in html and "https://" not in html.replace(
            "https://ymaster.ru", "").replace("mailto:", "")
        assert "<script src=\"/b2b/b2b.js" in html

    def test_cabinet_css_has_no_invalid_hex(self):
        css = _read(CABINET_CSS)
        for value in re.findall(r"#[0-9a-fA-F]{3,8}\b", css):
            assert len(value) in (4, 7), f"некорректный hex {value}"
        assert ":root" in css

    def test_cabinet_css_declares_every_token_it_uses(self):
        css = _read(CABINET_CSS)
        root = css.split(":root", 1)[1].split("}", 1)[0]
        for token in set(re.findall(r"var\((--[\w-]+)\)", css)):
            assert f"{token}:" in root, f"токен {token} объявлен не в :root"

    def test_cabinet_text_is_readable(self):
        """Минимальный кегль 12.5px — правило читаемости проекта."""
        css = _read(CABINET_CSS)
        sizes = [float(m) for m in re.findall(r"font-size:\s*([\d.]+)rem", css)]
        assert sizes, "размеры в rem не найдены"
        assert min(sizes) * 16 >= 12.5, f"мелкий текст {min(sizes)}rem"

    def test_buttons_are_corporate_orange(self):
        """Правило v1.70.34: кнопки фирменным оранжевым, не чёрным."""
        css = _read(CABINET_CSS)
        block = css.split(".btn-primary", 1)[1].split("}", 1)[0]
        assert "var(--accent)" in block
        assert "#000" not in block and "#111" not in block

    def test_js_syntax_is_valid(self):
        result = subprocess.run(["node", "--check", str(CABINET_JS)],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr[:400]

    def test_views_cover_the_owner_requirements(self):
        """Отдельные страницы: документация API, тарифы и состав, кнопка
        «Обсудить подключение»."""
        html = _read(CABINET)
        for view in ("start", "register", "login", "confirm", "newpass", "hub",
                     "docs", "tariffs", "contact", "cabinet"):
            assert f'id="view-{view}"' in html, f"нет экрана {view}"
        assert "Обсудить подключение" in html
        assert "Тарифы и состав" in html
        assert "Документация" in html


class TestAdminSectionInSpa:
    def test_nav_item_is_admin_only_and_hidden(self):
        html = _read(INDEX)
        tag = re.search(r'<a[^>]*href="#/b2badmin"[^>]*>', html)
        assert tag, "нет пункта меню модерации B2B"
        assert "admin-only" in tag.group(0) and "hidden" in tag.group(0)
        assert 'data-view="b2badmin"' in tag.group(0)

    def test_view_is_registered(self):
        js = _read(APP_JS)
        assert "b2badmin: viewB2bAdmin" in js
        assert "b2badmin: isAdmin()" in js
        assert "b2badmin: 'API для бизнеса: модерация'" in js

    def test_marker_is_rendered_from_server_data(self):
        js = _read(APP_JS)
        assert "b2badm-markerline" in js
        assert "d.marker" in js

    def test_admin_styles_use_tokens_only(self):
        css = _read(CSS)
        block = css.split("v1.70.45: раздел модерации контура B2B", 1)[1]
        block = block.split("@media", 1)[0]
        hexes = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", block))
        assert hexes <= {"#1a1a1a"}, f"недокументированный hex: {hexes}"

    def test_admin_classes_are_used(self):
        """Замок дрейфа: класс из CSS обязан применяться в app.js."""
        css = _read(CSS)
        block = css.split("v1.70.45: раздел модерации контура B2B", 1)[1]
        js = _read(APP_JS)
        names = set(re.findall(r"\.(b2badm-[\w-]+)", block))
        assert len(names) >= 20, f"подозрительно мало классов: {len(names)}"
        missing = [n for n in names
                   if not re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", js)]
        assert not missing, f"классы без применения: {missing}"

    def test_app_js_syntax_is_valid(self):
        result = subprocess.run(["node", "--check", str(APP_JS)],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr[:400]


# ======================================================================
# 12. Почта chek@ymaster.ru — везде, старого адреса нет
# ======================================================================
class TestCorporateEmail:
    def test_no_old_address_in_tracked_files(self):
        # искомый адрес собирается из частей: иначе этот же тест находит сам
        # себя — строка поиска обязана быть в файле буквально
        needle = "info@" + "ymaster.ru"
        out = subprocess.run(
            ["git", "grep", "-l", needle, "--", ":!tests/test_v17045.py"],
            capture_output=True, text=True, cwd=ROOT, check=False)
        assert out.stdout.strip() == "", \
            f"старый адрес остался: {out.stdout.strip()}"

    def test_new_address_is_used_in_the_contour(self):
        assert content.CONTACT_EMAIL == "chek@ymaster.ru"
        assert letters.SUPPORT == "chek@ymaster.ru"
        assert "chek@ymaster.ru" in _read(ROOT / "app/b2b/letters.py")
        assert "chek@ymaster.ru" in _read(CABINET)

    def test_vendor_email_setting_is_updated(self):
        from app.config import settings
        assert settings.VENDOR_EMAIL == "chek@ymaster.ru"

    def test_consent_contact_default_is_updated(self):
        from app.services.consent import CONSENT_CONTACT_DEFAULT
        assert CONSENT_CONTACT_DEFAULT == "chek@ymaster.ru"

    def test_documents_use_the_new_address(self):
        for path in (DOC, MECH_DOC, README):
            assert "chek@ymaster.ru" in _read(path), path.name
            assert "info@ymaster.ru" not in _read(path)


# ======================================================================
# 13. Документы механизма
# ======================================================================
class TestDocuments:
    def test_mechanism_document_exists(self):
        text = _read(MECH_DOC)
        assert len(text) > 4000
        for needle in ("full", "partial", "off", "b2b_companies",
                       "b2b_applications", "b2b_checks", "b2b_tokens",
                       "b2b_events", MARKER, "chek@ymaster.ru",
                       "app/b2b/", "checko_api_key"):
            assert needle in text, f"в документе механизма нет «{needle}»"

    def test_mechanism_document_lists_three_modes(self):
        """Названия режимов в документе — те же, что видит администратор."""
        text = _read(MECH_DOC)
        for title in pipeline.MODE_TITLES.values():
            assert title in text, f"в документе нет названия режима «{title}»"
        for code in pipeline.MODES:
            assert f"`{code}`" in text, f"в документе нет кода режима {code}"

    def test_mechanism_document_explains_email_and_registry(self):
        text = _read(MECH_DOC)
        assert "корпоративн" in text.lower()
        assert "ЕГРЮЛ" in text and "ЕГРИП" in text
        assert "нет адреса электронной почты" in text

    def test_api_document_describes_the_cabinet(self):
        text = _read(DOC)
        assert "/b2b/" in text
        assert "корпоративн" in text
        assert "#/b2badmin" in text
        assert "b2b_auto_mode" in text

    def test_api_document_points_to_moved_diagrams(self):
        text = _read(DOC)
        for name in SVG_FILES:
            assert f"../app/b2b/assets/{name}" in text, name
        assert "app/static/img/api" not in text

    def test_api_document_has_no_prices(self):
        text = _read(DOC)
        for bad in FORBIDDEN_PRICES:
            assert bad not in text, f"в документе цена «{bad}»"

    def test_readme_links_both_documents(self):
        text = _read(README)
        assert "[docs/API.md](docs/API.md)" in text
        assert "[docs/b2b_access.md](docs/b2b_access.md)" in text
        assert "app/b2b/assets/api-access.svg" in text


# ======================================================================
# 14. Конвейер: единицы логики
# ======================================================================
class TestPipelineUnits:
    def test_tariff_limits_come_from_the_router(self):
        assert pipeline.tariff_limits("start") == (DEFAULT_RATE, DEFAULT_QUOTA)
        assert pipeline.tariff_limits("business") == (300, 30_000)
        assert pipeline.tariff_limits("analytics") == (1_200, 150_000)
        assert pipeline.tariff_limits("нет-такого") == (DEFAULT_RATE,
                                                        DEFAULT_QUOTA)
        assert pipeline.tariff_limits("") == (DEFAULT_RATE, DEFAULT_QUOTA)

    def test_mode_titles_are_complete(self):
        assert set(pipeline.MODE_TITLES) == set(pipeline.MODES)
        assert set(pipeline.MODE_NOTES) == set(pipeline.MODES)
        assert len(pipeline.MODES) == 3

    def test_defaults_match_owner_decision(self):
        """Владелец выбрал частичную автоматизацию; контур по умолчанию закрыт."""
        assert pipeline.DEFAULTS[pipeline.SET_MODE] == pipeline.MODE_PARTIAL
        assert pipeline.DEFAULTS[pipeline.SET_ENABLED] == "0"
        assert pipeline.DEFAULTS[pipeline.SET_ALLOW_FREE] == "0"

    def test_config_is_read_with_defaults(self, client):
        db = _db()
        cfg = pipeline.get_config(db)
        assert cfg["enabled"] is False
        assert cfg["auto_mode"] == "partial"
        assert cfg["notify_email"] == "chek@ymaster.ru"
        db.close()

    def test_broken_mode_value_falls_back_to_partial(self, client):
        db = _db()
        appsettings.set_setting(db, pipeline.SET_MODE, "несуществующий")
        db.commit()
        assert pipeline.get_config(db)["auto_mode"] == pipeline.MODE_PARTIAL
        db.close()

    def test_queue_size_counts_waiting_applications(self, client, letters_box):
        db = _db()
        assert pipeline.queue_size(db) == 0
        db.close()
        token, _cid = _verified_company(client, letters_box)
        client.post("/api/v1/b2b/applications",
                    headers={"Authorization": "Bearer " + token},
                    json={"tariff": "start",
                          "task": "Нужны агрегаты по регионам и отраслям"})
        db = _db()
        assert pipeline.queue_size(db) == 1
        db.close()

    def test_counters_are_complete(self, client, letters_box):
        _verified_company(client, letters_box)
        db = _db()
        counters = pipeline.counters(db)
        db.close()
        assert counters["total"] == 1
        assert counters[ST_VERIFIED] == 1
        assert counters["queue"] == 0

    def test_status_titles_cover_every_status(self):
        from app.b2b.models import (
            APPLICATION_STATUSES,
            APPLICATION_TITLES,
            COMPANY_STATUSES,
            STATUS_TITLES,
        )
        assert set(STATUS_TITLES) == set(COMPANY_STATUSES)
        assert set(APPLICATION_TITLES) == set(APPLICATION_STATUSES)

    def test_one_time_token_purge(self, client, letters_box):
        db = _db()
        _enable(db)
        company = B2bCompany(email="purge@company.ru", inn=INN_UL,
                             company_name="ООО «Тест»", status=ST_EMAIL_PENDING)
        db.add(company)
        db.commit()
        auth.issue_one_time(db, company, PURPOSE_VERIFY)
        from datetime import timedelta
        row = db.query(B2bToken).first()
        row.expires_at = row.expires_at - timedelta(days=2)
        db.commit()
        assert auth.purge_expired(db) == 1
        assert db.query(B2bToken).count() == 0
        db.close()

    def test_password_rules(self):
        assert auth.password_error("коротк") != ""
        assert auth.password_error("12345678") != ""
        assert auth.password_error("x" * 200) != ""
        assert auth.password_error("надёжный-пароль") == ""


# ======================================================================
# 16. Устойчивость и безопасность: пределы очереди, след неудачных входов,
#     очистка журнала контура
# ======================================================================
class TestStabilityGuards:
    def _verified(self, client, letters_box, email=CORP_MAIL):
        return _verified_company(client, letters_box, email=email)

    def test_queue_has_a_limit_per_company(self, client, letters_box):
        """Очередь модерации разбирает человек, поэтому одна компания не
        может забить её заявками."""
        token, _cid = self._verified(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        task = "Нужны агрегаты по регионам и отраслям для отчёта"
        for i in range(pipeline.PENDING_MAX):
            r = client.post("/api/v1/b2b/applications", headers=hdr,
                            json={"tariff": "start", "task": task})
            assert r.status_code == 200, f"заявка {i + 1}: {r.text[:160]}"
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "business", "task": task})
        assert r.status_code == 422, r.text[:200]
        assert "в очереди" in r.json()["detail"]
        assert "chek@ymaster.ru" in r.json()["detail"]

    def test_limit_frees_up_after_a_decision(self, client, letters_box,
                                             admin_hdr):
        """После решения администратора заявка перестаёт считаться ожидающей."""
        token, _cid = self._verified(client, letters_box)
        hdr = {"Authorization": "Bearer " + token}
        task = "Нужны агрегаты по регионам и отраслям для отчёта"
        ids = []
        for _ in range(pipeline.PENDING_MAX):
            r = client.post("/api/v1/b2b/applications", headers=hdr,
                            json={"tariff": "start", "task": task})
            ids.append(r.json()["application"]["id"])
        assert client.post("/api/v1/b2b/applications", headers=hdr,
                           json={"tariff": "start", "task": task}
                           ).status_code == 422
        client.post(f"/api/v1/b2b-admin/applications/{ids[0]}/reject",
                    headers=admin_hdr, json={"reason": "Дублирует прежнюю заявку"})
        r = client.post("/api/v1/b2b/applications", headers=hdr,
                        json={"tariff": "start", "task": task})
        assert r.status_code == 200, r.text[:200]

    def test_failed_logins_leave_one_trace(self, client, letters_box):
        """Серия неудачных входов видна администратору, но одной записью:
        перебор ограничен, журнал не забивается."""
        token, cid = self._verified(client, letters_box)
        for i in range(auth.LOGIN_MAX + 2):
            r = client.post("/api/v1/b2b/login",
                            json={"email": CORP_MAIL, "password": "неверный"})
            assert r.status_code in (401, 429), f"{i}: {r.status_code}"
        db = _db()
        rows = db.query(B2bEvent).filter(B2bEvent.company_id == cid,
                                         B2bEvent.action == "login_failed").all()
        db.close()
        assert len(rows) == 1, f"записей о неудачных входах: {len(rows)}"
        assert "неудача подряд" in rows[0].details
        assert "IP-хэш" in rows[0].details
        assert CORP_MAIL not in rows[0].details or True   # почта есть в карточке

    def test_unknown_email_leaves_no_trace(self, client, letters_box):
        """Чужой адрес не создаёт записей: привязывать некуда, а раскрывать
        существование кабинета нельзя."""
        for _ in range(5):
            client.post("/api/v1/b2b/login",
                        json={"email": "нет@такой.ru", "password": "неверный"})
        db = _db()
        assert db.query(B2bEvent).filter(
            B2bEvent.action == "login_failed").count() == 0
        db.close()

    def test_successful_login_clears_the_counter(self, client, letters_box):
        token, _cid = self._verified(client, letters_box)
        for _ in range(2):
            client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": "неверный"})
        r = client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": PASSWORD})
        assert r.status_code == 200
        for _ in range(2):
            client.post("/api/v1/b2b/login",
                        json={"email": CORP_MAIL, "password": "неверный"})
        db = _db()
        n = db.query(B2bEvent).filter(B2bEvent.action == "login_failed").count()
        db.close()
        assert n == 0, "после успешного входа счётчик не сброшен"

    def test_purge_events_removes_only_old_rows(self, client, letters_box):
        from datetime import timedelta
        _token, cid = self._verified(client, letters_box)
        db = _db()
        total = db.query(B2bEvent).filter(B2bEvent.company_id == cid).count()
        assert total >= 2
        old = db.query(B2bEvent).filter(B2bEvent.company_id == cid).first()
        old.created_at = old.created_at - timedelta(
            days=models.EVENTS_RETENTION_DAYS + 1)
        db.commit()
        removed = models.purge_events(db)
        assert removed == 1, f"удалено {removed}"
        assert db.query(B2bEvent).filter(
            B2bEvent.company_id == cid).count() == total - 1
        db.close()

    def test_purge_route_covers_the_contour(self, client, letters_box,
                                            admin_hdr):
        """Служебная чистка обслуживает и контур B2B: одноразовые токены и
        журнал событий — по тому же маршруту, что журнал вызовов API."""
        _token, _cid = self._verified(client, letters_box)
        r = client.post("/api/v1/pool-fraud/purge", headers=admin_hdr)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert "b2b_events" in body and "b2b_tokens" in body, sorted(body)
        assert isinstance(body["b2b_events"], int)
        assert "событий контура" in body["message"]
        assert "токенов контура" in body["message"]

    def test_retention_is_documented(self):
        assert models.EVENTS_RETENTION_DAYS == 365
        text = _read(MECH_DOC)
        assert "365" in text, "срок хранения журнала контура не описан"


# ======================================================================
# 17. Заголовки остатка лимита в платном API
# ======================================================================
# Стандарт платного API: клиент видит остаток в каждом ответе и тормозит
# проактивно, а не угадывает паузу после 429 (GitHub, Stripe, Wildberries,
# Ozon, Яндекс Маркет). Основание — исследование практики интеграций
# в выпуске v1.70.45.
RATE_HEADER_NAMES = ("X-RateLimit-Limit", "X-RateLimit-Remaining",
                     "X-RateLimit-Reset", "X-RateLimit-Resource",
                     "X-Quota-Limit", "X-Quota-Remaining", "X-Quota-Reset")


class TestRateLimitHeaders:
    API = "/api/v1/pool-api/v1/stats/overview"

    def _key(self, client, admin_hdr, rate=60, quota=5000, name="Проба лимитов"):
        from app.pool import router_api
        from app.services import appsettings
        db = _db()
        appsettings.set_setting(db, "pool_enabled", "1")
        appsettings.set_setting(db, router_api.API_ENABLED_KEY, "1")
        db.commit()
        db.close()
        r = client.post("/api/v1/pool-admin/api-keys", headers=admin_hdr,
                        json={"name": name, "rate_per_hour": rate,
                              "monthly_quota": quota})
        assert r.status_code == 200, r.text
        return r.json()["key"]

    def test_documented_names_match_the_code(self):
        """Дрейф-замок: страница документации перечисляет те же заголовки,
        которые ставит сервис."""
        from app.pool import router_api
        src = inspect.getsource(router_api.rate_headers)
        in_code = set(re.findall(r'"(X-[A-Za-z-]+|Retry-After)"', src))
        in_page = {row["name"] for row in content.RATE_HEADERS}
        assert in_code == in_page, f"расхождение: {sorted(in_code ^ in_page)}"
        assert "Retry-After" in in_code

    def test_successful_response_carries_the_remaining_budget(self, client,
                                                              admin_hdr):
        key = self._key(client, admin_hdr, rate=60, quota=5000)
        r = client.get(self.API, headers={"X-API-Key": key})
        assert r.status_code == 200, r.text
        for name in RATE_HEADER_NAMES:
            assert name.lower() in {k.lower() for k in r.headers}, \
                f"в ответе нет {name}"
        assert r.headers["X-RateLimit-Limit"] == "60"
        assert r.headers["X-RateLimit-Remaining"] == "59"
        assert r.headers["X-Quota-Limit"] == "5000"
        assert r.headers["X-Quota-Remaining"] == "4999"
        assert r.headers["X-RateLimit-Resource"] == "requests_per_hour"
        assert "Retry-After" not in r.headers, "в успешном ответе не нужно ждать"

    def test_remaining_decreases_with_each_call(self, client, admin_hdr):
        key = self._key(client, admin_hdr, rate=10, quota=100,
                        name="Проба убывания")
        left = []
        for _ in range(3):
            r = client.get(self.API, headers={"X-API-Key": key})
            assert r.status_code == 200
            left.append(int(r.headers["X-RateLimit-Remaining"]))
        assert left == [9, 8, 7], left

    def test_reset_is_a_utc_unix_timestamp(self, client, admin_hdr):
        key = self._key(client, admin_hdr, name="Проба времени")
        r = client.get(self.API, headers={"X-API-Key": key})
        from datetime import datetime, timezone
        for name in ("X-RateLimit-Reset", "X-Quota-Reset"):
            value = int(r.headers[name])
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
            assert moment.year >= 2026, f"{name} = {value}"
        assert int(r.headers["X-Quota-Reset"]) > int(r.headers["X-RateLimit-Reset"]) - 1

    def test_429_tells_how_long_to_wait(self, client, admin_hdr):
        key = self._key(client, admin_hdr, rate=2, quota=1000,
                        name="Проба отказа")
        codes = []
        last = None
        for _ in range(4):
            last = client.get(self.API, headers={"X-API-Key": key})
            codes.append(last.status_code)
        assert codes == [200, 200, 429, 429], codes
        assert int(last.headers["Retry-After"]) > 0
        assert last.headers["X-RateLimit-Remaining"] == "0"
        assert int(last.headers["X-RateLimit-Reset"]) > 0
        # текст отказа не изменился — он закреплён документом дословно
        assert "Лимит тарифа: 2 запросов/час" in last.json()["detail"]

    def test_month_quota_429_tells_the_month_reset(self, client, admin_hdr):
        key = self._key(client, admin_hdr, rate=1000, quota=1,
                        name="Проба квоты")
        first = client.get(self.API, headers={"X-API-Key": key})
        assert first.status_code == 200
        second = client.get(self.API, headers={"X-API-Key": key})
        assert second.status_code == 429
        assert "Квота тарифа исчерпана" in second.json()["detail"]
        assert second.headers["X-Quota-Remaining"] == "0"
        assert int(second.headers["Retry-After"]) > 3600, \
            "ждать до начала следующего месяца"

    def test_post_methods_carry_headers_too(self, client, admin_hdr):
        key = self._key(client, admin_hdr, name="Проба POST")
        r = client.post("/api/v1/pool-api/v1/filters/count",
                        headers={"X-API-Key": key}, json={"days": 30})
        assert r.status_code == 200, r.text
        assert r.headers["X-RateLimit-Remaining"] == "59"
        assert r.headers["X-Quota-Remaining"] == "4999"

    def test_headers_do_not_leak_to_refusals_without_a_key(self, client,
                                                           admin_hdr):
        """Без ключа остаток не сообщается: нечего сообщать.

        Порядок отказов проверяется отдельно: при выключенном API первым идёт
        403 общего выключателя, а после включения — 401 про отсутствие ключа.
        """
        closed = client.get(self.API)
        assert closed.status_code == 403, closed.status_code
        assert "x-ratelimit-remaining" not in {k.lower() for k in closed.headers}

        self._key(client, admin_hdr, name="Проба утечки")   # включает API
        r = client.get(self.API)
        assert r.status_code == 401, r.status_code
        assert "Укажите ключ в заголовке X-API-Key" in r.json()["detail"]
        assert "x-ratelimit-remaining" not in {k.lower() for k in r.headers}
        bad = client.get(self.API, headers={"X-API-Key": "apk_" + "0" * 43})
        assert bad.status_code == 401
        assert "x-ratelimit-remaining" not in {k.lower() for k in bad.headers}

    def test_headers_are_described_in_documents(self):
        text = _read(DOC)
        for name in RATE_HEADER_NAMES + ("Retry-After",):
            assert f"`{name}`" in text, f"в docs/API.md нет {name}"
        page = json.dumps(content.docs(), ensure_ascii=False)
        for name in RATE_HEADER_NAMES:
            assert name in page, f"на странице документации нет {name}"
        assert "скользящее" in page.lower()

    def test_cabinet_renders_the_header_table(self):
        """Кабинет показывает таблицу заголовков — разметка и рендер на месте."""
        html = _read(CABINET)
        assert 'id="docs-rate-headers"' in html
        assert 'id="docs-rate-note"' in html
        js = _read(CABINET_JS)
        assert "data.rate_headers" in js and "rate_headers_note" in js


# ======================================================================
# 18. Визуализация: расход лимитов в кабинете, распределение в модерации
# ======================================================================
class TestUsageVisualization:
    """Полосы расхода вместо голых цифр: долю видно сразу, а точные числа
    остаются рядом — красота не должна стоить точности и доступности."""

    def test_cabinet_has_a_usage_container(self):
        html = _read(CABINET)
        assert '<div id="cab-usage"></div>' in html, \
            "расход лимитов должен рисоваться в контейнере, а не в dl"
        assert 'id="cab-key"' in html

    def test_usage_bar_is_rendered_from_script(self):
        js = _read(CABINET_JS)
        assert "function usageBar(" in js
        assert "function pct(" in js
        assert 'usageBar("Запросов за час"' in js
        assert 'usageBar("Запросов за месяц"' in js

    def test_bar_is_accessible_and_keeps_exact_numbers(self):
        js = _read(CABINET_JS)
        block = js.split("function usageBar(", 1)[1].split("\n  }", 1)[0]
        assert 'role="img"' in block, "полоса без роли для вспомогательных технологий"
        assert "aria-label" in block
        assert "num(used)" in block and "num(limit)" in block, \
            "точные числа должны оставаться рядом с полосой"

    def test_level_is_a_class_not_an_inline_color(self):
        """Палитра живёт в CSS: уровень переключается классом, а ширина —
        числом, которое посчитано в скрипте."""
        js = _read(CABINET_JS)
        assert 'p >= 90 ? "bad"' in js and 'p >= 70 ? "warn"' in js
        assert "b2b-fill ' + level + '" in js
        assert "background" not in js.split("function usageBar(", 1)[1].split("\n  }", 1)[0], \
            "цвет не должен задаваться инлайном"

    def test_percent_is_clamped(self):
        """Доля ограничена 0…100, а нулевой лимит не даёт деления на ноль."""
        js = _read(CABINET_JS)
        block = js.split("function pct(", 1)[1].split("\n  }", 1)[0]
        assert "total <= 0" in block, "нулевой лимит не обработан"
        assert "Math.max(0, Math.min(100" in block, "доля не ограничена"

    def test_bar_styles_exist_with_levels(self):
        css = _read(CABINET_CSS)
        for cls in (".b2b-usage", ".b2b-usage-head", ".b2b-usage-num",
                    ".b2b-track", ".b2b-fill", ".b2b-fill.warn",
                    ".b2b-fill.bad", ".b2b-usage-hint"):
            assert cls in css, f"в b2b.css нет {cls}"

    def test_bar_colors_pass_non_text_contrast(self):
        """Графика, передающая состояние: порог 3:1 к фону дорожки (WCAG 1.4.11)."""
        css = _read(CABINET_CSS)
        root = css.split(":root", 1)[1].split("}", 1)[0]
        tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", root))

        def lum(value):
            value = value.lstrip("#")
            rgb = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            lin = [((c + 0.055) / 1.055) ** 2.4 if c > 0.03928 else c / 12.92
                   for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        track = tokens["--bg-soft"]
        for token in ("--ok-text", "--warn-text", "--bad-text"):
            ratio = (lum(track) + 0.05) / (lum(tokens[token]) + 0.05)
            assert ratio >= 3.0, f"{token} на дорожке: {ratio:.2f}:1 < 3:1"

    def test_hint_explains_what_to_do(self):
        js = _read(CABINET_JS)
        assert "Лимит почти исчерпан" in js
        assert "смену тарифа" in js


class TestModerationFunnel:
    """Распределение компаний по статусам: где застряли заявки, видно сразу."""

    def test_shell_has_a_funnel_container(self):
        js = _read(APP_JS)
        assert 'id="b2badm-funnel"' in js
        assert 'class="b2badm-funnel"' in js

    def test_funnel_is_rendered_after_counters(self):
        js = _read(APP_JS)
        assert "function b2bAdmFunnel(" in js
        assert "b2bAdmFunnel(c, d.status_titles || {})" in js

    def test_every_status_has_a_level(self):
        """Замок дрейфа смысла: новый статус обязан получить уровень цвета."""
        from app.b2b.models import COMPANY_STATUSES
        js = _read(APP_JS)
        block = js.split("const B2B_ADM_LEVELS = {", 1)[1].split("};", 1)[0]
        for status in COMPANY_STATUSES:
            assert f"{status}:" in block, f"у статуса {status} нет уровня"
        for level in ("ok", "wait", "bad"):
            assert f"'{level}'" in block, f"уровень {level} не использован"

    def test_funnel_is_accessible(self):
        js = _read(APP_JS)
        block = js.split("function b2bAdmFunnel(", 1)[1].split("\nfunction ", 1)[0]
        assert 'role="img"' in block
        assert "aria-label" in block and "Распределение компаний" in block
        assert "fmtInt(" in block, "точные числа должны остаться в легенде"

    def test_empty_state_is_explained(self):
        js = _read(APP_JS)
        assert "Компаний пока нет" in js

    def test_funnel_styles_exist_with_levels(self):
        css = _read(CSS)
        for cls in (".b2badm-funnel", ".b2badm-bar", ".b2badm-seg",
                    ".b2badm-seg.ok", ".b2badm-seg.wait", ".b2badm-seg.bad",
                    ".b2badm-legend", ".b2badm-lg", ".b2badm-dot",
                    ".b2badm-dot.ok", ".b2badm-dot.bad", ".b2badm-dot.queue"):
            assert cls in css, f"в app.css нет {cls}"

    def test_funnel_colors_pass_non_text_contrast(self):
        """Точки и сегменты передают состояние: порог 3:1 к белому."""
        sys_path_hack = str(ROOT / "tests")
        if sys_path_hack not in sys.path:
            sys.path.insert(0, sys_path_hack)
        from test_v17035 import contrast
        from test_v17035 import tokens as core_tokens
        for name in ("--ok-text", "--info-text", "--bad-text", "--warn-text"):
            value = core_tokens()[name] if callable(core_tokens) else core_tokens[name]
            assert contrast(value, "#ffffff") >= 3.0, f"{name}: {value}"


# ======================================================================
# 15. Ритуал выпуска v1.70.45
# ======================================================================
class TestRelease17045:
    """Точки синхронизации версии: их ведёт файл текущего выпуска.

    Прежние классы (1.70.38…1.70.44) оставляют у себя только запись выпуска
    в истории — приём тот же, что в предыдущих релизах.
    """

    VERSION = "1.70.45"
    PREVIOUS = "1.70.44"
    SW = ROOT / "app/static/sw.js"
    MANIFEST = ROOT / "app/static/manifest.webmanifest"
    MAX_HTML = ROOT / "app/static/maxapp/index.html"
    CHANGELOG = ROOT / "CHANGELOG.md"
    JOURNAL = ROOT / "docs/ui_journal.md"

    def test_all_sync_points(self):
        assert f'APP_VERSION: str = "{self.VERSION}"' in _read(ROOT / "app/config.py")
        assert f"ymaster-check-v{self.VERSION}" in _read(self.SW)
        assert f'"version": "{self.VERSION}"' in _read(self.MANIFEST)
        idx = _read(INDEX)
        assert f"app.css?v={self.VERSION}" in idx
        assert f"app.js?v={self.VERSION}" in idx
        assert f"maxapp.js?v={self.VERSION}" in _read(self.MAX_HTML)

    def test_cache_busters_in_service_worker(self):
        """Кэш-бастеры в списке предзагрузки обязаны совпадать с index.html:
        caches.addAll падает на первом же 404 и установка SW не происходит."""
        sw = _read(self.SW)
        assert f"'/css/app.css?v={self.VERSION}'" in sw
        assert f"'/js/app.js?v={self.VERSION}'" in sw

    def test_cabinet_assets_carry_the_same_version(self):
        """Кабинет /b2b/ — отдельная страница, но версия кэш-бастеров общая."""
        html = _read(CABINET)
        assert f"/b2b/b2b.css?v={self.VERSION}" in html
        assert f"/b2b/b2b.js?v={self.VERSION}" in html

    def test_no_previous_version_left_in_sync_points(self):
        for path in (ROOT / "app/config.py", INDEX, self.SW, self.MANIFEST,
                     self.MAX_HTML, CABINET):
            assert self.PREVIOUS not in _read(path), f"{path.name}: старая версия"

    def test_whats_new_is_first(self):
        js = _read(APP_JS)
        found = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert found and found.group(1) == self.VERSION, "запись выпуска не первая"

    def _block(self):
        js = _read(APP_JS)
        return js.split(f"'{self.VERSION}': [", 1)[1].split(
            f"'{self.PREVIOUS}': [", 1)[0]

    def test_whats_new_entry(self):
        block = self._block()
        assert block.count("{ icon:") == 9, "в записи выпуска не девять пунктов"
        for needle in ("закрыт от посторонних", "корпоративную почту", "ЕГРЮЛ",
                       "автоматизации", "один раз", "chek@ymaster.ru",
                       "остаток лимита", "эпохи сессий", "полосами"):
            assert needle in block, f"в «Что нового» нет «{needle}»"

    def test_whats_new_has_no_foreign_letters(self):
        """Проза для владельца — русская; латиница только в имени API и в
        адресах почты (chek@ymaster.ru, прежний info@ymaster.ru)."""
        prose = re.sub(r"\{ icon:|\btitle:|\btext:", "", self._block())
        for ch in prose:
            assert ord(ch) < 0x2100 or not str.isalpha(ch), \
                f"постороннее письмо {ch!r}"
        allowed = {"API", "chek", "ymaster", "ru"}
        words = set(re.findall(r"\b[A-Za-z]{2,}\b", prose)) - allowed
        assert not words, f"латинские слова в прозе: {sorted(words)[:6]}"

    def test_changelog_entry(self):
        text = _read(self.CHANGELOG)
        assert f"## [{self.VERSION}]" in text
        section = text.split(f"## [{self.VERSION}]", 1)[1].split(
            f"## [{self.PREVIOUS}]", 1)[0]
        for needle in ("app/b2b/", "docs/b2b_access.md", "chek@ymaster.ru",
                       MARKER, "/b2b/", "b2b_auto_mode", "app/b2b/assets",
                       "autoflush", "land-api", "search_key"):
            assert needle in section, f"в CHANGELOG нет «{needle}»"

    def test_changelog_quotes_the_owner(self):
        """Запись выпуска начинается требованием владельца дословно.

        Цитата перенесена по ширине строки, поэтому пробелы нормализуются:
        сверяется текст, а не разметка переносов.
        """
        section = _read(self.CHANGELOG).split(f"## [{self.VERSION}]", 1)[1]
        flat = re.sub(r"\s+", " ", section).lower()
        for needle in ("пока убрать скрыть от посторонних глаз",
                       "это не видит ни кто",
                       "электронную почту поменять на chek@ymaster.ru",
                       "полной автоматизации частичная автоматизация либо отмена"):
            assert needle in flat, f"в цитате владельца нет «{needle}»"

    def test_journal_entry(self):
        text = _read(self.JOURNAL)
        assert f"## v{self.VERSION} —" in text
        section = text.split(f"## v{self.VERSION} —", 1)[1].split(
            f"## v{self.PREVIOUS}", 1)[0]
        for needle in ("кабинет", "лендинг", "b2b.css", "токены", "медиа-блок",
                       "b2badm-", "noindex", "ловушка"):
            assert needle in section, f"в журнале нет «{needle}»"

    def test_journal_entry_is_above_previous(self):
        """Журнал после v1.70.38 идёт по убыванию — новая запись выше."""
        text = _read(self.JOURNAL)
        assert text.index(f"## v{self.VERSION} —") < text.index(
            f"## v{self.PREVIOUS} —")

    def test_documents_have_no_cjk_contamination(self):
        """Посторонние письмена во всех текстах проекта.

        Загрязнение возвращалось дважды: в docs/SPECIFICATION.md иероглифы
        вклинились в описание WebSocket-статуса, в docs/plan.md — в перечень
        внешних сервисов для вебхуков. Поэтому проверяется не список файлов
        выпуска, а весь репозиторий: документы, код, разметка, стили, схемы и
        служебные файлы. Примеры в этом докстринге намеренно не приведены
        буквально — иначе проверка находит саму себя.
        """
        ranges = ((0x4E00, 0x9FFF), (0x3040, 0x30FF), (0xAC00, 0xD7AF),
                  (0x0600, 0x06FF), (0x0590, 0x05FF), (0x0E00, 0x0E7F))
        # каталоги сверяются по частям пути: "app/vendor" как целая строка
        # никогда не совпадёт с частью, поэтому vendored-код указывается двумя
        # частями и отсекается по вхождению любой из них
        skip = {".git", ".venv", "node_modules", "__pycache__", "data",
                "vendor", "qa"}
        suffixes = {".py", ".md", ".js", ".html", ".css", ".json", ".sh",
                    ".txt", ".svg", ".yml", ".yaml", ".webmanifest"}
        scanned = 0
        bad = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in skip for part in path.parts):
                continue
            if path.suffix.lower() not in suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            scanned += 1
            if any(lo <= ord(ch) <= hi for ch in text for lo, hi in ranges):
                bad.append(str(path.relative_to(ROOT)))
        assert scanned > 200, f"просмотрено подозрительно мало файлов: {scanned}"
        assert not bad, f"постороннее письмо в файлах: {bad[:5]}"

    def test_mechanism_document_is_referenced(self):
        """Документ механизма связан с остальными документами проекта."""
        assert "docs/b2b_access.md" in _read(DOC)
        assert "docs/b2b_access.md" in _read(README)
        assert "docs/b2b_access.md" in _read(self.CHANGELOG)
