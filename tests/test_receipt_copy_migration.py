"""Миграция копии не меняет оригинал и не скрывает конфликты."""
import sqlite3

import pytest

from qa.migrate_receipt_copy import migrate_copy


def _source(path):
    with sqlite3.connect(path) as db:
        for table in ('receipts', 'pool_receipts'):
            db.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, fn TEXT, fd TEXT, fp TEXT, '
                       'receipt_date TEXT, total_sum REAL, operation INTEGER, company_id TEXT, assigned_company_id TEXT)')
        db.execute('CREATE TABLE receipt_items (id TEXT PRIMARY KEY, receipt_id TEXT, name TEXT)')
        db.execute('CREATE TABLE pool_items (id TEXT PRIMARY KEY, receipt_id TEXT, name TEXT)')
        db.execute("INSERT INTO receipt_items VALUES ('ri1', 'r1', 'private item')")
        db.execute("INSERT INTO receipt_items VALUES ('ri2', 'r2', 'unmatched item')")
        db.execute("INSERT INTO pool_items VALUES ('pi1', 'p1', 'donor item')")
        db.execute("INSERT INTO receipts VALUES ('r1','123','4','5','2026-01-01 12:00:00',100,1,NULL,NULL)")
        db.execute("INSERT INTO pool_receipts VALUES ('p1','123','4','5','2026-01-01 12:00:00.000000',100,1,NULL,NULL)")
        db.execute("INSERT INTO receipts VALUES ('r2','','7','8',NULL,NULL,NULL,NULL,NULL)")
        db.execute("INSERT INTO pool_receipts VALUES ('p2','222','3','4',NULL,50,2,NULL,NULL)")


def test_copy_migration_merges_only_complete_identities(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    summary = migrate_copy(source, output)
    assert summary == {'documents': 2, 'registry_links': 1, 'pool_links': 2, 'needs_review': 1,
                       'registry_without_identity': 1, 'pool_without_identity': 0,
                       'registry_items_linked': 1, 'pool_items_linked': 1,
                       'registry_items_unlinked': 1, 'pool_items_unlinked': 0}
    with sqlite3.connect(output) as db:
        row = db.execute("SELECT id, receipt_date, total_sum, operation, review_mask "
                         "FROM unified_fiscal_documents WHERE fn='123'").fetchone()
        assert row[1:] == ('2026-01-01 12:00:00', 100.0, 1, 0)
        assert db.execute('SELECT count(*) FROM unified_receipt_origins WHERE document_id=?', (row[0],)).fetchone() == (2,)
        assert db.execute("SELECT review_mask FROM unified_fiscal_documents WHERE fn='222'").fetchone() == (1,)
        assert set(db.execute('SELECT origin_kind, item_id FROM unified_receipt_item_origins').fetchall()) == {
            ('registry', 'ri1'), ('pool', 'pi1')}
        assert db.execute("SELECT name FROM receipt_items WHERE id='ri2'").fetchone() == ('unmatched item',)
    with sqlite3.connect(source) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='unified_fiscal_documents'").fetchall()
        assert db.execute('SELECT count(*) FROM receipts').fetchone() == (2,)


def test_failed_migration_does_not_leave_copy_and_never_overwrites(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE unrelated (id INTEGER)')
    with pytest.raises(sqlite3.OperationalError):
        migrate_copy(source, output)
    assert not output.exists()
    output.write_text('do not overwrite')
    with pytest.raises(FileExistsError):
        migrate_copy(source, output)
    assert output.read_text() == 'do not overwrite'


@pytest.mark.parametrize('corruption', ['deleted_origin', 'changed_fiscal_identity', 'missing_link', 'missing_document', 'missing_item', 'wrong_item_origin', 'altered_amount', 'removed_review_flag'])
def test_verification_rejects_corrupted_migration(tmp_path, corruption):
    from qa.migrate_receipt_copy import _validate

    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    migrate_copy(source, output)
    with sqlite3.connect(output) as db:
        if corruption == 'deleted_origin':
            db.execute("DELETE FROM pool_receipts WHERE id='p1'")
        elif corruption == 'changed_fiscal_identity':
            db.execute("UPDATE receipts SET fn='changed' WHERE id='r1'")
        elif corruption == 'missing_link':
            db.execute("DELETE FROM unified_receipt_origins WHERE origin_kind='pool' AND origin_id='p1'")
        elif corruption == 'missing_document':
            # Соединение намеренно без FOREIGN KEY: имитируем повреждённую запись.
            db.execute("UPDATE unified_receipt_origins SET document_id='missing' WHERE origin_id='p1'")
        elif corruption == 'missing_item':
            db.execute("DELETE FROM unified_receipt_item_origins WHERE item_id='pi1'")
        elif corruption == 'wrong_item_origin':
            db.execute("UPDATE pool_items SET receipt_id='p2' WHERE id='pi1'")
        elif corruption == 'altered_amount':
            db.execute("UPDATE unified_fiscal_documents SET total_sum=123 WHERE fn='123'")
        else:
            db.execute("UPDATE unified_fiscal_documents SET review_mask=0 WHERE fn='222'")
        with pytest.raises(RuntimeError):
            _validate(db)


def test_small_amount_disagreement_is_not_silently_rounded(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE pool_receipts SET total_sum=100.001 WHERE id='p1'")
    migrate_copy(source, output)
    with sqlite3.connect(output) as db:
        amount, flags = db.execute(
            "SELECT total_sum, review_mask FROM unified_fiscal_documents WHERE fn='123'").fetchone()
        assert amount is None and flags & 2
