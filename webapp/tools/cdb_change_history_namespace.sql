-- Target: each namespace database, never main.
-- Operator: back up the namespace, connect explicitly to it with psql, and run
-- \i webapp/tools/cdb_change_history_namespace.sql
-- Requires the existing releases table. Review before running; no data backfill
-- or replacement of stat_changes is performed. Run once per namespace.
BEGIN;
CREATE TABLE cdb_changes (
    id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id INTEGER NOT NULL,
    attribute_name VARCHAR(100) NOT NULL,
    operation VARCHAR(20) NOT NULL,
    before_value TEXT,
    after_value TEXT,
    source VARCHAR(50) NOT NULL,
    created_by_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch_id VARCHAR(64),
    race_result_id INTEGER,
    stat_list_id INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    approved_by_id INTEGER,
    approved_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    context JSONB NOT NULL DEFAULT '{}',
    CONSTRAINT ck_cdb_changes_operation CHECK (operation IN ('insert', 'update', 'delete')),
    CONSTRAINT ck_cdb_changes_status CHECK (status IN ('pending', 'approved', 'rejected')),
    CONSTRAINT ck_cdb_changes_source_scope CHECK (race_result_id IS NULL OR stat_list_id IS NULL)
);
CREATE INDEX idx_cdb_changes_entity_created ON cdb_changes (entity_type, entity_id, created_at);
CREATE INDEX ix_cdb_changes_batch_id ON cdb_changes (batch_id);
CREATE INDEX ix_cdb_changes_race_result_id ON cdb_changes (race_result_id);
CREATE INDEX ix_cdb_changes_stat_list_id ON cdb_changes (stat_list_id);
CREATE TABLE cdb_change_events (
    id BIGSERIAL PRIMARY KEY,
    change_id BIGINT NOT NULL REFERENCES cdb_changes(id) ON DELETE RESTRICT,
    event_type VARCHAR(50) NOT NULL,
    actor_id INTEGER,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    release_id INTEGER REFERENCES releases(id) ON DELETE RESTRICT,
    context JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_cdb_change_events_change_time ON cdb_change_events (change_id, occurred_at);
CREATE INDEX ix_cdb_change_events_release_id ON cdb_change_events (release_id);
COMMIT;
