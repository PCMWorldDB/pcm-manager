"""Transactional import of the local Activity Monitor history into a namespace.

The source SQLite file is opened read-only. By default this module only
validates and reports aggregate counts. Writes require the explicit apply flag.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from webapp.models.ns.activity_monitor import (
    ActivityMonitorCDBImport,
    ActivityMonitorEligibilityEvent,
    ActivityMonitorHistoryImport,
    ActivityMonitorLegacyRecord,
    ActivityMonitorProfileLink,
    ActivityMonitorResult,
    ActivityMonitorRider,
    ActivityMonitorRiderSchedule,
    ActivityMonitorRosterSnapshot,
    ActivityMonitorRun,
    ActivityMonitorSourceCheck,
    ActivityMonitorTask,
    ActivityMonitorTaskEvent,
    ActivityMonitorReview,
    ActivityMonitorReviewEvent,
    ActivityMonitorIdentityAlert,
    ActivityMonitorSchedule,
)
from webapp.tools.activity_monitor_import_preview import (
    ROW_VALIDATION_RULES,
    parse_cdb_date,
    parse_date,
    validate_payloads_and_relations,
    validate_row_shapes,
    read_monitor_inventory,
)


EXCLUDED_TABLES = {
    "monitor_metadata",
    "fc_collector_state",
    "pcs_collector_state",
    "activity_source_cycle_pending",
}
SUPPORTED_TABLES = set(ROW_VALIDATION_RULES) | {"activity_source_cycle_history"}
TARGET_MODELS = {
    "activity_monitor_riders": ActivityMonitorRider,
    "activity_monitor_cdb_imports": ActivityMonitorCDBImport,
    "activity_monitor_roster_snapshots": ActivityMonitorRosterSnapshot,
    "activity_monitor_runs": ActivityMonitorRun,
    "activity_monitor_source_checks": ActivityMonitorSourceCheck,
    "activity_monitor_results": ActivityMonitorResult,
    "activity_monitor_profile_links": ActivityMonitorProfileLink,
    "activity_monitor_reviews": ActivityMonitorReview,
    "activity_monitor_review_events": ActivityMonitorReviewEvent,
    "activity_monitor_rider_schedules": ActivityMonitorRiderSchedule,
    "activity_monitor_eligibility_events": ActivityMonitorEligibilityEvent,
    "activity_monitor_tasks": ActivityMonitorTask,
    "activity_monitor_task_events": ActivityMonitorTaskEvent,
    "activity_monitor_identity_alerts": ActivityMonitorIdentityAlert,
    "activity_monitor_schedules": ActivityMonitorSchedule,
    "activity_monitor_legacy_records": ActivityMonitorLegacyRecord,
}


def _source_connection(path: Path):
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dt(value) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]), datetime.min.time())
        except ValueError as exc:
            raise ValueError("A source timestamp could not be parsed.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _safe_pcs_request_path(value) -> str | None:
    """Preserve only an allowlisted PCS rider path, never a local source path."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else "")
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "www.procyclingstats.com", "procyclingstats.com"
        }:
            return None
        path = parsed.path
    elif raw.startswith("/rider/"):
        path = raw
    else:
        return None
    if not path.startswith("/rider/"):
        return None
    return path.rstrip("/")[:500]


def backfill_imported_pcs_request_paths(namespace_engine, source_path: Path, *, apply: bool = False):
    """Restore only missing safe PCS request paths for an already imported source DB."""
    source_path = Path(source_path)
    if not source_path.is_file():
        raise RuntimeError("Activity Monitor source database does not exist.")
    source_hash = _sha256(source_path)
    with closing(_source_connection(source_path)) as source_db:
        source_rows = source_db.execute(
            "SELECT check_id, source, source_file FROM source_checks "
            "WHERE lower(source) IN ('pcs', 'pcs_html', 'procyclingstats')"
        ).fetchall()
    candidates = {
        f"source_checks:{int(row['check_id'])}": _safe_pcs_request_path(row['source_file'])
        for row in source_rows
    }
    candidates = {key: path for key, path in candidates.items() if path}
    with namespace_engine.begin() as connection:
        ledger = connection.execute(select(ActivityMonitorHistoryImport.__table__).where(
            ActivityMonitorHistoryImport.__table__.c.source_sha256 == source_hash,
            ActivityMonitorHistoryImport.__table__.c.status == 'completed',
        )).mappings().one_or_none()
        if not ledger:
            raise RuntimeError("This source database is not recorded as an already completed import.")
        missing = connection.execute(text("""
            SELECT c.id, c.source_record_key, c.source, c.request_path
            FROM activity_monitor_source_checks c
            WHERE c.source_record_key = ANY(:keys)
              AND c.request_path IS NULL
              AND c.source IN ('pcs', 'pcs_html', 'procyclingstats')
        """), {"keys": list(candidates)}).mappings().all() if candidates else []
        updates = [
            (int(row['id']), candidates[row['source_record_key']])
            for row in missing if row['source_record_key'] in candidates
        ]
        if apply and updates:
            for record_id, request_path in updates:
                connection.execute(text("""
                    UPDATE activity_monitor_source_checks
                    SET request_path = :request_path
                    WHERE id = :id AND request_path IS NULL
                """), {"id": record_id, "request_path": request_path})
    print(f"Safe PCS request paths found: {len(candidates)}")
    print(f"Missing paths matched to this import: {len(updates)}")
    print("Changes applied." if apply else "Dry run only; no changes made.")
    return {"candidate_paths": len(candidates), "matched_missing": len(updates), "applied": bool(apply)}


def _json(value, default=None):
    if value is None or not str(value).strip():
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("A source JSON field could not be parsed.") from exc


def _bool(value):
    return bool(value) if value is not None else False


def _insert_rows(connection, model, rows, key_factory=None, batch_size=1000):
    """Insert in bounded batches and return source-key -> generated target ID."""
    table = model.__table__
    id_map = {}
    total = 0
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        result = connection.execute(insert(table).returning(table.c.id), batch)
        ids = result.scalars().all()
        if len(ids) != len(batch):
            raise RuntimeError(f"Unexpected insert count for {table.name}.")
        if key_factory:
            id_map.update((key_factory(row), target_id) for row, target_id in zip(batch, ids))
        total += len(batch)
    return id_map, total


def _rows(db, table):
    # Table names and columns are discovered from sqlite_master and validated
    # against our allow-list before this helper is used.
    return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]


def _source_counts(db, inventory):
    return {name: count for name, count in inventory.items() if name in SUPPORTED_TABLES}


def _ensure_source_is_supported(inventory):
    unknown = sorted(
        name for name, count in inventory.items()
        if count and name not in SUPPORTED_TABLES and name not in EXCLUDED_TABLES
    )
    if unknown:
        raise RuntimeError("Non-empty source tables need an explicit mapping before import.")


def _validate_source(path: Path):
    inventory, schema_fingerprint = read_monitor_inventory(path)
    _ensure_source_is_supported(inventory)
    row_report = validate_row_shapes(path)
    payload_report = validate_payloads_and_relations(path)
    row_errors = [
        item for item in row_report
        if item["missing_required_rows"] or item["missing_key_rows"]
        or item["duplicate_key_groups"] or item["duplicate_key_rows"]
        or item["invalid_date_values"] or item["orphan_rider_references"]
    ]
    payload_errors = (
        any(item["invalid"] for item in payload_report["json"])
        or any(item["over_limit"] for item in payload_report["lengths"])
        or any(item["status_over_limit"] for item in payload_report["transformed_lengths"])
        or any(item["unresolved"] for item in payload_report["references"])
    )
    if row_errors or payload_errors:
        raise RuntimeError("Source validation found structural problems; import stopped.")
    return inventory, schema_fingerprint, row_report


def _target_rider_map(connection, source_riders):
    cdb_rows = connection.execute(text(
        "SELECT id, first_cycling_id, birth_date, team_id "
        "FROM rider WHERE first_cycling_id IS NOT NULL"
    )).mappings().all()
    by_fc = defaultdict(list)
    for row in cdb_rows:
        by_fc[int(row["first_cycling_id"])].append(row)

    source_by_fc = Counter(
        int(row["fc_id"]) for row in source_riders
        if row.get("fc_id") is not None and row.get("fc_id_quality") == "valid"
        and not _bool(row.get("identity_hold"))
    )
    matches = {}
    cdb_details = {}
    for rider in source_riders:
        key = (int(rider["worlddb_rider_id"]), int(rider.get("identity_generation") or 0))
        fc_id = rider.get("fc_id")
        matched = None
        if (
            fc_id is not None and rider.get("fc_id_quality") == "valid"
            and not _bool(rider.get("identity_hold"))
            and source_by_fc[int(fc_id)] == 1 and len(by_fc.get(int(fc_id), [])) == 1
        ):
            candidate = by_fc[int(fc_id)][0]
            if parse_date(rider.get("birthdate")) == parse_cdb_date(candidate["birth_date"]):
                matched = candidate
        matches[key] = matched
        if matched is not None:
            cdb_details[key] = matched
    return matches, cdb_details


def _rows_for_riders(source_riders, cdb_matches, now):
    records = []
    for source in source_riders:
        source_id = int(source["worlddb_rider_id"])
        generation = int(source.get("identity_generation") or 0)
        fc_id = source.get("fc_id")
        valid_fc = (
            fc_id is not None and source.get("fc_id_quality") == "valid"
            and not _bool(source.get("identity_hold"))
        )
        matched = cdb_matches.get((source_id, generation))
        identity_key = (
            f"fc:{int(fc_id)}:g{generation}" if valid_fc
            else f"source:{source_id}:g{generation}"
        )
        records.append({
            "identity_key": identity_key,
            "source_worlddb_rider_id": source_id,
            "identity_generation": generation,
            "first_cycling_id": int(fc_id) if fc_id is not None else None,
            "first_name": source.get("first_name") or "",
            "last_name": source.get("surname") or "",
            "birth_date": parse_date(source.get("birthdate")),
            "nationality": source.get("nationality") or None,
            "nationality_code": source.get("nationality_code") or None,
            "team_id": matched["team_id"] if matched else None,
            "team_name": source.get("team_name") or "",
            "pcs_rider_path": None,
            "identity_status": source.get("fc_id_quality") or "unverified",
            "identity_hold": _bool(source.get("identity_hold")),
            "identity_hold_reason": None,
            "eligible": _bool(source.get("eligible")),
            "present_in_cdb": _bool(source.get("present_in_cdb")),
            "eligibility_reason": source.get("eligibility_reason") or None,
            "last_seen_cdb_at": _dt(source.get("last_seen_cdb_at")),
            "created_at": now,
            "updated_at": now,
        })
    return records


def _keyed(rows, source_key, target_key):
    return {row[source_key]: row[target_key] for row in rows}


def _import_records(connection, db, inventory, source_hash, schema_hash, actor_user_id):
    from webapp.models.ns.activity_monitor import ActivityMonitorBase

    table_rows = {name: _rows(db, name) for name, count in inventory.items() if count and name in SUPPORTED_TABLES}
    now = datetime.utcnow()
    imported = Counter()
    source_counts = _source_counts(db, inventory)
    ledger = ActivityMonitorHistoryImport(
        source_sha256=source_hash,
        source_schema_fingerprint=schema_hash,
        status="running",
        initiated_by_user_id=actor_user_id,
        started_at=now,
        source_row_counts=source_counts,
        imported_row_counts={},
        unlinked_record_count=0,
    )
    connection.execute(insert(ActivityMonitorHistoryImport.__table__).values(
        source_sha256=ledger.source_sha256,
        source_schema_fingerprint=ledger.source_schema_fingerprint,
        status=ledger.status,
        initiated_by_user_id=ledger.initiated_by_user_id,
        started_at=ledger.started_at,
        source_row_counts=ledger.source_row_counts,
        imported_row_counts={},
        unlinked_record_count=0,
    ))

    riders = table_rows.get("riders", [])
    cdb_matches, _ = _target_rider_map(connection, riders)
    rider_records = _rows_for_riders(riders, cdb_matches, now)
    rider_ids, imported["riders"] = _insert_rows(
        connection, ActivityMonitorRider, rider_records,
        lambda row: (row["source_worlddb_rider_id"], row["identity_generation"]),
    )

    def rider_id(source_id, generation=0):
        if source_id is None:
            return None
        return rider_ids.get((int(source_id), int(generation or 0)))

    imports = table_rows.get("cdb_imports", [])
    cdb_import_records = [{
        "source_sha256": row["source_sha256"],
        "reference_date": parse_date(row["reference_date"]),
        "imported_at": _dt(row["imported_at"]),
        "imported_by_user_id": None,
        "rider_count": int(row["rider_count"]),
        "summary": _json(row.get("summary_json"), {}),
    } for row in imports]
    cdb_import_ids, imported["cdb_imports"] = _insert_rows(
        connection, ActivityMonitorCDBImport, cdb_import_records,
        lambda row: (row["source_sha256"], row["reference_date"]),
    )
    cdb_import_by_source = {
        (row["source_sha256"], row["reference_date"]): cdb_import_ids.get(
            (row["source_sha256"], row["reference_date"])
        ) for row in cdb_import_records
    }
    source_import_ids = {
        int(row["import_id"]): cdb_import_by_source[(row["source_sha256"], parse_date(row["reference_date"]))]
        for row in imports
    }

    source_runs = table_rows.get("fc_collector_runs", [])
    run_records = []
    for row in source_runs:
        start = _dt(row.get("started_at"))
        run_records.append({
            "source_record_key": f"fc_collector_runs:{row['run_id']}",
            "run_type": "firstcycling",
            "status": row["status"],
            "initiated_by_user_id": None,
            "reference_date": (start or _dt(row.get("finished_at"))).date(),
            "started_at": start,
            "finished_at": _dt(row.get("finished_at")),
            "requested_count": int(row.get("requested") or 0),
            "attempted_count": int(row.get("attempted") or 0),
            "successful_count": int(row.get("successful") or 0),
            "matched_count": int(row.get("matched") or 0),
            "needs_review_count": 0,
            "heartbeat_at": _dt(row.get("heartbeat_at")),
            "stop_reason": row.get("stop_reason") or None,
            "settings": {},
            "error_message": None,
            "created_at": start or _dt(row.get("finished_at")),
        })
    run_ids, imported["fc_collector_runs"] = _insert_rows(
        connection, ActivityMonitorRun, run_records,
        lambda row: row["source_record_key"].split(":", 1)[1],
    )

    checks = table_rows.get("source_checks", [])
    check_records = [{
        "source_record_key": f"source_checks:{row['check_id']}",
        "run_id": None,
        "rider_id": rider_id(row.get("worlddb_rider_id"), row.get("identity_generation")),
        "source_worlddb_rider_id": row.get("worlddb_rider_id"),
        "identity_generation": int(row.get("identity_generation") or 0),
        "first_cycling_id": row.get("fc_id"),
        "season": row.get("season"),
        "source": row["source"],
        "source_status": row["source_status"],
        "coverage_status": row["coverage_status"],
        "identity_result": "not_checked",
        "checked_at": _dt(row["checked_at"]),
        "http_status": None,
        "response_sha256": row.get("html_sha256"),
        "request_path": _safe_pcs_request_path(row.get("source_file")),
        "parser_version": None,
        "message": row.get("message") or None,
    } for row in checks]
    check_ids, imported["source_checks"] = _insert_rows(
        connection, ActivityMonitorSourceCheck, check_records,
        lambda row: row["source_record_key"],
    )
    source_check_ids = {int(row["check_id"]): check_ids[f"source_checks:{row['check_id']}"] for row in checks}

    api_downloads = table_rows.get("fc_api_downloads", [])
    api_records = [{
        "source_record_key": f"fc_api_downloads:{row['attempt_id']}",
        "run_id": run_ids.get(str(row.get("run_id"))) if row.get("run_id") is not None else None,
        "rider_id": rider_id(row.get("worlddb_rider_id"), row.get("identity_generation")),
        "source_worlddb_rider_id": row.get("worlddb_rider_id"),
        "identity_generation": int(row.get("identity_generation") or 0),
        "first_cycling_id": row.get("fc_id"),
        "season": None,
        "source": "firstcycling",
        "source_status": row["status"],
        "coverage_status": "not_applicable",
        "identity_result": "not_checked",
        "checked_at": _dt(row["attempted_at"]),
        "http_status": row.get("http_status"),
        "response_sha256": row.get("response_sha256"),
        "request_path": None,
        "parser_version": None,
        "message": row.get("message") or None,
    } for row in api_downloads]
    api_ids, imported["fc_api_downloads"] = _insert_rows(
        connection, ActivityMonitorSourceCheck, api_records,
        lambda row: row["source_record_key"],
    )

    results = table_rows.get("race_results", [])
    result_records = []
    source_rider_by_key = {
        (int(row["worlddb_rider_id"]), int(row.get("identity_generation") or 0)): row
        for row in riders
    }
    for row in results:
        generation = int(row.get("identity_generation") or 0)
        source_rider = source_rider_by_key.get((int(row["worlddb_rider_id"]), generation))
        identity_verified = bool(
            source_rider and source_rider.get("fc_id_quality") == "valid"
            and not _bool(source_rider.get("identity_hold"))
        )
        result_records.append({
            "source_result_key": row["result_key"],
            "rider_id": rider_id(row.get("worlddb_rider_id"), generation),
            "source_worlddb_rider_id": row.get("worlddb_rider_id"),
            "identity_generation": generation,
            "first_cycling_id": int(row["fc_id"]),
            "season": int(row["year"]),
            "race_id": row.get("race_id"),
            "edition_id": row.get("edition_id"),
            "race_name": row.get("race_name") or "",
            "race_category": row.get("race_category") or "",
            "race_url": row.get("race_url") or "",
            "result_date": parse_date(row.get("result_date")),
            "stage_id": row.get("stage_id"),
            "stage_number": row.get("stage_number"),
            "stage_name": row.get("stage_name") or "",
            "row_type": row["row_type"],
            "result_position": row.get("result_position") or "",
            "gc_position": row.get("gc_position") or "",
            "participation_status": row["participation_status"],
            "source": row["source"],
            "source_check_id": source_check_ids.get(row.get("source_check_id")),
            "identity_verified": identity_verified,
            "imported_at": _dt(row["imported_at"]),
            "source_payload": dict(row),
        })
    _, imported["race_results"] = _insert_rows(connection, ActivityMonitorResult, result_records)

    history = table_rows.get("rider_profile_history", [])
    profiles_by_rider = defaultdict(list)
    for row in history:
        key = (int(row["worlddb_rider_id"]), int(row.get("identity_generation") or 0))
        profiles_by_rider[key].append(row)

    def selected_verified(key):
        entries = profiles_by_rider.get(key, [])
        if not entries:
            return None
        return max(entries, key=lambda item: (str(item["verified_on"]), int(item["profile_id"])))

    profile_records = []
    for row in history:
        generation = int(row.get("identity_generation") or 0)
        key = (int(row["worlddb_rider_id"]), generation)
        latest = selected_verified(key)
        state = table_rows.get("pcs_profile_state", [])
        state_row = next((item for item in state if (int(item["worlddb_rider_id"]), int(item.get("identity_generation") or 0)) == key), {})
        overridden = bool(state_row.get("url_override"))
        is_current = bool(latest and row["profile_id"] == latest["profile_id"] and not overridden)
        verified_at = _dt(row["verified_on"])
        profile_records.append({
            "source_record_key": f"rider_profile_history:{row['profile_id']}",
            "rider_id": rider_id(row["worlddb_rider_id"], generation),
            "identity_generation": generation,
            "profile_path": row["profile_url"],
            "link_source": "verified_history",
            "link_status": "verified",
            "is_current": is_current,
            "notes": row.get("research_notes") or None,
            "created_at": verified_at,
            "verified_at": verified_at,
        })
    _, imported["rider_profile_history"] = _insert_rows(connection, ActivityMonitorProfileLink, profile_records)

    history_current = {key: selected_verified(key) for key in profiles_by_rider}
    state_records = []
    for row in table_rows.get("pcs_profile_state", []):
        generation = int(row.get("identity_generation") or 0)
        key = (int(row["worlddb_rider_id"]), generation)
        current_verified = history_current.get(key)
        if row.get("url_override"):
            chosen_path, source_name, is_current = row["url_override"], "manual_override", True
        elif current_verified:
            chosen_path, source_name, is_current = current_verified["profile_url"], "verified_history", True
        elif row.get("checked_url"):
            chosen_path, source_name, is_current = row["checked_url"], "checked", False
        else:
            chosen_path, source_name, is_current = None, "profile_state", False
        check_result = row.get("check_result") or "not_checked"
        check_detail = None
        if "; latest " in check_result:
            check_result, _, check_detail = check_result.partition("; latest ")
        state_records.append({
            "source_record_key": f"pcs_profile_state:{key[0]}:{generation}",
            "rider_id": rider_id(*key),
            "identity_generation": generation,
            "profile_path": chosen_path,
            "link_source": source_name,
            "link_status": row.get("match_status") or "not_checked",
            "is_current": is_current,
            "override_url": row.get("url_override") or None,
            "entered_value": row.get("override_input") or None,
            "override_updated_at": _dt(row.get("override_updated_at")),
            "override_error_at": _dt(row.get("override_error_at")),
            "exported_path": row.get("cdb_export_url") or None,
            "exported_override": row.get("exported_override") or None,
            "approved_profile_path": row.get("approved_url") or None,
            "cdb_profile_path": None,
            "profile_slug": row.get("profile_slug") or None,
            "cdb_link_updated_at": _dt(row.get("cdb_link_updated_at")),
            "checked_at": _dt(row.get("checked_at")),
            "checked_url": row.get("checked_url") or None,
            "http_status": row.get("http_status"),
            "check_result": check_result,
            "check_result_detail": check_detail,
            "profile_name": row.get("profile_name") or None,
            "profile_birth_date": parse_date(row.get("profile_dob")),
            "profile_nationality": row.get("profile_nationality") or None,
            "identity_reason": row.get("identity_reason") or None,
            "override_error": row.get("override_error") or None,
            "message": row.get("message") or None,
            "approved_pcs_birth_date": parse_date(row.get("approved_pcs_dob")),
            "approved_worlddb_birth_date": parse_date(row.get("approved_worlddb_dob")),
            "approved_at": _dt(row.get("approved_at")),
            "parser_version": row.get("parser_version"),
            "created_at": _dt(row["checked_at"]),
            "verified_at": _dt(current_verified["verified_on"]) if source_name == "verified_history" and current_verified else None,
        })
    _, imported["pcs_profile_state"] = _insert_rows(connection, ActivityMonitorProfileLink, state_records)

    reviews = table_rows.get("manual_reviews", [])
    review_records = [{
        "rider_id": rider_id(row["worlddb_rider_id"], row.get("identity_generation")),
        "identity_generation": int(row.get("identity_generation") or 0),
        "review_kind": row.get("review_kind") or "retirement",
        "review_status": row["review_status"],
        "notes": row.get("notes") or "",
        "supporting_links": _json(row.get("supporting_links"), []),
        "last_reviewed_at": _dt(row.get("last_reviewed_at")),
        "reviewed_by_user_id": None,
        "updated_at": _dt(row["updated_at"]),
    } for row in reviews]
    _, imported["manual_reviews"] = _insert_rows(connection, ActivityMonitorReview, review_records)

    review_events = table_rows.get("manual_review_history", [])
    review_event_records = [{
        "source_record_key": f"manual_review_history:{row['history_id']}",
        "rider_id": rider_id(row["worlddb_rider_id"], row.get("identity_generation")),
        "identity_generation": int(row.get("identity_generation") or 0),
        "review_kind": row.get("review_kind") or "retirement",
        "old_status": None,
        "new_status": row["review_status"],
        "notes": row.get("notes") or "",
        "supporting_links": _json(row.get("supporting_links"), []),
        "actor_user_id": None,
        "legacy_actor": row.get("actor") or None,
        "occurred_at": _dt(row.get("recorded_at") or row.get("reviewed_at")),
    } for row in review_events]
    _, imported["manual_review_history"] = _insert_rows(connection, ActivityMonitorReviewEvent, review_event_records)

    roster_records = []
    for row in table_rows.get("roster_snapshots", []):
        cdb_id = source_import_ids.get(int(row["import_id"]))
        target_rider = rider_id(row["worlddb_rider_id"])
        if cdb_id is None or target_rider is None:
            raise RuntimeError("A roster snapshot could not be linked to its source import and rider.")
        roster_records.append({
            "cdb_import_id": cdb_id,
            "rider_id": target_rider,
            "source_worlddb_rider_id": row["worlddb_rider_id"],
            "identity_generation": 0,
            "roster_data": _json(row["data_json"], {}),
        })
    _, imported["roster_snapshots"] = _insert_rows(connection, ActivityMonitorRosterSnapshot, roster_records)

    schedule_records = [{
        "source_record_key": f"rider_schedule_history:{row['event_id']}",
        "rider_id": rider_id(row["worlddb_rider_id"], row.get("identity_generation")),
        "source_worlddb_rider_id": row["worlddb_rider_id"],
        "identity_generation": int(row.get("identity_generation") or 0),
        "reference_date": parse_date(row["reference_date"]),
        "recorded_at": _dt(row["recorded_at"]),
        "schedule_data": _json(row["schedule_json"], {}),
    } for row in table_rows.get("rider_schedule_history", [])]
    _, imported["rider_schedule_history"] = _insert_rows(connection, ActivityMonitorRiderSchedule, schedule_records)

    eligibility_records = [{
        "source_record_key": f"eligibility_events:{row['event_id']}",
        "rider_id": rider_id(row["worlddb_rider_id"]),
        "source_worlddb_rider_id": row["worlddb_rider_id"],
        "cdb_import_id": source_import_ids.get(int(row["import_id"])) if row.get("import_id") is not None else None,
        "reference_date": parse_date(row["reference_date"]),
        "recorded_at": _dt(row["recorded_at"]),
        "old_eligible": None if row.get("old_eligible") is None else _bool(row["old_eligible"]),
        "new_eligible": _bool(row["new_eligible"]),
        "reason": row["reason"],
        "legacy_origin": row.get("legacy_origin") or None,
    } for row in table_rows.get("eligibility_events", [])]
    _, imported["eligibility_events"] = _insert_rows(connection, ActivityMonitorEligibilityEvent, eligibility_records)

    alerts = table_rows.get("identity_alerts", [])
    alert_records = [{
        "source_record_key": f"identity_alerts:{row['alert_id']}",
        "rider_id": rider_id(row["worlddb_rider_id"]),
        "source_worlddb_rider_id": row["worlddb_rider_id"],
        "cdb_import_id": source_import_ids.get(int(row["import_id"])) if row.get("import_id") is not None else None,
        "created_at": _dt(row["created_at"]),
        "old_identity": _json(row["old_identity_json"], {}),
        "new_identity": _json(row["new_identity_json"], {}),
        "reason": row["reason"],
        "status": row["status"],
        "resolved_at": _dt(row.get("resolved_at")),
        "resolved_by_user_id": None,
        "legacy_resolved_by": row.get("resolved_by") or None,
        "resolution": row.get("resolution") or None,
    } for row in alerts]
    _, imported["identity_alerts"] = _insert_rows(connection, ActivityMonitorIdentityAlert, alert_records)

    tasks = table_rows.get("research_tasks", [])
    task_records = [{
        "source_record_key": f"research_tasks:{row['task_id']}",
        "rider_id": rider_id(row["worlddb_rider_id"], row.get("identity_generation")),
        "source_worlddb_rider_id": row["worlddb_rider_id"],
        "identity_generation": int(row.get("identity_generation") or 0),
        "run_id": run_ids.get(str(row.get("run_id"))) if row.get("run_id") is not None else None,
        "state": row["state"],
        "source_scope": [],
        "reason": row["reason"],
        "reference_date": parse_date(row["reference_date"]),
        "assigned_to_user_id": None,
        "legacy_assignee": row.get("assigned_to") or None,
        "created_at": _dt(row["created_at"]),
        "updated_at": _dt(row["updated_at"]),
        "next_retry_at": _dt(row.get("next_retry_date")),
        "outcome": row.get("outcome") or "",
    } for row in tasks]
    task_ids, imported["research_tasks"] = _insert_rows(
        connection, ActivityMonitorTask, task_records,
        lambda row: row["source_record_key"],
    )

    task_events = table_rows.get("task_events", [])
    event_records = [{
        "source_record_key": f"task_events:{row['event_id']}",
        "task_id": task_ids.get(f"research_tasks:{row['task_id']}"),
        "old_state": row.get("old_state") or None,
        "new_state": row["new_state"],
        "actor_user_id": None,
        "legacy_actor": row.get("actor") or None,
        "occurred_at": _dt(row["occurred_at"]),
        "notes": row.get("notes") or "",
    } for row in task_events]
    _, imported["task_events"] = _insert_rows(connection, ActivityMonitorTaskEvent, event_records)

    cycle_rows = table_rows.get("activity_source_cycle_history", [])
    legacy_records = [{
        "record_type": "activity_source_cycle_history",
        "source_record_key": str(row["history_id"]),
        "rider_id": rider_id(row["worlddb_rider_id"], row.get("identity_generation")),
        "occurred_at": _dt(row.get("checked_at")),
        "record_data": dict(row),
        "imported_at": now,
    } for row in cycle_rows]
    finding_rows = table_rows.get("research_findings", [])
    legacy_records.extend({
        "record_type": "research_findings",
        "source_record_key": str(row["fingerprint"]),
        "rider_id": rider_id(row["worlddb_rider_id"], row.get("identity_generation")),
        "occurred_at": _dt(row.get("research_date")),
        # import_file is a machine-local path and must not become shared data.
        "record_data": {key: value for key, value in row.items() if key != "import_file"},
        "imported_at": now,
    } for row in finding_rows)
    _insert_rows(
        connection,
        ActivityMonitorLegacyRecord,
        legacy_records,
        lambda row: (row["record_type"], row["source_record_key"]),
    )
    imported["activity_source_cycle_history"] = len(cycle_rows)
    imported["research_findings"] = len(finding_rows)

    total_unlinked = sum(
        1 for collection in (check_records, api_records, result_records, profile_records, state_records, schedule_records, eligibility_records, alert_records, task_records, legacy_records)
        for row in collection if row.get("rider_id") is None
    )
    connection.execute(ActivityMonitorHistoryImport.__table__.update().where(
        ActivityMonitorHistoryImport.__table__.c.source_sha256 == source_hash
    ).values(
        status="completed",
        completed_at=datetime.utcnow(),
        imported_row_counts=dict(imported),
        unlinked_record_count=total_unlinked,
    ))
    return dict(imported), total_unlinked


def backfill_imported_activity_history(namespace_engine, source_path: Path, *, apply: bool = False):
    """Append new durable Arie Tool schedule history to an imported namespace.

    This is a tail backfill, not a second full import. It verifies that the
    source rider identities match the already imported roster, reports missing
    cycle/schedule/finding keys in read-only mode, and only inserts missing
    rows when explicitly applied.
    """
    source_path = Path(source_path)
    if not source_path.is_file():
        raise RuntimeError("Activity Monitor source database does not exist.")
    source_hash = _sha256(source_path)
    _validate_source(source_path)

    with closing(_source_connection(source_path)) as source_db:
        source_tables = {
            row[0] for row in source_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        source_riders = _rows(source_db, "riders")
        cycle_rows = _rows(source_db, "activity_source_cycle_history") if "activity_source_cycle_history" in source_tables else []
        schedule_rows = _rows(source_db, "rider_schedule_history") if "rider_schedule_history" in source_tables else []
        finding_rows = _rows(source_db, "research_findings") if "research_findings" in source_tables else []

    source_identities = {
        (int(row["worlddb_rider_id"]), int(row.get("identity_generation") or 0))
        for row in source_riders
    }

    def completed_import(connection, *, lock=False):
        statement = select(ActivityMonitorHistoryImport.__table__).where(
            ActivityMonitorHistoryImport.__table__.c.status == "completed"
        ).order_by(ActivityMonitorHistoryImport.__table__.c.completed_at.desc()).limit(1)
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise RuntimeError("This namespace has no completed Activity Monitor history import.")
        return row

    def read_missing(connection):
        target_identities = {
            (int(source_id), int(generation or 0))
            for source_id, generation in connection.execute(select(
                ActivityMonitorRider.__table__.c.source_worlddb_rider_id,
                ActivityMonitorRider.__table__.c.identity_generation,
            )).all()
            if source_id is not None
        }
        if source_identities != target_identities:
            raise RuntimeError("Source rider identities do not exactly match the imported namespace roster.")
        cycle_keys = set(connection.execute(select(
            ActivityMonitorLegacyRecord.__table__.c.source_record_key
        ).where(
            ActivityMonitorLegacyRecord.__table__.c.record_type == "activity_source_cycle_history"
        )).scalars().all())
        finding_keys = set(connection.execute(select(
            ActivityMonitorLegacyRecord.__table__.c.source_record_key
        ).where(
            ActivityMonitorLegacyRecord.__table__.c.record_type == "research_findings"
        )).scalars().all())
        schedule_keys = set(connection.execute(select(
            ActivityMonitorRiderSchedule.__table__.c.source_record_key
        )).scalars().all())
        missing_cycles = [row for row in cycle_rows if str(row["history_id"]) not in cycle_keys]
        missing_schedules = [row for row in schedule_rows
                             if f"rider_schedule_history:{row['event_id']}" not in schedule_keys]
        missing_findings = [row for row in finding_rows if str(row["fingerprint"]) not in finding_keys]
        return missing_cycles, missing_schedules, missing_findings

    if not apply:
        with namespace_engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                completed_import(connection)
                missing_cycles, missing_schedules, missing_findings = read_missing(connection)
        print(f"Completed source cycles: {len(cycle_rows):,}; missing from namespace: {len(missing_cycles):,}")
        print(f"Schedule snapshots: {len(schedule_rows):,}; missing from namespace: {len(missing_schedules):,}")
        print(f"Research findings: {len(finding_rows):,}; missing from namespace: {len(missing_findings):,}")
        print("Dry run only. No namespace data was changed. Use --apply to append missing history.")
        return {
            "missing_cycles": len(missing_cycles),
            "missing_schedules": len(missing_schedules),
            "missing_findings": len(missing_findings),
        }

    with closing(_source_connection(source_path)) as source_db:
        with namespace_engine.begin() as connection:
            ledger = completed_import(connection, lock=True)
            missing_cycles, missing_schedules, missing_findings = read_missing(connection)
            rider_ids = {
                (int(source_id), int(generation or 0)): target_id
                for target_id, source_id, generation in connection.execute(select(
                    ActivityMonitorRider.__table__.c.id,
                    ActivityMonitorRider.__table__.c.source_worlddb_rider_id,
                    ActivityMonitorRider.__table__.c.identity_generation,
                )).all()
                if source_id is not None
            }
            now = datetime.utcnow()
            legacy_records = [{
                "record_type": "activity_source_cycle_history",
                "source_record_key": str(row["history_id"]),
                "rider_id": rider_ids.get((int(row["worlddb_rider_id"]), int(row.get("identity_generation") or 0))),
                "occurred_at": _dt(row.get("checked_at")),
                "record_data": dict(row),
                "imported_at": now,
            } for row in missing_cycles]
            legacy_records.extend({
                "record_type": "research_findings",
                "source_record_key": str(row["fingerprint"]),
                "rider_id": rider_ids.get((int(row["worlddb_rider_id"]), int(row.get("identity_generation") or 0))),
                "occurred_at": _dt(row.get("research_date")),
                # import_file is a machine-local path and must not become shared data.
                "record_data": {key: value for key, value in row.items() if key != "import_file"},
                "imported_at": now,
            } for row in missing_findings)
            for offset in range(0, len(legacy_records), 1000):
                statement = pg_insert(ActivityMonitorLegacyRecord.__table__).values(
                    legacy_records[offset:offset + 1000]
                ).on_conflict_do_nothing(constraint="uq_activity_monitor_legacy_source_record").returning(
                    ActivityMonitorLegacyRecord.__table__.c.id
                )
                connection.execute(statement).scalars().all()

            schedule_records = [{
                "source_record_key": f"rider_schedule_history:{row['event_id']}",
                "rider_id": rider_ids.get((int(row["worlddb_rider_id"]), int(row.get("identity_generation") or 0))),
                "source_worlddb_rider_id": row["worlddb_rider_id"],
                "identity_generation": int(row.get("identity_generation") or 0),
                "reference_date": parse_date(row["reference_date"]),
                "recorded_at": _dt(row["recorded_at"]),
                "schedule_data": _json(row["schedule_json"], {}),
            } for row in missing_schedules]
            inserted_schedules = 0
            for offset in range(0, len(schedule_records), 1000):
                statement = pg_insert(ActivityMonitorRiderSchedule.__table__).values(
                    schedule_records[offset:offset + 1000]
                ).on_conflict_do_nothing().returning(ActivityMonitorRiderSchedule.__table__.c.id)
                inserted_schedules += len(connection.execute(statement).scalars().all())

            inserted_cycles = len(missing_cycles)
            inserted_findings = len(missing_findings)
            if inserted_cycles or inserted_schedules or inserted_findings:
                counts = dict(ledger["imported_row_counts"] or {})
                counts["activity_source_cycle_history_backfilled"] = (
                    int(counts.get("activity_source_cycle_history_backfilled") or 0) + inserted_cycles
                )
                counts["rider_schedule_history_backfilled"] = (
                    int(counts.get("rider_schedule_history_backfilled") or 0) + inserted_schedules
                )
                counts["research_findings_backfilled"] = (
                    int(counts.get("research_findings_backfilled") or 0) + inserted_findings
                )
                counts["last_backfill_source_sha256"] = source_hash
                connection.execute(update(ActivityMonitorHistoryImport.__table__).where(
                    ActivityMonitorHistoryImport.__table__.c.id == ledger["id"]
                ).values(imported_row_counts=counts))

    print(f"Appended source cycles: {inserted_cycles:,}")
    print(f"Appended schedule snapshots: {inserted_schedules:,}")
    print(f"Appended research findings: {inserted_findings:,}")
    return {
        "inserted_cycles": inserted_cycles,
        "inserted_schedules": inserted_schedules,
        "inserted_findings": inserted_findings,
    }


def run_history_import(namespace_engine, source_path: Path, *, apply: bool = False, actor_user_id=None):
    """Validate and optionally import history; no mutation unless ``apply``."""
    source_path = Path(source_path)
    if not source_path.is_file():
        raise RuntimeError("Activity Monitor source database does not exist.")
    source_hash = _sha256(source_path)
    inventory, schema_hash, row_report = _validate_source(source_path)
    with closing(_source_connection(source_path)) as source_db:
        source_riders = _rows(source_db, "riders")
        with namespace_engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                table_names = set(TARGET_MODELS)
                existing = connection.execute(text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                    "AND table_name = ANY(:names)"
                ), {"names": list(table_names)}).scalars().all()
                if set(existing) != table_names:
                    raise RuntimeError("Activity Monitor namespace schema is incomplete; import stopped.")
                existing_import = connection.execute(select(ActivityMonitorHistoryImport.__table__).where(
                    ActivityMonitorHistoryImport.__table__.c.source_sha256 == source_hash
                )).mappings().one_or_none()
                if existing_import and existing_import["status"] == "completed":
                    print("This exact source database has already been imported; no changes made.")
                    return {"already_imported": True, "row_counts": dict(inventory)}
                counts = connection.execute(text(
                    "SELECT " + ", ".join(
                        f"(SELECT COUNT(*) FROM {table}) AS {table}" for table in TARGET_MODELS
                    )
                )).mappings().one()
                nonempty = [table for table in TARGET_MODELS if counts[table] and table != "activity_monitor_schedules"]
                if nonempty and not existing_import:
                    raise RuntimeError("Target Activity Monitor tables are not empty and no matching import ledger exists.")
                cdb_rows = connection.execute(text(
                    "SELECT first_cycling_id, birth_date FROM rider "
                    "WHERE first_cycling_id IS NOT NULL"
                )).mappings().all()
                by_fc = defaultdict(list)
                for row in cdb_rows:
                    by_fc[int(row["first_cycling_id"])].append(row)
                source_counts = Counter(
                    int(row["fc_id"]) for row in source_riders
                    if row.get("fc_id") is not None and row.get("fc_id_quality") == "valid"
                    and not _bool(row.get("identity_hold"))
                )
                matched = 0
                ambiguous = 0
                for rider in source_riders:
                    fc_id = rider.get("fc_id")
                    if fc_id is None or rider.get("fc_id_quality") != "valid" or _bool(rider.get("identity_hold")):
                        continue
                    target = by_fc.get(int(fc_id), [])
                    if source_counts[int(fc_id)] != 1 or len(target) > 1:
                        ambiguous += 1
                    elif len(target) == 1 and parse_date(rider.get("birthdate")) == parse_cdb_date(target[0]["birth_date"]):
                        matched += 1
                rider_unlinked = len(source_riders) - matched
    print(f"Source SHA-256: {source_hash}")
    print(f"Source schema fingerprint: {schema_hash}")
    print(f"Source rows: {sum(inventory.values()):,} across {len(inventory)} tables")
    print(f"Current CDB identities corroborated by FirstCycling ID + birth date: {matched:,}")
    print(f"Historical identities without a current CDB match: {rider_unlinked:,}; ambiguous: {ambiguous:,}")
    print(f"Source checks without a rider reference: {sum(item['missing_rider_references'] for item in row_report):,}")
    if not apply:
        print("Dry run only. No namespace rows were changed. Use --apply after reviewing this report.")
        return {"already_imported": False, "row_counts": dict(inventory)}

    try:
        with closing(_source_connection(source_path)) as source_db:
            with namespace_engine.begin() as connection:
                existing_import = connection.execute(select(ActivityMonitorHistoryImport.__table__).where(
                    ActivityMonitorHistoryImport.__table__.c.source_sha256 == source_hash
                )).mappings().one_or_none()
                if existing_import and existing_import["status"] == "completed":
                    print("This exact source database has already been imported; no changes made.")
                    return {"already_imported": True, "row_counts": dict(inventory)}
                if existing_import:
                    connection.execute(text(
                        "DELETE FROM activity_monitor_history_imports WHERE source_sha256=:source_hash"
                    ), {"source_hash": source_hash})
                counts = connection.execute(text(
                    "SELECT " + ", ".join(
                        f"(SELECT COUNT(*) FROM {table}) AS {table}" for table in TARGET_MODELS
                    )
                )).mappings().one()
                nonempty = [table for table in TARGET_MODELS if counts[table] and table != "activity_monitor_schedules"]
                if nonempty:
                    raise RuntimeError("Target Activity Monitor tables became non-empty after dry run; import stopped.")
                imported, unlinked = _import_records(
                    connection, source_db, inventory, source_hash, schema_hash, actor_user_id
                )
    except Exception as exc:
        # The data transaction has rolled back. Record failure separately so a
        # retry remains possible and the attempted import is visible.
        with namespace_engine.begin() as audit_connection:
            existing = audit_connection.execute(select(ActivityMonitorHistoryImport.__table__).where(
                ActivityMonitorHistoryImport.__table__.c.source_sha256 == source_hash
            )).mappings().one_or_none()
            if existing:
                audit_connection.execute(text(
                    "UPDATE activity_monitor_history_imports SET status='failed', completed_at=:completed_at, "
                    "error_summary=:error WHERE source_sha256=:source_hash"
                ), {"completed_at": datetime.utcnow(), "error": f"{type(exc).__name__}: import transaction rolled back", "source_hash": source_hash})
            else:
                audit_connection.execute(insert(ActivityMonitorHistoryImport.__table__).values(
                    source_sha256=source_hash,
                    source_schema_fingerprint=schema_hash,
                    status="failed",
                    initiated_by_user_id=actor_user_id,
                    started_at=datetime.utcnow(),
                    completed_at=datetime.utcnow(),
                    source_row_counts=_source_counts(source_db, inventory),
                    imported_row_counts={},
                    unlinked_record_count=0,
                    error_summary=f"{type(exc).__name__}: import transaction rolled back",
                ))
        raise
    print("Import committed in one transaction.")
    print(f"Imported rows: {sum(imported.values()):,}; unlinked rider records: {unlinked:,}")
    for table_name, count in sorted(imported.items()):
        print(f"  {table_name}: {count:,}")
    return {"already_imported": False, "row_counts": imported, "unlinked": unlinked}
