-- Target: each existing namespace database with stat_list_items.
-- Back up the namespace, pause application writers, and confirm the target with
-- SELECT current_database(). Apply rename_rider_list_namespace.sql first.
-- Run the following command before deploying the matching code.
-- psql -v ON_ERROR_STOP=1 -d <namespace-database> -f migrations/rename_rider_list_item_namespace.sql
BEGIN;
ALTER TABLE stat_list_items RENAME TO rider_list_item;
ALTER TABLE rider_list_item RENAME COLUMN stat_list_id TO rider_list_id;
ALTER TABLE rider_list_item
    RENAME CONSTRAINT uq_stat_list_items_list_rider TO uq_rider_list_item_list_rider;
ALTER INDEX ix_stat_list_items_stat_list_id RENAME TO ix_rider_list_item_rider_list_id;
ALTER INDEX ix_stat_list_items_rider_id RENAME TO ix_rider_list_item_rider_id;
COMMIT;
