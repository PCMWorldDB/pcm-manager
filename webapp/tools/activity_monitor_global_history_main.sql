-- Manual schema addition for the shared Activity Monitor history archive.
-- Target: PCM Manager's main database. This script does not move or delete
-- records in any namespace database. Review and back up main before applying.

CREATE TABLE IF NOT EXISTS activity_monitor_global_history (
    id BIGSERIAL PRIMARY KEY,
    namespace_slug VARCHAR(100) NOT NULL,
    worlddb_rider_id BIGINT,
    record_type VARCHAR(80) NOT NULL,
    source_record_key VARCHAR(240) NOT NULL,
    occurred_at TIMESTAMP WITHOUT TIME ZONE,
    record_data JSONB NOT NULL,
    archived_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_activity_monitor_global_history_record
        UNIQUE (namespace_slug, record_type, source_record_key)
);

CREATE INDEX IF NOT EXISTS idx_activity_monitor_global_history_rider_date
    ON activity_monitor_global_history (worlddb_rider_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_activity_monitor_global_history_namespace
    ON activity_monitor_global_history (namespace_slug, record_type);
