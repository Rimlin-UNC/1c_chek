"""Изолированно: старая БД → запись оферты → откат → повторное обновление.

Только синтетическая временная SQLite; ни файлы, ни база рабочего сервера не используются.
Пример: .venv/bin/python -m qa.offer_rollback --from-ref 436dc0d
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = '''
from app.database import init_db, SessionLocal
from app.models import User
init_db()
with SessionLocal() as db:
    u = db.get(User, "synthetic-offer-user")
    if not u:
        db.add(User(id="synthetic-offer-user", username="synthetic-offer-user", password_hash="not-a-password"))
    else:
        u.full_name = "Изменено старой версией"
    db.commit()
'''
NEW_SAVE = '''
from app.database import init_db, SessionLocal
from app.legal.acceptance import OfferAcceptance, _digest
init_db()
with SessionLocal() as db:
    db.add(OfferAcceptance(id="a" * 64, user_id="synthetic-offer-user", edition="тест",
                           text="Синтетический текст оферты", sha256=_digest("Синтетический текст оферты")))
    db.commit()
'''
NEW_VERIFY = '''
from app.database import init_db, SessionLocal
from app.legal.acceptance import OfferAcceptance, _digest
from app.models import User
init_db()
with SessionLocal() as db:
    row = db.get(OfferAcceptance, "a" * 64)
    assert row and row.user_id == "synthetic-offer-user" and row.edition == "тест"
    assert row.text == "Синтетический текст оферты" and row.sha256 == _digest(row.text)
    assert db.get(User, "synthetic-offer-user").full_name == "Изменено старой версией"
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-ref', required=True, help='git ref старой версии')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ym-offer-rollback-') as directory:
        base = Path(directory)
        old = base / 'old'
        old.mkdir()
        archive = base / 'old.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', 'archive', '--format=tar', args.from_ref], cwd=ROOT, stdout=output, check=True)
        subprocess.run(['tar', '-xf', str(archive), '-C', str(old)], check=True)
        env = dict(os.environ, DATABASE_URL=f'sqlite:///{base / "test.db"}', YM_FETCHQ_OFF='1', YM_DOCREQ_OFF='1')
        for label, cwd, code in (('старая база', old, OLD), ('сохранение оферты', ROOT, NEW_SAVE),
                                  ('откат и изменение пользователя', old, OLD), ('повторное обновление', ROOT, NEW_VERIFY)):
            subprocess.run([sys.executable, '-c', code], cwd=cwd, env=env, check=True)
            print(f'OK: {label}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
