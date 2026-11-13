-- Target: each namespace database, never main.
-- Review and run manually before seeding the namespace field defaults.
BEGIN;

CREATE TABLE IF NOT EXISTS field_level_configuration (
    object varchar(50) NOT NULL,
    name varchar(100) NOT NULL,
    slug varchar(50) NOT NULL,
    is_mapped_to_cdb boolean NOT NULL,
    cdb_source varchar(255),
    cdb_source_import_policy varchar(20),
    cdb_source_import_null_handling varchar(20),
    is_editable boolean NOT NULL,
    is_require_approval boolean NOT NULL,
    PRIMARY KEY (object, name),
    CONSTRAINT ck_field_level_cdb_mapping CHECK (
        (is_mapped_to_cdb AND cdb_source IS NOT NULL
            AND cdb_source_import_policy IS NOT NULL
            AND cdb_source_import_policy IN ('identity', 'overwrite', 'preserve', 'reject')
            AND cdb_source_import_null_handling IS NOT NULL
            AND cdb_source_import_null_handling IN ('allow', 'ignore', 'reject'))
        OR (NOT is_mapped_to_cdb AND cdb_source IS NULL
            AND cdb_source_import_policy IS NULL AND cdb_source_import_null_handling IS NULL)
    )
);

ALTER TABLE stat_changes
    ADD COLUMN IF NOT EXISTS is_applied_immediately boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS field_configuration_audit (
    id bigserial PRIMARY KEY,
    actor_id integer,
    username varchar(100) NOT NULL,
    details jsonb NOT NULL,
    ip_address varchar(45),
    created_at timestamp NOT NULL DEFAULT now(),
    forwarded boolean NOT NULL DEFAULT false
);

COMMIT;
