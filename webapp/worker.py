"""Celery worker entrypoint and background task definitions."""
import json
import logging

from webapp import create_app
from webapp.extensions import celery as _celery, db
from webapp.integrations.firstcycling.api import FirstCyclingClient
from webapp.models import ExternalSyncLog

SYNC_TYPE_RACE = 'fc_race_calendar'
SYNC_TYPE_TEAMS = 'fc_teams'
SYNC_TYPE_RIDERS = 'fc_riders'
SYNC_TYPE_RACE_RESULTS = 'fc_race_results'
logger = logging.getLogger(__name__)


def _update_sync_log(log_id, **updates):
    log = ExternalSyncLog.query.get(log_id)
    if log is None:
        return None

    for key, value in updates.items():
        setattr(log, key, value)

    db.session.commit()
    return log


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
    if name == 'celery':
        create_app()
        # Cache the configured app so subsequent lookups do not recreate Flask.
        globals()['celery'] = _celery
        return _celery
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')