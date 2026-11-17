"""Приёмная папка первички (T-109, v1.73.0, ADR-60): сервер сам забирает УПД из каталога.

Проверяется то, на чём такой приём ломается на практике: недописанный файл, повторная выкладка той же
папки, путь вне разрешённых корней, ссылка наружу, карантин с причиной, отбор по ИНН, изоляция компаний,
интервал фонового обхода и ручная кнопка. Только синтетические документы (ADR-14); сеть и настоящий СБИС
не нужны — папка локальная.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.primary import inbox, service
from tests.conftest import login
from tests.primary_samples import inn10, upd_xml
from tests.test_v1140 import _mk_user


# ------------------------------------------------------------------ общие приспособления
@pytest.fixture
def box(tmp_path, monkeypatch):
    """Свой разрешённый корень и чистая настройка на каждый тест.

    Настройка приёмной папки своя таблица, а не app_settings: автоснимок настроек из conftest её не
    восстанавливает. Поэтому состояние сбрасывается здесь — иначе результат зависел бы от соседнего теста
    (этим уже болели настройки приложения, v1.70.39).
    """
    from app.database import SessionLocal
    from app.models import AppSetting
    from app.primary.models import PrimaryInboxFile, PrimaryInboxSetting

    root = tmp_path / "inbox-root"
    root.mkdir()
    monkeypatch.setenv("YM_PRIMARY_INBOX_ROOTS", str(root))
    monkeypatch.delenv("YM_PRIMARY_INBOX_OFF", raising=False)
    with SessionLocal() as db:
        db.query(PrimaryInboxSetting).delete(synchronize_session=False)
        db.query(PrimaryInboxFile).delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.like("primary_inbox_%")).delete(synchronize_session=False)
        db.commit()
    inbox._purged["day"] = ""
    yield root
    with SessionLocal() as db:
        db.query(PrimaryInboxSetting).delete(synchronize_session=False)
        db.query(PrimaryInboxFile).delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.like("primary_inbox_%")).delete(synchronize_session=False)
        db.commit()


def _company(client, hdr, role="accountant"):
    """Компания с случайным ИНН и её бухгалтер; возвращает словарь с id, ИНН и заголовком."""
    inn = inn10()
    res = client.post("/api/v1/companies", headers=hdr, json={"name": f"Приёмная {uuid4().hex}", "inn": inn})
    assert res.status_code in (200, 201), res.text
    company_id = res.json()["id"]
    acc = _mk_user(client, hdr, company_id, f"inbox_{uuid4().hex[:10]}", role=role)
    return {"id": company_id, "inn": inn, "hdr": acc}


def _settings(db, path, enabled=True, interval=10, user=None):
    return inbox.save(db, path=str(path), enabled=enabled, interval=interval, user=user)


def _put(path: Path, name: str, data: bytes) -> Path:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


# ------------------------------------------------------------------ путь: белый список корней
def test_path_outside_allowed_roots_is_refused():
    with SessionLocal() as db:
        with pytest.raises(inbox.InboxError) as exc:
            inbox.save(db, path="/etc", enabled=True, interval=10)
        assert exc.value.status == 403
        assert inbox.row(db).path == ""          # отказ ничего не меняет


def test_dotdot_cannot_escape_root(box):
    with SessionLocal() as db:
        with pytest.raises(inbox.InboxError):
            inbox.check_path(db, str(box / ".." / ".." / "etc"))


def test_missing_directory_has_clear_reason(box):
    with SessionLocal() as db:
        with pytest.raises(inbox.InboxError) as exc:
            inbox.save(db, path=str(box / "нет-такой"), enabled=True, interval=10)
        assert exc.value.status == 404 and "не найден" in str(exc.value)


def test_interval_limits(box):
    with SessionLocal() as db:
        _settings(db, box)
        for bad in (0, inbox.MAX_INTERVAL + 1):
            with pytest.raises(inbox.InboxError):
                inbox.save(db, path=str(box), enabled=True, interval=bad)


def test_symlink_to_outside_is_not_walked(box, tmp_path):
    """Ссылка внутри папки уводила бы обход наружу — она пропускается с замечанием."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "чужой.xml").write_bytes(upd_xml())
    os.symlink(outside, box / "ссылка")
    found, notes = inbox.walk(box)
    assert found == [] and any("ссылка" in n for n in notes)


def test_service_directories_are_skipped(box):
    _put(box, "processed/2026-11/старый.xml", upd_xml())
    _put(box, "quarantine/брак.xml", "<Файл/>".encode())
    _put(box, "новый.xml", upd_xml())
    found, _notes = inbox.walk(box)
    assert [p.name for p in found] == ["новый.xml"]


def test_half_written_file_is_not_read(box):
    """Первый замер совпал со вторым — читаем; файл дописывается — ждём следующий обход."""
    path = _put(box, "растёт.xml", "<Файл".encode())
    with SessionLocal():
        data, reason = inbox._read_ready(path, 0)
        assert data == "<Файл".encode() and reason == ""
    # имитация «дописывается»: второй stat видит другой размер
    calls = {"n": 0}
    real_stat = Path.stat

    def fake_stat(self, *a, **kw):
        calls["n"] += 1
        st = real_stat(self, *a, **kw)
        if calls["n"] >= 2:
            class _S:
                st_size = st.st_size + 5
                st_mtime_ns = st.st_mtime_ns + 1
            return _S()
        return st

    path2 = _put(box, "пишется.xml", "<Файл ИдФайл=\"1\"".encode())
    Path.stat = fake_stat
    try:
        data, reason = inbox._read_ready(path2, 0)
    finally:
        Path.stat = real_stat
    assert data is None and "пишется" in reason


def test_temp_and_empty_files_are_ignored(box):
    _put(box, "скачивается.xml.part", upd_xml())
    _put(box, "~$замок.xml", b"x")
    _put(box, ".скрытый.xml", b"x")
    _put(box, "пустой.xml", b"")
    found, _notes = inbox.walk(box)
    assert [p.name for p in found] == ["пустой.xml"]
    with SessionLocal():
        data, reason = inbox._read_ready(box / "пустой.xml", 0)
        assert data is None and "пустой" in reason


# ------------------------------------------------------------------ разбор и раскладка
@pytest.fixture
def two_companies(client):
    hdr = login(client, "admin", "admin123")
    a, b = _company(client, hdr), _company(client, hdr)
    yield hdr, a, b
    client.cookies.clear()


def test_file_goes_to_company_by_buyer_inn(box, two_companies):
    hdr, a, _b = two_companies
    own = upd_xml(buyer=("ООО «Покупатель»", a["inn"], "770401001"), number=f"ПРИ-{uuid4().hex[:6]}")
    _put(box, "входящий/УПД.xml", own)
    with SessionLocal() as db:
        _settings(db, box)
        res = inbox.run_once(db, pause=0)
    assert res["ok"] == 1, res
    assert res["files"] == 1
    assert (box / "processed").is_dir() and not (box / "входящий").exists()
    moved = list((box / "processed").rglob("УПД.xml"))
    assert len(moved) == 1                                  # исходный файл не удалён, а перенесён
    with SessionLocal() as db:
        from app.primary.models import PrimaryDocument
        doc = db.query(PrimaryDocument).filter(PrimaryDocument.buyer_inn == a["inn"]).one()
        assert doc.company_id == a["id"] and doc.channel == "inbox"


def test_foreign_document_goes_to_quarantine_with_reason(box, two_companies):
    _hdr, _a, _b = two_companies
    stranger = upd_xml(buyer=("ООО «Чужой»", inn10(), "770401001"), number=f"ЧУЖ-{uuid4().hex[:6]}")
    _put(box, "чужой.xml", stranger)
    with SessionLocal() as db:
        _settings(db, box)
        res = inbox.run_once(db, pause=0)
    assert res["rejected"] == 1 and res["foreign"] == 1, res
    quarantined = list((box / "quarantine").rglob("чужой.xml"))
    assert len(quarantined) == 1
    note = quarantined[0].with_name("чужой.xml.reason.txt")
    assert note.is_file() and "ИНН" in note.read_text(encoding="utf-8")


def test_second_run_of_same_folder_does_nothing(box, two_companies):
    """Повторная выкладка той же папки не задваивает: файл уже перенесён, а его хеш есть в журнале."""
    _hdr, a, _b = two_companies
    doc = upd_xml(buyer=("ООО «Покупатель»", a["inn"], "770401001"), number=f"ПОВ-{uuid4().hex[:6]}")
    _put(box, "один.xml", doc)
    with SessionLocal() as db:
        _settings(db, box)
        first = inbox.run_once(db, pause=0)
        # тот же файл снова в корне (например, синхронизация вернула копию)
        _put(box, "один-снова.xml", doc)
        second = inbox.run_once(db, pause=0)
    assert first["ok"] == 1 and second["files"] == 1
    assert second["duplicate"] == 1 and second["ok"] == 0


def test_zip_archive_is_unpacked_and_routed(box, two_companies):
    import io
    import zipfile
    _hdr, a, _b = two_companies
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ON_NSCHFDOPPR_1.xml", upd_xml(buyer=("ООО «П»", a["inn"], "770401001"),
                                                   number=f"ЗИП-{uuid4().hex[:6]}"))
        zf.writestr("ON_NSCHFDOPPR_2.xml", upd_xml(buyer=("ООО «Чужой»", inn10(), "770401001"),
                                                  number=f"ЗИП-{uuid4().hex[:6]}"))
    _put(box, "пакет.zip", buf.getvalue())
    with SessionLocal() as db:
        _settings(db, box)
        res = inbox.run_once(db, pause=0)
    assert res["ok"] == 1 and res["rejected"] == 1, res


def test_broken_xml_is_rejected_not_crashing(box):
    _put(box, "мусор.xml", "<это не УПД>".encode())
    with SessionLocal() as db:
        _settings(db, box)
        res = inbox.run_once(db, pause=0)
    assert res["rejected"] == 1 and res["files"] == 1
    assert list((box / "quarantine").rglob("мусор.xml"))


def test_journal_keeps_reason_and_document(box, two_companies):
    _hdr, a, _b = two_companies
    _put(box, "ждём.xml", upd_xml(buyer=("ООО «П»", a["inn"], "770401001"), number=f"ЖУР-{uuid4().hex[:6]}"))
    with SessionLocal() as db:
        _settings(db, box)
        inbox.run_once(db, pause=0)
        admin = db.query(__import__("app.models", fromlist=["User"]).User).filter_by(role="admin").first()
        page = inbox.log(db, admin)
    assert page["items"] and page["items"][0]["status"] == "ok"
    assert page["items"][0]["document_id"]
    assert page["counters"]["total"] >= 1


def test_log_is_scoped_to_own_company(client, box, two_companies):
    """Бухгалтер чужой компании не видит запись журнала о документе другой компании."""
    _hdr, a, b = two_companies
    _put(box, "для-А.xml", upd_xml(buyer=("ООО «П»", a["inn"], "770401001"), number=f"ОБЛ-{uuid4().hex[:6]}"))
    with SessionLocal() as db:
        _settings(db, box)
        inbox.run_once(db, pause=0)
    other = client.get("/api/v1/primary/inbox/log", headers=b["hdr"])
    assert other.status_code == 200
    assert other.json()["items"] == []
    own = client.get("/api/v1/primary/inbox/log", headers=a["hdr"])
    assert own.status_code == 200 and own.json()["items"]


# ------------------------------------------------------------------ расписание и кнопка
def test_due_respects_enabled_and_interval(box):
    with SessionLocal() as db:
        _settings(db, box, enabled=False, interval=10)
        assert inbox.due(db) is False
        _settings(db, box, enabled=True, interval=30)
        assert inbox.due(db) is True                        # ещё ни разу не обходили
        inbox.run_once(db, pause=0)
        assert inbox.due(db) is False                        # только что обошли
        item = inbox.row(db)
        item.last_run_at = inbox._now().replace(year=2020)
        db.commit()
        assert inbox.due(db) is True


def test_off_switch_stops_background(box, monkeypatch):
    monkeypatch.setenv("YM_PRIMARY_INBOX_OFF", "1")
    with SessionLocal() as db:
        _settings(db, box)
        assert inbox.due(db) is False
        assert "YM_PRIMARY_INBOX_OFF" in " ".join(inbox.state(db)["problems"])


def test_state_reports_problems_honestly(box):
    with SessionLocal() as db:
        item = inbox.row(db)
        item.path = str(box / "исчезла")
        db.commit()
        problems = inbox.state(db)["problems"]
    assert any("не найден" in p for p in problems)


# ------------------------------------------------------------------ API
def test_api_settings_admin_only(client, box, two_companies):
    _hdr, a, _b = two_companies
    body = {"path": str(box), "enabled": True, "interval_minutes": 15}
    accountant = client.put("/api/v1/primary/inbox/settings", json=body, headers=a["hdr"])
    assert accountant.status_code == 403
    admin = client.put("/api/v1/primary/inbox/settings", json=body, headers=login(client, "admin", "admin123"))
    assert admin.status_code == 200, admin.text
    assert admin.json()["path"] == str(box) and admin.json()["enabled"] is True


def test_api_settings_rejects_foreign_path(client):
    admin = login(client, "admin", "admin123")
    res = client.put("/api/v1/primary/inbox/settings",
                     json={"path": "/etc", "enabled": True, "interval_minutes": 15}, headers=admin)
    assert res.status_code == 403
    client.cookies.clear()


def test_api_run_now_and_state(client, box, two_companies):
    _hdr, a, _b = two_companies
    _put(box, "раз.xml", upd_xml(buyer=("ООО «П»", a["inn"], "770401001"), number=f"РАЗ-{uuid4().hex[:6]}"))
    admin = login(client, "admin", "admin123")
    assert client.put("/api/v1/primary/inbox/settings",
                      json={"path": str(box), "enabled": False, "interval_minutes": 10},
                      headers=admin).status_code == 200
    run = client.post("/api/v1/primary/inbox/run", headers=a["hdr"])
    assert run.status_code == 200, run.text
    assert run.json()["result"]["ok"] == 1
    state = client.get("/api/v1/primary/inbox/state", headers=a["hdr"])
    assert state.status_code == 200 and state.json()["last_run_at"]
    client.cookies.clear()


def test_api_route_does_not_shadow_document_card(client, two_companies):
    """GET /primary/inbox/log не должен перехватываться маршрутом карточки /primary/{doc_id}."""
    _hdr, a, _b = two_companies
    res = client.get("/api/v1/primary/inbox/log", headers=a["hdr"])
    assert res.status_code == 200 and "items" in res.json()


# ------------------------------------------------------------------ совместимость с прежним путём
def test_routed_accept_still_works_without_allowed_list(two_companies):
    """Прежний вызов «Папки с УПД» (без явного списка компаний) не сломан."""
    _hdr, a, _b = two_companies
    with SessionLocal() as db:
        from app.models import User
        admin = db.query(User).filter_by(role="admin").first()
        results = service.accept_routed(db, admin, upd_xml(buyer=("ООО «П»", a["inn"], "770401001"),
                                                          number=f"СТА-{uuid4().hex[:6]}"), "старый.xml")
    assert [r["result"] for r in results] == ["created"]


def test_archive_with_one_bad_file_goes_to_processed(box, two_companies):
    """Смешанный архив ЭДО: наш УПД принят, испорченная строка — только в журнале с причиной.

    Возвращать весь архив в карантин из-за одной строки нельзя: человек грузил бы его снова и снова,
    а принятый документ уже в реестре.
    """
    import io
    import zipfile
    _hdr, a, _b = two_companies
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ON_NSCHFDOPPR_наш.xml", upd_xml(buyer=("ООО «П»", a["inn"], "770401001"),
                                                    number=f"МИК-{uuid4().hex[:6]}"))
        zf.writestr("служебный.xml", "<это не УПД>".encode())
    _put(box, "смешанный.zip", buf.getvalue())
    with SessionLocal() as db:
        _settings(db, box)
        res = inbox.run_once(db, pause=0)
        from app.models import User
        admin = db.query(User).filter_by(role="admin").first()
        page = inbox.log(db, admin, limit=10)
    assert res["ok"] == 1 and res["rejected"] == 1, res
    assert (box / "processed").is_dir() and not (box / "quarantine").exists()
    by_status = {i["status"]: i for i in page["items"]}
    assert set(by_status) == {"ok", "rejected"}
    assert by_status["rejected"]["message"]            # причина отказа видна словами


def test_empty_subfolders_are_removed_after_move(box, two_companies):
    _hdr, a, _b = two_companies
    _put(box, "сбис/2026/ноябрь/УПД.xml",
         upd_xml(buyer=("ООО «П»", a["inn"], "770401001"), number=f"ПАП-{uuid4().hex[:6]}"))
    with SessionLocal() as db:
        _settings(db, box)
        inbox.run_once(db, pause=0)
    assert not (box / "сбис").exists()
    assert list((box / "processed").rglob("УПД.xml"))


def test_accountant_run_now_limited_to_own_companies(client, box, two_companies):
    """Бухгалтер нажал «Забрать сейчас»: документ чужой компании не принимается его правами."""
    _hdr, _a, _b = two_companies
    hdr_admin = login(client, "admin", "admin123")
    stranger_inn = inn10()
    client.post("/api/v1/companies", headers=hdr_admin, json={"name": f"Чужая {uuid4().hex}", "inn": stranger_inn})
    _put(box, "чужой.xml", upd_xml(buyer=("ООО «Чужой»", stranger_inn, "770401001"), number=f"ЧУЖ-{uuid4().hex[:6]}"))
    client.put("/api/v1/primary/inbox/settings", json={"path": str(box), "enabled": False, "interval_minutes": 10},
               headers=hdr_admin)
    _hdr, a, _b = two_companies
    run = client.post("/api/v1/primary/inbox/run", headers=a["hdr"])
    assert run.status_code == 200
    assert run.json()["result"]["ok"] == 0 and run.json()["result"]["rejected"] == 1
    client.cookies.clear()


def test_limit_leaves_rest_for_next_run(box, two_companies):
    _hdr, a, _b = two_companies
    for i in range(3):
        _put(box, f"пачка/УПД-{i}.xml", upd_xml(buyer=("ООО «П»", a["inn"], "770401001"),
                                                file_id=f"ON_NSCHFDOPPR_пачка_{i}_{uuid4().hex[:6]}",
                                                number=f"ЛИМ-{uuid4().hex[:6]}"))
    with SessionLocal() as db:
        _settings(db, box)
        first = inbox.run_once(db, pause=0, limit=2)
        second = inbox.run_once(db, pause=0)
    assert first["files"] == 2 and first["left"] == 1
    assert second["files"] == 1
