-- Target: each namespace database, never main. Back up and pause writes first.
-- Requires rider_checks_namespace.sql on existing databases.
-- Optional backfill: see webapp/README.md for the session-local staging table.
BEGIN;

ALTER TABLE rider ADD COLUMN IF NOT EXISTS pcs_birth_date DATE;

DO $$
BEGIN
    IF to_regclass('pg_temp.pcs_birth_date_backfill') IS NOT NULL THEN
        UPDATE rider AS r
        SET pcs_birth_date = p.birth_date,
            pcs_identity_status = CASE
                WHEN r.birth_date::text = to_char(p.birth_date, 'YYYYMMDD') THEN 'verified'
                ELSE 'review'
            END
        FROM pg_temp.pcs_birth_date_backfill AS p
        WHERE r.pcs_birth_date IS NULL
          AND r.is_deleted IS FALSE
          AND r.pcs_identity_status IN ('verified', 'review', 'unknown')
          AND btrim(r.pcs_slug) = p.rider_slug
          AND r.pcs_latest_updated_at = p.last_synced_at;
    END IF;
END;
$$;

-- Allow the identity scheduler to fetch dates that could not be recovered.
UPDATE rider
SET pcs_identity_status = 'unknown'
WHERE pcs_birth_date IS NULL AND pcs_identity_status IN ('verified', 'review');

ALTER TABLE rider DROP COLUMN IF EXISTS pcs_identity_dob_match;

COMMIT;
