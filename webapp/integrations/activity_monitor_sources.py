"""Source collectors for the Activity Monitor worker.

PCS and FirstCycling fetching are independently opt-in. The FC activity
collector uses the separate ``/dora/rider/{id}.json`` allResults feed, not the
standard API client route. No request is made just by importing this module.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import threading
import time
import unicodedata
from contextvars import ContextVar
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import select
from sqlalchemy.orm import Session

from webapp.integrations.activity_monitor_evidence import persist_source_outcome
from webapp.integrations.firstcycling.activity_profile import normalize_activity_payload
from webapp.integrations.pcs.activity_profile import (
    parse_pcs_html,
    to_activity_monitor_evidence,
)
from webapp.models.ns.activity_monitor import (
    ActivityMonitorProfileLink,
    ActivityMonitorRider,
    ActivityMonitorRun,
    ActivityMonitorSourceCheck,
    ActivityMonitorTask,
)

_request_throttle_lock = threading.Lock()
_last_request_started = 0.0
_request_delay_override = ContextVar('activity_monitor_request_delay_override', default=None)


def set_activity_monitor_request_delay(seconds):
    """Set the manual run's base pause for source requests in this task context."""
    return _request_delay_override.set(max(1, int(seconds)))


def reset_activity_monitor_request_delay(token):
    _request_delay_override.reset(token)


class ActivitySourceNotConfigured(RuntimeError):
    """Raised when a task has no enabled source collector."""


class ActivitySourceRetryable(RuntimeError):
    """Raised when a source could not be reached and a later retry may help."""


class _AllowedPCSRedirects(HTTPRedirectHandler):
    allowed_hosts = {"www.procyclingstats.com", "procyclingstats.com"}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in self.allowed_hosts:
            raise URLError("PCS redirected outside the approved HTTPS host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _AllowedFirstCyclingRedirects(HTTPRedirectHandler):
    allowed_hosts = {"www.firstcycling.com", "firstcycling.com"}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in self.allowed_hosts:
            raise URLError("FirstCycling redirected outside the approved HTTPS host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pcs_url(value):
    """Normalize an input to the PCS road-results profile or reject it."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"https://www.procyclingstats.com/{raw.lstrip('/')}")
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in {
        "www.procyclingstats.com", "procyclingstats.com"
    }:
        return None
    if not parsed.path.startswith("/rider/") or parsed.query or parsed.fragment:
        return None
    path = parsed.path.rstrip("/")
    if not path.casefold().endswith("/road"):
        path = f"{path}/road"
    return urlunsplit(("https", "www.procyclingstats.com", path, "", ""))


def _assumed_pcs_profile(first_name, last_name):
    """Build the same unverified name-based PCS candidate used by the UI."""
    raw = f"{first_name or ''} {last_name or ''}"
    raw = ''.join('-' if char in "'’‘`ʼʻ＇´" else char
                  for char in raw if unicodedata.category(char) != 'Cf')
    value = unicodedata.normalize('NFKD', raw)
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().translate(str.maketrans({
        'ø': 'o', 'ł': 'l', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'æ': 'ae',
        'œ': 'oe', 'ħ': 'h', 'ı': 'i', 'ŋ': 'ng', 'ŧ': 't', 'ƶ': 'z',
        'ƒ': 'f', 'ĸ': 'k',
    }))
    slug = re.sub(r'[^a-z0-9-]+', '-', value)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return f"https://www.procyclingstats.com/rider/{slug}/road" if slug else None


def _pcs_enabled():
    try:
        from flask import current_app, has_app_context

        configured = current_app.config.get("ACTIVITY_MONITOR_PCS_ENABLED", False) if has_app_context() else None
    except Exception:
        configured = None
    if configured is None:
        configured = os.getenv("ACTIVITY_MONITOR_PCS_ENABLED", "false")
    return str(configured).strip().lower() in {"1", "true", "yes", "on"}


def _firstcycling_enabled():
    try:
        from flask import current_app, has_app_context

        configured = current_app.config.get("ACTIVITY_MONITOR_FC_ENABLED", False) if has_app_context() else None
    except Exception:
        configured = None
    if configured is None:
        configured = os.getenv("ACTIVITY_MONITOR_FC_ENABLED", "false")
    return str(configured).strip().lower() in {"1", "true", "yes", "on"}


def _throttle_source_request():
    """Keep a paced, slightly varied gap between outbound source requests."""
    override = _request_delay_override.get()
    if override is not None:
        delay = random.choice((max(1, override - 1), override, override + 1))
    else:
        try:
            from flask import current_app, has_app_context

            configured = current_app.config.get("ACTIVITY_MONITOR_REQUEST_DELAY_SECONDS", 1.5) if has_app_context() else None
        except Exception:
            configured = None
        if configured is None:
            configured = os.getenv("ACTIVITY_MONITOR_REQUEST_DELAY_SECONDS", "1.5")
        try:
            delay = max(float(configured), 1.0)
        except (TypeError, ValueError):
            delay = 1.5

    global _last_request_started
    with _request_throttle_lock:
        now = time.monotonic()
        wait_for = delay - (now - _last_request_started)
        if wait_for > 0:
            time.sleep(wait_for)
        _last_request_started = time.monotonic()


def _fetch_bytes(url, *, opener, timeout_seconds=20, max_bytes=8_000_000):
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/html,application/xhtml+xml",
            "User-Agent": "WorldDB-ActivityMonitor/1.0",
        },
    )
    client = build_opener(opener())
    _throttle_source_request()
    try:
        response = client.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        body = exc.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("Source error response exceeded the configured size limit.")
        return exc.code, body
    except (URLError, TimeoutError, OSError) as exc:
        raise ActivitySourceRetryable(f"Source request failed: {type(exc).__name__}.") from exc
    with response:
        status = response.getcode()
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("Source response exceeded the configured size limit.")
    return status, body


def _fetch_pcs_html(url, timeout_seconds=20, max_bytes=8_000_000):
    return _fetch_bytes(
        url,
        opener=_AllowedPCSRedirects,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )


def _fetch_firstcycling_payload(first_cycling_id):
    url = f"https://firstcycling.com/dora/rider/{int(first_cycling_id)}.json"
    status, body = _fetch_bytes(url, opener=_AllowedFirstCyclingRedirects)
    if status != 200:
        return status, None, body, url
    try:
        import json

        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("FirstCycling activity response was not valid UTF-8 JSON.") from exc
    return status, payload, body, url


def _load_task_rider(engine, task_id):
    with Session(engine) as session:
        task = session.scalar(
            select(ActivityMonitorTask).where(ActivityMonitorTask.id == int(task_id))
        )
        if task is None:
            raise ValueError("Activity Monitor task was not found.")
        rider = session.scalar(
            select(ActivityMonitorRider).where(ActivityMonitorRider.id == task.rider_id)
        )
        if rider is None:
            raise ValueError("Activity Monitor rider was not found.")
        links = session.scalars(
            select(ActivityMonitorProfileLink).where(
                ActivityMonitorProfileLink.rider_id == rider.id,
                ActivityMonitorProfileLink.identity_generation == task.identity_generation,
                ActivityMonitorProfileLink.is_current.is_(True),
            ).order_by(ActivityMonitorProfileLink.id.desc())
        ).all()
        return {
            "task": {
                "id": task.id,
                "run_id": task.run_id,
                # The running transition timestamp identifies this worker
                # attempt while keeping a replay of that same attempt stable.
                "attempt_started_at": task.updated_at.isoformat() if task.updated_at else "",
                "reference_date": task.reference_date,
                "source_scope": list(task.source_scope or []),
                "identity_generation": task.identity_generation,
            },
            "rider": {
                "id": rider.id,
                "worlddb_id": rider.source_worlddb_rider_id,
                "first_cycling_id": rider.first_cycling_id,
                "first_name": rider.first_name,
                "last_name": rider.last_name,
                "birth_date": rider.birth_date,
                "nationality": rider.nationality,
                "eligible": rider.eligible,
            },
            "pcs_paths": [
                value
                for link in links
                for value in (
                    link.approved_profile_path,
                    link.override_url,
                    link.profile_path,
                )
                if str(value or "").strip()
            ] + [_assumed_pcs_profile(rider.first_name, rider.last_name)],
            "pcs_approvals": [
                {
                    "profile_path": link.approved_profile_path,
                    "pcs_birth_date": link.approved_pcs_birth_date,
                    "worlddb_birth_date": link.approved_worlddb_birth_date,
                }
                for link in links
                if link.approved_profile_path
                and link.approved_pcs_birth_date
                and link.approved_worlddb_birth_date
            ],
        }


def _run_stop_requested(engine, run_id):
    if run_id is None:
        return False
    with Session(engine) as session:
        return bool(session.scalar(
            select(ActivityMonitorRun.stop_reason).where(ActivityMonitorRun.id == int(run_id))
        ))


def _firstcycling_pause_status(engine, run_id):
    """Return a run-wide FC pause status set by an access/rate-limit response."""
    if run_id is None:
        return None
    with Session(engine) as session:
        settings = session.scalar(
            select(ActivityMonitorRun.settings).where(ActivityMonitorRun.id == int(run_id))
        ) or {}
    try:
        status = int(settings.get("firstcycling_pause_http_status"))
    except (AttributeError, TypeError, ValueError):
        return None
    return status if status in {401, 403, 429} else None


def _pause_firstcycling_for_run(engine, run_id, status):
    """Remember a run-wide FC pause without changing any rider/source history."""
    if run_id is None or status not in {401, 403, 429}:
        return
    with Session(engine) as session:
        run = session.scalar(
            select(ActivityMonitorRun)
            .where(ActivityMonitorRun.id == int(run_id))
            .with_for_update()
        )
        if run is None:
            return
        settings = dict(run.settings or {})
        if settings.get("firstcycling_pause_http_status") not in {401, 403, 429}:
            settings["firstcycling_pause_http_status"] = status
            run.settings = settings
        session.commit()


def _pcs_pause_status(engine, run_id):
    """Return a PCS pause status set by a rate-limit response in this run."""
    if run_id is None:
        return None
    with Session(engine) as session:
        settings = session.scalar(
            select(ActivityMonitorRun.settings).where(ActivityMonitorRun.id == int(run_id))
        ) or {}
    try:
        status = int(settings.get("pcs_pause_http_status"))
    except (AttributeError, TypeError, ValueError):
        return None
    return status if status == 429 else None


def _pause_pcs_for_run(engine, run_id, status):
    """Stop sending PCS requests for this run after a rate-limit response."""
    if run_id is None or status != 429:
        return
    with Session(engine) as session:
        run = session.scalar(
            select(ActivityMonitorRun)
            .where(ActivityMonitorRun.id == int(run_id))
            .with_for_update()
        )
        if run is None:
            return
        settings = dict(run.settings or {})
        settings.setdefault("pcs_pause_http_status", status)
        run.settings = settings
        session.commit()


def _saved_source_outcomes(engine, task, rider):
    """Load this run's latest source outcomes so retries resume per source."""
    if task["run_id"] is None:
        return {}
    with Session(engine) as session:
        checks = session.scalars(
            select(ActivityMonitorSourceCheck)
            .where(
                ActivityMonitorSourceCheck.run_id == task["run_id"],
                ActivityMonitorSourceCheck.rider_id == rider["id"],
                ActivityMonitorSourceCheck.identity_generation == task["identity_generation"],
            )
            .order_by(ActivityMonitorSourceCheck.checked_at, ActivityMonitorSourceCheck.id)
        ).all()
    outcomes = {}
    for check in checks:
        outcomes[check.source] = {
            "source": check.source,
            "status": check.source_status,
            "coverage_status": check.coverage_status,
            "identity_result": check.identity_result,
            "http_status": check.http_status,
            "request_path": check.request_path,
            "message": check.message,
        }
    return outcomes


def _record_pcs_outcome(engine, task, rider, url, status, body, checked_at):
    profile_url = url or "https://www.procyclingstats.com/rider/unavailable"
    parsed = None
    message = "No valid PCS rider profile link is available for this rider identity."
    identity_result = "not_checked"
    coverage_status = "unverified"
    source_status = "missing_link"
    evidence_rows = []
    if url and status == 200:
        parsed = parse_pcs_html(
            body,
            rider_id=rider["id"],
            profile_url=profile_url,
            expected_identity={
                "birthdate": rider["birth_date"].isoformat() if rider["birth_date"] else "",
                "nationality": rider["nationality"] or "",
            },
            year_override=task["reference_date"].year,
            as_of=task["reference_date"],
        )
        original_identity = parsed["identity"].get("identity_status")
        parsed_path = urlsplit(profile_url).path.rstrip("/").casefold()
        approval = next((
            item for item in rider.get("pcs_approvals", [])
            if urlsplit(_pcs_url(item.get("profile_path")) or "").path.rstrip("/").casefold() == parsed_path
            and item.get("worlddb_birth_date") == rider["birth_date"]
            and item.get("pcs_birth_date")
            and str(item["pcs_birth_date"])[:10] == str(parsed["identity"].get("birthdate") or "")[:10]
        ), None)
        if approval and original_identity == "mismatch":
            parsed["identity"]["identity_status"] = "verified"
            parsed["identity"]["identity_matches"] = True
            parsed["identity"]["identity_reasons"] = [
                "Manually approved for this exact PCS profile and WorldDB/PCS birthdate pair."
            ]
            if parsed.get("status") == "identity_not_verified":
                if parsed.get("rows"):
                    parsed["status"] = "results_parsed"
                elif parsed.get("result_table_rows") == 0 or parsed.get("excluded_future_rows", 0):
                    parsed["status"] = "results_table_empty"
        message = "; ".join(parsed["identity"].get("identity_reasons") or [])
        message = message or parsed["status"]
        excluded = []
        if parsed.get("excluded_future_rows"):
            excluded.append(f"ignored {parsed['excluded_future_rows']} future-dated row(s)")
        if excluded:
            message = f"{message}; {', '.join(excluded)}"
        identity_result = parsed["identity"].get("identity_status") or "ambiguous"
        if identity_result == "mismatch":
            source_status = "identity_mismatch"
        elif identity_result != "verified":
            source_status = "identity_not_verified"
        else:
            source_status = "success" if parsed["status"] in {
                "results_parsed", "results_table_empty"
            } else parsed["status"]
        if identity_result == "verified" and parsed["status"] in {"results_parsed", "results_table_empty"}:
            coverage_status = "partial"
        evidence_rows = to_activity_monitor_evidence(
            parsed,
            rider_id=rider["id"],
            source_worlddb_rider_id=rider["worlddb_id"],
            identity_generation=task["identity_generation"],
            first_cycling_id=rider["first_cycling_id"],
        )
        if parsed.get("identity", {}).get("identity_status") in {"verified", "mismatch", "ambiguous"}:
            check_result = {
                "verified": "verified",
                "mismatch": "identity_mismatch",
                "ambiguous": "ambiguous",
            }[parsed["identity"]["identity_status"]]
            facts = parsed["identity"]
            with Session(engine) as session:
                current_links = session.scalars(select(ActivityMonitorProfileLink).where(
                    ActivityMonitorProfileLink.rider_id == rider["id"],
                    ActivityMonitorProfileLink.identity_generation == task["identity_generation"],
                    ActivityMonitorProfileLink.is_current.is_(True),
                )).all()
                matching_link = None
                for link in current_links:
                    selected_url = _pcs_url(
                        link.approved_profile_path or link.override_url
                        or link.profile_path
                    )
                    if urlsplit(selected_url or "").path.rstrip("/").casefold() == parsed_path:
                        matching_link = link
                        break

                # Save an automatically suggested link as a confirmed profile
                # only after PCS has verified that it belongs to this rider.
                if check_result in {"verified", "mismatch"} and matching_link is None:
                    profile_key = hashlib.sha256(parsed_path.encode("utf-8")).hexdigest()[:16]
                    source_record_key = f"assumed-pcs:{rider['id']}:{task['identity_generation']}:{profile_key}"
                    matching_link = session.scalar(select(ActivityMonitorProfileLink).where(
                        ActivityMonitorProfileLink.source_record_key == source_record_key,
                    ))
                    if check_result == "verified":
                        for link in current_links:
                            link.is_current = False
                    if matching_link is None:
                        slug_match = re.search(r"/rider/([^/]+)", parsed_path)
                        matching_link = ActivityMonitorProfileLink(
                            source_record_key=source_record_key,
                            rider_id=rider["id"],
                            identity_generation=task["identity_generation"],
                            profile_path=parsed_path,
                            link_source="assumed",
                            link_status="verified" if check_result == "verified" else "candidate",
                            is_current=(check_result == "verified" or not current_links),
                            profile_slug=slug_match.group(1) if slug_match else None,
                            created_at=checked_at,
                        )
                        session.add(matching_link)
                    elif check_result == "verified" or not current_links:
                        matching_link.is_current = True

                if matching_link is not None:
                    matching_link.checked_at = checked_at
                    matching_link.checked_url = profile_url
                    matching_link.http_status = status
                    matching_link.check_result = check_result
                    matching_link.check_result_detail = parsed.get("status")
                    matching_link.profile_name = facts.get("display_name") or facts.get("heading") or None
                    matching_link.profile_birth_date = date.fromisoformat(facts["birthdate"]) if facts.get("birthdate") else None
                    matching_link.profile_nationality = facts.get("nationality") or None
                    matching_link.identity_reason = "; ".join(parsed["identity"].get("identity_reasons") or []) or None
                    matching_link.message = message
                    matching_link.parser_version = 1
                    if check_result == "verified":
                        matching_link.link_status = "verified"
                        matching_link.verified_at = checked_at
                    elif check_result == "mismatch" and matching_link.link_source == "assumed":
                        matching_link.link_status = "candidate"
                session.commit()
    elif url:
        source_status = "http_error"
        message = f"PCS returned HTTP {status}."

    path = urlsplit(url or profile_url).path
    response_hash = hashlib.sha256(body or b"").hexdigest() if body is not None else None
    key_material = (
        f"{task['id']}|{task.get('attempt_started_at', '')}|pcs|{path}|"
        f"{response_hash or source_status}"
    )
    source_key = "activity-check:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    check = {
        "source_record_key": source_key,
        "run_id": task["run_id"],
        "rider_id": rider["id"],
        "source_worlddb_rider_id": rider["worlddb_id"],
        "identity_generation": task["identity_generation"],
        "first_cycling_id": rider["first_cycling_id"],
        "season": task["reference_date"].year,
        "source": "pcs",
        "source_status": source_status,
        "coverage_status": coverage_status,
        "identity_result": identity_result,
        "checked_at": checked_at,
        "http_status": status,
        "response_sha256": response_hash,
        "request_path": path,
        "parser_version": 1,
        "message": message,
    }
    stored = persist_source_outcome(engine, check=check, evidence_rows=evidence_rows)
    return {
        "source": "pcs",
        "status": source_status,
        "http_status": status,
        "request_path": path,
        "coverage_status": coverage_status,
        "identity_result": identity_result,
        "rows_saved": stored["evidence_rows_inserted"],
        "message": message,
    }


def _record_request_failure(
    engine, task, rider, source, url, error, checked_at, *, source_status="network_error"
):
    path = urlsplit(url or "").path
    key_material = (
        f"{task['id']}|{task.get('attempt_started_at', '')}|{source}|{path}|"
        f"{source_status}|{type(error).__name__}"
    )
    message = f"{source} source outcome: {source_status} ({type(error).__name__})."
    check = {
        "source_record_key": "activity-check:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest(),
        "run_id": task["run_id"],
        "rider_id": rider["id"],
        "source_worlddb_rider_id": rider["worlddb_id"],
        "identity_generation": task["identity_generation"],
        "first_cycling_id": rider["first_cycling_id"],
        "season": task["reference_date"].year,
        "source": source,
        "source_status": source_status,
        "coverage_status": "unverified",
        "identity_result": "not_checked",
        "checked_at": checked_at,
        "http_status": None,
        "response_sha256": None,
        "request_path": path or None,
        "parser_version": 1,
        "message": message,
    }
    persist_source_outcome(engine, check=check)
    return {
        "source": source,
        "status": source_status,
        "coverage_status": "unverified",
        "identity_result": "not_checked",
        "http_status": None,
        "rows_saved": 0,
        "message": message,
    }


def _record_firstcycling_outcome(engine, task, rider, checked_at):
    fc_id = rider["first_cycling_id"]
    status, payload, body, url = _fetch_firstcycling_payload(fc_id)
    normalized = normalize_activity_payload(
        payload,
        expected_fc_id=fc_id,
        rider_id=rider["id"],
        source_worlddb_rider_id=rider["worlddb_id"],
        identity_generation=task["identity_generation"],
    )
    source_status = "success" if normalized["status"] == "success" else normalized["status"]
    identity_result = "verified" if normalized["identity_verified"] else (
        "mismatch" if normalized["status"] == "identity_mismatch" else "not_checked"
    )
    response_hash = hashlib.sha256(body or b"").hexdigest() if body is not None else None
    path = urlsplit(url).path
    key_material = (
        f"{task['id']}|{task.get('attempt_started_at', '')}|firstcycling|{path}|"
        f"{response_hash or status}"
    )
    check = {
        "source_record_key": "activity-check:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest(),
        "run_id": task["run_id"],
        "rider_id": rider["id"],
        "source_worlddb_rider_id": rider["worlddb_id"],
        "identity_generation": task["identity_generation"],
        "first_cycling_id": fc_id,
        "season": task["reference_date"].year,
        "source": "firstcycling",
        "source_status": source_status,
        "coverage_status": normalized["coverage_status"],
        "identity_result": identity_result,
        "checked_at": checked_at,
        "http_status": status,
        "response_sha256": response_hash,
        "request_path": path,
        "parser_version": 1,
        "message": normalized["message"],
    }
    stored = persist_source_outcome(
        engine,
        check=check,
        evidence_rows=normalized["rows"],
    )
    return {
        "source": "firstcycling",
        "status": source_status,
        "http_status": status,
        "coverage_status": normalized["coverage_status"],
        "identity_result": identity_result,
        "rows_saved": stored["evidence_rows_inserted"],
        "message": normalized["message"],
    }


def collect_activity_monitor_task(engine, task_id):
    """Collect configured sources and preserve partial outcomes for resumption."""
    record = _load_task_rider(engine, task_id)
    task = record["task"]
    rider = {**record["rider"], "pcs_approvals": record.get("pcs_approvals", [])}
    if not rider["eligible"]:
        raise ValueError("Rider is no longer eligible for Activity Monitor checks.")
    if _run_stop_requested(engine, task["run_id"]):
        return {"state": "cancelled", "sources": [], "stop_requested": True}

    outcomes = []
    retryable_failures = []
    scope = task["source_scope"]
    saved_outcomes = _saved_source_outcomes(engine, task, rider)
    if "pcs" in scope:
        prior_pcs = saved_outcomes.get("pcs")
        url = next((normalized for value in record["pcs_paths"]
                    if (normalized := _pcs_url(value))), None)
        current_path = urlsplit(url or "").path.rstrip("/").casefold()
        previous_path = str((prior_pcs or {}).get("request_path") or "").rstrip("/").casefold()
        same_checked_profile = bool(current_path and current_path == previous_path)
        if prior_pcs and same_checked_profile and prior_pcs["status"] != "network_error":
            outcomes.append(prior_pcs)
        elif _pcs_pause_status(engine, task["run_id"]) is not None:
            outcomes.append({
                "source": "pcs",
                "status": "paused",
                "http_status": 429,
                "identity_result": "not_checked",
                "message": "PCS requests paused for the rest of this run after HTTP 429; this rider was not requested from PCS.",
            })
        elif not _pcs_enabled():
            raise ActivitySourceNotConfigured(
                "PCS collection is disabled by ACTIVITY_MONITOR_PCS_ENABLED."
            )
        else:
            checked_at = datetime.utcnow()
            try:
                status, body = _fetch_pcs_html(url) if url else (None, None)
                if status in {408, 425} or (status is not None and 500 <= status < 600):
                    raise ActivitySourceRetryable(f"PCS temporarily returned HTTP {status}.")
                pcs_outcome = _record_pcs_outcome(
                    engine, task, rider, url, status, body, checked_at
                )
                outcomes.append(pcs_outcome)
                if pcs_outcome.get("http_status") == 429:
                    _pause_pcs_for_run(engine, task["run_id"], 429)
            except ActivitySourceRetryable as exc:
                outcomes.append(_record_request_failure(engine, task, rider, "pcs", url, exc, checked_at))
                retryable_failures.append(exc)
            except ValueError as exc:
                outcomes.append(_record_request_failure(
                    engine, task, rider, "pcs", url, exc, checked_at,
                    source_status="invalid_response",
                ))

    stopped_after_request = _run_stop_requested(engine, task["run_id"])
    if "firstcycling" in scope and not stopped_after_request:
        prior_fc = saved_outcomes.get("firstcycling")
        pause_status = _firstcycling_pause_status(engine, task["run_id"])
        if prior_fc and prior_fc["status"] != "network_error":
            outcomes.append(prior_fc)
        elif pause_status is not None:
            outcomes.append({
                "source": "firstcycling",
                "status": "paused",
                "http_status": pause_status,
                "message": (
                    f"FirstCycling requests paused for the rest of this run after HTTP "
                    f"{pause_status}; this rider was not requested from FirstCycling."
                ),
            })
        elif not _firstcycling_enabled():
            raise ActivitySourceNotConfigured(
                "FirstCycling collection is disabled by ACTIVITY_MONITOR_FC_ENABLED."
            )
        else:
            checked_at = datetime.utcnow()
            try:
                firstcycling_outcome = _record_firstcycling_outcome(
                    engine, task, rider, checked_at
                )
                outcomes.append(firstcycling_outcome)
                if firstcycling_outcome.get("http_status") in {401, 403, 429}:
                    _pause_firstcycling_for_run(
                        engine, task["run_id"], firstcycling_outcome["http_status"]
                    )
            except ActivitySourceRetryable as exc:
                fc_url = f"https://firstcycling.com/dora/rider/{rider['first_cycling_id']}.json"
                outcomes.append(_record_request_failure(
                    engine, task, rider, "firstcycling", fc_url, exc, checked_at
                ))
                retryable_failures.append(exc)
            except ValueError as exc:
                fc_url = f"https://firstcycling.com/dora/rider/{rider['first_cycling_id']}.json"
                outcomes.append(_record_request_failure(
                    engine, task, rider, "firstcycling", fc_url, exc, checked_at,
                    source_status="invalid_response",
                ))

    if not outcomes:
        if stopped_after_request:
            return {"state": "cancelled", "sources": [], "stop_requested": True}
        raise ActivitySourceNotConfigured("No Activity Monitor source collector is enabled.")
    if retryable_failures and not stopped_after_request:
        raise ActivitySourceRetryable(
            "One or more source requests failed; completed source outcomes were saved."
        )

    verified = any(item["identity_result"] == "verified" for item in outcomes)
    source_error = any(item["status"] not in {"success"} for item in outcomes)
    pcs_succeeded = any(
        item.get("source") == "pcs"
        and item.get("status") == "success"
        and item.get("identity_result") == "verified"
        for item in outcomes
    )
    final_state = "completed" if pcs_succeeded or (verified and not source_error) else "needs_review"
    return {
        "state": final_state,
        "sources": outcomes,
        "stop_requested": stopped_after_request,
    }
