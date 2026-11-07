-- Target: each namespace database, never main. Review and back up first.
-- Requires rider, stat_changes and cdb_changes (apply history schema first).
-- Pause application/worker writes, connect explicitly to the namespace, then:
-- \i webapp/tools/rider_state_namespace.sql
-- Run once per namespace before deploying code that uses these columns.
BEGIN;
ALTER TABLE rider
    ADD COLUMN identity_pcs_status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    ADD COLUMN has_pending_stat_changes BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN has_pending_attribute_changes BOOLEAN NOT NULL DEFAULT FALSE,
    ADD CONSTRAINT ck_rider_identity_pcs_status
        CHECK (identity_pcs_status IN ('unknown', 'verified', 'review', 'failed'));
UPDATE rider r SET
    has_pending_stat_changes = EXISTS (
        SELECT 1 FROM stat_changes s WHERE s.rider_id=r.id AND s.is_approved=FALSE
    ) OR EXISTS (
        SELECT 1 FROM cdb_changes c WHERE c.entity_type='rider' AND c.entity_id=r.id
        AND c.status='pending' AND c.applied_at IS NULL AND LEFT(c.attribute_name,5)='stat_'
    ),
    has_pending_attribute_changes = EXISTS (
        SELECT 1 FROM cdb_changes c WHERE c.entity_type='rider' AND c.entity_id=r.id
        AND c.status='pending' AND c.applied_at IS NULL AND LEFT(c.attribute_name,5)<>'stat_'
    );
COMMIT;
-- Resume writes after successful deployment. No CDB SQLite fields are added.
