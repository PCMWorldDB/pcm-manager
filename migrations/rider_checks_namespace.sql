-- Target: each namespace database, never main. Back up and pause writes first.
-- Requires migrations/rider_pcs_slug_namespace.sql on existing databases.
-- Connect explicitly to the namespace and run before deploying the rider checks.
BEGIN;
ALTER TABLE rider
    ADD COLUMN pcs_identity_verified_override BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN pcs_identity_dob_match BOOLEAN,
    ADD COLUMN pcs_latest_updated_at TIMESTAMP WITHOUT TIME ZONE;
COMMIT;
