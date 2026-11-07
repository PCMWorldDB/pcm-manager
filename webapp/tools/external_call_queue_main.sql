-- MANUAL migration: MAIN database only. Stop web, worker and beat first.
-- Back up main and confirm database identity. Run with psql -v ON_ERROR_STOP=1.
-- Historical logs stay historical; old requests are never enqueued.
BEGIN;
ALTER TABLE external_sync_log RENAME TO external_call_task_group;
ALTER TABLE external_call_task_group ADD COLUMN namespace_id integer REFERENCES namespaces(id) ON DELETE SET NULL;
CREATE INDEX ix_external_call_task_group_namespace_id ON external_call_task_group(namespace_id);
ALTER TABLE external_call_task_group ALTER COLUMN provider DROP NOT NULL;
ALTER TABLE external_call_task_group ALTER COLUMN sync_type DROP NOT NULL;
ALTER TABLE external_sync_api_call_log RENAME TO external_call_task;
ALTER TABLE external_call_task RENAME COLUMN external_sync_log_id TO task_group_id;
ALTER TABLE external_call_task ALTER COLUMN request_url DROP NOT NULL;
ALTER TABLE external_call_task ALTER COLUMN outcome DROP NOT NULL;
ALTER TABLE external_call_task ADD COLUMN status varchar(20) NOT NULL DEFAULT 'historical';
ALTER TABLE external_call_task ADD COLUMN available_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE external_call_task ADD COLUMN started_at timestamp;
ALTER TABLE external_call_task ADD COLUMN finished_at timestamp;
ALTER TABLE external_call_task ADD COLUMN result_json text;
CREATE INDEX ix_external_call_task_status ON external_call_task(status);
CREATE INDEX ix_external_call_task_available_at ON external_call_task(available_at);
COMMIT;
-- PostgreSQL automatically preserves existing foreign keys through the renames.
