-- MANUAL migration: MAIN database only, after external_call_queue_main.sql.
-- Back up main, confirm database identity and stop web, worker and beat first.
-- Run with psql -v ON_ERROR_STOP=1. This script intentionally fails if repeated.
BEGIN;
ALTER TABLE external_call_task ADD COLUMN related_to_id integer;
ALTER TABLE external_call_task ADD COLUMN related_to_table varchar(100);
CREATE INDEX ix_external_call_task_related_record
    ON external_call_task(task_group_id, related_to_table, related_to_id);
COMMIT;
-- Existing tasks have NULL references. No namespace database changes are needed.
-- To undo, stop services and export references before manually dropping the
-- index and both columns; doing so removes all saved record associations.
