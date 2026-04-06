"""Background task definitions."""
import json
import logging

from webapp.extensions import celery, db
from webapp.integrations.firstcycling import FirstCyclingClient
from webapp.models import ExternalSyncLog

SYNC_TYPE_RACE = 'fc_race_calendar'
SYNC_TYPE_TEAMS = 'fc_teams'
SYNC_TYPE_RIDERS = 'fc_riders'
logger = logging.getLogger(__name__)


def _update_sync_log(log_id, **updates):
    log = ExternalSyncLog.query.get(log_id)
    if log is None:
        return None

    for key, value in updates.items():
        setattr(log, key, value)

    db.session.commit()
    return log


@celery.task(name='webapp.tasks.run_external_sync')
def run_external_sync_task(log_id, sync_type, season_year, gender, api_path=None):
    """Run one external sync type in the background and update status log."""
    try:
        logger.info(
            'External sync task started: log_id=%s sync_type=%s year=%s gender=%s',
            log_id,
            sync_type,
            season_year,
            gender,
        )
        client = FirstCyclingClient(api_path=api_path)

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
            message = (
                f"Team sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count})"
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
            message = (
                f"Rider sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count})"
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
