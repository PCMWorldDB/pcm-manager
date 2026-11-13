-- Target: each namespace database, never main. Back up and pause writes first.
-- If rider workflow columns are absent, run webapp/tools/rider_state_namespace.sql first.
-- Connect explicitly to the namespace database and run once before deploying:
-- \i migrations/rider_pcs_slug_namespace.sql
BEGIN;
ALTER TABLE rider ADD COLUMN pcs_slug VARCHAR(500);
ALTER TABLE rider RENAME COLUMN identity_pcs_status TO pcs_identity_status;
ALTER TABLE rider RENAME CONSTRAINT ck_rider_identity_pcs_status TO ck_rider_pcs_identity_status;
COMMIT;
