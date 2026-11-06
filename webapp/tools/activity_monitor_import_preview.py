"""Read-only preview of mapping an Arie Activity Monitor database to a namespace.

This tool never writes to the source monitor or to PCM Manager databases. It
prints aggregate counts only; it does not print rider names or URLs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path

DEFAULT_MONITOR_DB = Path.home() / "AppData/Local/WorldDB Activity Monitor/worlddb_activity.sqlite"

SOURCE_TABLE_DESTINATIONS = {
    "riders": "activity_monitor_riders (match by FirstCycling ID + birth date)",
    "cdb_imports": "activity_monitor_cdb_imports (drop source_path)",
    "roster_snapshots": "activity_monitor_roster_snapshots",
    "race_results": "activity_monitor_results",
    "source_checks": "activity_monitor_source_checks (keep only a safe PCS rider request path)",
    "fc_collector_runs": "activity_monitor_runs",
    "fc_api_downloads": "activity_monitor_source_checks (drop cache_path)",
    "pcs_profile_state": "activity_monitor_profile_links (preserve link provenance)",
    "rider_profile_history": "activity_monitor_profile_links (verified-link history)",
    "manual_reviews": "activity_monitor_reviews",
    "manual_review_history": "activity_monitor_review_events",
    "identity_alerts": "activity_monitor_identity_alerts",
    "eligibility_events": "activity_monitor_eligibility_events",
    "rider_schedule_history": "activity_monitor_rider_schedules",
    "research_tasks": "activity_monitor_tasks",
    "task_events": "activity_monitor_task_events",
    "activity_source_cycle_history": "activity_monitor_legacy_records (durable completed-check scheduling history)",
    "activity_source_cycle_pending": "exclude ephemeral in-progress queue state (rebuild for online worker)",
    "research_findings": "activity_monitor_legacy_records (normalize in a later step)",
    "scheduler_runs": "activity_monitor_legacy_records (preserve run history)",
    "sheet_baselines": "activity_monitor_legacy_records (preserve integration history)",
    "sheet_exports": "activity_monitor_legacy_records (preserve integration history)",
    "sheet_import_log": "activity_monitor_legacy_records (drop source_file)",
    "sheet_receipts": "activity_monitor_legacy_records (preserve integration history)",
    "legacy_activity_research_checks": "activity_monitor_legacy_records (retain compatibility history)",
    "legacy_general_eligibility_events": "activity_monitor_legacy_records (retain compatibility history)",
    "legacy_general_review_history": "activity_monitor_legacy_records (retain compatibility history)",
    "legacy_general_reviews": "activity_monitor_legacy_records (retain compatibility history)",
    "legacy_task_monitor_scopes": "activity_monitor_legacy_records (retain compatibility history)",
    "fc_collector_state": "exclude machine-local collector state",
    "pcs_collector_state": "exclude machine-local collector state",
    "monitor_metadata": "review keys before deciding; do not copy values blindly",
}

SOURCE_EXCLUDED_COLUMNS = {
    "cdb_imports": ("source_path",),
    "fc_api_downloads": ("cache_path",),
    "fc_collector_runs": ("owner_pid",),
}

# These checks deliberately report counts only. They are a first migration
# review, not a complete conversion validator or an import allow-list.
ROW_VALIDATION_RULES = {
    "riders": {
        "key": ("worlddb_rider_id", "identity_generation"),
        "required": ("worlddb_rider_id", "identity_generation"),
        "dates": ("birthdate", "last_seen_cdb_at"),
        "rider_ref": None,
    },
    "cdb_imports": {
        "key": ("import_id",),
        "required": ("import_id", "imported_at", "reference_date", "source_sha256", "rider_count"),
        "dates": ("imported_at", "reference_date"),
        "rider_ref": None,
    },
    "roster_snapshots": {
        "key": ("import_id", "worlddb_rider_id"),
        "required": ("import_id", "worlddb_rider_id", "data_json"),
        "dates": (),
        "rider_ref": "worlddb_rider_id",
    },
    "race_results": {
        "key": ("result_key",),
        "required": ("result_key", "fc_id", "year", "race_name", "row_type", "participation_status", "source", "imported_at", "identity_generation"),
        "dates": ("result_date", "imported_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "source_checks": {
        "key": ("check_id",),
        "required": ("check_id", "source", "source_status", "coverage_status", "checked_at"),
        "dates": ("checked_at",),
        "rider_ref": "worlddb_rider_id",
    },
    "fc_collector_runs": {
        "key": ("run_id",),
        "required": ("run_id", "status"),
        "dates": ("started_at", "finished_at", "heartbeat_at"),
        "rider_ref": None,
    },
    "fc_api_downloads": {
        "key": ("attempt_id",),
        "required": ("attempt_id", "attempted_at", "status"),
        "dates": ("attempted_at",),
        "rider_ref": "worlddb_rider_id",
    },
    "pcs_profile_state": {
        "key": ("worlddb_rider_id", "identity_generation"),
        "required": ("worlddb_rider_id", "identity_generation"),
        "dates": ("checked_at", "override_updated_at", "approved_at", "override_error_at", "cdb_link_updated_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "rider_profile_history": {
        "key": ("profile_id",),
        "required": ("profile_id", "worlddb_rider_id", "profile_url", "verified_on"),
        "dates": ("verified_on",),
        "rider_ref": "worlddb_rider_id",
    },
    "manual_reviews": {
        "key": ("worlddb_rider_id", "identity_generation", "review_kind"),
        "required": ("worlddb_rider_id", "identity_generation", "review_status", "updated_at", "review_kind"),
        "dates": ("last_reviewed_at", "updated_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "manual_review_history": {
        "key": ("history_id",),
        "required": ("history_id", "worlddb_rider_id", "identity_generation", "review_status", "recorded_at", "review_kind"),
        "dates": ("reviewed_at", "recorded_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "identity_alerts": {
        "key": ("alert_id",),
        "required": ("alert_id", "worlddb_rider_id", "created_at", "reason", "status"),
        "dates": ("created_at", "resolved_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "eligibility_events": {
        "key": ("event_id",),
        "required": ("event_id", "worlddb_rider_id", "reference_date", "recorded_at", "new_eligible", "reason"),
        "dates": ("reference_date", "recorded_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "rider_schedule_history": {
        "key": ("event_id",),
        "required": ("event_id", "worlddb_rider_id", "identity_generation", "reference_date", "recorded_at", "schedule_json"),
        "dates": ("reference_date", "recorded_at"),
        "rider_ref": "worlddb_rider_id",
    },
    "research_tasks": {
        "key": ("task_id",),
        "required": ("task_id", "identity_generation", "state", "reason", "reference_date", "created_at", "updated_at"),
        "dates": ("reference_date", "created_at", "updated_at", "next_retry_date"),
        "rider_ref": "worlddb_rider_id",
    },
    "task_events": {
        "key": ("event_id",),
        "required": ("event_id", "task_id", "new_state", "occurred_at"),
        "dates": ("occurred_at",),
        "rider_ref": None,
    },
    "activity_source_cycle_history": {
        "key": ("history_id",),
        "required": ("history_id", "worlddb_rider_id", "identity_generation", "checked_at", "source_mode"),
        "dates": ("checked_at", "latest_participation_date"),
        "rider_ref": "worlddb_rider_id",
    },
    "scheduler_runs": {
        "key": ("run_id",),
        "required": ("run_id", "job_name", "status", "reference_date"),
        "dates": ("reference_date", "started_at", "finished_at"),
        "rider_ref": None,
    },
    "research_findings": {
        "key": ("fingerprint",),
        "required": ("finding_id", "fingerprint", "worlddb_rider_id", "race_date", "race_name", "source_name", "research_status", "imported_at"),
        "dates": ("race_date", "research_date", "imported_at"),
        "rider_ref": "worlddb_rider_id",
    },
}

JSON_VALIDATION_FIELDS = {
    "cdb_imports": ("summary_json",),
    "roster_snapshots": ("data_json",),
    "identity_alerts": ("old_identity_json", "new_identity_json"),
    "manual_reviews": ("supporting_links",),
    "manual_review_history": ("supporting_links",),
    "rider_schedule_history": ("schedule_json",),
    "scheduler_runs": ("settings_json",),
    "research_findings": ("checked_sources", "newer_evidence"),
}

# These values need a semantic transform before they fit the target column.
# Keep the full source string separately so a shortened status never discards
# the historical detail.
TRANSFORMED_TEXT_VALIDATION_FIELDS = {
    "pcs_profile_state": {
        "check_result": {
            "target_field": "check_result",
            "detail_field": "check_result_detail",
            "separator": "; latest ",
            "limit": 40,
        },
    },
}

# Source column -> (target field limit, target field name). Only direct text
# mappings are checked here; transformed/composed fields remain explicit review.
TEXT_LENGTH_VALIDATION_FIELDS = {
    "riders": {
        "first_name": (120, "first_name"),
        "surname": (160, "last_name"),
        "nationality": (100, "nationality"),
        "nationality_code": (10, "nationality_code"),
        "team_name": (255, "team_name"),
    },
    "race_results": {
        "result_key": (200, "source_result_key"),
        "race_name": (255, "race_name"),
        "race_category": (100, "race_category"),
        "race_url": (1000, "race_url"),
        "stage_name": (255, "stage_name"),
        "row_type": (40, "row_type"),
        "result_position": (40, "result_position"),
        "gc_position": (40, "gc_position"),
        "participation_status": (40, "participation_status"),
        "source": (20, "source"),
    },
    "source_checks": {
        "source": (20, "source"),
        "source_status": (40, "source_status"),
        "coverage_status": (40, "coverage_status"),
    },
    "fc_collector_runs": {"status": (30, "status")},
    "fc_api_downloads": {"status": (40, "source_status")},
    "pcs_profile_state": {
        "url_override": (500, "override_url"),
        "override_input": (1000, "entered_value"),
        "approved_url": (500, "approved_profile_path"),
        "cdb_export_url": (500, "exported_path"),
        "exported_override": (500, "exported_override"),
        "profile_name": (255, "profile_name"),
        "profile_nationality": (100, "profile_nationality"),
        "profile_slug": (255, "profile_slug"),
        "match_status": (30, "link_status"),
    },
    "rider_profile_history": {"profile_url": (500, "profile_path")},
    "manual_reviews": {
        "review_status": (30, "review_status"),
        "review_kind": (30, "review_kind"),
    },
    "manual_review_history": {
        "review_status": (30, "new_status"),
        "review_kind": (30, "review_kind"),
        "actor": (120, "legacy_actor"),
    },
    "identity_alerts": {"status": (30, "status")},
    "research_tasks": {
        "state": (30, "state"),
        "assigned_to": (120, "legacy_assignee"),
    },
    "task_events": {
        "old_state": (30, "old_state"),
        "new_state": (30, "new_state"),
        "actor": (120, "legacy_actor"),
    },
}

STATUS_VALIDATION_FIELDS = {
    "riders": ("fc_id_quality",),
    "race_results": ("row_type", "participation_status", "source"),
    "source_checks": ("source", "source_status", "coverage_status"),
    "fc_collector_runs": ("status",),
    "fc_api_downloads": ("status",),
    "pcs_profile_state": ("match_status", "check_result"),
    "manual_reviews": ("review_status", "review_kind"),
    "manual_review_history": ("review_status", "review_kind"),
    "identity_alerts": ("status",),
    "research_tasks": ("state",),
    "task_events": ("old_state", "new_state"),
}

CROSS_TABLE_REFERENCES = (
    ("roster_snapshots", "import_id", "cdb_imports", "import_id"),
    ("fc_api_downloads", "run_id", "fc_collector_runs", "run_id"),
    ("identity_alerts", "import_id", "cdb_imports", "import_id"),
    ("eligibility_events", "import_id", "cdb_imports", "import_id"),
    ("race_results", "source_check_id", "source_checks", "check_id"),
    ("research_tasks", "run_id", "fc_collector_runs", "run_id"),
    ("task_events", "task_id", "research_tasks", "task_id"),
)


def validate_row_shapes(path: Path) -> list[dict]:
    """Summarize missing fields, invalid dates, duplicate keys, and orphans."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    reports = []
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "riders" not in tables:
            raise RuntimeError("Monitor database is missing the riders table.")
        for table, rules in ROW_VALIDATION_RULES.items():
            if table not in tables:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")
            }
            total = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            required = [column for column in rules["required"] if column in columns]
            missing_sql = " OR ".join(
                f'"{column}" IS NULL OR TRIM(CAST("{column}" AS TEXT)) = ""'
                for column in required
            )
            missing = (
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE {missing_sql}"
                ).fetchone()[0]
                if missing_sql else 0
            )

            key = [column for column in rules["key"] if column in columns]
            key_present = " AND ".join(
                f'"{column}" IS NOT NULL AND TRIM(CAST("{column}" AS TEXT)) != ""'
                for column in key
            )
            duplicate_groups = duplicate_rows = 0
            missing_key = 0
            if key:
                key_sql = ", ".join(f'"{column}"' for column in key)
                key_missing_sql = " OR ".join(
                    f'"{column}" IS NULL OR TRIM(CAST("{column}" AS TEXT)) = ""'
                    for column in key
                )
                missing_key = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE {key_missing_sql}"
                ).fetchone()[0]
                duplicate_query = (
                    f"SELECT COUNT(*), COALESCE(SUM(n - 1), 0) FROM ("
                    f"SELECT COUNT(*) AS n FROM {quoted} WHERE {key_present} "
                    f"GROUP BY {key_sql} HAVING COUNT(*) > 1)"
                )
                duplicate_groups, duplicate_rows = connection.execute(duplicate_query).fetchone()

            invalid_dates = 0
            for column in rules["dates"]:
                if column not in columns:
                    continue
                column_sql = '"' + column.replace('"', '""') + '"'
                invalid_dates += connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE {column_sql} IS NOT NULL "
                    f"AND TRIM(CAST({column_sql} AS TEXT)) != '' "
                    f"AND date(substr(CAST({column_sql} AS TEXT), 1, 10)) IS NULL"
                ).fetchone()[0]

            missing_rider_refs = orphan_refs = 0
            rider_column = rules["rider_ref"]
            if rider_column and rider_column in columns:
                rider_sql = '"' + rider_column.replace('"', '""') + '"'
                missing_rider_refs = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE {rider_sql} IS NULL"
                ).fetchone()[0]
                orphan_refs = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} s WHERE s.{rider_sql} IS NOT NULL "
                    f"AND NOT EXISTS (SELECT 1 FROM riders r "
                    f"WHERE r.worlddb_rider_id = s.{rider_sql})"
                ).fetchone()[0]

            reports.append({
                "table": table,
                "rows": total,
                "missing_required_rows": missing,
                "missing_key_rows": missing_key,
                "duplicate_key_groups": duplicate_groups,
                "duplicate_key_rows": duplicate_rows,
                "invalid_date_values": invalid_dates,
                "missing_rider_references": missing_rider_refs,
                "orphan_rider_references": orphan_refs,
                "key": tuple(key),
            })
    return reports


def validate_payloads_and_relations(path: Path) -> dict:
    """Inspect JSON, mapped text lengths, status vocabularies, and foreign keys."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    report = {"json": [], "lengths": [], "transformed_lengths": [], "statuses": [], "references": []}
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, fields in JSON_VALIDATION_FIELDS.items():
            if table not in tables:
                continue
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            for field in fields:
                if field not in columns:
                    continue
                quoted_field = '"' + field.replace('"', '""') + '"'
                parsed = invalid = empty = 0
                for (value,) in connection.execute(
                    f"SELECT {quoted_field} FROM {quoted_table}"
                ):
                    if value is None or not str(value).strip():
                        empty += 1
                        continue
                    try:
                        json.loads(value)
                        parsed += 1
                    except (TypeError, ValueError):
                        invalid += 1
                report["json"].append({
                    "table": table, "field": field,
                    "parsed": parsed, "empty": empty, "invalid": invalid,
                })

        for table, fields in TEXT_LENGTH_VALIDATION_FIELDS.items():
            if table not in tables:
                continue
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            for field, (limit, target_field) in fields.items():
                if field not in columns:
                    continue
                quoted_field = '"' + field.replace('"', '""') + '"'
                row = connection.execute(
                    f"SELECT COALESCE(MAX(LENGTH(CAST({quoted_field} AS TEXT))), 0), "
                    f"COALESCE(SUM(CASE WHEN LENGTH(CAST({quoted_field} AS TEXT)) > ? "
                    f"THEN 1 ELSE 0 END), 0) FROM {quoted_table}",
                    (limit,),
                ).fetchone()
                report["lengths"].append({
                    "table": table, "field": field, "target_field": target_field,
                    "limit": limit, "maximum": row[0], "over_limit": row[1],
                })

        for table, fields in TRANSFORMED_TEXT_VALIDATION_FIELDS.items():
            if table not in tables:
                continue
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            for field, transform in fields.items():
                if field not in columns:
                    continue
                quoted_field = '"' + field.replace('"', '""') + '"'
                max_status = max_detail = detail_rows = over_limit = 0
                total = 0
                for (value,) in connection.execute(
                    f"SELECT {quoted_field} FROM {quoted_table} WHERE {quoted_field} IS NOT NULL"
                ):
                    source_value = str(value)
                    status, separator, detail = source_value.partition(transform["separator"])
                    total += 1
                    max_status = max(max_status, len(status))
                    if len(status) > transform["limit"]:
                        over_limit += 1
                    if separator:
                        detail_rows += 1
                        max_detail = max(max_detail, len(detail))
                report["transformed_lengths"].append({
                    "table": table, "field": field,
                    "target_field": transform["target_field"],
                    "detail_field": transform["detail_field"],
                    "rows": total, "detail_rows": detail_rows,
                    "maximum_status_length": max_status,
                    "status_over_limit": over_limit,
                    "maximum_detail_length": max_detail,
                })

        for table, fields in STATUS_VALIDATION_FIELDS.items():
            if table not in tables:
                continue
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            for field in fields:
                if field not in columns:
                    continue
                quoted_field = '"' + field.replace('"', '""') + '"'
                row = connection.execute(
                    f"SELECT COUNT(DISTINCT {quoted_field}), "
                    f"COALESCE(SUM(CASE WHEN {quoted_field} IS NULL OR "
                    f"TRIM(CAST({quoted_field} AS TEXT)) = '' THEN 1 ELSE 0 END), 0), "
                    f"COALESCE(MAX(LENGTH(CAST({quoted_field} AS TEXT))), 0) "
                    f"FROM {quoted_table}"
                ).fetchone()
                report["statuses"].append({
                    "table": table, "field": field,
                    "distinct_values": row[0], "empty_rows": row[1],
                    "maximum_length": row[2],
                })

        for child, child_field, parent, parent_field in CROSS_TABLE_REFERENCES:
            if child not in tables or parent not in tables:
                continue
            child_table = '"' + child.replace('"', '""') + '"'
            parent_table = '"' + parent.replace('"', '""') + '"'
            child_column = '"' + child_field.replace('"', '""') + '"'
            parent_column = '"' + parent_field.replace('"', '""') + '"'
            child_columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({child_table})")
            }
            parent_columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({parent_table})")
            }
            if child_field not in child_columns or parent_field not in parent_columns:
                continue
            missing = connection.execute(
                f"SELECT COUNT(*) FROM {child_table} c "
                f"WHERE c.{child_column} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {parent_table} p WHERE p.{parent_column} = c.{child_column})"
            ).fetchone()[0]
            report["references"].append({
                "child": child, "child_field": child_field,
                "parent": parent, "parent_field": parent_field,
                "unresolved": missing,
            })
    return report


def print_row_validation(path: Path) -> int:
    """Print aggregate row-shape diagnostics without exposing record values."""
    if not path.is_file():
        raise RuntimeError(f"Monitor database was not found at: {path}")
    reports = validate_row_shapes(path)
    payloads = validate_payloads_and_relations(path)
    print("READ-ONLY ROW VALIDATION - aggregate counts only; no records changed")
    print("No rider names, URLs, notes, or source values are displayed.")
    totals = Counter()
    for row in reports:
        totals["tables"] += 1
        for field in (
            "rows", "missing_required_rows", "missing_key_rows",
            "duplicate_key_groups", "duplicate_key_rows",
            "invalid_date_values", "missing_rider_references",
            "orphan_rider_references",
        ):
            totals[field] += row[field]
        key = ",".join(row["key"]) or "unavailable"
        print(
            f"{row['table']}: {row['rows']:,} rows; key=({key}); "
            f"missing required={row['missing_required_rows']:,}; "
            f"missing key={row['missing_key_rows']:,}; "
            f"duplicate groups/extra rows={row['duplicate_key_groups']:,}/"
            f"{row['duplicate_key_rows']:,}; invalid dates={row['invalid_date_values']:,}; "
            f"missing rider references={row['missing_rider_references']:,}; "
            f"unmatched rider references={row['orphan_rider_references']:,}"
        )
    print(
        "TOTAL: {rows:,} rows across {tables:,} reviewed tables; "
        "missing required={missing_required_rows:,}; missing key={missing_key_rows:,}; "
        "duplicate groups/extra rows={duplicate_key_groups:,}/{duplicate_key_rows:,}; "
        "invalid dates={invalid_date_values:,}; missing rider references={missing_rider_references:,}; "
        "unmatched rider references={orphan_rider_references:,}"
        .format(**totals)
    )
    print("These are review counts, not approved import counts; duplicate keys and orphan references need explicit handling.")
    print("\nJSON fields (syntactic validity only; payload contents are not displayed):")
    for row in payloads["json"]:
        print(
            f"  {row['table']}.{row['field']}: valid={row['parsed']:,}; "
            f"empty={row['empty']:,}; invalid={row['invalid']:,}"
        )
    print("\nDirect text mappings against target field limits:")
    for row in payloads["lengths"]:
        print(
            f"  {row['table']}.{row['field']} -> {row['target_field']} "
            f"(limit {row['limit']}): max length={row['maximum']:,}; "
            f"over limit={row['over_limit']:,}"
        )
    print("\nTransformed text mappings (source values omitted):")
    for row in payloads["transformed_lengths"]:
        print(
            f"  {row['table']}.{row['field']} -> {row['target_field']} "
            f"plus {row['detail_field']}: rows={row['rows']:,}; "
            f"rows with detail={row['detail_rows']:,}; "
            f"max status length={row['maximum_status_length']:,}; "
            f"status over limit={row['status_over_limit']:,}; "
            f"max retained detail length={row['maximum_detail_length']:,}"
        )
    print("\nStatus vocabulary inventory (values omitted; target fields are strings, not constrained enums):")
    for row in payloads["statuses"]:
        print(
            f"  {row['table']}.{row['field']}: distinct={row['distinct_values']:,}; "
            f"empty={row['empty_rows']:,}; max length={row['maximum_length']:,}"
        )
    print("\nCross-table references:")
    for row in payloads["references"]:
        print(
            f"  {row['child']}.{row['child_field']} -> "
            f"{row['parent']}.{row['parent_field']}: unresolved={row['unresolved']:,}"
        )
    print("Status names and JSON shapes still need semantic review before import; this pass checks counts, parsing, and field capacity only.")
    return 0


def classify_destination(destination: str) -> str:
    """Classify a table's planned destination without claiming row validation."""
    lowered = destination.lower()
    if lowered.startswith("exclude"):
        return "excluded local/runtime state"
    if lowered.startswith("review"):
        return "review required"
    if "activity_monitor_legacy_records" in lowered:
        return "preserved in legacy holding area"
    return "mapped to shared Activity Monitor tables"


def read_monitor(path: Path) -> tuple[list[dict], Counter]:
    """Read monitor rider identities and history counts without opening write access."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        required = {"riders", "race_results", "source_checks", "rider_schedule_history", "rider_profile_history"}
        found = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = required - found
        if missing:
            raise RuntimeError(f"Monitor database is missing expected tables: {', '.join(sorted(missing))}")

        riders = [
            dict(row)
            for row in connection.execute(
                """SELECT worlddb_rider_id, fc_id, birthdate, fc_id_quality,
                          eligible, present_in_cdb, identity_generation, identity_hold
                   FROM riders"""
            )
        ]

        history = Counter()
        history_tables = {
            "race results": "race_results",
            "source checks": "source_checks",
            "FirstCycling download attempts": "fc_api_downloads",
            "schedule snapshots": "rider_schedule_history",
            "profile history": "rider_profile_history",
            "eligibility changes": "eligibility_events",
            "roster snapshots": "roster_snapshots",
        }
        for label, table in history_tables.items():
            if table in found:
                history[label] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return riders, history


def read_monitor_inventory(path: Path) -> tuple[dict[str, int], str]:
    """Return source table counts and a schema fingerprint without row values."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        tables = connection.execute(
            """SELECT name, sql FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        ).fetchall()
        inventory = {}
        schema_material = []
        for name, definition in tables:
            quoted_name = '"' + name.replace('"', '""') + '"'
            inventory[name] = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_name}"
            ).fetchone()[0]
            schema_material.append(definition or name)
        fingerprint = hashlib.sha256(
            "\n".join(schema_material).encode("utf-8")
        ).hexdigest()
        return inventory, fingerprint


def read_monitor_columns(path: Path) -> dict[str, list[str]]:
    """Read SQLite column names only; never select source row values."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        table_names = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: [row[1] for row in connection.execute(
                f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
            )]
            for table in table_names
        }


def read_monitor_stdin() -> tuple[list[dict], Counter, dict[str, int], str | None, dict[str, list[str]]]:
    """Read only the minimum identity fields as JSON from a read-only pipe."""
    payload = json.load(sys.stdin)
    return (
        payload["riders"],
        Counter(payload["history"]),
        payload.get("table_inventory", {}),
        payload.get("source_schema_fingerprint"),
        payload.get("table_columns", {}),
    )


def list_namespaces() -> int:
    """List namespace slugs with direct read-only SQL, without Flask startup."""
    from sqlalchemy import create_engine, text

    main_url = os.environ.get("DATABASE_URL")
    if not main_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    engine = create_engine(main_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    text("SELECT slug, status, is_deleted, is_template FROM namespaces ORDER BY slug")
                ).mappings().all()
        if not rows:
            print("No namespaces found.")
            return 0
        for row in rows:
            if not row["is_deleted"] and not row["is_template"]:
                print(f"{row['slug']} | {row['status']}")
        return 0
    finally:
        engine.dispose()


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def parse_cdb_date(value) -> date | None:
    """Decode this app's known PCM DYN_cyclist YYYYMMDD integer format."""
    if value is None:
        return None
    raw = str(value).strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def print_source_inventory(path: Path) -> int:
    """Print table names, aggregate counts, and planned handling only."""
    if not path.is_file():
        raise RuntimeError(f"Monitor database was not found at: {path}")
    inventory, fingerprint = read_monitor_inventory(path)
    columns = read_monitor_columns(path)
    print("READ-ONLY IMPORT DRY RUN - schema and counts only; no records changed")
    print(f"Schema fingerprint: {fingerprint}")
    totals = Counter()
    for table_name, row_count in sorted(inventory.items()):
        destination = SOURCE_TABLE_DESTINATIONS.get(
            table_name,
            "review required; do not silently skip",
        )
        disposition = classify_destination(destination)
        totals[disposition] += row_count
        excluded = SOURCE_EXCLUDED_COLUMNS.get(table_name, ())
        suffix = f"; omit columns: {', '.join(excluded)}" if excluded else ""
        print(f"{table_name}: {row_count:,} rows -> {disposition}; {destination}{suffix}")
    print("\nRow totals by planned disposition (counts are table rows, not validated imports):")
    for disposition, total in totals.most_common():
        print(f"  {disposition}: {total:,}")
    print("Field-level validation, transforms, and duplicate handling remain to be reviewed before any importer is written or run.")
    return 0


def preview(namespace_slug: str, monitor_path: Path | None, from_stdin: bool = False) -> int:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    main_url = os.environ.get("DATABASE_URL")
    provisioner_url = os.environ.get("PROVISIONER_DATABASE_URL")
    if not main_url or not provisioner_url:
        raise RuntimeError("Set DATABASE_URL and PROVISIONER_DATABASE_URL in the PCM Manager environment.")
    if from_stdin:
        (
            monitor_riders,
            history_counts,
            table_inventory,
            source_schema_fingerprint,
            table_columns,
        ) = read_monitor_stdin()
    elif monitor_path is None or not monitor_path.is_file():
        raise RuntimeError(f"Monitor database was not found at: {monitor_path}")
    else:
        monitor_riders, history_counts = read_monitor(monitor_path)
        table_inventory, source_schema_fingerprint = read_monitor_inventory(monitor_path)
        table_columns = read_monitor_columns(monitor_path)

    # Use plain SQLAlchemy engines so no Flask startup, migration, or app
    # initialization hook can change a database during this preview.
    main_engine = create_engine(main_url, pool_pre_ping=True)
    namespace_engine = None
    try:
        with main_engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    text(
                        """SELECT slug, database_name, status, is_deleted, is_template
                           FROM namespaces WHERE slug = :slug"""
                    ),
                    {"slug": namespace_slug},
                ).mappings().one_or_none()
                if row is None:
                    raise RuntimeError(f"Namespace '{namespace_slug}' was not found.")
                namespace = dict(row)

        if namespace["is_deleted"] or namespace["is_template"]:
            raise RuntimeError("Selected namespace is deleted or a template; preview stopped.")

        namespace_url = make_url(provisioner_url).set(database=namespace["database_name"])
        namespace_engine = create_engine(namespace_url, pool_pre_ping=True)

        with namespace_engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                cyclists = connection.execute(
                    text("SELECT id, first_cycling_id, birth_date FROM rider")
                ).mappings().all()

        target_by_fc_id = defaultdict(list)
        for cyclist in cyclists:
            if cyclist["first_cycling_id"] is not None:
                target_by_fc_id[cyclist["first_cycling_id"]].append(cyclist)

        counts = Counter()
        ambiguous_target_ids = {key for key, rows in target_by_fc_id.items() if len(rows) > 1}
        usable_monitor_ids = Counter(
            rider["fc_id"]
            for rider in monitor_riders
            if rider["fc_id"] is not None and rider["fc_id_quality"] == "valid" and not rider["identity_hold"]
        )

        for rider in monitor_riders:
            counts["source riders"] += 1
            if rider["present_in_cdb"] and rider["eligible"]:
                counts["current monitor roster"] += 1
            fc_id = rider["fc_id"]
            if (
                fc_id is None
                or rider["fc_id_quality"] != "valid"
                or rider["identity_hold"]
                or usable_monitor_ids[fc_id] != 1
            ):
                counts["manual: source identity needs review"] += 1
                continue
            matches = target_by_fc_id.get(fc_id, [])
            if len(matches) > 1 or fc_id in ambiguous_target_ids:
                counts["ambiguous namespace match"] += 1
                continue
            if not matches:
                counts["no namespace match"] += 1
                continue
            counts["unique FirstCycling ID candidates"] += 1
            source_dob = parse_date(rider["birthdate"])
            target_dob = parse_cdb_date(matches[0]["birth_date"])
            if source_dob is None:
                counts["manual: source birth date missing"] += 1
            elif target_dob is None:
                counts["manual: namespace birth date missing or invalid"] += 1
            elif source_dob != target_dob:
                counts["manual: birth dates differ"] += 1
            else:
                counts["corroborated match candidates"] += 1

        manual_categories = (
            "manual: source identity needs review",
            "manual: source birth date missing",
            "manual: namespace birth date missing or invalid",
            "manual: birth dates differ",
        )
        counts["needs manual review"] = sum(counts[label] for label in manual_categories)

        print("READ-ONLY PREVIEW — no records were written")
        print(f"Namespace: {namespace['slug']} (status: {namespace['status']})")
        print(f"PCM Manager riders: {len(cyclists):,}")
        for label in (
            "source riders",
            "current monitor roster",
            "unique FirstCycling ID candidates",
            "corroborated match candidates",
            "needs manual review",
            "ambiguous namespace match",
            "no namespace match",
        ):
            print(f"{label}: {counts[label]:,}")
        for label in manual_categories:
            if counts[label]:
                print(f"  {label[8:]}: {counts[label]:,}")
        print("History present in source monitor:")
        for label, total in history_counts.items():
            print(f"  {label}: {total:,}")
        if table_inventory:
            print("\nImport table mapping (schema and counts only):")
            disposition_totals = Counter()
            print(f"Schema fingerprint: {source_schema_fingerprint}")
            for table_name, row_count in sorted(table_inventory.items()):
                destination = SOURCE_TABLE_DESTINATIONS.get(
                    table_name,
                    "review required; do not silently skip",
                )
                disposition = classify_destination(destination)
                disposition_totals[disposition] += row_count
                excluded = SOURCE_EXCLUDED_COLUMNS.get(table_name, ())
                suffix = f"; omit columns: {', '.join(excluded)}" if excluded else ""
                print(f"  {table_name}: {row_count:,} rows -> {disposition}; {destination}{suffix}")
                if table_name in table_columns and disposition == "review required":
                    print(f"    source columns: {', '.join(table_columns[table_name])}")
            print("Planned row totals by disposition (not validated import counts):")
            for disposition, total in disposition_totals.most_common():
                print(f"  {disposition}: {total:,}")
        print("\nCandidate counts are not a write approval; a later importer still needs reviewed identity rules and shared namespace-scoped storage.")
        return 0
    finally:
        if namespace_engine is not None:
            namespace_engine.dispose()
        main_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace-slug", help="PCM Manager namespace slug")
    parser.add_argument("--monitor-db", type=Path, default=DEFAULT_MONITOR_DB)
    parser.add_argument("--monitor-json-stdin", action="store_true", help="Read minimal rider identities and history totals from standard input")
    parser.add_argument("--list-namespaces", action="store_true", help="List namespace slugs using a read-only database connection")
    parser.add_argument("--inventory-only", action="store_true", help="Print source table counts and import handling without connecting to PostgreSQL")
    parser.add_argument("--row-validation-only", action="store_true", help="Print aggregate row-shape diagnostics without connecting to PostgreSQL")
    args = parser.parse_args()
    try:
        if args.inventory_only:
            if args.monitor_json_stdin or args.row_validation_only:
                parser.error("--inventory-only cannot be combined with --monitor-json-stdin or --row-validation-only")
            return print_source_inventory(args.monitor_db)
        if args.row_validation_only:
            if args.monitor_json_stdin or args.list_namespaces or args.namespace_slug:
                parser.error("--row-validation-only cannot be combined with namespace or stdin preview options")
            return print_row_validation(args.monitor_db)
        if args.list_namespaces:
            return list_namespaces()
        if not args.namespace_slug:
            parser.error("--namespace-slug is required unless --list-namespaces is used")
        return preview(args.namespace_slug, None if args.monitor_json_stdin else args.monitor_db, args.monitor_json_stdin)
    except Exception as error:
        print(f"Preview stopped safely: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
