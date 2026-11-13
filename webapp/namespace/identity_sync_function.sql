CREATE OR REPLACE FUNCTION queue_external_identity_pair() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.first_cycling_id IS NOT DISTINCT FROM OLD.first_cycling_id
       AND NEW.pcs_slug IS NOT DISTINCT FROM OLD.pcs_slug
       AND NEW.is_deleted IS NOT DISTINCT FROM OLD.is_deleted
       AND (to_jsonb(NEW)->>'pcs_slug_is_assumed') IS NOT DISTINCT FROM (to_jsonb(OLD)->>'pcs_slug_is_assumed') THEN
        RETURN NEW;
    END IF;
    -- Resolve the outbox in the changed table's schema, never search_path/main.
    IF to_regclass(format('%I.identity_sync_outbox', TG_TABLE_SCHEMA)) IS NULL
       OR NEW.is_deleted OR NEW.first_cycling_id IS NULL OR NEW.first_cycling_id <= 0
       OR NEW.pcs_slug IS NULL OR NEW.pcs_slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$'
       OR COALESCE((to_jsonb(NEW)->>'pcs_slug_is_assumed')::boolean, false) THEN
        RETURN NEW;
    END IF;
    EXECUTE format(
        'INSERT INTO %I.identity_sync_outbox (entity_type, firstcycling_id, pcs_slug)
         VALUES ($1, $2, $3) ON CONFLICT (entity_type, firstcycling_id, pcs_slug)
         DO UPDATE SET status = ''pending'', error = NULL, published_at = NULL',
        TG_TABLE_SCHEMA)
    USING TG_TABLE_NAME, NEW.first_cycling_id, NEW.pcs_slug;
    RETURN NEW;
END;
$$;
