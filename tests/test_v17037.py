# -*- coding: utf-8 -*-
# Ямастер Чек — тесты v1.70.37: регистрация через MAX, метка канала,
# заполнение карточки и согласие на обработку персональных данных (152-ФЗ).
# ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
#
# Критерии приёмки:
#  - участник, пришедший через мини-приложение MAX, помечается каналом
#    reg_source='max' (Telegram-бот — 'tg'); администратор видит метку в
#    списке участников и в профиле;
#  - ДО согласия в карточке не хранятся имя, фамилия, ник, фото и язык:
#    сессия создаёт участника только по идентификатору MAX (п. 5 ч. 1 ст. 6);
#  - текст согласия версионируется и содержит реквизиты ч. 4 ст. 9 152-ФЗ:
#    оператор, цели, перечень данных, действия, срок, порядок отзыва, права;
#  - согласие принимается только по подписанной платформой initData —
#    подделка и чужой токен дают 401 и ничего не заполняют;
#  - после согласия карточка заполняется данными MAX (имя, фамилия, ник,
#    язык, фото только по https) и обновляется при следующих запусках;
#  - отзыв согласия очищает персональные поля (ст. 21), баллы сохраняются;
#  - замки: v1.70.36 (подпись initData), v1.69.0 (API мини-приложения),
#    v1.41.0 (список участников) не задеты.
import re

from tests.test_v1690 import BOT_TOKEN, build_init_data

PHOTO = "https://max.ru/photos/u123.jpg"


# ------------------------------------------------------------------ версии
class TestVersion17037:
    def test_version_is_release(self):
        cfg = open("app/config.py", encoding="utf-8").read()
        ver = re.search(r'APP_VERSION: str = "([^"]+)"', cfg).group(1)
        assert tuple(int(x) for x in ver.split(".")) >= (1, 70, 37)


# ------------------------------------------------------------------ схема
class TestSchema:
    """Колонки карточки и безопасная миграция существующих БД."""

    def test_pool_user_has_channel_and_consent_columns(self):
        from app.pool.models import PoolUser
        cols = {c.name: c for c in PoolUser.__table__.columns}
        for name in ("reg_source", "first_name", "last_name", "photo_url",
                     "language", "consent_at", "consent_version",
                     "consent_source"):
            assert name in cols, f"нет колонки {name}"

    def test_consent_at_is_nullable(self):
        from app.pool.models import PoolUser
        col = {c.name: c for c in PoolUser.__table__.columns}["consent_at"]
        assert col.nullable is True, "consent_at обязан допускать NULL"

    def test_migration_covers_every_new_column(self):
        src = open("app/database.py", encoding="utf-8").read()
        assert '"pool_users": [' in src, "нет блока миграции pool_users"
        block = src.split('"pool_users": [', 1)[1].split("\n        ],", 1)[0]
        for name in ("reg_source", "first_name", "last_name", "photo_url",
                     "language", "consent_at", "consent_version",
                     "consent_source"):
            assert f'("{name}",' in block, f"миграция не добавляет {name}"
            assert f"ADD COLUMN {name}" in block, f"нет ALTER для {name}"

    def test_migration_keeps_old_data_safe_defaults(self):
        src = open("app/database.py", encoding="utf-8").read()
        block = src.split('"pool_users": [', 1)[1].split("\n        ],", 1)[0]
        # текстовые поля — с пустым значением по умолчанию, дата — NULL:
        # существующие участники не получают «согласие задним числом»
        assert "consent_at DATETIME NULL" in block
        for name in ("reg_source", "first_name", "last_name", "photo_url",
                     "language", "consent_version", "consent_source"):
            assert re.search(rf"ADD COLUMN {name} VARCHAR\(\d+\) DEFAULT ''",
                             block), f"{name}: нет DEFAULT ''"


# ------------------------------------------------------------------ текст
class TestConsentText:
    """Содержание согласия — ч. 4 ст. 9 152-ФЗ."""

    def test_endpoint_returns_current_redaction(self, client):
        from app.services.consent import CONSENT_VERSION
        r = client.get("/api/v1/maxapp/consent")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["version"] == CONSENT_VERSION
        assert d["title"] and d["lead"] and d["operator"] and d["contact"]
        assert len(d["sections"]) >= 6
        assert d["text"], "плоский текст согласия обязателен для журнала"

    def test_text_names_operator_and_law(self):
        from app.services import consent
        txt = consent.full_text()
        assert "152-ФЗ" in txt
        assert "ООО «Ямастер»" in txt
        assert "@" in consent.CONSENT_CONTACT_DEFAULT

    def test_no_invented_requisites_in_code(self):
        """Юридические факты не выдумываются: в коде нет «значений по
        умолчанию» для ИНН, ОГРН и адреса — их заполняет владелец."""
        src = read("app/services/consent.py")
        assert not re.search(r"ИНН\s*\d{6,}", src), "в коде зашит ИНН"
        assert not re.search(r"ОГРН\s*\d{6,}", src), "в коде зашит ОГРН"
        assert not re.search(r"адрес:\s*\d{6}", src), "в коде зашит адрес"
        # а вот что в коде есть — только подтверждённое репозиторием
        assert 'OPERATOR_NAME = "ООО «Ямастер»"' in src
        assert 'OPERATOR_SITE = "https://ymaster.ru"' in src
        assert 'CONSENT_CONTACT_DEFAULT = "chek@ymaster.ru"' in src

    def test_default_operator_line_is_honest(self):
        """Без реквизитов в настройках текст не придумывает адрес, а указывает
        общеизвестный источник и почту для запроса."""
        from app.services import consent
        line = consent.operator_line()
        assert "ООО «Ямастер»" in line and "ymaster.ru" in line
        assert "ЕГРЮЛ" in line and consent.CONSENT_CONTACT_DEFAULT in line
        assert consent.requisites_complete() is False

    def test_text_has_every_mandatory_element(self):
        from app.services import consent
        txt = consent.full_text()
        # цели, перечень, действия, срок, отзыв, права — все разделы на месте
        for part in ("Цели обработки", "Перечень персональных данных",
                     "Действия с данными", "Срок действия и отзыв согласия",
                     "Права субъекта персональных данных"):
            assert part in txt, f"в согласии нет раздела «{part}»"
        # перечень данных совпадает с тем, что реально заполняется
        for item in ("идентификатор аккаунта MAX", "имя и фамилия",
                     "ник", "фотографию профиля", "код языка"):
            assert item in txt, f"в перечне нет «{item}»"
        assert "Роскомнадзор" in txt, "не указан орган для обжалования"
        assert "отозвано" in txt or "отзыв" in txt

    def test_text_is_freely_given_wording(self):
        from app.services import consent
        # ст. 9: согласие должно быть конкретным, информированным и сознательным
        assert "своей волей и в своём интересе" in consent.CONSENT_LEAD
        assert "трансграничная передача не осуществляется" in consent.full_text()

    def test_profile_fields_match_consent_list(self):
        from app.services import consent
        # заполняем ровно то, что перечислено в согласии — не больше
        assert set(consent.PROFILE_FIELDS) == {"first_name", "last_name",
                                               "photo_url", "language"}
        txt = consent.full_text()
        assert "имя и фамилия" in txt and "фотографию профиля" in txt \
            and "код языка" in txt and "ник" in txt


class TestOperatorRequisites:
    """Реквизиты оператора — настройка администратора, а не константа в коде."""

    INN = "5501280595"
    OGRN = "1245500017062"
    ADDR = "644085, г. Омск, пр-кт Мира, д. 183, офис 27"
    MAIL = "privacy@example.com"  # RFC 2606: зарезервирован для примеров

    def _put(self, client, hdr, **kw):
        return client.put("/api/v1/settings/consent", headers=hdr, json=kw)

    def _reset(self, client, hdr):
        self._put(client, hdr, inn="", ogrn="", address="", contact="")

    def test_admin_fills_requisites_and_text_updates(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        try:
            before = client.get("/api/v1/settings/consent", headers=hdr).json()
            assert before["complete"] is False
            r = self._put(client, hdr, inn=self.INN, ogrn=self.OGRN,
                          address=self.ADDR, contact=self.MAIL)
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["ok"] is True and d["complete"] is True
            assert self.INN in d["operator_line"] and self.ADDR in d["operator_line"]
            # текст согласия (публичный и в сессии мини-приложения) обновился
            doc = client.get("/api/v1/maxapp/consent").json()
            assert doc["requisites_complete"] is True
            assert self.INN in doc["operator"] and self.ADDR in doc["operator"]
            assert self.MAIL in doc["text"]
            assert "ЕГРЮЛ" not in doc["operator"], "осталась ссылка вместо адреса"
            st = client.get("/api/v1/settings/consent", headers=hdr).json()
            assert st["inn"] == self.INN and st["ogrn"] == self.OGRN
            assert st["address"] == self.ADDR and st["contact"] == self.MAIL
        finally:
            self._reset(client, hdr)
        assert client.get("/api/v1/settings/consent",
                          headers=hdr).json()["complete"] is False

    def test_digits_are_extracted_from_noisy_input(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        try:
            r = self._put(client, hdr, inn=" 55 01-28 0595 ", ogrn="1 2 4 5 5 0 0 0 1 7 0 6 2")
            assert r.status_code == 200, r.text
            st = client.get("/api/v1/settings/consent", headers=hdr).json()
            assert st["inn"] == self.INN and st["ogrn"] == self.OGRN
        finally:
            self._reset(client, hdr)

    def test_bad_requisites_are_refused(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        try:
            assert self._put(client, hdr, inn="12345").status_code == 422
            assert self._put(client, hdr, ogrn="12345").status_code == 422
            r = self._put(client, hdr, contact="не почта")
            assert r.status_code == 422 and "chek@" in r.json()["detail"]
            # отказ ничего не записал
            st = client.get("/api/v1/settings/consent", headers=hdr).json()
            assert st["inn"] == "" and st["contact"] == "chek@ymaster.ru"
        finally:
            self._reset(client, hdr)

    def test_partial_fill_is_allowed_but_marked(self, client):
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        try:
            r = self._put(client, hdr, inn=self.INN)
            assert r.status_code == 200
            d = r.json()
            assert d["complete"] is False
            assert "п. 2 ч. 4 ст. 9 152-ФЗ" in d["message"]
            assert self.INN in d["operator_line"] and "ЕГРЮЛ" in d["operator_line"]
        finally:
            self._reset(client, hdr)

    def test_requires_admin(self, client):
        """Реквизиты оператора — только администратор. Проверяем СВЕЖИМ
        клиентом: общий `client` несёт cookie прежнего входа, и с ней запрос
        оказался бы авторизованным не потому, что так задумано."""
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as anon:
            assert anon.get("/api/v1/settings/consent").status_code in (401, 403)
            assert anon.put("/api/v1/settings/consent",
                            json={"inn": self.INN}).status_code in (401, 403)

    def test_action_is_written_to_audit(self, client, monkeypatch):
        """Изменение реквизитов оператора — событие журнала действий: кто и
        когда поправил текст согласия, должно быть доказуемо.

        Проверяем сам факт вызова `log_action`, а не строку в таблице: журнал
        пишет отдельной сессией и сам назначает `id = max + 1`, поэтому при
        параллельном прогоне (`-n 2`) возможна гонка идентификаторов — это
        известное свойство журнала (см. журнал UI, v1.18.0), а не поведение
        настроек согласия."""
        from tests.conftest import login
        # `log_action` импортирован в модуль настроек на уровне файла, поэтому
        # подменяем имя именно там: патч исходного модуля не подействовал бы.
        import app.routers.settings_routes as sr
        calls = []
        monkeypatch.setattr(
            sr, "log_action",
            lambda user, action, **kw: calls.append((action, kw)))
        hdr = login(client, "admin", "admin123")
        calls.clear()                       # вход сам пишется в журнал
        try:
            self._put(client, hdr, inn=self.INN, address=self.ADDR)
            assert calls, "сохранение реквизитов не попало в журнал"
            action, kw = calls[0]
            assert action == "consent_requisites_updated"
            assert self.INN in str(kw.get("details")), "в журнале нет ИНН"
        finally:
            self._reset(client, hdr)

    def test_settings_card_exists(self):
        js = read(APP_JS)
        assert 'id="st-consent"' in js
        assert "'/api/v1/settings/consent'" in js
        for i in ("cs-inn", "cs-ogrn", "cs-address", "cs-contact",
                  "cs-save", "cs-preview"):
            assert f"#{i}" in js or f'id="{i}"' in js, f"нет элемента {i}"
        assert "п. 2 ч. 4 ст. 9 152-ФЗ" in js
        assert "CONSENT_VERSION" in js, "не сказано, как поднять редакцию"
        # программа не заполняет реквизиты за владельца
        assert "не выдумывает юридические факты" in js


# ------------------------------------------------------------------ сессия
class TestSessionGate:
    """До согласия карточка пустая: только идентификатор и канал."""

    def _setup(self):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
            appsettings.set_setting(s, "pool_enabled", "1")
        finally:
            s.rollback(); s.close()

    def test_session_marks_channel_and_holds_personal_data(self, client):
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        self._setup()
        uid = 771001
        r = client.post("/api/v1/maxapp/session", json={"init_data":
                        build_init_data(uid, photo_url=PHOTO)})
        assert r.status_code == 200, r.text
        d = r.json()
        me = d["me"]
        assert me["reg_source"] == "max"
        assert me["consent"]["given"] is False
        assert me["first_name"] == "" and me["last_name"] == ""
        assert me["photo_url"] == "" and me["language"] == ""
        assert me["name"] == "", "имя не должно появляться до согласия"
        # первый вход — клиент обязан показать текст согласия
        assert "consent_doc" in d and d["consent_doc"]["version"]
        assert "152-ФЗ" in d["consent_doc"]["text"]
        # в базе — тоже пусто
        s = SessionLocal()
        try:
            u = s.query(PoolUser).filter(PoolUser.tg_user_id == str(uid)).one()
            assert u.reg_source == "max"
            assert u.consent_at is None
            assert (u.first_name or "") == "" and (u.photo_url or "") == ""
            assert (u.tg_username or "") == "", "ник записан до согласия"
        finally:
            s.close()

    def test_repeat_session_still_empty_without_consent(self, client):
        self._setup()
        uid = 771002
        for _ in range(2):
            r = client.post("/api/v1/maxapp/session", json={"init_data":
                            build_init_data(uid, photo_url=PHOTO)})
            assert r.status_code == 200, r.text
            assert r.json()["me"]["first_name"] == ""
            assert r.json()["me"]["consent"]["given"] is False

    def test_token_is_still_required(self, client):
        """Замок v1.70.36: initData чужим токеном не проходит."""
        self._setup()
        bad = build_init_data(771003, token="999." + "Z" * 40,
                              photo_url=PHOTO)
        r = client.post("/api/v1/maxapp/session", json={"init_data": bad})
        assert r.status_code == 401


# ------------------------------------------------------------------ согласие
class TestConsentGive:
    def _setup(self):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
            appsettings.set_setting(s, "pool_enabled", "1")
        finally:
            s.rollback(); s.close()

    def test_consent_fills_card(self, client):
        from app.services.consent import CONSENT_VERSION, SOURCE_MAXAPP
        self._setup()
        uid = 771010
        init = build_init_data(uid, first_name="Мария", last_name="Иванова",
                               photo_url=PHOTO)
        r = client.post("/api/v1/maxapp/session", json={"init_data": init})
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = client.post("/api/v1/maxapp/consent", json={"init_data": init})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d["consent"]["given"] is True
        assert d["consent"]["version"] == CONSENT_VERSION
        assert d["consent"]["source"] == SOURCE_MAXAPP
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                        d["consent"]["at"]), d["consent"]
        assert set(d["filled"]) >= {"first_name", "last_name", "photo_url",
                                    "language", "tg_username"}
        me = d["me"]
        assert me["first_name"] == "Мария" and me["last_name"] == "Иванова"
        assert me["photo_url"] == PHOTO and me["language"] == "ru"
        assert me["name"] == "Мария Иванова"
        assert me["consent"]["stale"] is False
        # /me отдаёт то же самое по токену участника
        r = client.get("/api/v1/maxapp/me", headers=tok)
        assert r.json()["first_name"] == "Мария"

    def test_consent_persists_in_db(self, client):
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        from app.services.consent import CONSENT_VERSION
        self._setup()
        uid = 771011
        init = build_init_data(uid, first_name="Пётр", photo_url=PHOTO)
        client.post("/api/v1/maxapp/session", json={"init_data": init})
        assert client.post("/api/v1/maxapp/consent",
                           json={"init_data": init}).status_code == 200
        s = SessionLocal()
        try:
            u = s.query(PoolUser).filter(PoolUser.tg_user_id == str(uid)).one()
            assert u.consent_at is not None
            assert u.consent_version == CONSENT_VERSION
            assert u.consent_source == "max-miniapp"
            assert u.first_name == "Пётр" and u.photo_url == PHOTO
            assert u.tg_username == f"u{uid}"
        finally:
            s.close()

    def test_next_session_refreshes_profile(self, client):
        self._setup()
        uid = 771012
        init = build_init_data(uid, first_name="Ольга", photo_url=PHOTO)
        client.post("/api/v1/maxapp/session", json={"init_data": init})
        client.post("/api/v1/maxapp/consent", json={"init_data": init})
        # сменил фамилию и фото в MAX — при следующем запуске карточка свежая
        init2 = build_init_data(uid, first_name="Ольга", last_name="Петрова",
                                photo_url="https://max.ru/p/new.jpg")
        r = client.post("/api/v1/maxapp/session", json={"init_data": init2})
        me = r.json()["me"]
        assert me["last_name"] == "Петрова"
        assert me["photo_url"] == "https://max.ru/p/new.jpg"
        # и текст согласия повторно не навязывается
        assert "consent_doc" not in r.json()

    def test_http_photo_is_refused(self, client):
        self._setup()
        uid = 771013
        init = build_init_data(uid, photo_url="http://max.ru/insecure.jpg")
        client.post("/api/v1/maxapp/session", json={"init_data": init})
        d = client.post("/api/v1/maxapp/consent",
                        json={"init_data": init}).json()
        assert d["me"]["photo_url"] == "", "незащищённая ссылка на фото принята"
        assert d["me"]["first_name"] == "Максим", "остальные поля заполняются"

    def test_consent_requires_valid_signature(self, client):
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        self._setup()
        uid = 771014
        init = build_init_data(uid, photo_url=PHOTO)
        client.post("/api/v1/maxapp/session", json={"init_data": init})
        # чужой токен бота
        foreign = build_init_data(uid, token="888." + "Y" * 40,
                                  photo_url=PHOTO)
        assert client.post("/api/v1/maxapp/consent",
                           json={"init_data": foreign}).status_code == 401
        # мусор
        assert client.post("/api/v1/maxapp/consent",
                           json={"init_data": "abc"}).status_code in (401, 422)
        # просроченная сессия
        old = build_init_data(uid, photo_url=PHOTO,
                              auth_date=1_600_000_000)
        assert client.post("/api/v1/maxapp/consent",
                           json={"init_data": old}).status_code == 401
        s = SessionLocal()
        try:
            u = s.query(PoolUser).filter(PoolUser.tg_user_id == str(uid)).one()
            assert u.consent_at is None, "согласие записано без проверки подписи"
            assert (u.first_name or "") == ""
        finally:
            s.close()

    def test_consent_for_another_max_id_does_not_leak(self, client):
        """Согласие принимается только для того id, который подписан MAX."""
        self._setup()
        mine = build_init_data(771015, first_name="Анна", photo_url=PHOTO)
        other = build_init_data(771016, first_name="Борис", photo_url=PHOTO)
        client.post("/api/v1/maxapp/session", json={"init_data": mine})
        d = client.post("/api/v1/maxapp/consent",
                        json={"init_data": other}).json()
        assert d["me"]["max_id"] == "771016", "согласие ушло не тому участнику"
        assert d["me"]["first_name"] == "Борис"


# ------------------------------------------------------------------ отзыв
class TestConsentRevoke:
    def _setup(self):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
            appsettings.set_setting(s, "pool_enabled", "1")
        finally:
            s.rollback(); s.close()

    def test_revoke_clears_profile_and_keeps_points(self, client):
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        self._setup()
        uid = 771020
        init = build_init_data(uid, first_name="Кирилл", photo_url=PHOTO)
        r = client.post("/api/v1/maxapp/session", json={"init_data": init})
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
        client.post("/api/v1/maxapp/consent", json={"init_data": init})
        s = SessionLocal()
        try:
            u = s.query(PoolUser).filter(PoolUser.tg_user_id == str(uid)).one()
            u.points = 42
            s.commit()
        finally:
            s.close()
        r = client.post("/api/v1/maxapp/consent/revoke", headers=tok)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d["consent"]["given"] is False
        assert d["me"]["first_name"] == "" and d["me"]["photo_url"] == ""
        assert d["me"]["name"] == "" and d["me"]["language"] == ""
        assert "152-ФЗ" in d["message"] or "обезлич" in d["message"]
        s = SessionLocal()
        try:
            u = s.query(PoolUser).filter(PoolUser.tg_user_id == str(uid)).one()
            assert u.consent_at is None and u.consent_version == ""
            assert (u.first_name or "") == "" and (u.photo_url or "") == ""
            assert (u.tg_username or "") == "", "ник пережил отзыв согласия"
            assert int(u.points or 0) == 42, "баллы потеряны при отзыве"
            assert u.reg_source == "max", "канал регистрации — не персональные"
        finally:
            s.close()

    def test_after_revoke_session_offers_consent_again(self, client):
        self._setup()
        uid = 771021
        init = build_init_data(uid, photo_url=PHOTO)
        r = client.post("/api/v1/maxapp/session", json={"init_data": init})
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
        client.post("/api/v1/maxapp/consent", json={"init_data": init})
        client.post("/api/v1/maxapp/consent/revoke", headers=tok)
        r = client.post("/api/v1/maxapp/session", json={"init_data": init})
        d = r.json()
        assert d["me"]["consent"]["given"] is False
        assert "consent_doc" in d, "после отзыва текст согласия не предложен"
        assert d["me"]["first_name"] == "", "профиль заполнился без согласия"

    def test_revoke_requires_pool_token(self, client):
        assert client.post("/api/v1/maxapp/consent/revoke").status_code in (401, 403)

    def test_double_revoke_is_safe(self, client):
        self._setup()
        uid = 771022
        init = build_init_data(uid, photo_url=PHOTO)
        r = client.post("/api/v1/maxapp/session", json={"init_data": init})
        tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
        client.post("/api/v1/maxapp/consent", json={"init_data": init})
        assert client.post("/api/v1/maxapp/consent/revoke",
                           headers=tok).status_code == 200
        r = client.post("/api/v1/maxapp/consent/revoke", headers=tok)
        assert r.status_code == 200, r.text
        assert r.json()["consent"]["given"] is False


# ------------------------------------------------------------------ админ
class TestAdminSeesChannel:
    """Администратор видит метку MAX в списке и в профиле участника."""

    def _setup(self):
        from app.database import SessionLocal
        from app.services import appsettings
        s = SessionLocal()
        try:
            appsettings.set_setting(s, "max_bot_token", BOT_TOKEN)
            appsettings.set_setting(s, "pool_enabled", "1")
        finally:
            s.rollback(); s.close()

    def _max_user(self, client, uid, first, last="Тестов", photo=PHOTO):
        """Сессия MAX (+ согласие, если нужно) — возвращает initData."""
        self._setup()
        init = build_init_data(uid, first_name=first, last_name=last,
                               photo_url=photo)
        client.post("/api/v1/maxapp/session", json={"init_data": init})
        return init

    def test_participants_expose_channel_and_consent(self, client):
        from tests.conftest import login
        uid = 771030
        init = self._max_user(client, uid, "Дарья", "Смирнова")
        client.post("/api/v1/maxapp/consent", json={"init_data": init})
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/pool-admin/participants?q=u771030", headers=hdr)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert items, "участник не найден в списке"
        u = items[0]
        assert u["reg_source"] == "max"
        assert u["first_name"] == "Дарья" and u["last_name"] == "Смирнова"
        assert u["photo_url"] == PHOTO
        assert u["consent_given"] is True and u["consent_version"]
        assert u["consent_at"], "момент согласия не отдаётся администратору"

    def test_participant_without_consent_is_marked(self, client):
        """До согласия в списке нет ни имени, ни ника (152-ФЗ): участник
        опознаётся только меткой канала и идентификатором."""
        from tests.conftest import login
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        uid = 771031
        self._max_user(client, uid, "Иван")
        s = SessionLocal()
        try:
            pid = s.query(PoolUser).filter(
                PoolUser.tg_user_id == str(uid)).one().id
        finally:
            s.close()
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/pool-admin/participants?limit=200", headers=hdr)
        items = [i for i in r.json()["items"] if i["id"] == pid]
        assert items, "участник без согласия пропал из списка"
        u = items[0]
        assert u["reg_source"] == "max"
        assert u["consent_given"] is False
        assert u["first_name"] == "" and u["last_name"] == ""
        assert u["nickname"] == "", "ник отдан без согласия"
        assert u["photo_url"] == "" and u["consent_at"] == ""

    def test_telegram_participant_marked_tg(self, client):
        from app.database import SessionLocal
        from app.pool import ingest
        s = SessionLocal()
        try:
            u = ingest.get_or_create_user(s, tg_user_id="tg-771032",
                                          tg_username="tg_nick")
            if not (u.reg_source or ""):
                u.reg_source = "tg"
            s.commit()
            uid = u.id
        finally:
            s.close()
        from tests.conftest import login
        hdr = login(client, "admin", "admin123")
        r = client.get("/api/v1/pool-admin/participants?q=tg_nick", headers=hdr)
        items = r.json()["items"]
        assert items and items[0]["reg_source"] == "tg"
        assert items[0]["consent_given"] is False

    def test_impersonate_shows_channel_and_name(self, client):
        from tests.conftest import login
        from app.database import SessionLocal
        from app.pool.models import PoolUser
        uid = 771033
        init = self._max_user(client, uid, "Светлана", "Козлова")
        client.post("/api/v1/maxapp/consent", json={"init_data": init})
        s = SessionLocal()
        try:
            pid = s.query(PoolUser).filter(
                PoolUser.tg_user_id == str(uid)).one().id
        finally:
            s.close()
        hdr = login(client, "admin", "admin123")
        r = client.post(f"/api/v1/pool-admin/impersonate-pool/{pid}", headers=hdr)
        assert r.status_code == 200, r.text
        u = r.json()["user"]
        assert u["reg_source"] == "max"
        assert u["name"] == "Светлана Козлова"
        assert u["photo_url"] == PHOTO
        assert u["consent_given"] is True


# ============================================================ фронтенд
APP_JS = "app/static/js/app.js"
APP_CSS = "app/static/css/app.css"
INDEX = "app/static/index.html"
MX_JS = "app/static/maxapp/maxapp.js"
MX_HTML = "app/static/maxapp/index.html"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _lum(hexcolor):
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    ch = []
    for part in (h[0:2], h[2:4], h[4:6]):
        v = int(part, 16) / 255.0
        ch.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def contrast(fg, bg):
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


class TestMaxBadge:
    """Значок MAX: разметка, применение и контраст."""

    def test_helper_exists_and_is_used_everywhere(self):
        js = read(APP_JS)
        assert "function srcBadge(src)" in js
        assert "if (src !== 'max') return '';" in js, "значок не только для MAX"
        # список участников, полоса просмотра, окно подтверждения, кабинет
        assert js.count("srcBadge(") >= 5, "значок применён не во всех местах"
        for ctx in ("pp-rows", "viewas-text", "startViewAsPool",
                    "pool-summary"):
            assert ctx in js

    def test_badge_precedes_name_in_participants_row(self):
        js = read(APP_JS)
        row = js.split("rows.innerHTML = d.items.length", 1)[1][:900]
        i_badge = row.index("srcBadge(u.reg_source)")
        i_name = row.index("<b>${esc(nm || '—')}</b>")
        assert i_badge < i_name, "значок должен стоять ПЕРЕД именем"
        assert "participantName(u)" in js

    def test_participant_name_prefers_max_profile(self):
        js = read(APP_JS)
        fn = js.split("function participantName(u)", 1)[1][:300]
        assert "first_name" in fn and "last_name" in fn
        assert "u.email" in fn and "u.nickname" in fn

    def test_consent_state_visible_to_admin(self):
        js = read(APP_JS)
        assert "согласие 152-ФЗ не дано" in js
        assert "u.reg_source === 'max' && !u.consent_given" in js
        assert "info.consent" in js and "согласие 152-ФЗ от " in js

    def test_search_covers_name(self):
        js = read(APP_JS)
        assert "Поиск по e-mail или имени" in js
        src = read("app/pool/router_admin.py")
        assert 'prof["last_name"]' in src and "u.tg_username or \"\"" in src

    def test_css_declares_badge(self):
        css = read(APP_CSS)
        assert ".src-max {" in css
        block = css.split(".src-max {", 1)[1].split("}", 1)[0]
        assert "var(--accent)" in block and "#1a1a1a" in block
        assert "border-radius: 999px" in block

    def test_badge_contrast_passes_aa(self):
        # тёмный текст на корпоративном оранжевом — 6.66:1 при пороге 4.5:1
        assert contrast("#1a1a1a", "#ff7a00") >= 4.5
        assert round(contrast("#1a1a1a", "#ff7a00"), 2) == 6.66

    def test_no_undefined_vars_in_new_css(self):
        css = read(APP_CSS)
        declared = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
        assert used - declared == set(), f"не объявлены: {sorted(used - declared)}"


class TestMiniAppConsent:
    """Экран согласия и профиль в мини-приложении MAX."""

    def test_ids_in_js_exist_in_markup(self):
        html = read(MX_HTML)
        for i in ("view-consent", "consent-title", "consent-body", "consent-ver",
                  "consent-agree", "btn-consent-ok", "btn-consent-skip",
                  "consent-note", "consent-msg", "me-card", "me-photo",
                  "me-name", "me-src", "me-consent", "btn-consent-toggle"):
            assert f'id="{i}"' in html, f"в разметке нет #{i}"

    def test_consent_view_is_hidden_by_default(self):
        html = read(MX_HTML)
        assert '<div id="view-consent" class="hidden">' in html
        # кнопка согласия неактивна, пока галочка не поставлена
        ok = html.split('id="btn-consent-ok"', 1)[1][:120]
        assert "disabled" in ok

    def test_note_explains_what_is_stored_without_consent(self):
        html = read(MX_HTML)
        note = html.split('id="consent-note"', 1)[1][:600]
        assert "152-ФЗ" in note or "идентификатор MAX" in note
        assert "Не сохраняются" in note or "не сохраняются" in note

    def test_js_gates_session_on_consent(self):
        js = read(MX_JS)
        assert "if (d.consent_doc" in js
        i_gate = js.index("if (d.consent_doc")
        i_app = js.index("await afterBoot(sp)")
        assert i_gate < i_app, "карточка показывается до экрана согласия"
        # подтверждение входа с сайта согласием не задерживается
        assert "sp.indexOf('login_') !== 0" in js

    def test_consent_post_carries_init_data(self):
        js = read(MX_JS)
        call = js.split("'/api/v1/maxapp/consent'", 1)[1][:200]
        assert "init_data: app.initData" in call

    def test_revoke_is_two_step(self):
        js = read(MX_JS)
        assert "REVOKE_ARMED" in js
        assert "'/api/v1/maxapp/consent/revoke'" in js
        assert "Точно отозвать" in js

    def test_site_login_passes_init_data(self):
        js = read(MX_JS)
        for path in ("confirm-login", "invite/accept"):
            call = js.split(f"'/api/v1/maxapp/{path}'", 1)[1][:300]
            assert "init_data" in call, f"{path} без initData"

    def test_profile_photo_only_when_present(self):
        js = read(MX_JS)
        fn = js.split("function renderMe()", 1)[1][:900]
        assert "ME.photo_url" in fn and "removeAttribute('src')" in fn
        assert "classList.toggle('hidden', ME.reg_source !== 'max')" in fn

    def test_spacing_classes_are_declared(self):
        html = read(MX_HTML)
        style = html.split("<style>", 1)[1].split("</style>", 1)[0]
        for c in ("gap-8", "mb-6", "text-center"):
            assert f".{c} {{" in style or f".{c}{{" in style, f".{c} не объявлен"

    def test_consent_css_uses_theme_vars(self):
        html = read(MX_HTML)
        style = html.split("<style>", 1)[1].split("</style>", 1)[0]
        for c in (".consent-box", ".consent-check", ".me-photo", ".src-max"):
            assert c in style, f"нет стиля {c}"
        block = style.split(".src-max {", 1)[1].split("}", 1)[0]
        assert "var(--accent)" in block and "var(--accent-fg)" in block


class TestFirstVisit:
    """Подсказка первого визита на странице входа."""

    def test_markup_is_present_and_hidden(self):
        html = read(INDEX)
        assert '<div class="max-intro hidden" id="max-intro" role="note"' in html
        assert 'aria-labelledby="max-intro-title"' in html
        for i in ("max-intro-title", "max-intro-close", "max-intro-reg",
                  "max-intro-scan", "max-intro-doc"):
            assert f'id="{i}"' in html, f"в разметке нет #{i}"
        assert 'aria-label="Скрыть подсказку"' in html

    def test_two_max_options_are_offered(self):
        html = read(INDEX)
        box = html.split('id="max-intro"', 1)[1][:1800]
        assert "Регистрация через MAX" in box
        assert "Сдать чек в MAX" in box
        assert "Инструкция" in box
        assert "152-ФЗ" in box, "не сказано про согласие"

    def test_shown_once_and_dismissible(self):
        js = read(APP_JS)
        assert "MAX_INTRO_KEY = 'ymaster_max_intro_seen'" in js
        assert "localStorage.getItem(MAX_INTRO_KEY)" in js
        assert "localStorage.setItem(MAX_INTRO_KEY" in js
        assert "if (seen) return;" in js
        # каждое действие человека гасит подсказку
        for btn in ("max-intro-close", "max-intro-reg", "max-intro-scan",
                    "max-intro-doc"):
            handler = js.split(f"$('#{btn}')", 1)[1][:220]
            assert "hide(true)" in handler, f"{btn} не гасит подсказку"

    def test_nothing_happens_by_itself(self):
        """Безопасность: блок не редиректит и не открывает MAX сам."""
        js = read(APP_JS)
        fn = js.split("function initMaxIntro()", 1)[1].split("\nasync function", 1)[0]
        assert "location.href" not in fn
        assert "location.replace" not in fn
        # открытие MAX — только внутри обработчиков кнопок
        assert fn.count("window.open(") == 2
        assert "openManual()" in fn

    def test_bot_link_reuses_configured_nick(self):
        js = read(APP_JS)
        fn = js.split("function maxBotLink(", 1)[1][:220]
        assert "window.MXBOT || 'chek.ymaster.bot'" in fn
        assert "encodeURIComponent(startParam)" in fn

    def test_wired_on_login_screen(self):
        js = read(APP_JS)
        assert "initMaxIntro();" in js
        shown = js.split("function showLogin()", 1)[1][:900]
        assert "initMaxIntro();" in shown

    def test_css_uses_declared_tokens(self):
        css = read(APP_CSS)
        assert ".max-intro {" in css
        block = css.split(".max-intro {", 1)[1].split("}", 1)[0]
        assert "var(--accent-soft)" in block
        assert "--text-muted" not in css, "необъявленная переменная вернулась"

    def test_phone_rule_lives_in_existing_block(self):
        css = read(APP_CSS)
        # урок v1.70.35: отдельный блок media ломает порядок каскада и проверки
        assert css.count("@media (max-width: 520px)") == 1
        block = css.split("@media (max-width: 520px)", 1)[1]
        assert ".max-intro-actions .btn" in block


class TestDocs:
    """Документация для приложения MAX."""

    def test_manual_section_covers_consent(self):
        src = read("app/services/manual_content.py")
        sec = src.split('"id": "maxapp"', 1)[1].split('"id": "assistants"', 1)[0]
        for part in ("Согласие на данные профиля (152-ФЗ)", "152-ФЗ",
                     "Метка MAX", "Что происходит при отзыве",
                     "Первый визит на сайт", "ст. 21 152-ФЗ"):
            assert part in sec, f"в разделе mini-приложения нет «{part}»"
        assert "п. 5 ч. 1 ст. 6 152-ФЗ" in sec

    def test_manual_section_covers_admin_view(self):
        src = read("app/services/manual_content.py")
        sec = src.split('"id": "viewas"', 1)[1].split('"id": "update"', 1)[0]
        assert "метка канала регистрации" in sec
        assert "согласие 152-ФЗ не дано" in sec
        assert "1.70.37" in sec

    def test_privacy_note_matches_implementation(self):
        src = read("app/services/manual_content.py")
        sec = src.split('"id": "maxapp"', 1)[1].split('"id": "assistants"', 1)[0]
        # документация не обещает того, чего нет в коде
        assert "только идентификатор" in sec
        assert "фото профиля сервер не скачивает" in sec
        cs = read("app/services/consent.py")
        assert 'startswith("https://")' in cs


class TestVersion17037Sync:
    """v1.70.38: выпуск 1.70.37 отгружен.

    Точки синхронизации показывают ТЕКУЩУЮ версию (её проверяет test_v17038),
    поэтому здесь остаётся запись выпуска в истории — там, где она и должна жить.
    """

    def test_release_record_stays_in_history(self):
        assert "## [1.70.37]" in read("CHANGELOG.md")
        assert "'1.70.37': [" in read(APP_JS)
        # в инструкции версия упоминается без «v»: «С версии 1.70.37 …»
        assert "1.70.37" in read("app/services/manual_content.py")
        assert "v1.70.37" in read("docs/ui_journal.md")

    def test_whats_new_entry(self):
        js = read(APP_JS)
        block = js.split("'1.70.37': [", 1)[1].split("'1.70.36': [", 1)[0]
        assert block.count("{ icon:") == 4
        for part in ("значок MAX", "152-ФЗ", "Первый визит", "localStorage",
                     "gap-8"):
            assert part in block, f"в «Что нового» нет «{part}»"


class TestRelease:
    """Требование «своя запись первая» живёт в файле текущего выпуска."""

    VERSION = "1.70.55"

    def test_whats_new_is_first(self):
        js = read(APP_JS)
        m = re.search(r"WHATS_NEW\s*=\s*\{[\s\S]{0,400}?'([\d.]+)'", js)
        assert m and m.group(1) == self.VERSION, "запись выпуска не первая"

    def test_changelog_entry(self):
        assert f"## [{self.VERSION}]" in read("CHANGELOG.md")

    def test_journal_entry(self):
        # v1.70.38: раздел журнала СВОЕГО выпуска проверяется по своей версии,
        # а не по self.VERSION — тот теперь следит за текущим выпуском (1.70.38),
        # и раздел v1.70.37 в журнале остаётся историей, а не «текущим».
        ver = "1.70.37"
        j = read("docs/ui_journal.md")
        assert f"## v{ver} —" in j, "в журнале нет раздела выпуска"
        sec = j.split(f"## v{ver} —", 1)[1].split("## v1.70.38", 1)[0]
        for needle in ("ст. 9 152-ФЗ", "reg_source", "6.66:1", "54 failed",
                       "localStorage"):
            assert needle in sec, f"в журнале нет «{needle}»"
