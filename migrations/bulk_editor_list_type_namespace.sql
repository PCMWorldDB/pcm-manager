-- Target: each namespace database, never main. Run once per existing namespace.
-- Back up the namespace, confirm the target with SELECT current_database(),
-- then run \i migrations/bulk_editor_list_type_namespace.sql in psql.
-- Attribute Lists use the existing CDB change history tables and rider workflow
-- flags. Install the namespace scripts cdb_change_history_namespace.sql and
-- rider_state_namespace.sql from webapp/tools/ first if those are missing.
BEGIN;
ALTER TABLE stat_lists ADD COLUMN list_type VARCHAR(20) NOT NULL DEFAULT 'stat'
    CONSTRAINT ck_stat_lists_list_type CHECK (list_type IN ('stat', 'attribute'));
COMMIT;
