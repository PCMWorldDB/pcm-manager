-- MANUAL ONLY: run against one selected NAMESPACE database, never main.
-- Back up the database and stop web/worker writes before running this transaction.
-- Only the exact legacy single-rider profile lists are converted.
BEGIN;
LOCK TABLE stat_lists, stat_list_items, stat_changes, stat_change_snapshot IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE profile_scopes ON COMMIT DROP AS
SELECT sl.id AS list_id, MIN(i.cdb_cyclist_id) AS rider_id
FROM stat_lists sl JOIN stat_list_items i ON i.stat_list_id = sl.id
GROUP BY sl.id, sl.list_name
HAVING COUNT(*) = 1 AND sl.list_name = 'Rider profile #' || MIN(i.cdb_cyclist_id)::text;

DO $$
BEGIN
    IF EXISTS (SELECT rider_id FROM profile_scopes GROUP BY rider_id HAVING COUNT(*) > 1)
       OR EXISTS (
           SELECT 1 FROM profile_scopes p JOIN stat_changes c ON c.stat_list_id = p.list_id
           WHERE c.race_result_id IS NOT NULL OR c.cdb_cyclist_id <> p.rider_id
       ) OR EXISTS (
           SELECT 1 FROM profile_scopes p JOIN stat_change_snapshot s ON s.stat_list_id = p.list_id
           WHERE s.race_result_id IS NOT NULL OR s.cdb_cyclist_id <> p.rider_id
       ) OR EXISTS (
           SELECT 1 FROM profile_scopes p JOIN stat_changes c ON c.cdb_cyclist_id = p.rider_id
           WHERE c.race_result_id IS NULL AND c.stat_list_id IS NULL
       ) OR EXISTS (
           SELECT 1 FROM profile_scopes p JOIN stat_change_snapshot s ON s.cdb_cyclist_id = p.rider_id
           WHERE s.race_result_id IS NULL AND s.stat_list_id IS NULL
       ) THEN
        RAISE EXCEPTION 'Ambiguous profile scope or existing individual edits: review manually; no changes applied';
    END IF;
END $$;

UPDATE stat_changes c SET stat_list_id = NULL FROM profile_scopes p WHERE c.stat_list_id = p.list_id;
UPDATE stat_change_snapshot s SET stat_list_id = NULL FROM profile_scopes p WHERE s.stat_list_id = p.list_id;
DELETE FROM stat_list_items i USING profile_scopes p WHERE i.stat_list_id = p.list_id;
DELETE FROM stat_lists sl USING profile_scopes p WHERE sl.id = p.list_id;
COMMIT;
