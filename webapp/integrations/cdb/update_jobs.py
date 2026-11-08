"""Small file-backed status records for background CDB namespace updates."""
import json
import os
import re
import tempfile
from datetime import datetime

from webapp.config import get_cdb_import_path

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_cdb_update_job_id(job_id):
    return bool(_JOB_ID_RE.fullmatch(str(job_id or "")))


def cdb_update_job_status_path(namespace_slug, job_id):
    if not is_valid_cdb_update_job_id(job_id):
        raise ValueError("Invalid CDB update job ID")
    return os.path.join(get_cdb_import_path(namespace_slug), f".update-{job_id}.json")


def write_cdb_update_job_status(namespace_slug, job_id, status, **values):
    path = cdb_update_job_status_path(namespace_slug, job_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"job_id": job_id, "status": status, **values}
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".update-{job_id}-", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as status_file:
            json.dump(payload, status_file, ensure_ascii=False)
            status_file.flush()
            os.fsync(status_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return payload


def activity_monitor_restore_checkpoint_path(namespace_slug):
    return os.path.join(
        get_cdb_import_path(namespace_slug), ".activity-monitor-restore-checkpoint.json"
    )


def read_activity_monitor_restore_checkpoint(namespace_slug):
    path = activity_monitor_restore_checkpoint_path(namespace_slug)
    try:
        with open(path, encoding="utf-8") as checkpoint_file:
            payload = json.load(checkpoint_file)
        value = payload.get("archived_before")
        return datetime.fromisoformat(value) if value else None
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def write_activity_monitor_restore_checkpoint(namespace_slug, archived_before):
    path = activity_monitor_restore_checkpoint_path(namespace_slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".activity-monitor-restore-", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as checkpoint_file:
            json.dump({"archived_before": archived_before.isoformat()}, checkpoint_file)
            checkpoint_file.flush()
            os.fsync(checkpoint_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def read_cdb_update_job_status(namespace_slug, job_id):
    path = cdb_update_job_status_path(namespace_slug, job_id)
    try:
        with open(path, encoding="utf-8") as status_file:
            payload = json.load(status_file)
    except FileNotFoundError:
        return None
    return payload if payload.get("job_id") == job_id else None
