"""Изолированная проверка T-88: старая версия → договор → откат → повторное обновление.

Только синтетические данные во временной SQLite-базе. Требуется локальный git ref старой версии.
Пример: .venv/bin/python -m qa.contract_rollback --from-ref 67513d5
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_CREATE = '''
from app.database import init_db, SessionLocal
from app.models import Company
init_db()
with SessionLocal() as db:
    db.add(Company(id="contract-rollback", name="Синтетическая компания"))
    db.commit()
'''
NEW_SAVE = '''
from app.database import init_db, SessionLocal
from app.models import Company
from app.clients.contract import ContractIn, save
init_db()
with SessionLocal() as db:
    company = db.get(Company, "contract-rollback")
    assert company.name == "Синтетическая компания" and company.contract_no is None
    save(company, ContractIn(number="Тестовый договор", date="2026-01-01", service_since="2026-02-01",
         service_until="2026-12-31", fee="123456789012.34", frequency="month", currency="RUB", manager=""))
    db.commit()
'''
OLD_EDIT = '''
from app.database import init_db, SessionLocal
from app.models import Company
init_db()
with SessionLocal() as db:
    company = db.get(Company, "contract-rollback")
    assert company.name == "Синтетическая компания"
    company.name = "Изменено старой версией"
    db.commit()
'''
NEW_VERIFY = '''
from app.database import init_db, SessionLocal
from app.models import Company
from app.clients.contract import as_dict
init_db()
with SessionLocal() as db:
    company = db.get(Company, "contract-rollback")
    assert company.name == "Изменено старой версией"
    assert as_dict(company) == {"number": "Тестовый договор", "date": "2026-01-01",
        "service_since": "2026-02-01", "service_until": "2026-12-31", "fee": "123456789012.34",
        "frequency": "month", "currency": "RUB", "manager": ""}
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-ref', required=True, help='git ref старой версии')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ym-contract-rollback-') as directory:
        base = Path(directory)
        old = base / 'old'
        old.mkdir()
        archive = base / 'old.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', 'archive', '--format=tar', args.from_ref], cwd=ROOT, stdout=output, check=True)
        # Архив создан локальным git из собственного репозитория.
        subprocess.run(['tar', '-xf', str(archive), '-C', str(old)], check=True)
        env = dict(os.environ, DATABASE_URL=f'sqlite:///{base / "test.db"}', YM_FETCHQ_OFF='1', YM_DOCREQ_OFF='1')
        for label, cwd, code in (('старая база', old, OLD_CREATE), ('договор', ROOT, NEW_SAVE),
                                  ('откат и правка компании', old, OLD_EDIT), ('повторное обновление', ROOT, NEW_VERIFY)):
            subprocess.run([sys.executable, '-c', code], cwd=cwd, env=env, check=True)
            print(f'OK: {label}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
