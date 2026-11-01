"""Миграция копии не меняет оригинал и не скрывает конфликты."""
import sqlite3

import pytest

from qa.migrate_receipt_copy import migrate_copy


def _source(path):
    with sqlite3.connect(path) as db:
        for table in ('receipts', 'pool_receipts'):
            db.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, fn TEXT, fd TEXT, fp TEXT, '
                       'receipt_date TEXT, total_sum REAL, operation INTEGER, company_id TEXT, assigned_company_id TEXT, assigned_at TEXT)')
        db.execute('CREATE TABLE receipt_items (id TEXT PRIMARY KEY, receipt_id TEXT, name TEXT)')
        db.execute('CREATE TABLE pool_items (id TEXT PRIMARY KEY, receipt_id TEXT, name TEXT)')
        db.execute("INSERT INTO receipt_items VALUES ('ri1', 'r1', 'private item')")
        db.execute("INSERT INTO receipt_items VALUES ('ri2', 'r2', 'unmatched item')")
        db.execute("INSERT INTO pool_items VALUES ('pi1', 'p1', 'donor item')")
        db.execute("INSERT INTO receipts VALUES ('r1','123','4','5','2026-01-01 12:00:00',100,1,NULL,NULL,NULL)")
        db.execute("INSERT INTO pool_receipts VALUES ('p1','123','4','5','2026-01-01 12:00:00.000000',100,1,NULL,NULL,NULL)")
        db.execute("INSERT INTO receipts VALUES ('r2','','7','8',NULL,NULL,NULL,NULL,NULL,NULL)")
        db.execute("INSERT INTO pool_receipts VALUES ('p2','222','3','4',NULL,50,2,NULL,NULL,NULL)")
        for field in ('merchant_name', 'merchant_inn', 'merchant_address'):
            db.execute(f'ALTER TABLE pool_receipts ADD COLUMN {field} TEXT')


def test_copy_migration_merges_only_complete_identities(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    summary = migrate_copy(source, output)
    assert summary == {'documents': 2, 'company_claims': 0, 'company_accounts': 0, 'unresolved_ownership': 0, 'registry_links': 1, 'pool_links': 2, 'needs_review': 1,
                       'item_sets_single_source': 1, 'item_sets_needing_review': 1, 'unified_item_refs': 0,
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


@pytest.mark.parametrize('corruption', ['deleted_origin', 'changed_fiscal_identity', 'missing_link', 'missing_document', 'missing_item', 'wrong_item_origin', 'altered_item', 'altered_item_hash', 'altered_seller', 'altered_seller_hash', 'altered_amount', 'removed_review_flag'])
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
        elif corruption == 'altered_item':
            db.execute("UPDATE pool_items SET name='modified' WHERE id='pi1'")
        elif corruption == 'altered_item_hash':
            db.execute("UPDATE unified_receipt_item_origins SET content_hash='wrong' WHERE item_id='pi1'")
        elif corruption == 'altered_seller':
            db.execute("UPDATE pool_receipts SET merchant_name='modified' WHERE id='p1'")
        elif corruption == 'altered_seller_hash':
            db.execute("UPDATE unified_receipt_origins SET fiscal_source_hash=NULL WHERE origin_id='p1'")
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


@pytest.mark.parametrize('registry_owner,pool_owner,expected,conflicts', [
    ('company-A', 'company-A', 'company-A', 0),
    ('company-A', None, 'company-A', 0),
    ('company-A', 'company-B', None, 1),
    (None, 'company-B', None, 1),
])
def test_copy_migration_preserves_ownership_without_inventing_it(
        tmp_path, registry_owner, pool_owner, expected, conflicts):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE receipts SET company_id=? WHERE id='r1'", (registry_owner,))
        db.execute("UPDATE pool_receipts SET assigned_company_id=? WHERE id='p1'", (pool_owner,))
    summary = migrate_copy(source, output)
    assert summary['company_claims'] == int(expected is not None)
    assert summary['unresolved_ownership'] == conflicts
    with sqlite3.connect(output) as db:
        owner = db.execute("SELECT c.company_id FROM unified_company_claims c "
                           "JOIN unified_fiscal_documents d ON d.id=c.document_id WHERE d.fn='123'").fetchone()
        assert (owner[0] if owner else None) == expected
        # Источники, в том числе конфликтные, не меняются и не исчезают.
        assert db.execute("SELECT company_id FROM receipts WHERE id='r1'").fetchone() == (registry_owner,)
        assert db.execute("SELECT assigned_company_id FROM pool_receipts WHERE id='p1'").fetchone() == (pool_owner,)


def test_schema_parser_handles_semicolon_in_comments(tmp_path, monkeypatch):
    import qa.migrate_receipt_copy as migration

    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    monkeypatch.setattr(migration, '_SCHEMA', '-- комментарий; не конец SQL\n' + migration._SCHEMA)
    assert migration.migrate_copy(source, output)['documents'] == 2


def test_missing_seller_columns_fail_closed_without_modifying_source(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    with sqlite3.connect(source) as db:
        db.execute('ALTER TABLE pool_receipts DROP COLUMN merchant_address')
    with pytest.raises(sqlite3.OperationalError):
        migrate_copy(source, output)
    assert not output.exists()
    with sqlite3.connect(source) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='unified_fiscal_documents'").fetchall()
        assert db.execute('SELECT count(*) FROM pool_receipts').fetchone() == (2,)


def test_single_source_uses_original_positions_and_multiple_sources_wait_for_review(tmp_path):
    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE pool_receipts SET receipt_date='2026-01-01 12:00:00' WHERE id='p2'")
        db.execute("INSERT INTO pool_items VALUES ('pi2', 'p2', 'single source item')")
    migrate_copy(source, output)
    with sqlite3.connect(output) as db:
        merged = db.execute("SELECT id FROM unified_fiscal_documents WHERE fn='123'").fetchone()[0]
        single = db.execute("SELECT id FROM unified_fiscal_documents WHERE fn='222'").fetchone()[0]
        assert db.execute('SELECT state,origin_kind,origin_id FROM unified_fiscal_item_sets '
                          'WHERE document_id=?', (merged,)).fetchone() == ('needs_review', None, None)
        assert db.execute('SELECT count(*) FROM unified_fiscal_item_refs WHERE document_id=?',
                          (merged,)).fetchone() == (0,)
        assert db.execute('SELECT state,origin_kind,origin_id FROM unified_fiscal_item_sets '
                          'WHERE document_id=?', (single,)).fetchone() == ('single_source', 'pool', 'p2')
        assert db.execute('SELECT item_id FROM unified_fiscal_item_refs WHERE document_id=?',
                          (single,)).fetchall() == [('pi2',)]
        # Only references are recorded; original data for both sources remains intact.
        assert db.execute("SELECT name FROM pool_items WHERE id='pi2'").fetchone() == ('single source item',)
        assert db.execute("SELECT name FROM receipt_items WHERE id='ri1'").fetchone() == ('private item',)
        db.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO unified_fiscal_item_refs VALUES (?, 'pool', 'p1', 'pi1')", (single,))


@pytest.mark.parametrize('corruption', ['missing_set', 'missing_ref', 'unexpected_review_ref'])
def test_unified_item_set_validation_rejects_corruption(tmp_path, corruption):
    from qa.migrate_receipt_copy import _validate

    source, output = tmp_path / 'source.db', tmp_path / 'copy.db'
    _source(source)
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO pool_items VALUES ('pi2', 'p2', 'single')")
    migrate_copy(source, output)
    with sqlite3.connect(output) as db:
        single = db.execute("SELECT id FROM unified_fiscal_documents WHERE fn='222'").fetchone()[0]
        merged = db.execute("SELECT id FROM unified_fiscal_documents WHERE fn='123'").fetchone()[0]
        if corruption == 'missing_set':
            db.execute('DELETE FROM unified_fiscal_item_sets WHERE document_id=?', (single,))
        elif corruption == 'missing_ref':
            db.execute('DELETE FROM unified_fiscal_item_refs WHERE document_id=?', (single,))
        else:
            # Connection intentionally has foreign_keys disabled to simulate external corruption.
            db.execute("INSERT INTO unified_fiscal_item_refs VALUES (?, 'pool', 'p1', 'pi1')", (merged,))
        with pytest.raises(RuntimeError):
            _validate(db)
