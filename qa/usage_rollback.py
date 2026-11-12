"""Синтетический откат: старая БД → событие → откат → повторное обновление.

Только временная SQLite; проверка не касается рабочего сервера и его данных.
Пример: .venv/bin/python -m qa.usage_rollback --from-ref 8c78787
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
from app.models import Company
init_db()
with SessionLocal() as db:
    company = db.get(Company, "synthetic-usage-company")
    if not company:
        db.add(Company(id="synthetic-usage-company", name="Синтетическая компания"))
    else:
        company.name = "Изменена старой версией"
    db.commit()
'''
NEW_SAVE = '''
from app.database import init_db, SessionLocal
from app.usage.ledger import record
init_db()
with SessionLocal() as db:
    assert record(db, "synthetic-usage-company", "synthetic", "period", "event-key")
    db.commit()
'''
NEW_VERIFY = '''
from app.database import init_db, SessionLocal
from app.usage.ledger import record, summary
from app.models import Company
init_db()
with SessionLocal() as db:
    assert db.get(Company, "synthetic-usage-company").name == "Изменена старой версией"
    assert summary(db, "synthetic-usage-company", "synthetic", "period", limit=3)["remaining"] == 2
    assert not record(db, "synthetic-usage-company", "synthetic", "period", "event-key")
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-ref', required=True, help='git ref старой версии')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ym-usage-rollback-') as directory:
        base = Path(directory)
        old = base / 'old'
        old.mkdir()
        archive = base / 'old.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', 'archive', '--format=tar', args.from_ref], cwd=ROOT, stdout=output, check=True)
        subprocess.run(['tar', '-xf', str(archive), '-C', str(old)], check=True)
        env = dict(os.environ, DATABASE_URL=f'sqlite:///{base / "test.db"}', YM_FETCHQ_OFF='1', YM_DOCREQ_OFF='1')
        for label, cwd, code in (('старая база', old, OLD), ('запись события', ROOT, NEW_SAVE),
                                 ('откат и изменение компании', old, OLD), ('повторное обновление', ROOT, NEW_VERIFY)):
            subprocess.run([sys.executable, '-c', code], cwd=cwd, env=env, check=True)
            print(f'OK: {label}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
