"""Archive namespace Activity Monitor history in the shared main database.

The namespace remains the source of truth for its current roster and workflow.
This append-oriented archive is a cross-namespace history index keyed by the
WorldDB rider ID, never by a namespace-local primary key.
"""

from __future__ import annotations

import json
import hashlib
from datetime import date, datetime
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy import Date as SQLDate, DateTime as SQLDateTime, inspect, text


ARCHIVE_TABLE = "activity_monitor_global_history"

RECOVERABLE_TABLES = (
    "activity_monitor_source_checks",
    "activity_monitor_results",
    "activity_monitor_rider_schedules",
    "activity_monitor_profile_links",
    "activity_monitor_reviews",
    "activity_monitor_review_events",
    "activity_monitor_identity_alerts",
    "activity_monitor_eligibility_events",
)

# The live roster and singleton scheduler settings are current state, not
# history. All other Activity Monitor tables are retained as archived records.
EXCLUDED_TABLES = {"activity_monitor_riders", "activity_monitor_schedules"}
TIME_COLUMNS = (
    "occurred_at", "checked_at", "result_date", "recorded_at", "created_at",
    "imported_at", "started_at", "finished_at", "updated_at", "last_seen_cdb_at",
)


def install_global_history_schema(main_engine):
    """Create the explicitly installed shared archive table in the main DB."""
    with main_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS activity_monitor_global_history (
                id BIGSERIAL PRIMARY KEY,
                namespace_slug VARCHAR(100) NOT NULL,
                worlddb_rider_id BIGINT,
                record_type VARCHAR(80) NOT NULL,
                source_record_key VARCHAR(240) NOT NULL,
                occurred_at TIMESTAMP WITHOUT TIME ZONE,
                record_data JSONB NOT NULL,
                archived_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_activity_monitor_global_history_record
                    UNIQUE (namespace_slug, record_type, source_record_key)
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_activity_monitor_global_history_rider_date
            ON activity_monitor_global_history (worlddb_rider_id, occurred_at DESC)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_activity_monitor_global_history_namespace
            ON activity_monitor_global_history (namespace_slug, record_type)
        """))


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def archive_namespace_history(
    source_engine, main_engine, namespace_slug, *, apply=False, worlddb_rider_id=None
):
    """Preview or idempotently archive durable records from one namespace.

    Local integer IDs are included only inside the opaque payload. The archive
    key always includes namespace and source table, and cross-namespace rider
    association uses ``source_worlddb_rider_id``.
    """
    inspector = inspect(source_engine)
    available = set(inspector.get_table_names())
    tables = sorted(
        table for table in available
        if table.startswith("activity_monitor_") and table not in EXCLUDED_TABLES
    )
    if not tables:
        return {"namespace": namespace_slug, "tables": {}, "records": 0, "written": 0}

    archive_exists = inspect(main_engine).has_table(ARCHIVE_TABLE)
    if apply and not archive_exists:
        raise RuntimeError(
            "The shared history archive is not installed in the main database. "
            "Run the explicit install command first."
        )

    rider_worlddb_ids = {}
    task_worlddb_ids = {}
    matching_rider_ids = []
    if "activity_monitor_riders" in available:
        with source_engine.connect() as connection:
            rider_worlddb_ids = dict(connection.execute(text("""
                SELECT id, source_worlddb_rider_id
                FROM activity_monitor_riders
                WHERE source_worlddb_rider_id IS NOT NULL
            """)).all())
            if worlddb_rider_id is not None:
                matching_rider_ids = list(connection.execute(text("""
                    SELECT id FROM activity_monitor_riders
                    WHERE source_worlddb_rider_id = :worlddb_rider_id
                """), {"worlddb_rider_id": int(worlddb_rider_id)}).scalars())
    if "activity_monitor_tasks" in available:
        with source_engine.connect() as connection:
            task_rows = connection.execute(text("""
                SELECT id, source_worlddb_rider_id, rider_id
                FROM activity_monitor_tasks
            """)).all()
        task_worlddb_ids = {
            task_id: source_worlddb_id or rider_worlddb_ids.get(rider_id)
            for task_id, source_worlddb_id, rider_id in task_rows
        }

    counts = {}
    payloads = []
    for table_name in tables:
        columns = [column["name"] for column in inspector.get_columns(table_name)]
        if "id" not in columns:
            continue
        query = f'SELECT * FROM "{table_name}"'
        params = {}
        if worlddb_rider_id is not None:
            if "source_worlddb_rider_id" in columns:
                query += " WHERE source_worlddb_rider_id = :worlddb_rider_id"
                params["worlddb_rider_id"] = int(worlddb_rider_id)
                if "rider_id" in columns and matching_rider_ids:
                    query += " OR rider_id = ANY(:rider_ids)"
                    params["rider_ids"] = matching_rider_ids
            elif "rider_id" in columns:
                if not matching_rider_ids:
                    continue
                query += " WHERE rider_id = ANY(:rider_ids)"
                params["rider_ids"] = matching_rider_ids
            elif table_name == "activity_monitor_task_events" and matching_rider_ids:
                query += " WHERE task_id IN (SELECT id FROM activity_monitor_tasks WHERE rider_id = ANY(:rider_ids))"
                params["rider_ids"] = matching_rider_ids
            else:
                continue
        query += " ORDER BY id"
        with source_engine.connect() as connection:
            rows = connection.execute(text(query), params).mappings().all()
        counts[table_name] = len(rows)
        for row in rows:
            record = dict(row)
            record_id = record.get("id")
            source_key = str(record.get("source_record_key") or f"id:{record_id}")[:240]
            rider_id = record.get("rider_id")
            worlddb_id = record.get("source_worlddb_rider_id")
            if worlddb_id is None and rider_id is not None:
                worlddb_id = rider_worlddb_ids.get(rider_id)
            if worlddb_id is None and record.get("task_id") is not None:
                worlddb_id = task_worlddb_ids.get(record["task_id"])
            occurred_at = next((record.get(name) for name in TIME_COLUMNS if record.get(name) is not None), None)
            payloads.append({
                "namespace_slug": namespace_slug,
                "worlddb_rider_id": worlddb_id,
                "record_type": table_name,
                "source_record_key": source_key,
                "occurred_at": occurred_at,
                "record_data": json.dumps(record, ensure_ascii=False, default=_json_value),
            })

    summary = {
        "namespace": namespace_slug,
        "worlddb_rider_id": int(worlddb_rider_id) if worlddb_rider_id is not None else None,
        "tables": counts,
        "records": sum(counts.values()),
        "records_to_sync": len(payloads),
        "written": 0,
        "apply": bool(apply),
    }
    if apply and payloads:
        with main_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO activity_monitor_global_history
                    (namespace_slug, worlddb_rider_id, record_type,
                     source_record_key, occurred_at, record_data)
                VALUES
                    (:namespace_slug, :worlddb_rider_id, :record_type,
                     :source_record_key, :occurred_at, CAST(:record_data AS JSONB))
                ON CONFLICT (namespace_slug, record_type, source_record_key)
                DO UPDATE SET
                    worlddb_rider_id = EXCLUDED.worlddb_rider_id,
                    occurred_at = EXCLUDED.occurred_at,
                    record_data = EXCLUDED.record_data,
                    archived_at = CURRENT_TIMESTAMP
            """), payloads)
        summary["written"] = len(payloads)
    return summary


def _restore_value(column, value):
    if value is None:
        return None
    if isinstance(column.type, SQLDateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(column.type, SQLDate) and not isinstance(column.type, SQLDateTime) and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def sync_activity_monitor_roster(namespace_engine):
    from webapp.models.ns.activity_monitor import ActivityMonitorRider

    if not inspect(namespace_engine).has_table(ActivityMonitorRider.__tablename__):
        return []
    matched = []
    active_rider_row_ids = []
    with namespace_engine.begin() as connection:
        cyclists = connection.execute(text("""
            SELECT c.id, c.first_cycling_id, c.first_name, c.last_name,
                   c.birth_date, c.team_id, COALESCE(t.team_name, t.short_name, '') AS team_name
            FROM cdb_cyclist c
            LEFT JOIN cdb_team t ON t.id = c.team_id
            ORDER BY c.id
        """)).mappings().all()
        for cyclist in cyclists:
            worlddb_id = int(cyclist["id"])
            rider = connection.execute(text("""
                SELECT id, identity_generation FROM activity_monitor_riders
                WHERE source_worlddb_rider_id = :worlddb_id
                  AND present_in_cdb = TRUE
                ORDER BY identity_generation DESC LIMIT 1
            """), {"worlddb_id": worlddb_id}).mappings().first()
            birth_date = None
            raw_birth_date = cyclist["birth_date"]
            if raw_birth_date:
                try:
                    birth_date = datetime.strptime(str(raw_birth_date), "%Y%m%d").date()
                except ValueError:
                    birth_date = None
            now = datetime.utcnow()
            if rider:
                connection.execute(text("""
                    UPDATE activity_monitor_riders
                    SET first_cycling_id = :fc_id, first_name = :first_name,
                        last_name = :last_name, birth_date = COALESCE(:birth_date, birth_date),
                        team_id = :team_id, team_name = :team_name,
                        present_in_cdb = TRUE, last_seen_cdb_at = :now, updated_at = :now
                    WHERE id = :id
                """), {"fc_id": cyclist["first_cycling_id"], "first_name": cyclist["first_name"] or "",
                       "last_name": cyclist["last_name"] or "", "birth_date": birth_date,
                       "team_id": cyclist["team_id"], "team_name": cyclist["team_name"] or "",
                       "now": now, "id": rider["id"]})
            else:
                generation = connection.execute(text("""
                    SELECT COALESCE(MAX(identity_generation), -1) + 1
                    FROM activity_monitor_riders WHERE source_worlddb_rider_id = :worlddb_id
                """), {"worlddb_id": worlddb_id}).scalar_one()
                connection.execute(text("""
                    INSERT INTO activity_monitor_riders
                        (identity_key, source_worlddb_rider_id, identity_generation,
                         first_cycling_id, first_name, last_name, birth_date, team_id,
                         team_name, identity_status, identity_hold, eligible,
                         present_in_cdb, last_seen_cdb_at, created_at, updated_at)
                    VALUES (:identity_key, :worlddb_id, :generation, :fc_id, :first_name,
                            :last_name, :birth_date, :team_id, :team_name,
                            'unverified', FALSE, TRUE, TRUE, :now, :now, :now)
                """), {"identity_key": f"worlddb:{worlddb_id}:g{generation}",
                       "worlddb_id": worlddb_id, "generation": generation,
                       "fc_id": cyclist["first_cycling_id"], "first_name": cyclist["first_name"] or "",
                       "last_name": cyclist["last_name"] or "", "birth_date": birth_date,
                       "team_id": cyclist["team_id"], "team_name": cyclist["team_name"] or "", "now": now})
                rider_id = connection.execute(text("""
                    SELECT id FROM activity_monitor_riders
                    WHERE source_worlddb_rider_id = :worlddb_id
                      AND identity_generation = :generation
                """), {"worlddb_id": worlddb_id, "generation": generation}).scalar_one()
            if rider:
                rider_id = rider["id"]
            active_rider_row_ids.append(rider_id)
            matched.append(worlddb_id)
        connection.execute(text("""
            UPDATE activity_monitor_riders
            SET present_in_cdb = FALSE, updated_at = :now
            WHERE source_worlddb_rider_id IS NOT NULL
              AND id != ALL(:active_ids) AND present_in_cdb = TRUE
        """), {"now": datetime.utcnow(), "active_ids": active_rider_row_ids})
    return matched


def restore_worlddb_history(main_engine, namespace_engine, worlddb_rider_ids):
    """Restore supported append-only records for riders already matched locally."""
    from webapp.models.ns.activity_monitor import ActivityMonitorBase

    rider_ids = {int(value) for value in worlddb_rider_ids}
    if not rider_ids or not inspect(main_engine).has_table(ARCHIVE_TABLE):
        return {"riders": 0, "restored": {}}

    metadata = ActivityMonitorBase.metadata
    destination = namespace_engine.connect()
    transaction = destination.begin()
    restored = {table: 0 for table in RECOVERABLE_TABLES}
    try:
        local_riders = {
            worlddb_id: (rider_id, generation)
            for worlddb_id, rider_id, generation in destination.execute(text("""
            SELECT source_worlddb_rider_id, id, identity_generation
            FROM activity_monitor_riders
            WHERE source_worlddb_rider_id = ANY(:worlddb_ids)
              AND present_in_cdb = TRUE
        """), {"worlddb_ids": sorted(rider_ids)}).all()
        }
        if not local_riders:
            transaction.commit()
            return {"riders": 0, "restored": restored}

        with main_engine.connect() as archive:
            archived = archive.execute(text("""
                SELECT namespace_slug, worlddb_rider_id, record_type, source_record_key, record_data
                FROM activity_monitor_global_history
                WHERE worlddb_rider_id = ANY(:worlddb_ids)
                  AND record_type = ANY(:record_types)
                ORDER BY CASE record_type
                    WHEN 'activity_monitor_source_checks' THEN 0
                    WHEN 'activity_monitor_results' THEN 1
                    ELSE 2 END, id
            """), {"worlddb_ids": list(local_riders), "record_types": list(RECOVERABLE_TABLES)}).mappings().all()

        check_id_map = {}
        for entry in archived:
            table_name = entry["record_type"]
            record = entry["record_data"]
            worlddb_id = entry["worlddb_rider_id"]
            if worlddb_id not in local_riders:
                continue
            table = metadata.tables[table_name]
            target_generation = local_riders[worlddb_id][1]
            if ("identity_generation" in table.c and
                    int(record.get("identity_generation") or 0) != target_generation):
                continue
            values = {key: _restore_value(table.c[key], value)
                      for key, value in record.items()
                      if key in table.c and key != "id"}
            if "rider_id" in table.c:
                values["rider_id"] = local_riders[worlddb_id][0]
            if "identity_generation" in table.c:
                values["identity_generation"] = target_generation
            if "source_worlddb_rider_id" in table.c:
                values["source_worlddb_rider_id"] = worlddb_id
            source_key_column = "source_result_key" if table_name == "activity_monitor_results" else "source_record_key"
            if source_key_column in table.c:
                key_digest = hashlib.sha256(
                    f"{entry['namespace_slug']}\0{table_name}\0{entry['source_record_key']}".encode()
                ).hexdigest()
                values[source_key_column] = f"archive:{key_digest}"
            if table_name == "activity_monitor_source_checks":
                values["run_id"] = None
            elif table_name == "activity_monitor_results":
                old_check_id = record.get("source_check_id")
                values["source_check_id"] = check_id_map.get((entry["namespace_slug"], old_check_id))
            elif table_name == "activity_monitor_rider_schedules":
                pass
            elif table_name == "activity_monitor_identity_alerts":
                values["cdb_import_id"] = None
            elif table_name == "activity_monitor_eligibility_events":
                values["cdb_import_id"] = None
            elif table_name == "activity_monitor_profile_links" and values.get("is_current"):
                current_exists = destination.execute(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM activity_monitor_profile_links
                        WHERE rider_id = :rider_id
                          AND identity_generation = :generation
                          AND is_current = TRUE
                    )
                """), {"rider_id": values["rider_id"],
                       "generation": values["identity_generation"]}).scalar_one()
                if current_exists:
                    continue
            statement = pg_insert(table).values(**values).on_conflict_do_nothing().returning(table.c.id)
            inserted_id = destination.execute(statement).scalar_one_or_none()
            was_inserted = inserted_id is not None
            if inserted_id is None and source_key_column in table.c:
                inserted_id = destination.execute(
                    text(f'SELECT id FROM "{table_name}" WHERE "{source_key_column}" = :key'),
                    {"key": values[source_key_column]},
                ).scalar_one_or_none()
            if table_name == "activity_monitor_source_checks" and record.get("id") is not None and inserted_id is not None:
                check_id_map[(entry["namespace_slug"], record["id"])] = inserted_id
            if was_inserted:
                restored[table_name] += 1
        transaction.commit()
        return {"riders": len(local_riders), "restored": restored}
    except Exception:
        transaction.rollback()
        raise
    finally:
        destination.close()
