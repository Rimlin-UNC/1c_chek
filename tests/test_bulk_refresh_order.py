"""#50 (v1.70.99): массовое обновление карточек ЕГРЮЛ берёт следующие компании, а не одни и те же.

Раньше выборка «первые 40» шла без порядка, и при больше чем 40 компаниях остальные не обновлялись никогда.
Теперь сначала ни разу не обновлённые, затем самые давние; ответ сообщает, сколько ещё ждут обновления.
Checko подменён, ИНН синтетические (ADR-14).
"""
from __future__ import annotations

from uuid import uuid4

from app.database import SessionLocal
from app.models import Company
from app.services import checko as ck
from tests.primary_samples import inn10
from tests.test_v1140 import login


def test_each_run_takes_the_next_companies(client, monkeypatch):
    hdr = login(client, "admin", "admin123")
    assert client.put("/api/v1/settings/checko", json={"api_key": "bulkkey1234567890"}, headers=hdr).status_code == 200
    seen: list[str] = []

    def fake_fetch(_key, inn):
        seen.append(inn)
        return {"card": {"kind": "legal", "name_full": f"ООО {inn}", "inn": inn, "status": "Действует"}, "raw": {}}

    monkeypatch.setattr(ck, "fetch_card", fake_fetch)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    mine = []
    for _ in range(3):
        for _attempt in range(5):
            res = client.post("/api/v1/companies", json={"name": f"ООО Очередь {uuid4().hex[:8]}", "inn": inn10()},
                              headers=hdr)
            if res.status_code != 409:
                break
        assert res.status_code in (200, 201), res.text
        mine.append(res.json())
    ids = [c["id"] for c in mine]
    with SessionLocal() as db:            # в общей базе воркера есть чужие компании — на время теста выключаем
        others = (db.query(Company).filter(Company.is_active.is_(True), Company.inn.isnot(None), Company.inn != "",
                                           Company.id.notin_(ids)).all())
        other_ids = [c.id for c in others]
        for c in others:
            c.is_active = False
        db.commit()
    try:
        first = client.post("/api/v1/companies/bulk-refresh", json={"limit": 2}, headers=hdr).json()
        assert first["updated"] == 2 and first["total"] == 3 and first["remaining"] == 1
        assert "ещё ждут обновления: 1" in first["message"]
        second = client.post("/api/v1/companies/bulk-refresh", json={"limit": 2}, headers=hdr).json()
        assert second["updated"] == 2 and second["remaining"] == 0 and "ещё ждут" not in second["message"]
        inns = {c["inn"] for c in mine}
        assert set(seen[:2]) < inns and seen[2] == (inns - set(seen[:2])).pop()   # вторым запуском — третья компания
        with SessionLocal() as db:
            assert all(db.get(Company, i).card_updated_at for i in ids)        # обновлены все, никто не забыт
    finally:
        with SessionLocal() as db:
            for c in db.query(Company).filter(Company.id.in_(other_ids)):
                c.is_active = True
            db.commit()
