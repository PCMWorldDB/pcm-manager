"""Archive namespace Activity Monitor history in the shared main database.

The namespace remains the source of truth for its current roster and workflow.
This append-oriented archive is a cross-namespace history index keyed by the
WorldDB rider ID, never by a namespace-local primary key.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import inspect, text


ARCHIVE_TABLE = "activity_monitor_global_history"

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
