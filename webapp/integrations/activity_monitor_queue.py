"""Namespace-scoped Activity Monitor run and task lifecycle helpers.

These helpers write durable queue state. They do not contact PCS/FC or start a
Celery task themselves; the web route dispatches the queued run to the worker.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from webapp.models.ns.activity_monitor import (
    ActivityMonitorRider,
    ActivityMonitorRun,
    ActivityMonitorTask,
    ActivityMonitorTaskEvent,
)


TERMINAL_TASK_STATES = {"completed", "needs_review", "failed", "cancelled"}
ALLOWED_TASK_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"retry_wait", "completed", "needs_review", "failed", "cancelled"},
    "retry_wait": {"running", "cancelled"},
    "completed": set(),
    "needs_review": set(),
    "failed": set(),
    "cancelled": set(),
}


def enqueue_activity_monitor_run(
    engine,
    *,
    riders,
    source_mode,
    initiated_by_user_id=None,
    reference_date=None,
    run_type="manual",
    run_settings=None,
    max_riders=25,
):
    """Create one shared run and one queued task per currently eligible rider.

    ``riders`` contains Activity Monitor rider IDs selected in the UI. Current
    eligibility and source links are re-read inside the transaction so stale
    previews cannot queue riders that have since changed.
    """
    from datetime import date

    if source_mode not in {"combined", "pcs_only"}:
        raise ValueError("Unsupported Activity Monitor source mode.")
    if run_type not in {"manual", "scheduled"}:
        raise ValueError("Unsupported Activity Monitor run type.")
    rider_ids = list(dict.fromkeys(int(value) for value in riders))
    if not rider_ids:
        raise ValueError("Select at least one rider.")
    if max_riders not in {25, 250}:
        raise ValueError("Unsupported Activity Monitor run size.")
    if len(rider_ids) > max_riders:
        raise ValueError(f"A run can include at most {max_riders} riders.")

    now = datetime.utcnow()
    reference_date = reference_date or date.today()
    session = Session(engine)
    try:
        with session.begin():
            session.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'), {
                'lock_key': 713459201,
            })
            active_run = session.scalar(
                select(ActivityMonitorRun.id)
                .where(ActivityMonitorRun.status.in_({'queued', 'running', 'retry_wait'}))
                .limit(1)
            )
            if active_run is not None:
                raise ValueError(
                    "Another Activity Monitor run is in progress in this namespace. "
                    "Wait for it to finish before starting another."
                )
            current_riders = session.scalars(
                select(ActivityMonitorRider)
                .where(ActivityMonitorRider.id.in_(rider_ids))
                .with_for_update()
            ).all()
            by_id = {rider.id: rider for rider in current_riders}
            if len(by_id) != len(rider_ids):
                raise ValueError("A selected rider is no longer available in this namespace.")

            scopes = {}
            unavailable = []
            for rider_id in rider_ids:
                rider = by_id[rider_id]
                if not rider.eligible or not rider.present_in_cdb or rider.identity_hold:
                    raise ValueError("A selected rider is no longer eligible; refresh the selection.")
                uses_firstcycling = (
                    source_mode == "combined"
                    and rider.team_id != 119
                    and rider.first_cycling_id is not None
                )
                scope = []
                if uses_firstcycling:
                    scope.append("firstcycling")
                # Every eligible rider has a PCS check: use a saved profile
                # when available, otherwise the worker tries the name-based
                # candidate and still verifies the rider before saving results.
                scope.append("pcs")
                if source_mode == "pcs_only":
                    scope = [source for source in scope if source == "pcs"]
                if not scope:
                    unavailable.append(f"{rider.first_name} {rider.last_name}".strip())
                scopes[rider_id] = scope
            if unavailable:
                raise ValueError(
                    "No current source link is available for: " + ", ".join(unavailable)
                )

            record_prefix = "scheduled" if run_type == "scheduled" else "manual"
            run = ActivityMonitorRun(
                source_record_key=f"{record_prefix}-run:{uuid4()}",
                run_type=run_type,
                status="queued",
                initiated_by_user_id=initiated_by_user_id,
                reference_date=reference_date,
                requested_count=len(rider_ids),
                settings={"source_mode": source_mode, **(run_settings or {})},
                created_at=now,
            )
            session.add(run)
            session.flush()

            tasks = []
            for rider_id in rider_ids:
                rider = by_id[rider_id]
                task = ActivityMonitorTask(
                    source_record_key=f"{record_prefix}-task:{uuid4()}",
                    rider_id=rider.id,
                    source_worlddb_rider_id=rider.source_worlddb_rider_id,
                    identity_generation=rider.identity_generation,
                    run_id=run.id,
                    state="queued",
                    source_scope=scopes[rider_id],
                    reason=(
                        "Queued by the namespace's enabled Activity Monitor schedule."
                        if run_type == "scheduled"
                        else "Manual Activity Monitor check selected by an authorized user."
                    ),
                    reference_date=reference_date,
                    created_at=now,
                    updated_at=now,
                    outcome="",
                )
                session.add(task)
                tasks.append(task)
            session.flush()

            for task in tasks:
                session.add(ActivityMonitorTaskEvent(
                    source_record_key=f"task-event:{uuid4()}",
                    task_id=task.id,
                    old_state=None,
                    new_state="queued",
                    actor_user_id=initiated_by_user_id,
                    occurred_at=now,
                    notes="Task added to the shared namespace queue; no source request was made.",
                ))

            return {"run_id": run.id, "task_ids": [task.id for task in tasks]}
    finally:
        session.close()


def transition_activity_monitor_task(
    engine,
    task_id,
    new_state,
    *,
    actor_user_id=None,
    notes="",
    outcome=None,
    next_retry_at=None,
    expected_state=None,
):
    """Apply an audited task transition and refresh the parent run counters."""
    if new_state not in ALLOWED_TASK_TRANSITIONS:
        raise ValueError(f"Unsupported Activity Monitor task state: {new_state}")

    now = datetime.utcnow()
    session = Session(engine)
    try:
        with session.begin():
            task_ref = session.get(ActivityMonitorTask, int(task_id))
            if task_ref is None:
                raise ValueError("Activity Monitor task was not found.")

            run = None
            run_id = task_ref.run_id
            if run_id is not None:
                run = session.scalar(
                    select(ActivityMonitorRun)
                    .where(ActivityMonitorRun.id == run_id)
                    .with_for_update()
                )

            # Always lock the run before its tasks. Stop requests use the same
            # order, so simultaneous task transitions cannot deadlock while
            # refreshing the parent run's counters.
            task = session.scalar(
                select(ActivityMonitorTask)
                .where(ActivityMonitorTask.id == int(task_id))
                .with_for_update()
            )
            if task is None:
                raise ValueError("Activity Monitor task was not found.")
            if task.run_id != run_id:
                raise ValueError("Activity Monitor task changed runs while being updated.")

            old_state = task.state
            if expected_state is not None:
                allowed_current = (
                    {expected_state}
                    if isinstance(expected_state, str)
                    else set(expected_state)
                )
                if old_state not in allowed_current:
                    return {
                        "task_id": task.id,
                        "state": task.state,
                        "changed": False,
                    }
            if old_state == new_state:
                return {"task_id": task.id, "state": task.state, "changed": False}
            if new_state not in ALLOWED_TASK_TRANSITIONS.get(old_state, set()):
                raise ValueError(f"Task cannot move from {old_state} to {new_state}.")

            task.state = new_state
            task.updated_at = now
            task.next_retry_at = next_retry_at if new_state == "retry_wait" else None
            if outcome is not None:
                task.outcome = str(outcome)
            session.add(ActivityMonitorTaskEvent(
                source_record_key=f"task-event:{uuid4()}",
                task_id=task.id,
                old_state=old_state,
                new_state=new_state,
                actor_user_id=actor_user_id,
                occurred_at=now,
                notes=notes or "",
            ))

            if run is not None:
                tasks = session.scalars(
                    select(ActivityMonitorTask)
                    .where(ActivityMonitorTask.run_id == run.id)
                    .with_for_update()
                ).all()
                states = [item.state for item in tasks]
                run.attempted_count = sum(
                    state in {"running", "retry_wait", "completed", "needs_review", "failed"}
                    for state in states
                )
                run.successful_count = sum(
                    state in {"completed", "needs_review"} for state in states
                )
                run.matched_count = states.count("completed")
                run.needs_review_count = states.count("needs_review")
                run.heartbeat_at = now
                if run.started_at is None and new_state == "running":
                    run.started_at = now

                if states and all(state in TERMINAL_TASK_STATES for state in states):
                    if all(state == "cancelled" for state in states):
                        run.status = "cancelled"
                    elif "failed" in states and any(
                        state in {"completed", "needs_review"} for state in states
                    ):
                        run.status = "partial"
                    elif "failed" in states:
                        run.status = "failed"
                    else:
                        run.status = "completed"
                    run.finished_at = now
                elif any(state == "running" for state in states):
                    run.status = "running"
                    run.finished_at = None
                elif "retry_wait" in states and "queued" not in states:
                    run.status = "retry_wait"
                    run.finished_at = None
                else:
                    run.status = "queued"
                    run.finished_at = None

            return {"task_id": task.id, "state": task.state, "changed": True}
    finally:
        session.close()


def request_activity_monitor_run_stop(engine, run_id, *, actor_user_id=None):
    """Request a manual run stop and cancel rider tasks that have not started."""
    session = Session(engine)
    try:
        with session.begin():
            run = session.scalar(
                select(ActivityMonitorRun)
                .where(ActivityMonitorRun.id == int(run_id))
                .with_for_update()
            )
            if run is None or not str(run.source_record_key or "").startswith(("manual-run:", "scheduled-run:")):
                raise ValueError("Manual Activity Monitor run was not found.")
            if run.status in {"completed", "partial", "failed", "cancelled"}:
                return {"run_id": run.id, "status": run.status, "changed": False, "cancelled_count": 0}
            if run.stop_reason:
                return {"run_id": run.id, "status": run.status, "changed": False, "cancelled_count": 0}

            run.stop_reason = "user_requested_after_current_request"
            pending_tasks = session.scalars(
                select(ActivityMonitorTask)
                .where(
                    ActivityMonitorTask.run_id == run.id,
                    ActivityMonitorTask.state.in_({"queued", "retry_wait"}),
                )
                .with_for_update()
            ).all()
            now = datetime.utcnow()
            for task in pending_tasks:
                old_state = task.state
                task.state = "cancelled"
                task.next_retry_at = None
                task.updated_at = now
                session.add(ActivityMonitorTaskEvent(
                    source_record_key=f"task-event:{uuid4()}",
                    task_id=task.id,
                    old_state=old_state,
                    new_state="cancelled",
                    actor_user_id=actor_user_id,
                    occurred_at=now,
                    notes="Cancelled because the user requested the run to stop.",
                ))

            states = session.scalars(
                select(ActivityMonitorTask.state).where(ActivityMonitorTask.run_id == run.id)
            ).all()
            run.attempted_count = sum(
                state in {"running", "retry_wait", "completed", "needs_review", "failed"}
                for state in states
            )
            run.successful_count = sum(state in {"completed", "needs_review"} for state in states)
            run.matched_count = states.count("completed")
            run.needs_review_count = states.count("needs_review")
            run.heartbeat_at = now
            if states and all(state in TERMINAL_TASK_STATES for state in states):
                if all(state == "cancelled" for state in states):
                    run.status = "cancelled"
                elif "failed" in states and any(
                    state in {"completed", "needs_review"} for state in states
                ):
                    run.status = "partial"
                elif "failed" in states:
                    run.status = "failed"
                else:
                    run.status = "completed"
                run.finished_at = now
            elif "running" in states:
                run.status = "running"
                run.finished_at = None
            else:
                run.status = "queued"
                run.finished_at = None

            return {
                "run_id": run.id,
                "status": run.status,
                "changed": True,
                "cancelled_count": len(pending_tasks),
            }
    finally:
        session.close()


def resume_activity_monitor_run(engine, run_id, *, actor_user_id=None):
    """Requeue only the riders left waiting when a manual run was stopped."""
    session = Session(engine)
    try:
        with session.begin():
            run = session.scalar(
                select(ActivityMonitorRun)
                .where(ActivityMonitorRun.id == int(run_id))
                .with_for_update()
            )
            if run is None or not str(run.source_record_key or "").startswith(("manual-run:", "scheduled-run:")):
                raise ValueError("Activity Monitor run was not found.")
            if not run.stop_reason:
                return {
                    "run_id": run.id,
                    "changed": run.status in {"queued", "running", "retry_wait"},
                    "requeued_count": 0,
                    "status": run.status,
                }
            if run.status not in {"completed", "partial", "failed", "cancelled"}:
                raise ValueError("The current check must finish stopping before it can be resumed.")

            active_run_id = session.scalar(
                select(ActivityMonitorRun.id)
                .where(
                    ActivityMonitorRun.id != run.id,
                    ActivityMonitorRun.status.in_({"queued", "running", "retry_wait"}),
                )
                .limit(1)
            )
            if active_run_id is not None:
                raise ValueError("Another check run is already in progress. Wait for it to finish before resuming this one.")

            tasks = session.scalars(
                select(ActivityMonitorTask)
                .where(
                    ActivityMonitorTask.run_id == run.id,
                    ActivityMonitorTask.state == "cancelled",
                )
                .with_for_update()
            ).all()
            if not tasks:
                return {"run_id": run.id, "changed": False, "requeued_count": 0, "status": run.status}

            now = datetime.utcnow()
            for task in tasks:
                task.state = "queued"
                task.next_retry_at = None
                task.updated_at = now
                session.add(ActivityMonitorTaskEvent(
                    source_record_key=f"task-event:{uuid4()}",
                    task_id=task.id,
                    old_state="cancelled",
                    new_state="queued",
                    actor_user_id=actor_user_id,
                    occurred_at=now,
                    notes="Requeued because the user resumed the stopped run.",
                ))

            states = session.scalars(
                select(ActivityMonitorTask.state).where(ActivityMonitorTask.run_id == run.id)
            ).all()
            run.stop_reason = None
            run.status = "queued"
            run.finished_at = None
            run.heartbeat_at = now
            run.attempted_count = sum(
                state in {"running", "retry_wait", "completed", "needs_review", "failed"}
                for state in states
            )
            run.successful_count = sum(state in {"completed", "needs_review"} for state in states)
            run.matched_count = states.count("completed")
            run.needs_review_count = states.count("needs_review")

            return {
                "run_id": run.id,
                "changed": True,
                "requeued_count": len(tasks),
                "status": run.status,
            }
    finally:
        session.close()
