-- Manual schema update for one Activity Monitor Namespace database.
-- Back up the target Namespace database before running this script.
-- Do not run this against the shared main database.
BEGIN;

ALTER TABLE activity_monitor_results
    ALTER COLUMN first_cycling_id DROP NOT NULL;

COMMIT;
