-- Target: one namespace database containing Activity Monitor tables.
-- Run each statement outside a transaction; CONCURRENTLY avoids blocking writes.
-- These indexes support latest-per-rider schedule, check, result, and legacy lookups.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_monitor_schedule_rider_latest
    ON activity_monitor_rider_schedules
    (rider_id, reference_date DESC, recorded_at DESC, id DESC)
    WHERE rider_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_monitor_check_rider_generation_time
    ON activity_monitor_source_checks
    (rider_id, identity_generation, checked_at DESC, id DESC)
    WHERE rider_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_monitor_result_rider_generation_date
    ON activity_monitor_results
    (rider_id, identity_generation, result_date DESC)
    WHERE identity_verified = TRUE AND rider_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_monitor_result_source_check
    ON activity_monitor_results (source_check_id)
    WHERE source_check_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_monitor_legacy_rider_type_time
    ON activity_monitor_legacy_records
    (rider_id, record_type, occurred_at DESC, id DESC)
    WHERE rider_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_monitor_identity_alert_rider_status
    ON activity_monitor_identity_alerts (rider_id, status)
    WHERE rider_id IS NOT NULL;
