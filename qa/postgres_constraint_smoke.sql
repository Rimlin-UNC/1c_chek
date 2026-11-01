-- PostgreSQL-only smoke test; creates temporary tables only and rolls back.
-- This is not the application schema, a migration, or a concurrency test.
BEGIN;
CREATE TEMP TABLE qa_identity_lock (key varchar(64) PRIMARY KEY) ON COMMIT DROP;
CREATE TEMP TABLE qa_fiscal_document (
    id text PRIMARY KEY, fn text NOT NULL, fd text NOT NULL, fp text NOT NULL,
    UNIQUE(fn, fd, fp)
) ON COMMIT DROP;
CREATE TEMP TABLE qa_claim (
    document_id text PRIMARY KEY REFERENCES qa_fiscal_document(id),
    company_id text NOT NULL CHECK(length(trim(company_id)) > 0),
    UNIQUE(document_id, company_id)
) ON COMMIT DROP;
DO $$
BEGIN
    INSERT INTO qa_identity_lock(key) VALUES (repeat('a',64))
        ON CONFLICT(key) DO UPDATE SET key=excluded.key;
    INSERT INTO qa_identity_lock(key) VALUES (repeat('a',64))
        ON CONFLICT(key) DO UPDATE SET key=excluded.key;
    IF (SELECT count(*) FROM qa_identity_lock) <> 1 THEN
        RAISE EXCEPTION 'identity lock is not idempotent';
    END IF;
    INSERT INTO qa_fiscal_document VALUES ('doc','fn','fd','fp');
    INSERT INTO qa_claim VALUES ('doc','company1');
    BEGIN
        INSERT INTO qa_claim VALUES ('doc','company2');
        RAISE EXCEPTION 'second company unexpectedly accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO qa_fiscal_document VALUES ('doc2','fn','fd','fp');
        RAISE EXCEPTION 'duplicate fiscal identity unexpectedly accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO qa_claim VALUES ('missing','company1');
        RAISE EXCEPTION 'orphan claim unexpectedly accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
END $$;
ROLLBACK;
SELECT 'temporary constraint smoke test passed; transaction rolled back' AS result;
