"""Transactional storage for validated Activity Monitor source outcomes.

The caller supplies the selected namespace engine. A source check and its
verified evidence are written atomically; retries are idempotent by their
source record keys. This module performs no network requests and creates no
namespace schema.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from webapp.models.ns.activity_monitor import (
    ActivityMonitorResult,
    ActivityMonitorSourceCheck,
)


def persist_source_outcome(engine, *, check: dict, evidence_rows=()) -> dict:
    """Append one source check and, only when safe, its verified result rows.

    ``check`` must contain at least ``source``, ``source_status``,
    ``coverage_status``, ``identity_result``, and ``checked_at``. Results are
    stored only for a successful response with verified identity. Failed,
    incomplete, or identity-uncertain checks remain visible as source checks
    and never create activity evidence.
    """
    values = dict(check)
    required = ("source", "source_status", "coverage_status", "identity_result", "checked_at")
    missing = [key for key in required if values.get(key) is None]
    if missing:
        raise ValueError(f"Source check is missing required fields: {', '.join(missing)}")
    if values["source"] not in {"firstcycling", "pcs"}:
        raise ValueError("Activity Monitor source must be 'firstcycling' or 'pcs'.")
    values.setdefault("source_record_key", f"activity-check:{uuid4().hex}")
    values.setdefault("coverage_status", "unverified")
    values.setdefault("identity_result", "not_checked")
    values.setdefault("checked_at", datetime.utcnow())
    values.setdefault("source_status", "unknown")

    rows = [dict(row) for row in evidence_rows or ()]
    received_count = len(rows)
    can_store_rows = (
        str(values["source_status"] or "").strip().casefold().replace("_", " ")
        in {"success", "results parsed", "results table empty"}
        and values["identity_result"] == "verified"
        and values["coverage_status"] in {"partial", "complete"}
    )
    if can_store_rows:
        if any(row.get("source") != values["source"] for row in rows):
            raise ValueError("Evidence source does not match its source check.")
        if any(row.get("identity_verified") is not True for row in rows):
            raise ValueError("Unverified evidence cannot be attached to a verified source check.")
    else:
        rows = []

    check_table = ActivityMonitorSourceCheck.__table__
    result_table = ActivityMonitorResult.__table__
    with engine.begin() as connection:
        monitor_ready = connection.execute(text("""
            SELECT to_regclass('activity_monitor_source_checks') IS NOT NULL
               AND to_regclass('activity_monitor_results') IS NOT NULL
        """)).scalar_one()
        if not monitor_ready:
            raise RuntimeError("Activity Monitor schema is not installed in this namespace.")

        connection.execute(
            pg_insert(check_table).values(**values).on_conflict_do_nothing(
                index_elements=[check_table.c.source_record_key]
            )
        )
        check_id = connection.execute(
            select(check_table.c.id).where(
                check_table.c.source_record_key == values["source_record_key"]
            )
        ).scalar_one()

        inserted_rows = 0
        if rows:
            for row in rows:
                row["source_check_id"] = check_id
            result = connection.execute(
                pg_insert(result_table).values(rows).on_conflict_do_nothing(
                    index_elements=[result_table.c.source_result_key]
                )
            )
            inserted_rows = max(result.rowcount or 0, 0)

    return {
        "source_check_id": check_id,
        "evidence_rows_received": received_count,
        "evidence_rows_inserted": inserted_rows,
        "evidence_saved": bool(rows),
        "evidence_skipped_reason": None if can_store_rows else (
            "Source outcome or identity is not verified; no activity evidence was attached."
        ),
    }
