-- MANUAL migration: MAIN database only, after external_call_queue_main.sql.
-- Back up main, confirm its identity, and stop web and worker/Beat first.
-- Operator: psql -v ON_ERROR_STOP=1 -d <main-database> -f webapp/tools/external_call_group_priority_main.sql
-- Existing groups default to manual/high; review pending groups before restarting.
BEGIN;
ALTER TABLE external_call_task_group
    ADD COLUMN priority varchar(4) NOT NULL DEFAULT 'high',
    ADD COLUMN origin varchar(9) NOT NULL DEFAULT 'manual',
    ADD COLUMN is_worker_queued boolean NOT NULL DEFAULT false,
    ADD COLUMN worker_message_id varchar(36),
    ADD COLUMN worker_queued_at timestamp,
    ADD CONSTRAINT ck_external_group_priority CHECK (priority IN ('high', 'low')),
    ADD CONSTRAINT ck_external_group_origin CHECK (origin IN ('manual', 'scheduled'));
CREATE INDEX ix_external_group_dispatch
    ON external_call_task_group(status, is_worker_queued, priority, timestamp, id);
COMMIT;
-- Restart web and the single combined worker/Beat after successful review/application.
