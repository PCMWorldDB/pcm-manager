"""Archive namespace Activity Monitor history in the shared main database.

The namespace remains the source of truth for its current roster and workflow.
This append-oriented archive is a cross-namespace history index keyed by the
WorldDB rider ID, never by a namespace-local primary key.
"""

from __future__ import annotations

import json
import hashlib
import hmac
from collections import defaultdict
from datetime import date, datetime, timezone
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy import Date as SQLDate, DateTime as SQLDateTime, inspect, text


ARCHIVE_TABLE = "activity_monitor_global_history"
TRANSFER_MARKER_TYPE = "__history_bundle_import__"
HISTORY_BUNDLE_FORMAT = "worlddb-activity-monitor-history"
HISTORY_BUNDLE_VERSION = 1

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


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_history_bundle(main_engine):
    """Build a portable bundle from the installed shared archive, without changing it."""
    if not inspect(main_engine).has_table(ARCHIVE_TABLE):
        raise RuntimeError("The shared Activity Monitor history archive is not installed.")
    with main_engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT namespace_slug, worlddb_rider_id, record_type,
                   source_record_key, occurred_at, record_data, archived_at
            FROM {ARCHIVE_TABLE}
            WHERE record_type != :marker_type
            ORDER BY namespace_slug, record_type, source_record_key
        """), {"marker_type": TRANSFER_MARKER_TYPE}).mappings().all()
    records = []
    for row in rows:
        records.append({
            "namespace_slug": row["namespace_slug"],
            "worlddb_rider_id": row["worlddb_rider_id"],
            "record_type": row["record_type"],
            "source_record_key": row["source_record_key"],
            "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
            "record_data": row["record_data"],
            "archived_at": row["archived_at"].isoformat() if row["archived_at"] else None,
        })
    digest = hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()
    bundle = {
        "format": HISTORY_BUNDLE_FORMAT,
        "version": HISTORY_BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records_sha256": digest,
        "records": records,
    }
    return _canonical_json(bundle).encode("utf-8")


def validate_history_bundle(raw_bytes):
    """Validate an uploaded bundle before any database writes."""
    try:
        bundle = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The file is not a valid Activity Monitor history bundle.") from exc
    if not isinstance(bundle, dict) or bundle.get("format") != HISTORY_BUNDLE_FORMAT:
        raise ValueError("This file is not an Activity Monitor history export.")
    if bundle.get("version") != HISTORY_BUNDLE_VERSION:
        raise ValueError("This history export uses an unsupported format version.")
    records = bundle.get("records")
    if not isinstance(records, list) or bundle.get("record_count") != len(records):
        raise ValueError("The history export record count is invalid.")
    digest = hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(bundle.get("records_sha256", "")), digest):
        raise ValueError("The history export is incomplete or has been changed.")
    unique = set()
    rider_ids = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("The history export contains an invalid record.")
        namespace_slug = record.get("namespace_slug")
        record_type = record.get("record_type")
        source_key = record.get("source_record_key")
        data = record.get("record_data")
        if (not isinstance(namespace_slug, str) or not namespace_slug or len(namespace_slug) > 100
                or not isinstance(record_type, str) or not record_type.startswith("activity_monitor_")
                or record_type in EXCLUDED_TABLES or record_type == TRANSFER_MARKER_TYPE
                or len(record_type) > 80 or not isinstance(source_key, str) or not source_key
                or len(source_key) > 240 or not isinstance(data, dict)):
            raise ValueError("The history export contains a record with invalid fields.")
        key = (namespace_slug, record_type, source_key)
        if key in unique:
            raise ValueError("The history export contains duplicate records.")
        unique.add(key)
        worlddb_id = record.get("worlddb_rider_id")
        if worlddb_id is not None:
            try:
                rider_ids.add(int(worlddb_id))
            except (TypeError, ValueError) as exc:
                raise ValueError("The history export contains an invalid WorldDB ID.") from exc
        if record.get("occurred_at") is not None:
            try:
                datetime.fromisoformat(record["occurred_at"])
            except (TypeError, ValueError) as exc:
                raise ValueError("The history export contains an invalid date.") from exc
    return {"bundle": bundle, "rider_ids": rider_ids}


def preview_history_bundle(main_engine, namespace_engine, validated):
    """Report import and rider-match counts without writing to either database."""
    if not inspect(main_engine).has_table(ARCHIVE_TABLE):
        raise RuntimeError("The shared Activity Monitor history archive is not installed.")
    if not inspect(namespace_engine).has_table("activity_monitor_riders"):
        raise RuntimeError("Activity Monitor history has not been installed for this namespace.")
    records = validated["bundle"]["records"]
    rider_ids = sorted(validated["rider_ids"])
    with main_engine.connect() as connection:
        existing = connection.execute(text(f"""
            SELECT COUNT(*) FROM {ARCHIVE_TABLE} a
            JOIN jsonb_to_recordset(CAST(:records AS JSONB)) AS r(
                namespace_slug TEXT, record_type TEXT, source_record_key TEXT
            ) ON a.namespace_slug = r.namespace_slug
             AND a.record_type = r.record_type
             AND a.source_record_key = r.source_record_key
        """), {"records": json.dumps([
            {"namespace_slug": item["namespace_slug"], "record_type": item["record_type"],
             "source_record_key": item["source_record_key"]} for item in records
        ])}).scalar_one() if records else 0
    with namespace_engine.connect() as connection:
        matched = connection.execute(text("""
            SELECT COUNT(DISTINCT source_worlddb_rider_id)
            FROM activity_monitor_riders
            WHERE present_in_cdb = TRUE AND source_worlddb_rider_id = ANY(:ids)
        """), {"ids": rider_ids}).scalar_one() if rider_ids else 0
    return {
        "records": len(records), "already_present": int(existing),
        "new_records": len(records) - int(existing), "riders_in_bundle": len(rider_ids),
        "riders_matched": int(matched), "unmatched_riders": len(rider_ids) - int(matched),
        "bundle_sha256": hashlib.sha256(_canonical_json(validated["bundle"]).encode("utf-8")).hexdigest(),
    }


def import_and_restore_history_bundle(main_engine, namespace_engine, validated, *, actor_user_id=None):
    """Import archive rows once, then restore matching rider history idempotently."""
    bundle = validated["bundle"]
    bundle_hash = hashlib.sha256(_canonical_json(bundle).encode("utf-8")).hexdigest()
    if not inspect(main_engine).has_table(ARCHIVE_TABLE):
        raise RuntimeError("The shared Activity Monitor history archive is not installed.")
    if not inspect(namespace_engine).has_table("activity_monitor_riders"):
        raise RuntimeError("Activity Monitor history has not been installed for this namespace.")
    with main_engine.begin() as connection:
        marker = connection.execute(text(f"""
            SELECT record_data FROM {ARCHIVE_TABLE}
            WHERE namespace_slug = '__pcm_manager_transfer__'
              AND record_type = :marker_type AND source_record_key = :bundle_hash
        """), {"marker_type": TRANSFER_MARKER_TYPE, "bundle_hash": bundle_hash}).scalar_one_or_none()
        if marker and marker.get("status") == "complete":
            raise ValueError("This history bundle has already been imported successfully.")
        rows = []
        for item in bundle["records"]:
            rows.append({
                "namespace_slug": item["namespace_slug"],
                "worlddb_rider_id": item["worlddb_rider_id"],
                "record_type": item["record_type"],
                "source_record_key": item["source_record_key"],
                "occurred_at": datetime.fromisoformat(item["occurred_at"]) if item["occurred_at"] else None,
                "record_data": json.dumps(item["record_data"], ensure_ascii=False),
            })
        if rows:
            connection.execute(text(f"""
                INSERT INTO {ARCHIVE_TABLE}
                    (namespace_slug, worlddb_rider_id, record_type, source_record_key, occurred_at, record_data)
                VALUES
                    (:namespace_slug, :worlddb_rider_id, :record_type, :source_record_key,
                     :occurred_at, CAST(:record_data AS JSONB))
                ON CONFLICT (namespace_slug, record_type, source_record_key) DO NOTHING
            """), rows)
        connection.execute(text(f"""
            INSERT INTO {ARCHIVE_TABLE}
                (namespace_slug, worlddb_rider_id, record_type, source_record_key, record_data)
            VALUES ('__pcm_manager_transfer__', NULL, :marker_type, :bundle_hash,
                    CAST(:record_data AS JSONB))
            ON CONFLICT (namespace_slug, record_type, source_record_key)
            DO UPDATE SET record_data = EXCLUDED.record_data, archived_at = CURRENT_TIMESTAMP
        """), {"marker_type": TRANSFER_MARKER_TYPE, "bundle_hash": bundle_hash,
               "record_data": json.dumps({"status": "importing", "record_count": len(rows),
                                          "imported_by_user_id": actor_user_id})})

    from webapp.tools.activity_monitor_global_history import sync_activity_monitor_roster
    synced_ids = sync_activity_monitor_roster(namespace_engine)
    restored = restore_worlddb_history(main_engine, namespace_engine, synced_ids)
    with main_engine.begin() as connection:
        connection.execute(text(f"""
            UPDATE {ARCHIVE_TABLE} SET record_data = CAST(:record_data AS JSONB), archived_at = CURRENT_TIMESTAMP
            WHERE namespace_slug = '__pcm_manager_transfer__'
              AND record_type = :marker_type AND source_record_key = :bundle_hash
        """), {"marker_type": TRANSFER_MARKER_TYPE, "bundle_hash": bundle_hash,
               "record_data": json.dumps({"status": "complete", "record_count": len(rows),
                                          "restored": restored["restored"],
                                          "imported_by_user_id": actor_user_id,
                                          "completed_at": datetime.now(timezone.utc).isoformat()})})
    return {"records_imported": len(rows), "riders_matched": restored["riders"],
            "restored": restored["restored"], "bundle_sha256": bundle_hash}


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
        monitor_riders_by_fc_id = defaultdict(list)
        for monitor_rider in connection.execute(text("""
            SELECT id, source_worlddb_rider_id, first_cycling_id, birth_date, identity_hold
            FROM activity_monitor_riders
            WHERE first_cycling_id IS NOT NULL
        """)).mappings().all():
            monitor_riders_by_fc_id[monitor_rider["first_cycling_id"]].append(monitor_rider)
        cyclist_counts = defaultdict(int)
        for cyclist in cyclists:
            if cyclist["first_cycling_id"] is not None:
                cyclist_counts[cyclist["first_cycling_id"]] += 1
        for cyclist in cyclists:
            worlddb_id = int(cyclist["id"])
            fc_id = cyclist["first_cycling_id"]
            if fc_id is not None and cyclist_counts[fc_id] == 1:
                try:
                    cdb_birth_date = datetime.strptime(str(cyclist["birth_date"]), "%Y%m%d").date()
                except ValueError:
                    cdb_birth_date = None
                matching_monitor_rows = [
                    rider for rider in monitor_riders_by_fc_id.get(fc_id, [])
                    if rider["source_worlddb_rider_id"] != worlddb_id
                    and not rider["identity_hold"]
                    and cdb_birth_date is not None
                    and rider["birth_date"] == cdb_birth_date
                ]
                unlinked = matching_monitor_rows[0] if len(matching_monitor_rows) == 1 else None
                if unlinked and unlinked["source_worlddb_rider_id"] is None:
                    now = datetime.utcnow()
                    connection.execute(text("""
                        UPDATE activity_monitor_riders
                        SET present_in_cdb = TRUE, last_seen_cdb_at = :now, updated_at = :now
                        WHERE id = :id
                    """), {"now": now, "id": unlinked["id"]})
                    active_rider_row_ids.append(unlinked["id"])
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
            WHERE id != ALL(:active_ids) AND present_in_cdb = TRUE
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
