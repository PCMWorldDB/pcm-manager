"""Celery worker entrypoint and background task definitions."""
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from celery.signals import worker_process_init
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from webapp import create_app
from webapp.extensions import celery as _celery, db
from webapp.integrations.firstcycling.api import FirstCyclingClient
from webapp.models import ExternalSyncLog
from webapp.models.main.namespace import Namespace
from webapp.integrations.cdb.update_jobs import (
    read_activity_monitor_restore_checkpoint,
    write_activity_monitor_restore_checkpoint,
    write_cdb_update_job_status,
)

SYNC_TYPE_RACE = 'fc_race_calendar'
SYNC_TYPE_TEAMS = 'fc_teams'
SYNC_TYPE_RIDERS = 'fc_riders'
SYNC_TYPE_RACE_RESULTS = 'fc_race_results'
logger = logging.getLogger(__name__)
_worker_app = None


@worker_process_init.connect
def reset_database_pools_after_fork(**_kwargs):
    """Give every prefork worker child its own SQLAlchemy connections."""
    if _worker_app is None:
        return
    with _worker_app.app_context():
        for engine in db.engines.values():
            engine.dispose(close=False)
    logger.info('Reset SQLAlchemy connection pools after Celery worker fork.')


def _update_sync_log(log_id, **updates):
    log = ExternalSyncLog.query.get(log_id)
    if log is None:
        return None

    for key, value in updates.items():
        setattr(log, key, value)

    db.session.commit()
    return log


@_celery.task(name='webapp.tasks.update_namespace_cdb')
def update_namespace_cdb_task(
    namespace_id, job_id, source_filename, sqlite_filename, extension, actor_user_id,
    ip_address=None, user_agent=None,
):
    """Convert, validate, and replace one namespace CDB outside the web request."""
    from webapp.config import get_cdb_import_path
    from webapp.integrations.cdb.converter import convert_cdb_to_sqlite, validate_sqlite_cdb
    from webapp.models import AuditAction, AuditLog, User
    from webapp.namespace.setup import CDBImporter

    namespace = Namespace.query.get(namespace_id)
    if namespace is None:
        return {'status': 'failed', 'message': 'The namespace no longer exists.'}

    import_path = os.path.realpath(get_cdb_import_path(namespace.slug))
    source_path = os.path.realpath(os.path.join(import_path, source_filename))
    sqlite_path = os.path.realpath(os.path.join(import_path, sqlite_filename))
    if (
        os.path.commonpath([import_path, source_path]) != import_path
        or os.path.commonpath([import_path, sqlite_path]) != import_path
        or os.path.basename(source_filename) != source_filename
        or os.path.basename(sqlite_filename) != sqlite_filename
    ):
        raise ValueError('Invalid uploaded CDB path.')

    lock_path = os.path.join(import_path, '.cdb-update.lock')
    lock_fd = None
    imported = False
    warning = None
    phase_durations = {}
    task_started_at = time.monotonic()
    paths_to_clean = [source_path, sqlite_path] if extension == '.cdb' else [source_path]
    namespace_engine = None
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(lock_fd, job_id.encode('ascii'))
        except FileExistsError as exc:
            raise RuntimeError('Another CDB update is already running for this namespace.') from exc

        phase_started_at = time.monotonic()
        current_phase = None

        def set_status(status, message):
            nonlocal phase_started_at, current_phase
            now = time.monotonic()
            if current_phase is not None:
                phase_durations[current_phase] = round(now - phase_started_at, 1)
            current_phase = status
            phase_started_at = now
            write_cdb_update_job_status(
                namespace.slug, job_id, status, message=message,
                phase_durations_seconds=dict(phase_durations),
                elapsed_seconds=round(now - task_started_at, 1),
                updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            )

        set_status('converting', 'Preparing and checking the uploaded CDB…')
        if extension == '.cdb':
            convert_cdb_to_sqlite(source_path, sqlite_path)
        else:
            validate_sqlite_cdb(sqlite_path)

        set_status('archiving', 'Saving the current Activity Monitor history…')
        from webapp.tools.activity_monitor_global_history import archive_namespace_history

        namespace_engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
        restore_checkpoint = read_activity_monitor_restore_checkpoint(namespace.slug)
        main_engine = db.engines.get('main') or db.engine

        def report_archive_progress(table_name, record_count, duration_seconds):
            write_cdb_update_job_status(
                namespace.slug, job_id, 'archiving',
                message=(
                    f'Saving Activity Monitor history: {table_name} '
                    f'({record_count:,} records processed, {duration_seconds:.1f}s).'
                ),
                phase_durations_seconds=dict(phase_durations),
                elapsed_seconds=round(time.monotonic() - task_started_at, 1),
                updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            )

        if restore_checkpoint is not None and inspect(main_engine).has_table(
            'activity_monitor_global_history'
        ):
            set_status(
                'archiving',
                'Shared Activity Monitor history is already synchronized; skipping the full archive scan…',
            )
            logger.info(
                'Skipping full Activity Monitor archive scan for namespace %s; '
                'incremental restore checkpoint exists.',
                namespace.slug,
            )
        else:
            archive_namespace_history(
                namespace_engine,
                main_engine,
                namespace.slug,
                apply=True,
                progress_callback=report_archive_progress,
            )

        set_status('importing', 'Replacing the CDB tables in this namespace…')
        with CDBImporter(
            sqlite_path,
            namespace.build_connection_string(),
            namespace.pcm_version,
        ) as importer:
            stats = importer.import_all_tables(replace_existing=True)
        imported = True

        namespace.sqlite_file_path = f'cdb/import/{sqlite_filename}'
        db.session.commit()

        set_status('restoring', 'Restoring Activity Monitor links and history…')
        try:
            from webapp.tools.activity_monitor_global_history import (
                restore_worlddb_history,
                sync_activity_monitor_roster,
            )

            worlddb_ids = sync_activity_monitor_roster(namespace_engine)
            if worlddb_ids:
                with main_engine.connect() as archive_connection:
                    restore_cutoff = archive_connection.execute(text(
                        'SELECT MAX(archived_at) FROM activity_monitor_global_history'
                    )).scalar()

                def report_restore_progress(table_name, record_count, duration_seconds):
                    phase_durations['restoring'] = round(
                        time.monotonic() - phase_started_at, 1
                    )
                    write_cdb_update_job_status(
                        namespace.slug, job_id, 'restoring',
                        message=(
                            f'Restoring Activity Monitor history: {table_name} '
                            f'({record_count:,} archived records processed, '
                            f'{duration_seconds:.1f}s).'
                        ),
                        phase_durations_seconds=dict(phase_durations),
                        elapsed_seconds=round(time.monotonic() - task_started_at, 1),
                        updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                    )

                restore_worlddb_history(
                    main_engine,
                    namespace_engine,
                    worlddb_ids,
                    progress_callback=report_restore_progress,
                    archived_after=restore_checkpoint,
                    archived_before=restore_cutoff,
                )
                if restore_cutoff is not None:
                    try:
                        write_activity_monitor_restore_checkpoint(namespace.slug, restore_cutoff)
                    except OSError:
                        logger.exception(
                            'Activity Monitor history restored but its checkpoint could not be saved for %s',
                            namespace.slug,
                        )
                        warning = (
                            'Activity Monitor history was restored, but its incremental checkpoint '
                            'could not be saved; the next CDB update may rescan archived history.'
                        )
        except Exception as exc:
            logger.exception('CDB update imported but Activity Monitor restore failed for %s', namespace.slug)
            warning = f'The CDB was updated, but Activity Monitor history could not be fully restored: {exc}'

        try:
            actor = User.query.get(actor_user_id)
            AuditLog.log_action(
                action=AuditAction.NAMESPACE_CDB_IMPORTED,
                user=actor,
                resource_type='namespace',
                resource_id=namespace.id,
                resource_name=namespace.name,
                details={
                    'import_stats': stats,
                    'cdb_file': source_filename,
                    'operation': 'replace_mapped_cdb_data',
                    'activity_monitor_history_preserved': True,
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.exception('CDB update audit entry could not be saved for %s', namespace.slug)
            warning = warning or f'The CDB was updated, but its audit entry could not be saved: {exc}'

        phase_durations[current_phase] = round(time.monotonic() - phase_started_at, 1)
        total_duration = round(time.monotonic() - task_started_at, 1)
        timings = ', '.join(
            f'{phase}: {duration:g}s' for phase, duration in phase_durations.items()
        )
        message = (
            f"CDB updated for {namespace.name}. Replaced {stats.get('cyclists', 0):,} riders; "
            f'Activity Monitor history was kept. Finished in {total_duration:g}s ({timings}).'
        )
        if warning:
            message += ' ' + warning
        set_status('complete_with_warning' if warning else 'complete', message)
        logger.info('Background CDB update completed for %s: %s', namespace.slug, stats)
        return {'status': 'complete_with_warning' if warning else 'complete', 'stats': stats}
    except Exception as exc:
        db.session.rollback()
        logger.exception('Background CDB update failed for namespace %s', namespace.slug)
        elapsed_seconds = round(time.monotonic() - task_started_at, 1)
        if not imported:
            for path in paths_to_clean:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    logger.exception('Could not remove failed CDB update artifact %s', path)
            message = f'The new CDB could not be imported. The previous CDB data and namespace history were kept. {exc}'
            status = 'failed'
        else:
            message = f'The CDB tables were replaced, but a later step failed. Check this namespace before retrying. {exc}'
            status = 'failed_after_import'
        try:
            write_cdb_update_job_status(
                namespace.slug, job_id, status, message=message,
                phase_durations_seconds=dict(phase_durations),
                elapsed_seconds=elapsed_seconds,
                updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            )
        except Exception:
            logger.exception('Could not save failed CDB update status for %s', namespace.slug)
        return {'status': status, 'message': message}
    finally:
        db.session.remove()
        if namespace_engine is not None:
            namespace_engine.dispose()
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                os.remove(lock_path)
            except OSError:
                logger.exception('Could not remove CDB update lock %s', lock_path)


@_celery.task(name='webapp.tasks.run_external_sync')
def run_external_sync_task(log_id, sync_type, season_year, gender, api_path=None, start_date=None, end_date=None):
    """Run one external sync type in the background and update status log."""
    try:
        logger.info(
            'External sync task started: log_id=%s sync_type=%s year=%s gender=%s',
            log_id,
            sync_type,
            season_year,
            gender,
        )
        client = FirstCyclingClient(api_path=api_path, external_sync_log_id=log_id)

        if sync_type == SYNC_TYPE_RACE:
            _update_sync_log(
                log_id,
                status='running',
                message=f'Syncing race calendar for {season_year} [{gender}]',
            )
            summary = client.sync_race_calendar(
                year=season_year,
                gender=gender,
                commit=True,
            )
            requested_count = summary['requested']
            synced_count = summary['synced']
            failed_count = summary['skipped']
            status = 'partial' if failed_count else 'success'
            message = (
                f"Race calendar sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count})"
            )
        elif sync_type == SYNC_TYPE_TEAMS:
            _update_sync_log(
                log_id,
                status='running',
                message=f'Syncing teams for {season_year} [{gender}]',
            )
            summary = client.sync_teams(
                year=season_year,
                gender=gender,
                commit=True,
            )
            requested_count = summary['requested']
            synced_count = summary['fetched']
            failed_count = summary['failed']
            status = 'partial' if failed_count else 'success'
            applied = bool(summary.get('applied'))
            applied_mode = summary.get('applied_mode')
            message = (
                f"Teams sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count}; "
                f"applied={applied_mode or ('yes' if applied else 'no')})"
            )
        elif sync_type == SYNC_TYPE_RIDERS:
            _update_sync_log(
                log_id,
                status='running',
                message=f'Syncing riders for {season_year} [{gender}]',
            )
            summary = client.sync_riders(
                year=season_year,
                gender=gender,
                commit=True,
            )
            requested_count = summary['requested']
            synced_count = summary['fetched']
            failed_count = summary['failed']
            status = 'partial' if failed_count else 'success'
            applied = bool(summary.get('applied'))
            applied_mode = summary.get('applied_mode')
            message = (
                f"Riders sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count}; "
                f"applied={applied_mode or ('yes' if applied else 'no')})"
            )
        elif sync_type == SYNC_TYPE_RACE_RESULTS:
            if not start_date or not end_date:
                raise ValueError('start_date and end_date are required for race results sync')

            _update_sync_log(
                log_id,
                status='running',
                message=(
                    f'Syncing race results for {season_year} [{gender}] '
                    f'({start_date} to {end_date})'
                ),
            )
            summary = client.sync_race_results_window(
                year=season_year,
                gender=gender,
                start_date=start_date,
                end_date=end_date,
                commit=True,
            )
            requested_count = summary['requested']
            synced_count = summary['synced']
            failed_count = summary['failed']
            skipped_count = summary.get('skipped', 0)
            status = 'partial' if failed_count or skipped_count else 'success'
            message = (
                f"Race results sync complete for {season_year} [{gender}] "
                f"({start_date} to {end_date}; "
                f"synced={synced_count}/{requested_count}, skipped={skipped_count}, failed={failed_count})"
            )
        else:
            raise ValueError(f'Unsupported sync type: {sync_type}')

        _update_sync_log(
            log_id,
            status=status,
            requested_count=requested_count,
            synced_count=synced_count,
            failed_count=failed_count,
            message=message,
            details_json=json.dumps(summary),
        )
        logger.info(
            'External sync task finished: log_id=%s sync_type=%s status=%s requested=%s synced=%s failed=%s',
            log_id,
            sync_type,
            status,
            requested_count,
            synced_count,
            failed_count,
        )
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            'External sync task failed: log_id=%s sync_type=%s year=%s gender=%s',
            log_id,
            sync_type,
            season_year,
            gender,
        )
        _update_sync_log(
            log_id,
            status='failed',
            message=str(exc),
            failed_count=1,
        )
        raise


def __getattr__(name):
    """Initialize Flask only when the Celery CLI loads the worker app."""
    global _worker_app
    if name == 'celery':
        if _worker_app is None:
            _worker_app = create_app()
        # Cache the configured app so subsequent lookups do not recreate Flask.
        globals()['celery'] = _celery
        return _celery
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
def _execute_activity_monitor_task(namespace_slug, task_id, request_delay_seconds=None):
    """Run one queued task, deferring safely until source collectors exist."""
    from webapp.integrations.activity_monitor_queue import transition_activity_monitor_task
    from webapp.integrations.activity_monitor_sources import (
        ActivitySourceNotConfigured,
        ActivitySourceRetryable,
        collect_activity_monitor_task,
        reset_activity_monitor_request_delay,
        set_activity_monitor_request_delay,
    )
    from webapp.models.ns.activity_monitor import ActivityMonitorRun, ActivityMonitorTask

    namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if namespace is None or not namespace.is_active:
        return {"task_id": task_id, "status": "namespace_unavailable"}

    engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
    request_delay_token = None
    source_worlddb_rider_id = None
    try:
        with Session(engine) as session:
            task = session.get(ActivityMonitorTask, int(task_id))
            if task is None:
                return {"task_id": task_id, "status": "task_missing"}
            run_id = task.run_id
            source_worlddb_rider_id = task.source_worlddb_rider_id
            if (
                task.state == "retry_wait"
                and task.next_retry_at is not None
                and task.next_retry_at > datetime.utcnow()
            ):
                return {
                    "task_id": task_id,
                    "status": "retry_not_due",
                    "next_retry_at": task.next_retry_at.isoformat(),
                }

        def run_stop_requested():
            if run_id is None:
                return False
            with Session(engine) as session:
                return bool(session.scalar(
                    select(ActivityMonitorRun.stop_reason).where(ActivityMonitorRun.id == run_id)
                ))

        claimed = transition_activity_monitor_task(
            engine,
            task_id,
            "running",
            notes="Claimed by the Activity Monitor background worker.",
            expected_state={"queued", "retry_wait"},
        )
        if not claimed["changed"]:
            return {"task_id": task_id, "status": "not_claimed", "state": claimed["state"]}

        if request_delay_seconds is not None:
            request_delay_token = set_activity_monitor_request_delay(request_delay_seconds)
        try:
            collector_result = collect_activity_monitor_task(engine, task_id)
        except ActivitySourceNotConfigured as exc:
            stopped = run_stop_requested()
            next_state = "cancelled" if stopped else "retry_wait"
            transition_activity_monitor_task(
                engine,
                task_id,
                next_state,
                notes=("Cancelled after the current request because the run was stopped."
                       if stopped else str(exc)),
                outcome=str(exc),
            )
            return {"task_id": task_id, "status": next_state, "reason": str(exc)}
        except ActivitySourceRetryable as exc:
            stopped = run_stop_requested()
            next_state = "cancelled" if stopped else "retry_wait"
            transition_activity_monitor_task(
                engine,
                task_id,
                next_state,
                notes=("Cancelled after the current request because the run was stopped."
                       if stopped else str(exc)),
                outcome=str(exc),
                next_retry_at=None if stopped else datetime.utcnow() + timedelta(minutes=5),
            )
            return {"task_id": task_id, "status": next_state, "reason": str(exc)}

        final_state = (collector_result or {}).get("state")
        if final_state not in {"completed", "needs_review", "cancelled"}:
            raise ValueError("Activity Monitor collector returned no valid terminal task state.")
        outcome = json.dumps(collector_result, ensure_ascii=False, default=str)
        transition_activity_monitor_task(
            engine,
            task_id,
            final_state,
            notes=(
                "Task cancelled before another source request because the run was stopped."
                if final_state == "cancelled"
                else "Source collection and evidence persistence completed."
            ),
            outcome=outcome,
            expected_state="running",
        )
        if source_worlddb_rider_id is not None:
            try:
                from webapp.tools.activity_monitor_global_history import archive_namespace_history

                archive_namespace_history(
                    engine,
                    db.engines.get('main') or db.engine,
                    namespace.slug,
                    apply=True,
                    worlddb_rider_id=source_worlddb_rider_id,
                )
            except Exception:
                # The namespace check remains authoritative. Archive outages
                # must not turn a completed source check into a failed check.
                logger.exception(
                    "Could not archive Activity Monitor history: namespace=%s worlddb_rider_id=%s",
                    namespace_slug,
                    source_worlddb_rider_id,
                )
        return {"task_id": task_id, "status": final_state}
    except Exception as exc:
        logger.exception(
            "Activity Monitor task failed: namespace=%s task_id=%s",
            namespace_slug,
            task_id,
        )
        try:
            transition_activity_monitor_task(
                engine,
                task_id,
                "failed",
                notes="The background worker encountered an unexpected error.",
                outcome=str(exc),
                expected_state="running",
            )
        except Exception:
            logger.exception("Could not record Activity Monitor task failure: task_id=%s", task_id)
        return {"task_id": task_id, "status": "failed"}
    finally:
        if request_delay_token is not None:
            reset_activity_monitor_request_delay(request_delay_token)
        engine.dispose()


@_celery.task(name="webapp.tasks.run_activity_monitor_run")
def run_activity_monitor_run_task(namespace_slug, run_id):
    """Process queued namespace tasks without crossing namespace boundaries."""
    from webapp.integrations.activity_monitor_queue import transition_activity_monitor_task
    from webapp.models.ns.activity_monitor import ActivityMonitorRun, ActivityMonitorTask

    namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if namespace is None or not namespace.is_active:
        return {"run_id": run_id, "status": "namespace_unavailable"}

    engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
    lock_connection = engine.connect()
    lock_acquired = False
    try:
        lock_acquired = bool(lock_connection.execute(text(
            'SELECT pg_try_advisory_lock(:lock_key, :run_id)'
        ), {'lock_key': 713459201, 'run_id': int(run_id)}).scalar_one())
        lock_connection.commit()
        if not lock_acquired:
            return {"run_id": run_id, "status": "already_running"}

        with Session(engine) as session:
            run = session.get(ActivityMonitorRun, int(run_id))
            if run is None:
                return {"run_id": run_id, "status": "run_missing"}
            current_run_id = int(run.id)
            run_type = run.run_type
            run_settings = dict(run.settings or {})
            interrupted_task_ids = session.scalars(
                select(ActivityMonitorTask.id).where(
                    ActivityMonitorTask.run_id == run.id,
                    ActivityMonitorTask.state == 'running',
                )
            ).all()

        # Owning the run-level advisory lock proves no other current runner is
        # processing these rows. Requeue interrupted tasks through the normal
        # audited transition path before selecting runnable work.
        for task_id in interrupted_task_ids:
            transition_activity_monitor_task(
                engine,
                task_id,
                'retry_wait',
                notes='Resumed after the previous background worker stopped mid-check.',
                next_retry_at=datetime.utcnow(),
                expected_state='running',
            )

        with Session(engine) as session:
            task_ids = session.scalars(
                select(ActivityMonitorTask.id)
                .where(
                    ActivityMonitorTask.run_id == current_run_id,
                    (
                        (ActivityMonitorTask.state == "queued")
                        | (
                            (ActivityMonitorTask.state == "retry_wait")
                            & (
                                ActivityMonitorTask.next_retry_at.is_(None)
                                | (ActivityMonitorTask.next_retry_at <= datetime.utcnow())
                            )
                        )
                    ),
                )
                .order_by(ActivityMonitorTask.id)
            ).all()
        results = []
        request_delay = (
            run_settings.get('request_delay_seconds') if run_type == 'manual'
            else (int(run_settings.get('delay_min_seconds', 59)) + int(run_settings.get('delay_max_seconds', 61))) // 2
            if run_type == 'scheduled' else None
        )
        for task_id in task_ids:
            results.append(_execute_activity_monitor_task(
                namespace_slug, task_id,
                request_delay_seconds=request_delay,
            ))
        retry_at = None
        with Session(engine) as session:
            retry_at = session.scalar(
                select(func.min(ActivityMonitorTask.next_retry_at)).where(
                    ActivityMonitorTask.run_id == int(run_id),
                    ActivityMonitorTask.state == "retry_wait",
                    ActivityMonitorTask.next_retry_at.is_not(None),
                )
            )
        if retry_at is not None:
            delay = max(1, int((retry_at - datetime.utcnow()).total_seconds()))
            run_activity_monitor_run_task.apply_async(
                args=[namespace_slug, int(run_id)],
                countdown=delay,
            )

        statuses = {result["status"] for result in results}
        if statuses == {"deferred"} or not results:
            status = "deferred"
        elif "failed" in statuses:
            status = "partial" if len(statuses) > 1 else "failed"
        else:
            status = "processed"
        return {"run_id": run_id, "status": status, "tasks": results}
    finally:
        if lock_acquired:
            try:
                lock_connection.execute(text(
                    'SELECT pg_advisory_unlock(:lock_key, :run_id)'
                ), {'lock_key': 713459201, 'run_id': int(run_id)})
                lock_connection.commit()
            except Exception:
                logger.exception('Could not release Activity Monitor run lock: run_id=%s', run_id)
        lock_connection.close()
        engine.dispose()


@_celery.task(name="webapp.tasks.poll_activity_monitor_schedules")
def poll_activity_monitor_schedules():
    """Queue due riders for enabled namespace schedules; never fetch inline."""
    from flask import current_app

    from webapp.integrations.activity_monitor_planning import due_activity_riders
    from webapp.integrations.activity_monitor_queue import enqueue_activity_monitor_run
    from webapp.models.ns.activity_monitor import ActivityMonitorRun, ActivityMonitorSchedule
    from webapp.tools.activity_monitor_schedule import next_planned_round, planned_rounds

    now = datetime.now(ZoneInfo('Europe/Amsterdam')).replace(tzinfo=None)
    now_utc = datetime.utcnow()
    recovery_before = now_utc - timedelta(minutes=5)
    # is_active is a Python property, not a database column; filter on the
    # persisted fields so enabled namespace schedules are actually polled.
    namespaces = Namespace.query.filter_by(status='active', is_deleted=False).all()
    queued_runs = []
    resumed_runs = []
    for namespace in namespaces:
        engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
        pcs_enabled = bool(current_app.config.get('ACTIVITY_MONITOR_PCS_ENABLED', False))
        fc_enabled = bool(current_app.config.get('ACTIVITY_MONITOR_FC_ENABLED', False))
        selected_ids = []
        settings = None
        source_mode = None
        try:
            with engine.connect() as connection:
                tables = connection.execute(text("""
                    SELECT to_regclass('activity_monitor_schedules') IS NOT NULL AS has_schedule,
                           to_regclass('activity_monitor_runs') IS NOT NULL AS has_runs,
                           to_regclass('activity_monitor_tasks') IS NOT NULL AS has_tasks
                """)).mappings().one()
                if tables['has_runs'] and tables['has_tasks']:
                    pending_run_id = connection.execute(text("""
                        SELECT r.id
                        FROM activity_monitor_runs r
                        WHERE r.status IN ('queued', 'running', 'retry_wait')
                          AND COALESCE(r.heartbeat_at, r.started_at, r.created_at) <= :recovery_before
                          AND EXISTS (
                              SELECT 1 FROM activity_monitor_tasks t
                              WHERE t.run_id = r.id
                                AND (
                                    t.state IN ('queued', 'running')
                                    OR (
                                        t.state = 'retry_wait'
                                        AND t.next_retry_at IS NOT NULL
                                        AND t.next_retry_at <= :now_utc
                                    )
                                    OR (
                                        t.state = 'retry_wait'
                                        AND t.next_retry_at IS NULL
                                        AND NOT EXISTS (
                                            SELECT 1
                                            FROM jsonb_array_elements_text(
                                                COALESCE(t.source_scope::jsonb, '[]'::jsonb)
                                            ) AS required_source(source)
                                            WHERE (required_source.source = 'pcs' AND NOT :pcs_enabled)
                                               OR (required_source.source = 'firstcycling' AND NOT :fc_enabled)
                                        )
                                    )
                                )
                          )
                        ORDER BY r.created_at, r.id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """), {
                        'now_utc': now_utc,
                        'recovery_before': recovery_before,
                        'pcs_enabled': pcs_enabled,
                        'fc_enabled': fc_enabled,
                    }).scalar_one_or_none()
                    if pending_run_id is not None:
                        connection.execute(text("""
                            UPDATE activity_monitor_runs
                            SET heartbeat_at = :now_utc
                            WHERE id = :run_id
                        """), {'now_utc': now_utc, 'run_id': pending_run_id})
                        connection.commit()
                else:
                    pending_run_id = None
            if pending_run_id is not None:
                run_activity_monitor_run_task.apply_async(
                    args=[namespace.slug, int(pending_run_id)],
                )
                resumed_runs.append({
                    'namespace': namespace.slug,
                    'run_id': int(pending_run_id),
                })
            if not tables['has_schedule']:
                continue
            with Session(engine) as session:
                with session.begin():
                    schedule = session.scalar(
                        select(ActivityMonitorSchedule)
                        .where(ActivityMonitorSchedule.schedule_key == 'activity-monitor')
                        .with_for_update(skip_locked=True)
                    )
                    if schedule is None or not schedule.enabled:
                        continue

                    today_slots = planned_rounds(
                        now.date(), schedule.window_start, schedule.window_end,
                        schedule.checks_per_hour,
                    )
                    scheduled_at = schedule.next_run_at
                    if scheduled_at not in today_slots:
                        schedule.next_run_at = next_planned_round(
                            now, schedule.window_start, schedule.window_end,
                            schedule.checks_per_hour,
                        )
                        continue
                    if scheduled_at > now:
                        continue

                    # The minute poll may be a little late, but missed rounds
                    # are skipped instead of being caught up later.
                    if now >= scheduled_at + timedelta(minutes=2):
                        schedule.next_run_at = next_planned_round(
                            now, schedule.window_start, schedule.window_end,
                            schedule.checks_per_hour,
                        )
                        continue

                    schedule.next_run_at = next_planned_round(
                        scheduled_at + timedelta(seconds=1),
                        schedule.window_start, schedule.window_end,
                        schedule.checks_per_hour,
                    )

                    active_run_id = session.scalar(
                        select(ActivityMonitorRun.id)
                        .where(ActivityMonitorRun.status.in_({'queued', 'running', 'retry_wait'}))
                        .limit(1)
                    )
                    if active_run_id is not None:
                        logger.info(
                            'Skipping overlapping Activity Monitor round: namespace=%s',
                            namespace.slug,
                        )
                        continue
                    if not pcs_enabled:
                        continue

                    source_mode = 'combined' if fc_enabled else 'pcs_only'
                    with engine.connect() as planning_connection:
                        # Include upcoming checks as well as overdue checks; the
                        # planner sorts both by their scheduled date. Riders
                        # already checked today remain excluded by the planner.
                        candidate_count, due_rows = due_activity_riders(
                            planning_connection, now.date(), limit=25,
                            source_mode=source_mode,
                            include_upcoming=True,
                        )
                    selected_ids = [row['monitor_rider_id'] for row in due_rows]
                    settings = {
                        'delay_min_seconds': schedule.delay_min_seconds,
                        'delay_max_seconds': schedule.delay_max_seconds,
                        'scheduled_by': 'activity-monitor-namespace-schedule',
                    }
                    if not selected_ids and candidate_count:
                        logger.warning(
                            'Activity Monitor schedule returned no selectable riders: namespace=%s candidates=%s',
                            namespace.slug, candidate_count,
                        )
            if not selected_ids:
                continue
            result = enqueue_activity_monitor_run(
                engine,
                riders=selected_ids,
                source_mode=source_mode,
                reference_date=now.date(),
                run_type='scheduled',
                run_settings=settings,
            )
            with Session(engine) as session:
                with session.begin():
                    schedule = session.scalar(
                        select(ActivityMonitorSchedule)
                        .where(ActivityMonitorSchedule.schedule_key == 'activity-monitor')
                        .with_for_update()
                    )
                    if schedule is not None:
                        schedule.last_run_at = now
            queued_runs.append({
                'namespace': namespace.slug,
                'run_id': result['run_id'],
                'riders': len(selected_ids),
            })
            run_activity_monitor_run_task.apply_async(
                args=[namespace.slug, result['run_id']],
            )
        except Exception:
            logger.exception(
                'Activity Monitor automatic scheduling failed for namespace %s',
                namespace.slug,
            )
        finally:
            engine.dispose()
    return {'queued_runs': queued_runs, 'resumed_runs': resumed_runs}


def __getattr__(name):
    """Initialize Flask only when the Celery CLI loads the worker app."""
    global _worker_app
    if name == 'celery':
        if _worker_app is None:
            _worker_app = create_app()
        globals()['celery'] = _celery
        return _celery
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
