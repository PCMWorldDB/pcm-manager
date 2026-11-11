-- Target: each existing namespace database with stat_lists. Never run on main.
-- Back up the namespace, pause application writers, and confirm the target with
-- SELECT current_database(). Apply bulk_editor_list_type_namespace.sql first
-- if needed. Run the following command before deploying the matching code.
-- psql -v ON_ERROR_STOP=1 -d <namespace-database> -f migrations/rename_rider_list_namespace.sql
BEGIN;
ALTER TABLE stat_lists RENAME TO rider_list;
COMMIT;
