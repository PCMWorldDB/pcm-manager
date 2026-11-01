"""Offline normalizer for FirstCycling Activity Monitor rider JSON.

This module accepts an already obtained JSON payload. It never performs HTTP
requests or writes to the Activity Monitor database. FirstCycling's API history
is partial coverage, so its rows are evidence of activity, not evidence of
inactivity when no recent row is present.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime


_SUPPORTED_TYPES = {"STAGE", "OVERALL"}
_CONFIRMED_POSITIONS = {"DNF", "OOT", "OTL"}


def _positive_int(value, label, *, optional=False):
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if parsed <= 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a positive integer.")
    return parsed


def _result_date(value):
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("Result date must contain exactly one date.")
        value = value[0]
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid FirstCycling result date: {value!r}") from exc
    if parsed.isoformat() != str(value):
        raise ValueError(f"FirstCycling date is not ISO YYYY-MM-DD: {value!r}")
    return parsed


def _validated_rows(all_results):
    """Validate the full feed before returning any evidence rows."""
    if not isinstance(all_results, dict) or not all_results:
        raise ValueError("FirstCycling response has no usable allResults history.")

    rows = []
    stage_editions = set()
    for season_text, items in all_results.items():
        if not re.fullmatch(r"\d{4}", str(season_text)) or not isinstance(items, list):
            raise ValueError("allResults must map four-digit seasons to arrays.")
        season = int(season_text)
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("allResults contains a non-object row.")
            row_type = str(item.get("type") or "").upper()
            if row_type not in _SUPPORTED_TYPES:
                raise ValueError(f"Unsupported result type: {row_type!r}")
            race_id = _positive_int(item.get("raceId"), "raceId")
            edition_id = _positive_int(item.get("editionId"), "editionId")
            stage_id = _positive_int(item.get("stageId"), "stageId", optional=True)
            race_name = str(item.get("raceName") or "").strip()
            if not race_name:
                raise ValueError("A result row has no raceName.")
            result_day = _result_date(item.get("date"))
            if result_day.year != season:
                raise ValueError(f"Result date {result_day} does not match season {season}.")
            if row_type == "STAGE":
                stage_editions.add(edition_id)
            rows.append({
                "season": season,
                "race_id": race_id,
                "edition_id": edition_id,
                "stage_id": stage_id,
                "race_name": race_name,
                "category": str(item.get("cat") or "").strip(),
                "date": result_day,
                "source_type": row_type,
                "stage_name": str(item.get("stageName") or "").strip(),
                "position": str(item.get("pos") or "").strip().upper(),
                "item": item,
            })

    # An empty list for every season is not enough to conclude anything about
    # activity. Treat it as an incomplete/empty response for explicit review.
    if not rows:
        raise ValueError("FirstCycling allResults contained no result rows.")

    for row in rows:
        position = row["position"]
        numeric = bool(re.fullmatch(r"\d+", position))
        has_started = numeric or position in _CONFIRMED_POSITIONS
        if position == "DNS":
            participation = "dns_only"
        elif position == "DSQ":
            participation = "confirmed_participation"
        elif row["source_type"] == "STAGE":
            participation = "confirmed_participation" if has_started else "classification_only"
        else:
            is_stage_race = row["edition_id"] in stage_editions or bool(
                re.match(r"^2(?:\.|$)", row["category"], re.I)
            )
            participation = (
                "classification_only" if is_stage_race else
                "confirmed_participation" if has_started else
                "classification_only"
            )
        stage_match = re.search(r"\b(?:stage|prologue)\s+(\d+)\b", row["stage_name"], re.I)
        row["stage_number"] = int(stage_match.group(1)) if stage_match else None
        row["participation_status"] = participation
    return rows


def normalize_activity_payload(payload: dict, *, expected_fc_id: int,
                               rider_id: int,
                               source_worlddb_rider_id: int | None,
                               identity_generation: int,
                               source_check_id: int | None = None,
                               imported_at: datetime | None = None) -> dict:
    """Validate and map an FC ``allResults`` response to monitor evidence.

    The expected FC ID must come from the selected namespace rider record.
    Identity mismatch and unusable feeds return explicit statuses and no rows.
    A successful response always reports partial coverage.
    """
    try:
        expected_fc_id = _positive_int(expected_fc_id, "expected_fc_id")
        if not isinstance(payload, dict) or not isinstance(payload.get("description"), dict):
            raise ValueError("FirstCycling response has no description object.")
        returned_fc_id = _positive_int(payload["description"].get("id"), "description.id")
    except ValueError as exc:
        return {"status": "invalid_response", "coverage_status": "unverified",
                "identity_verified": False, "message": str(exc), "rows": []}

    if returned_fc_id != expected_fc_id:
        return {"status": "identity_mismatch", "coverage_status": "unverified",
                "identity_verified": False,
                "message": f"Selected FirstCycling ID {expected_fc_id}; response contained {returned_fc_id}.",
                "rows": []}

    try:
        source_rows = _validated_rows(payload.get("allResults"))
    except ValueError as exc:
        return {"status": "invalid_response", "coverage_status": "unverified",
                "identity_verified": True, "message": str(exc), "rows": []}

    now = imported_at or datetime.utcnow()
    mapped = []
    for row in source_rows:
        stable_fields = {
            "fc_id": returned_fc_id,
            "generation": identity_generation,
            "edition_id": row["edition_id"],
            "stage_id": row["stage_id"] or 0,
            "type": row["source_type"],
            "date": row["date"].isoformat(),
            "race_id": row["race_id"],
        }
        stable_value = json.dumps(stable_fields, sort_keys=True, separators=(",", ":"))
        source_key = hashlib.sha256(stable_value.encode("utf-8")).hexdigest()
        profile_url = f"https://firstcycling.com/rider.php?r={returned_fc_id}"
        mapped.append({
            "source_result_key": f"fc-json:{rider_id}:{identity_generation}:{source_key}",
            "rider_id": rider_id,
            "source_worlddb_rider_id": source_worlddb_rider_id,
            "identity_generation": identity_generation,
            "first_cycling_id": returned_fc_id,
            "season": row["season"],
            "race_id": row["race_id"],
            "edition_id": row["edition_id"],
            "race_name": row["race_name"],
            "race_category": row["category"],
            "race_url": profile_url,
            "result_date": row["date"],
            "stage_id": row["stage_id"],
            "stage_number": row["stage_number"],
            "stage_name": row["stage_name"],
            "row_type": "stage_result" if row["source_type"] == "STAGE" else "classification",
            "result_position": row["position"],
            "gc_position": "",
            "participation_status": row["participation_status"],
            "source": "firstcycling",
            "source_check_id": source_check_id,
            "identity_verified": True,
            "imported_at": now,
            "source_payload": row["item"],
        })
    return {
        "status": "success",
        "coverage_status": "partial",
        "identity_verified": True,
        "message": (
            f"Validated {len(mapped)} FirstCycling allResults rows. "
            "Feed coverage is partial; absence of a recent row is not proof of inactivity."
        ),
        "rows": mapped,
    }
